"""Agente agêntico de candidatura no Chrome logado via CDP.

Fluxo: abre a vaga pelo ID real, segue o redirecionamento para o portal da
empresa, lê o formulário, usa o resolvedor local + DGX para decidir cada campo,
pede APROVAÇÃO HUMANA persistida e só então clica em Submit. Após o envio,
grava candidatura, ``has_applied`` e o cadastro da empresa.

O conhecimento de cada site é guardado em ``SitePatternStore`` (memória local),
para que a próxima candidatura reutilize o padrão aprendido.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.config import Settings
from ..core.errors import BlockedByPlatformError, HumanActionRequired, ValidationError
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.forms.answers import AnswerResolver
from ..execution.forms.field import FormField
from ..execution.web.linkedin_public import is_remote_public
from ..execution.web.form_dom import (
    click_submit,
    ensure_resume_uploaded,
    fill_field,
    snapshot_form,
    submission_state,
)
from ..execution.web.linkedin_logged import LinkedInLoggedBrowser
from ..execution.web.site_patterns import SitePatternStore, domain_of
from ..models.domain import (
    AnswerRecord,
    Application,
    ApprovalRequest,
    CandidateProfile,
    JobOpportunity,
)
from ..storage.database import Repository
from .approval import ApprovalGate
from .qualification import classify_channel, is_remote_location
from .qualification_memory import QualificationMemory

_EASY_APPLY_LABELS = (
    "Easy Apply to this job", "Easy Apply",
    "Candidatura simplificada", "Candidatura rápida", "Candidatura rapida",
)
_EXTERNAL_APPLY_LABELS = (
    "Apply on company website", "Apply externally", "Solicitar no site da empresa",
)
_APPLY_LABELS = _EASY_APPLY_LABELS + _EXTERNAL_APPLY_LABELS


@dataclass
class FillDecision:
    index: int
    signature: str
    selector: str
    kind: str
    value: str
    source: str
    required: bool
    category: str = "general"


@dataclass
class PreparedForm:
    url: str
    domain: str
    decisions: list[FillDecision]
    submit_label: str = ""


@dataclass
class ApplyObservation:
    """Evidência de Easy Apply colhida na MESMA abertura usada para candidatar."""

    easy_apply: bool | None
    label: str = ""


def _value_from_source(profile: CandidateProfile, source: str) -> str:
    """Resolve um caminho aprendido (``profile.personal.email`` etc.)."""
    if not source.startswith("profile."):
        return ""
    current: Any = profile
    for part in source.split(".")[1:]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return ""
    return str(current)


class BrowserApplyAgent:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        profile: CandidateProfile,
        resolver: AnswerResolver,
        gate: ApprovalGate,
        bus: EventBus | None = None,
        *,
        cdp_url: str = "http://127.0.0.1:9222",
        memory: QualificationMemory | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.profile = profile
        self.resolver = resolver
        self.gate = gate
        self.bus = bus
        self.memory = memory
        self.browser = LinkedInLoggedBrowser(cdp_url, timeout_ms=settings.browser.timeout_ms)
        self.patterns = SitePatternStore(settings.resolve("data/site_patterns.json"))
        self._pending: dict[str, PreparedForm] = {}

    @staticmethod
    def job_url(job: JobOpportunity) -> str:
        match = re.search(r"(\d{6,})", job.external_id or job.url or "")
        if not match:
            raise ValidationError("vaga sem ID real do LinkedIn; nunca usar URL sintética")
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}"

    def _open(
        self, job: JobOpportunity
    ) -> tuple[Any, dict[str, Any], ApplyObservation]:
        """Abre a vaga, aciona Apply e devolve a evidência de Easy Apply.

        A sondagem acontece aqui — depois da classificação — para não abrir uma
        aba extra e para que vagas reprovadas nunca cheguem ao Chrome logado.
        """
        page = self.browser.open_tab(self.job_url(job))
        snapshot = snapshot_form(page)
        if domain_of(snapshot.get("url", "")) != "linkedin.com":
            return page, snapshot, ApplyObservation(False, "portal_externo")

        title = str(snapshot.get("title", "") or "").lower()
        text = str(snapshot.get("text", "") or "").lower()
        closed_marks = (
            "não aceita mais candidaturas",
            "nao aceita mais candidaturas",
            "no longer accepting applications",
        )
        if any(mark in text for mark in closed_marks):
            self.browser.close_tab(page)
            raise ValidationError("a vaga não aceita mais candidaturas no LinkedIn")
        has_easy_text = any(label.lower() in text for label in _EASY_APPLY_LABELS)
        has_external_text = any(label.lower() in text for label in _EXTERNAL_APPLY_LABELS)
        has_apply_label = has_easy_text or has_external_text
        if "vagas | linkedin" in title and not has_apply_label:
            self.browser.close_tab(page)
            raise ValidationError("página da vaga não carregou no Chrome logado; nada foi preparado")

        observation = ApplyObservation(None)
        clicked = False
        for label in _APPLY_LABELS:
            try:
                locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if locator.count() == 0:
                    continue
                before = {id(p) for p in page.context.pages}
                locator.first.click()
                clicked = True
                easy = label in _EASY_APPLY_LABELS
                observation = ApplyObservation(easy, label)
                log_event("browser_apply_clicked", label=label, external_id=job.external_id)
                page.wait_for_timeout(1800)
                new_pages = [p for p in page.context.pages if id(p) not in before]
                if new_pages:
                    target = new_pages[0]
                    for extra in new_pages[1:]:
                        self.browser.close_tab(extra)
                    self.browser.close_tab(page)
                    page = target
                    page.wait_for_timeout(1800)
                break
            except Exception:
                continue
        if not clicked:
            # Fallback quando o botão está em iframe/aria-label e só o texto
            # renderizado é confiável. Nunca inventa Easy Apply sem evidência.
            if has_easy_text:
                observation = ApplyObservation(True, "texto_easy_apply")
            elif has_external_text:
                observation = ApplyObservation(False, "texto_apply_externo")
        if clicked:
            snapshot = snapshot_form(page)
        return page, snapshot, observation

    def _record_easy_apply(self, job: JobOpportunity, observation: ApplyObservation) -> None:
        """Persiste o achado somente para vagas já classificadas como válidas."""
        if observation.easy_apply is None:
            return
        elegiveis = {
            JobStatus.QUALIFIED,
            JobStatus.PENDING_APPROVAL,
            JobStatus.APPLYING,
        }
        if job.status not in elegiveis:
            log_event(
                "browser_apply_easy_apply_ignored",
                external_id=job.external_id,
                status=job.status.value,
            )
            return
        novo_canal = (
            "easy_apply" if observation.easy_apply else classify_channel(job, self.settings)
        )
        if (
            job.easy_apply == observation.easy_apply
            and job.easy_apply_source == "browser_apply"
            and job.channel == novo_canal
        ):
            return
        job.easy_apply = observation.easy_apply
        job.easy_apply_source = "browser_apply"
        job.channel = novo_canal
        self.repo.update_job(job)
        log_event(
            "easy_apply_detected_after_classification",
            external_id=job.external_id,
            easy_apply=observation.easy_apply,
            label=observation.label,
            channel=novo_canal,
        )

    def _recheck_remote(self, job: JobOpportunity, snapshot: dict[str, Any]) -> None:
        """Barreira final: 100% remoto continua obrigatório no momento do envio."""
        if not self.settings.search.remote_only:
            return
        analysis_remote = job.analysis.is_remote if job.analysis else None
        if analysis_remote is False:
            raise ValidationError("a vaga deixou de ser 100% remota; envio bloqueado")
        page_text = str(snapshot.get("text", "") or "")
        remote_ok = (
            analysis_remote is True
            or is_remote_location(job.location, job.title)
            or is_remote_public(page_text)
        )
        if not remote_ok:
            raise ValidationError("remoto não confirmado na rechecagem pré-envio")

    def _handle_resume(
        self, page: Any, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Anexa o CV internacional (config) sem duplicar e atualiza o snapshot."""
        cv_path = self.settings.profile.cv_path or self.profile.documents.get("resume", "")
        status = ensure_resume_uploaded(page, cv_path)
        log_event(
            "browser_apply_resume",
            status=status,
            filename=Path(cv_path).name if cv_path else "",
        )
        if status == "blocked":
            raise BlockedByPlatformError("verificação/CAPTCHA ao anexar currículo")
        invalid = {
            "missing", "select_failed", "upload_button_not_found",
            "file_input_not_found", "unverified",
        }
        if status in invalid:
            raise ValidationError(f"upload do currículo não confirmado: {status}")
        if status in {"uploaded", "selected"}:
            # O upload insere um novo radio: os índices do snapshot mudam.
            page.wait_for_timeout(1200)
            return snapshot_form(page)
        return snapshot

    def _decide(self, snapshot: dict[str, Any]) -> list[FillDecision]:
        url = snapshot.get("url", "")
        decisions: list[FillDecision] = []
        for raw in snapshot.get("fields", []):
            field = FormField(
                label=raw.get("label", ""),
                field_type=raw.get("type", "text"),
                required=bool(raw.get("required")),
                options=list(raw.get("options", [])),
                autocomplete=raw.get("autocomplete", ""),
                identifier=raw.get("identifier", ""),
                placeholder=raw.get("placeholder", ""),
                nearby_text=raw.get("nearby_text", ""),
            )
            signature = field.signature()
            learned = self.patterns.suggest(url, signature)
            value = ""
            source = ""
            needs_human = False
            category = "general"

            if learned and learned.get("source"):
                value = _value_from_source(self.profile, str(learned["source"]))
                source = f"learned:{learned['source']}"
                needs_human = not value
            else:
                decision = self.resolver.resolve(field)
                value = decision.value
                source = decision.source
                needs_human = decision.needs_human
                category = decision.reason if hasattr(decision, "reason") else "general"
                category = "sensitive" if needs_human and "sens" in str(category).lower() else category

            if needs_human or not value:
                if not field.required:
                    log_event("browser_apply_optional_field_skipped", signature=signature)
                    continue
                raise HumanActionRequired(
                    "campo de formulário", f"{field.label or signature} não tem evidência no perfil"
                )
            decisions.append(
                FillDecision(
                    index=int(raw["index"]),
                    signature=signature,
                    selector=str(raw.get("selector", "")),
                    kind=str(raw.get("type", "text")),
                    value=value,
                    source=source,
                    required=field.required,
                    category=category,
                )
            )
        return decisions

    def prepare(self, job: JobOpportunity) -> ApprovalRequest:
        """Lê o formulário e cria/reusa a aprovação. Não clica em Submit."""
        if job.status not in {JobStatus.QUALIFIED, JobStatus.PENDING_APPROVAL}:
            raise ValidationError(
                "vaga precisa estar classificada como QUALIFIED antes de abrir o Chrome"
            )
        if self.repo.has_application_for(job.id):
            raise ValidationError("candidatura duplicada para esta vaga")
        existing = self.repo.get_approval_for_job(job.id)
        if existing is not None and existing.status == "APPROVED":
            raise ValidationError("aprovação já registrada; use o modo de envio")
        page, snapshot, observation = self._open(job)
        self._record_easy_apply(job, observation)
        try:
            snapshot = self._handle_resume(page, snapshot)
            decisions = self._decide(snapshot)
            if not decisions:
                raise HumanActionRequired(
                    "formulário", "nenhum campo preenchível encontrado; requer inspeção humana"
                )
            prepared = PreparedForm(
                url=str(snapshot.get("url", "")),
                domain=domain_of(str(snapshot.get("url", ""))),
                decisions=decisions,
            )
        finally:
            self.browser.close_tab(page)

        if existing is not None and existing.status == "PENDING":
            approval = existing
        else:
            approval = ApprovalRequest(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                score=job.score,
                summary=job.analysis.summary if job.analysis else "",
                answers=[
                    AnswerRecord(
                        field=d.signature,
                        source=d.source,
                        value_redacted="<preenchido>",
                        required=d.required,
                        category=d.category,
                    )
                    for d in decisions
                ],
            )
            self.repo.create_approval(approval)
        self._pending[job.id] = prepared
        job.status = JobStatus.PENDING_APPROVAL
        self.repo.update_job(job)
        log_event(
            "browser_apply_prepared",
            company=job.company,
            fields=len(decisions),
            domain=prepared.domain,
            approval_id=approval.id,
        )
        if self.bus:
            self.bus.emit("application.prepared", company=job.company, title=job.title)
        return approval

    def confirm(self, approval: ApprovalRequest) -> bool:
        """Pergunta ao humano. Não persiste a decisão (uso legado)."""
        return self.gate.request(approval)

    def approve(self, job: JobOpportunity, approval: ApprovalRequest) -> bool:
        """Aprovação humana obrigatória, persistida ANTES de qualquer envio."""
        approved = self.gate.request(approval)
        self.repo.resolve_approval(approval.id, approved)
        if not approved:
            job.status = JobStatus.REJECTED_BY_FILTER
            self.repo.update_job(job)
            log_event("browser_apply_rejected", approval_id=approval.id, company=job.company)
            return False
        log_event("browser_apply_approved", approval_id=approval.id, company=job.company)
        return True

    def submit(
        self, job: JobOpportunity, approval: ApprovalRequest, *, confirmed: bool = False
    ) -> Application | None:
        """Preenche e envia. Nunca é chamado sem APPROVED persistido."""
        stored = self.repo.get_approval(approval.id)
        if stored is None:
            raise HumanActionRequired("aprovação", "aprovação não encontrada; rode prepare")
        if stored.status != "APPROVED":
            if confirmed:
                raise HumanActionRequired(
                    "aprovação", "envio confirmado exige status APPROVED persistido"
                )
            if not self.gate.request(stored):
                job.status = JobStatus.REJECTED_BY_FILTER
                self.repo.update_job(job)
                self.repo.resolve_approval(stored.id, False)
                return None
            self.repo.resolve_approval(stored.id, True)

        job.status = JobStatus.APPLYING
        self.repo.update_job(job)

        page, snapshot, observation = self._open(job)
        self._record_easy_apply(job, observation)
        try:
            self._recheck_remote(job, snapshot)
            snapshot = self._handle_resume(page, snapshot)
            decisions = self._decide(snapshot)
            prepared = self._pending.get(job.id)
            url = str(snapshot.get("url", "") or "")
            domain = domain_of(url)
            if prepared is not None and prepared.domain == domain:
                prepared.decisions = decisions
                prepared.url = url or prepared.url
            else:
                prepared = PreparedForm(url=url, domain=domain, decisions=decisions)
            filled = 0
            for decision in decisions:
                if fill_field(page, decision.index, decision.kind, decision.value):
                    filled += 1
            log_event("browser_apply_filled", fields=filled, domain=prepared.domain)

            if not click_submit(page):
                raise ValidationError("botão de envio não encontrado no formulário")
            page.wait_for_timeout(2500)
            state = submission_state(page)
            if state == "blocked":
                raise BlockedByPlatformError("verificação/CAPTCHA durante a candidatura")
            if state != "submitted":
                # Alguns portais exigem Review antes de Submit.
                click_submit(page, "Review application")
                page.wait_for_timeout(1200)
                click_submit(page)
                page.wait_for_timeout(2500)
                state = submission_state(page)
            if state != "submitted":
                job.status = JobStatus.FAILED
                self.repo.update_job(job)
                raise ValidationError("envio não confirmado pelo portal")

            application = Application(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                status="SUBMITTED",
                channel=job.channel,
                resume_path=self.profile.documents.get("resume", ""),
                portal_url=prepared.url,
                answers=[
                    AnswerRecord(
                        field=d.signature,
                        source=d.source,
                        value_redacted="<preenchido>",
                        required=d.required,
                        category=d.category,
                    )
                    for d in prepared.decisions
                ],
            )
            self.repo.record_application(application)
            job.status = JobStatus.SUBMITTED
            job.has_applied = True
            job.applied_at = application.applied_at or datetime.now()
            self.repo.update_job(job)

            company_id = job.company_id
            if not company_id:
                company = self.repo.ensure_company(job.company)
                company_id = company.id if company else ""
            existing = self.repo.get_company_by_name(job.company)
            self.repo.mark_company_application(
                company_id, created_account=not bool(existing and existing.has_account)
            )

            for decision in prepared.decisions:
                self.patterns.record(
                    prepared.url,
                    decision.signature,
                    decision.selector,
                    decision.source,
                    success=True,
                )
            self._pending.pop(job.id, None)
            if self.memory is not None:
                self.memory.record_outcome(job, "SUBMITTED")
            log_event("browser_apply_submitted", company=job.company, title=job.title)
            if self.bus:
                self.bus.emit("application.submitted", company=job.company, title=job.title)
            return application
        except Exception as exc:
            if self.memory is not None:
                self.memory.record_outcome(job, "FAILED", str(exc))
            raise
        finally:
            self.browser.close_tab(page)
