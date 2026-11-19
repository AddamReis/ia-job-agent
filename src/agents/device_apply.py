"""Agente 9 — fila de Easy Apply que exige sessão logada no celular.

A página pública do LinkedIn não permite candidatura simplificada. As vagas
 aprovadas ficam em ``WAITING_DEVICE``; quando o dispositivo conecta, este
agente abre cada vaga pelo ID real (deep link), sobe o CV, responde o que for
possível e envia — sempre com aprovação humana registrada no banco.

Nada é enviado sem um ``ApprovalRequest`` aprovado. Ações exclusivamente
humanas (vídeo, câmera, visto) viram dependências pendentes, nunca são
auto-marcadas como concluídas.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from ..core.config import Settings
from ..core.errors import HumanActionRequired, ValidationError
from ..core.events import EventBus
from ..core.logging import log_event, set_trace_id
from ..core.state import JobStatus
from ..execution.android.accessibility import extract_form_fields
from ..execution.android.adb import ADBClient
from ..execution.android.appium_driver import AppiumDevice
from ..execution.forms.answers import AnswerResolver
from ..execution.web.linkedin_logged import (
    LinkedInLoggedBrowser,
    LoggedBrowserUnavailable,
)
from ..models.domain import (
    AnswerRecord,
    Application,
    ApprovalRequest,
    CandidateProfile,
    JobOpportunity,
)
from ..storage.database import Repository
from .approval import ApprovalGate

_ID_RE = re.compile(r"(\d{6,})")

_UPLOAD_LABELS = (
    "Upload resume", "Upload CV", "Choose file", "Choose File",
    "Anexar currículo", "Anexar CV", "Selecionar arquivo", "Escolher arquivo",
    "Upload", "Anexar",
)
_SUBMIT_LABELS = (
    "Submit application", "Enviar candidatura", "Submit", "Enviar",
)
# Rótulo exato do botão de candidatura simplificada na tela do celular.
_EASY_APPLY_LABELS = (
    "Easy Apply to this job",
    "Easy Apply",
    "Candidatura rápida",
    "Candidatura rapida",
)


class DeviceApplicationAgent:
    """Processa a fila de candidaturas simplificadas no dispositivo logado."""

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

    # ---- utilidades ----
    @staticmethod
    def job_url(job: JobOpportunity) -> str:
        """URL pública com o ID real do LinkedIn — nunca URL sintética."""
        match = _ID_RE.search(job.external_id or "")
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
        match = _ID_RE.search(job.url or "")
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
        return job.external_apply_url or job.url

    def has_device(self) -> bool:
        """Indica se há celular autorizado, sem interromper a busca."""
        return self.adb.has_device()

    # ---- fila ----
    def queue(self, job: JobOpportunity) -> bool:
        """Aprova e enfileira uma vaga Easy Apply para o celular.

        A aprovação é coletada agora (com o contexto da vaga) e persistida;
        no momento do envio o agente apenas confirma que ela existe.
        """
        if self.repo.has_application_for(job.id):
            return False
        if job.easy_apply is not True:
            log_event(
                "easy_apply_not_confirmed",
                external_id=job.external_id,
                reason="botão de Easy Apply não foi visto na tela do celular",
            )
            return False
        approval = ApprovalRequest(
            trace_id=job.trace_id,
            opportunity_id=job.id,
            company=job.company,
            title=job.title,
            score=job.score,
            summary=job.analysis.summary if job.analysis else "",
        )
        self.repo.create_approval(approval)
        approved = self.gate.request(approval)
        self.repo.resolve_approval(approval.id, approved)
        if not approved:
            job.status = JobStatus.REJECTED_BY_FILTER
            self.repo.update_job(job)
            return False

        job.status = JobStatus.WAITING_DEVICE
        job.apply_failure_reason = ""
        self.repo.update_job(job)
        log_event(
            "application_queued_for_device",
            trace_id=job.trace_id,
            external_id=job.external_id,
            company=job.company,
        )
        if self.bus:
            self.bus.emit(
                "application.waiting_device",
                company=job.company,
                title=job.title,
                trace_id=job.trace_id,
            )
        return True

    def pending_jobs(self, limit: int | None = None) -> list[JobOpportunity]:
        """Vagas aguardando o celular, na ordem de prioridade configurada."""
        jobs = self.repo.list_jobs(status=JobStatus.WAITING_DEVICE)
        regions = {
            name: index
            for index, name in enumerate(self.settings.application.region_priority)
        }
        channels = {
            name: index
            for index, name in enumerate(self.settings.application.channel_priority)
        }
        jobs.sort(
            key=lambda job: (
                regions.get(job.region, len(regions)),
                channels.get(job.channel, len(channels)),
                -(job.posted_at.timestamp() if job.posted_at else 0),
                job.level_priority or 0,
                -(job.score or 0),
            )
        )
        return jobs[:limit] if limit else jobs

    # ---- execução no dispositivo ----
    def _ensure_approval(self, job: JobOpportunity) -> bool:
        """Garante aprovação humana registrada antes de qualquer envio."""
        approval = self.repo.get_approval_for_job(job.id)
        if approval is None:
            approval = ApprovalRequest(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                score=job.score,
                summary=job.analysis.summary if job.analysis else "",
            )
            self.repo.create_approval(approval)
        if approval.status == "APPROVED":
            return True
        if not self.gate.request(approval):
            self.repo.resolve_approval(approval.id, False)
            job.status = JobStatus.REJECTED_BY_FILTER
            self.repo.update_job(job)
            return False
        self.repo.resolve_approval(approval.id, True)
        return True

    @staticmethod
    def _normalized(text: str) -> str:
        """Normaliza texto da UI para comparação estável de título/empresa."""
        return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))

    def _wait_for_job_screen(self, job: JobOpportunity, timeout: float = 15.0) -> None:
        """Só prossegue quando a tela mostrar a empresa E o cargo esperados.

        Sem esta checagem, um deep link em tela antiga pode fazer o agente
        preencher a candidatura da vaga errada (ex.: cargo de manager).
        """
        expected_title = self._normalized(job.title)
        expected_company = self._normalized(job.company)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            visible = self._normalized(self.device.page_source())
            if (not expected_title or expected_title in visible) and (
                not expected_company or expected_company in visible
            ):
                return
            time.sleep(1.0)
        raise ValidationError(
            "tela do app não corresponde à vaga "
            f"{job.external_id}: esperado {job.company} / {job.title}"
        )

    def _assert_remote_screen(self, job: JobOpportunity) -> None:
        """Terceira validação: exige remoto explícito e ausência de presencial."""
        raw = self.device.page_source()
        # "hybrid cloud" é tecnologia, não modelo de trabalho.
        cleaned = re.sub(
            r"\bhybrid\s+(?:cloud|app|mobile|application|framework|integration|database)\b",
            " ", raw, flags=re.I,
        )
        if re.search(
            r"\bhybrid\b|\bh[íi]brido\b|\bon[\s-]?site\b|\bpresencial\b|\bin[\s-]?office\b",
            cleaned, re.I,
        ):
            raise ValidationError(
                f"vaga híbrida/presencial detectada na tela: {job.external_id}"
            )
        if not re.search(
            r"\bremote\b|\bremoto\b|work from home|\bwfh\b|anywhere|distributed|teletrabalho",
            cleaned, re.I,
        ):
            raise ValidationError(
                f"tela da vaga {job.external_id} não comprova 100% remoto"
            )

    def _wait_for_application_form(
        self, job: JobOpportunity, timeout: float = 12.0
    ) -> None:
        """Exige que o formulário aberto pertença à empresa da vaga aprovada.

        O app pode manter a tela anterior (ex.: outro empregador) mesmo depois
        do deep link. Sem esta checagem o agente preenche a candidatura errada.
        """
        expected_company = self._normalized(job.company)
        markers = (
            "apply to", "easy apply", "contact info", "candidatura",
            "informacoes de contato", "next", "proximo",
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            visible = self._normalized(self.device.page_source())
            has_form = any(self._normalized(m) in visible for m in markers)
            right_company = not expected_company or expected_company in visible
            if has_form and right_company:
                return
            time.sleep(1.0)
        raise ValidationError(
            "formulário exibido não corresponde à vaga "
            f"{job.external_id}: esperado {job.company}"
        )

    def _open_job_details(self, job: JobOpportunity) -> None:
        """Abre a vaga pelo ID real no app logado e valida empresa/título/remoto.

        Não clica em nenhum botão de candidatura: serve tanto para a sondagem
        de Easy Apply quanto para a candidatura propriamente dita.
        """
        self.device.page_source()  # inicia a sessão Appium/desbloqueia
        url = self.job_url(job)
        self.adb.open_url(
            url,
            label=job.external_id or job.id,
            package=self.settings.device.linkedin_package,
        )
        time.sleep(2.5)
        self._wait_for_job_screen(job)
        self._assert_remote_screen(job)
        self.device.detect_block()

    def _easy_apply_visible(self) -> bool:
        """Detecta o botão na tela SEM clicar, pelo rótulo da candidatura simplificada."""
        return any(self.device.has_text(label) for label in _EASY_APPLY_LABELS)

    def probe_easy_apply(self, job: JobOpportunity) -> bool:
        """Abre UMA vaga no celular e registra True/False na coluna tri-estado.

        A API pública não informa Easy Apply: a única fonte é o botão visto
        nesta tela. Nenhuma candidatura é iniciada aqui.
        """
        self._open_job_details(job)
        present = self._easy_apply_visible()
        job.easy_apply = present
        job.easy_apply_source = "device_screen"
        self.repo.update_job(job)
        log_event(
            "easy_apply_probe",
            external_id=job.external_id,
            easy_apply=present,
            company=job.company,
            title=job.title,
        )
        if self.bus:
            self.bus.emit(
                "easy_apply.probed",
                external_id=job.external_id,
                easy_apply=present,
            )
        return present

    def probe_easy_apply_logged(
        self, job: JobOpportunity, browser: LinkedInLoggedBrowser | None = None
    ) -> bool | None:
        """Sonda o botão no navegador LOGADO via CDP e grava o resultado.

        Abre uma aba nova no link direto da vaga, verifica o botão no DOM e
        fecha aaba. É a fonte preferencial: mais rápida e sem desbloqueio do
        celular. Retorna ``None`` quando não foi possível ler (ex.: CDP off).
        Nenhuma candidatura é iniciada e nenhuma aprovação é pedida aqui.
        """
        owns_browser = browser is None
        browser = browser or LinkedInLoggedBrowser()
        try:
            presente = browser.has_easy_apply(self.job_url(job))
        except LoggedBrowserUnavailable as exc:
            log_event(
                "easy_apply_probe_logged_unavailable",
                external_id=job.external_id,
                error=str(exc)[:200],
            )
            return None
        finally:
            if owns_browser:
                browser.close()
        if presente is None:
            return None
        job.easy_apply = presente
        job.easy_apply_source = "logged_browser"
        self.repo.update_job(job)
        log_event(
            "easy_apply_probe",
            external_id=job.external_id,
            easy_apply=presente,
            source="logged_browser",
            company=job.company,
            title=job.title,
        )
        if self.bus:
            self.bus.emit(
                "easy_apply.probed",
                external_id=job.external_id,
                easy_apply=presente,
                source="logged_browser",
            )
        return presente

    def probe_pending_jobs_logged(self, limit: int = 1) -> tuple[int, int]:
        """Sonda vagas ainda não analisadas no navegador logado, uma por vez.

        Retorna ``(com_easy_apply, sem_easy_apply)``. Somente grava a coluna
        tri-estado — NÃO enfileira nem pede aprovação. O envio de teste no
        celular é uma decisão humana posterior.
        """
        pending = [
            job for job in self.repo.list_jobs(status=JobStatus.QUALIFIED)
            if job.easy_apply is None
        ]
        if not pending:
            return 0, 0
        # Uma conexão CDP reutilizada: abrir/fechar o endpoint a cada vaga
        # derrubaria o Chrome do usuário.
        browser = LinkedInLoggedBrowser()
        try:
            browser.connect()
        except LoggedBrowserUnavailable as exc:
            log_event(
                "easy_apply_probe_logged_unavailable",
                error=str(exc)[:200],
            )
            return 0, 0
        yes = no = 0
        try:
            for index, job in enumerate(pending[:limit]):
                # Ritmo conservador: abrir vagas seguidas demais dispara o
                # authwall (HTTP 999) do LinkedIn. 1.2s entre elas.
                if index:
                    time.sleep(1.2)
                try:
                    resultado = self.probe_easy_apply_logged(job, browser=browser)
                    if resultado is True:
                        yes += 1
                    elif resultado is False:
                        no += 1
                except Exception as exc:
                    log_event(
                        "easy_apply_probe_logged_failed",
                        external_id=job.external_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        finally:
            browser.close()
        return yes, no

    def probe_pending_jobs(self, limit: int = 1) -> tuple[int, int]:
        """Sonda vagas qualificadas ainda não analisadas no celular.

        Retorna ``(com_easy_apply, sem_easy_apply)``. Uma vaga por vez, sempre
        pelo ID real, atualizando a coluna antes de seguir para a próxima.
        """
        if not self.has_device():
            log_event("easy_apply_probe_skipped_no_device")
            return 0, 0
        pending = [
            job for job in self.repo.list_jobs(status=JobStatus.QUALIFIED)
            if job.easy_apply is None
        ]
        yes = no = 0
        for job in pending[:limit]:
            self.adb.require_device()
            self.adb.unlock_if_needed()
            try:
                if self.probe_easy_apply(job):
                    yes += 1
                    self.queue(job)
                else:
                    no += 1
            except Exception as exc:
                log_event(
                    "easy_apply_probe_failed",
                    external_id=job.external_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return yes, no

    def _open_job(self, job: JobOpportunity) -> None:
        """Abre a vaga pelo ID real e entra no formulário de candidatura."""
        self._open_job_details(job)
        # O deep link pode abrir direto no formulário ou no detalhe da vaga.
        opened = any(self.device.click_text(label) for label in _EASY_APPLY_LABELS)
        if not opened:
            raise ValidationError(
                f"botão de Easy Apply não encontrado na tela: {job.external_id}"
            )
        # Evidência direta da tela: o botão de candidatura rápida existe.
        if job.easy_apply is not True:
            job.easy_apply = True
            job.easy_apply_source = "device_screen"
            self.repo.update_job(job)
        self._wait_for_application_form(job)

    def _upload_resume(self) -> None:
        """Copia o PDF para o celular e tenta anexá-lo no formulário."""
        resume = self.profile.documents.get("resume", "")
        if not resume:
            raise HumanActionRequired(
                "anexar CV", "nenhum PDF de currículo cadastrado no perfil"
            )
        remote = self.adb.push_file(resume, "candidate_resume.pdf")
        for label in _UPLOAD_LABELS:
            if self.device.click_text(label):
                break
        else:
            raise HumanActionRequired(
                "anexar CV",
                "botão de upload não encontrado; o pdf está em " + remote,
            )
        time.sleep(1.0)
        if not self.device.click_text("candidate_resume.pdf"):
            raise HumanActionRequired(
                "selecionar CV",
                "escolha candidate_resume.pdf no seletor de arquivos: " + remote,
            )
        log_event("resume_uploaded_on_device")

    def process_one(self, job: JobOpportunity) -> Application | None:
        """Executa uma candidatura simplificada no celular conectado."""
        if job.easy_apply is not True:
            log_event(
                "easy_apply_not_confirmed",
                external_id=job.external_id,
                reason="sem evidência de Easy Apply na tela do celular",
            )
            return None
        if not self._ensure_approval(job):
            return None
        if job.status != JobStatus.WAITING_DEVICE:
            job.status = JobStatus.WAITING_DEVICE
        job.status = JobStatus.APPLYING
        self.repo.update_job(job)

        self._open_job(job)
        self.device.detect_block()
        self._upload_resume()

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

        if sensitive:
            job.status = JobStatus.REQUIRES_HUMAN_ACTION
            job.has_applied = True
            job.applied_at = datetime.now()
            self.repo.update_job(job)
            log_event(
                "device_apply_requires_human",
                external_id=job.external_id,
                fields=", ".join(sensitive[:10]),
            )
            raise HumanActionRequired("campos sensíveis", ", ".join(sensitive[:10]))

        filled = self.device.fill_visible_inputs(values)
        log_event("device_apply_filling", fields=filled)
        # Revalida empresa e modelo de trabalho logo antes de revisar/enviar:
        # se o app trocou de tela, aborta sem clicar em Submit.
        self._wait_for_application_form(job, timeout=5.0)
        self._assert_remote_screen(job)
        self.device.click_text("Review") or self.device.click_text("Revisar")
        for label in _SUBMIT_LABELS:
            if self.device.click_text(label):
                break
        else:
            job.status = JobStatus.FAILED
            job.apply_failure_reason = "botão de envio não encontrado no app"
            self.repo.update_job(job)
            raise ValidationError(job.apply_failure_reason)

        application = Application(
            trace_id=job.trace_id,
            opportunity_id=job.id,
            company=job.company,
            title=job.title,
            status="SUBMITTED",
            channel="easy_apply",
            resume_path=self.profile.documents.get("resume", ""),
            portal_url=self.job_url(job),
            answers=answers,
        )
        self.repo.record_application(application)
        job.status = JobStatus.SUBMITTED
        job.has_applied = True
        job.applied_at = application.applied_at
        job.apply_failure_reason = ""
        self.repo.update_job(job)
        log_event(
            "device_application_submitted",
            trace_id=job.trace_id,
            external_id=job.external_id,
            company=job.company,
        )
        if self.bus:
            self.bus.emit(
                "application.submitted",
                company=job.company,
                title=job.title,
                channel="easy_apply",
            )
        return application

    def process_queue(
        self,
        max_jobs: int | None = None,
        control_check=None,
    ) -> tuple[int, int]:
        """Processa a fila quando o celular conecta.

        Retorna ``(enviadas, pendentes)``. Falhas por vaga não derrubam o
        restante da fila: a vaga volta para ``WAITING_DEVICE`` para nova tentativa.
        """
        if not self.has_device():
            log_event("device_queue_skipped_no_device")
            return 0, len(self.pending_jobs())

        # Primeiro descobre o Easy Apply no celular (uma vaga por vez) e grava
        # True/False na coluna. Só depois processa a fila de aprovadas.
        yes, no = self.probe_pending_jobs(limit=max_jobs or 1)
        log_event("easy_apply_probe_batch", com_easy_apply=yes, sem_easy_apply=no)

        daily = self.repo.get_daily_limit(
            limit=self.settings.limits.max_applications_per_day
        )
        limit = min(
            max_jobs or self.settings.limits.max_applications_per_run,
            self.settings.limits.max_applications_per_run,
            daily.remaining,
        )
        jobs = self.pending_jobs(limit=limit)
        submitted = 0
        for job in jobs:
            if control_check:
                control_check()
            if job.trace_id:
                set_trace_id(job.trace_id)
            if not self.repo.reserve_daily_slot(
                self.settings.limits.max_applications_per_day
            ):
                log_event("device_queue_daily_limit_reached")
                break
            self.adb.require_device()
            self.adb.unlock_if_needed()
            try:
                result = self.process_one(job)
                if result is not None:
                    submitted += 1
                else:
                    self.repo.release_daily_slot()
            except HumanActionRequired as exc:
                self.repo.release_daily_slot()
                # Se a exceção veio do upload/formulário, a vaga ficou em
                # APPLYING. Devolve para a fila com o motivo registrado.
                if job.status == JobStatus.APPLYING:
                    job.status = JobStatus.WAITING_DEVICE
                    job.apply_failure_reason = str(exc)
                    self.repo.update_job(job)
                log_event(
                    "device_apply_human_required",
                    external_id=job.external_id,
                    reason=str(exc),
                    status=job.status.value,
                )
            except Exception as exc:
                self.repo.release_daily_slot()
                reason = f"{type(exc).__name__}: {exc}"
                # Híbrido/vaga errada é rejeição definitiva, não retry.
                if any(
                    token in reason
                    for token in ("híbrida", "hibrida", "presencial", "não corresponde")
                ):
                    job.status = JobStatus.REJECTED_BY_FILTER
                    event = "device_apply_rejected_wrong_job"
                else:
                    job.status = JobStatus.WAITING_DEVICE
                    event = "device_apply_retry_queued"
                job.apply_failure_reason = reason
                self.repo.update_job(job)
                log_event(event, external_id=job.external_id, reason=reason)
        return submitted, len(self.pending_jobs())
