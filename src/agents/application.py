"""Agente 4/5 — preparação e envio de candidaturas (Easy Apply / portais)."""
from __future__ import annotations

from datetime import datetime
import time

from ..core.config import Settings
from ..core.errors import HumanActionRequired, ValidationError
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.android.accessibility import extract_form_fields
from ..execution.android.adb import ADBClient
from ..execution.android.appium_driver import AppiumDevice
from ..execution.forms.answers import AnswerResolver
from ..models.domain import (
    AnswerRecord,
    Application,
    ApprovalRequest,
    CandidateProfile,
    JobOpportunity,
)
from ..storage.database import Repository
from .approval import ApprovalGate


class ApplicationAgent:
    def __init__(
        self,
        device: AppiumDevice,
        adb: ADBClient,
        profile: CandidateProfile,
        resolver: AnswerResolver,
        gate: ApprovalGate,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.device = device
        self.adb = adb
        self.profile = profile
        self.resolver = resolver
        self.gate = gate
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self._pending: dict[str, list[str]] = {}

    def _open_job(self, job: JobOpportunity) -> None:
        # Sempre pelo ID real da vaga — nunca por título (risco de vaga errada).
        from .device_apply import DeviceApplicationAgent

        url = DeviceApplicationAgent.job_url(job)
        self.adb.open_url(
            url,
            label=job.external_id or job.id,
            package=self.settings.device.linkedin_package,
        )
        time.sleep(2.5)
        easy_apply = self.device.click_text("Easy Apply to this job") or self.device.click_text(
            "Easy Apply"
        ) or self.device.click_text("Candidatura rápida")
        if not easy_apply and not self.settings.device.allow_easy_apply:
            raise HumanActionRequired("Easy Apply", "opção desabilitada na configuração")
        if not easy_apply:
            raise HumanActionRequired(
                "Easy Apply", "botão não encontrado; a vaga pode redirect para portal externo"
            )

    def prepare(self, job: JobOpportunity) -> ApprovalRequest:
        """Preenche os dados em memória e para antes do envio."""
        if self.repo.has_application_for(job.id):
            raise ValidationError("candidatura duplicada para esta vaga")
        self._open_job(job)
        self.device.detect_block()
        fields = extract_form_fields(self.device.page_source())

        answers: list[AnswerRecord] = []
        values: list[str] = []
        sensitive: list[str] = []
        for field in fields:
            decision = self.resolver.resolve(field)
            name = field.label or field.identifier or field.field_type
            if decision.needs_human:
                sensitive.append(name)
            else:
                values.append(decision.value)
            answers.append(
                AnswerRecord(
                    field=name,
                    source=decision.source,
                    value_redacted="<preenchido>" if decision.value else "",
                    required=field.required,
                    category="sensitive" if decision.needs_human else "general",
                )
            )

        resume = self.profile.documents.get("resume", "")
        if resume:
            self.adb.push_file(resume, "candidate_resume.pdf")

        if sensitive:
            job.status = JobStatus.REQUIRES_HUMAN_ACTION
            # Candidatura parcial: iniciada, mas depende de ação humana.
            job.has_applied = True
            job.applied_at = datetime.now()
            self.repo.update_job(job)
            raise HumanActionRequired(
                "campos sensíveis", ", ".join(sensitive[:10])
            )

        approval = ApprovalRequest(
            trace_id=job.trace_id,
            opportunity_id=job.id,
            company=job.company,
            title=job.title,
            score=job.score,
            summary=job.analysis.summary if job.analysis else "",
            answers=answers,
        )
        self.repo.create_approval(approval)
        self._pending[job.id] = values
        job.status = JobStatus.PENDING_APPROVAL
        self.repo.update_job(job)
        log_event("application_prepared", company=job.company, title=job.title, fields=len(fields))
        if self.bus:
            self.bus.emit("application.prepared", company=job.company, title=job.title)
        return approval

    def confirm(self, approval: ApprovalRequest) -> bool:
        """Solicita a aprovação humana antes de qualquer escrita na interface."""
        return self.gate.request(approval)

    def submit(
        self, job: JobOpportunity, approval: ApprovalRequest, *, confirmed: bool = False
    ) -> Application | None:
        if not confirmed and not self.gate.request(approval):
            job.status = JobStatus.REJECTED_BY_FILTER
            self.repo.update_job(job)
            self.repo.resolve_approval(approval.id, False)
            return None

        self.repo.resolve_approval(approval.id, True)
        job.status = JobStatus.APPLYING
        self.repo.update_job(job)

        values = self._pending.pop(job.id, [])
        filled = self.device.fill_visible_inputs(values)
        log_event("application_filling", fields=filled)

        self.device.click_text("Review") or self.device.click_text("Revisar")
        submitted = (
            self.device.click_text("Submit application")
            or self.device.click_text("Enviar candidatura")
            or self.device.click_text("Submit")
            or self.device.click_text("Enviar")
        )
        if not submitted:
            job.status = JobStatus.FAILED
            self.repo.update_job(job)
            raise ValidationError("botão de envio não encontrado na interface")

        application = Application(
            trace_id=job.trace_id,
            opportunity_id=job.id,
            company=job.company,
            title=job.title,
            status="SUBMITTED",
            resume_path=self.profile.documents.get("resume", ""),
            portal_url=job.url,
            answers=[
                AnswerRecord(
                    field=answer.field,
                    source=answer.source,
                    value_redacted="<preenchido>",
                    required=answer.required,
                    category=answer.category,
                )
                for answer in approval.answers
            ],
        )
        self.repo.record_application(application)
        job.status = JobStatus.SUBMITTED
        job.has_applied = True
        job.applied_at = application.applied_at
        self.repo.update_job(job)
        log_event("application_submitted", company=job.company, title=job.title)
        if self.bus:
            self.bus.emit("application.submitted", company=job.company, title=job.title)
        return application
