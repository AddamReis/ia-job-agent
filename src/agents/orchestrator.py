"""Agente 1 — orquestrador: estado, prioridades, limites e checkpoint."""
from __future__ import annotations

import threading
from datetime import datetime
from uuid import uuid4

from ..core.config import Settings
from ..core.errors import (
    BlockedByPlatformError,
    HumanActionRequired,
    LimitReached,
    PausedError,
    StoppedError,
)
from ..core.events import EventBus
from ..core.logging import clear_trace_id, configure_logging, log_event, set_trace_id
from ..core.state import ControlFlags, ExecutionStatus, JobStatus, RunState
from ..execution.android.adb import ADBClient
from ..execution.android.appium_driver import AppiumDevice
from ..execution.android.credential_store import CredentialStore
from ..execution.forms.answers import AnswerResolver
from ..execution.gmail.gmail_client import GmailClient
from ..execution.profile.builder import build_profile
from ..execution.web.linkedin_logged import LinkedInLoggedBrowser
from ..execution.web.linkedin_public import LinkedInPublicBrowser
from ..llm.factory import LLMRouter
from ..models.domain import CandidateProfile, ExecutionHistory, JobOpportunity
from ..storage.database import Repository
from .application import ApplicationAgent
from .approval import ApprovalGate
from .browser_research import BrowserResearchAgent
from .company_suppression import CompanySuppressionAgent
from .dependencies import DependencyAgent
from .device_apply import DeviceApplicationAgent
from .eligibility import EligibilityAgent
from .email import EmailVerificationAgent
from .notifications import NotificationAgent
from .qualification import JobQualificationAgent, stale_reason
from .qualification_memory import QualificationMemory
from .run_report import RunReportAgent
from .research import JobResearchAgent
from .validation import ValidationAgent


def sort_application_queue(
    jobs: list[JobOpportunity],
    channel_priority: dict[str, int],
    region_priority: dict[str, int] | None = None,
) -> list[JobOpportunity]:
    """Região, canal, recência, nível explícito (sem nível primeiro) e score."""
    regions = region_priority or {}
    return sorted(
        jobs,
        key=lambda job: (
            regions.get(job.region, len(regions)),
            channel_priority.get(job.channel, len(channel_priority)),
            -(job.posted_at.timestamp() if job.posted_at else 0),
            job.level_priority or 0,
            -(job.score or 0),
        ),
    )


class Orchestrator:
    def __init__(self, settings: Settings, bus: EventBus | None = None) -> None:
        self.settings = settings
        self.bus = bus or EventBus()
        configure_logging(settings.resolve(settings.storage.log_dir))
        self.repo = Repository(
            settings.resolve(settings.storage.database),
            known_portals=settings.application.known_portals,
        )
        self.control = ControlFlags(settings.resolve(settings.storage.control_file))

        self.profile = self.repo.load_profile(CandidateProfile) or build_profile(settings)
        self.repo.save_profile(self.profile)

        self.llm = LLMRouter(settings, self.bus)
        unlock = settings.device.unlock_secret
        self.adb = ADBClient(
            settings.device.adb_path,
            CredentialStore(unlock.keyring_service, unlock.keyring_username),
        )
        self.device = AppiumDevice(settings, self.adb)
        self.resolver = AnswerResolver(self.profile, self.llm, settings, self.bus)
        # Memória de classificação: decisões humanas e desfechos voltam como
        # contexto auditável para as próximas qualificações.
        self.qualification_memory = QualificationMemory(self.repo)
        self.gate = ApprovalGate(
            settings,
            self.bus,
            on_decision=self.qualification_memory.record_approval,
        )

        self.research = JobResearchAgent(self.device, self.repo, settings, self.bus)
        self.browser = LinkedInPublicBrowser(settings)
        self.browser_research = BrowserResearchAgent(
            self.browser, self.repo, settings, self.bus
        )
        self._search_errors: list[str] = []
        self.current_trace_id = ""
        self.dependencies = DependencyAgent(self.repo, settings, self.bus)
        self.repo.seed_known_companies()
        self.eligibility = EligibilityAgent(settings, self.llm, self.repo)
        # Supressão temporária: confirmada em outras vagas antes de marcar a empresa.
        self.company_suppression = CompanySuppressionAgent(
            settings, self.repo, self.llm, eligibility=self.eligibility
        )
        self.qualification = JobQualificationAgent(
            self.llm, self.profile, self.repo, settings, self.bus,
            self.dependencies, self.eligibility,
            suppression=self.company_suppression,
            memory=self.qualification_memory,
        )
        self.application = ApplicationAgent(
            self.device, self.adb, self.profile, self.resolver, self.gate,
            self.repo, settings, self.bus,
        )
        self.device_apply = DeviceApplicationAgent(
            self.device, self.adb, self.profile, self.resolver, self.gate,
            self.repo, settings, self.bus,
        )
        self.validation = ValidationAgent(self.device, self.repo, self.bus)
        self.gmail = GmailClient(settings)
        self.email = EmailVerificationAgent(self.gmail, self.repo, settings, self.bus)
        self.notifications = NotificationAgent(self.gmail, self.repo, settings, self.bus)
        self.run_report = RunReportAgent(self.gmail, self.repo, settings)

    def refresh_profile_from_linkedin(self, profile_url: str = "") -> list[str]:
        """Atualiza o perfil com as skills atuais do LinkedIn logado.

        O documento usado pelo analista semântico passa a conter CV + skills
        reais do perfil. O objeto é alterado em vez de substituído para que os
        agentes já construídos vejam a atualização imediatamente.
        """
        url = (profile_url or self.profile.personal.linkedin).strip()
        if not url:
            url = "https://www.linkedin.com/in/me"
        skills = LinkedInLoggedBrowser().read_profile_skills(url)
        if not skills:
            return []

        atual = list(self.profile.skills.get("linkedin", []))
        vistos = {item.casefold() for item in atual}
        for skill in skills:
            if skill.casefold() not in vistos:
                atual.append(skill)
                vistos.add(skill.casefold())
        self.profile.skills["linkedin"] = sorted(atual, key=str.casefold)
        fonte = f"linkedin_skills:{url}"
        if fonte not in self.profile.source_files:
            self.profile.source_files.append(fonte)
        self.repo.save_profile(self.profile)

        for agent in (
            self.resolver,
            self.qualification,
            self.application,
            self.device_apply,
        ):
            if hasattr(agent, "profile"):
                agent.profile = self.profile

        log_event("profile_linkedin_skills_refreshed", skills=len(skills), url=url)
        return self.profile.skills["linkedin"]

    def reclassify_jobs(
        self,
        statuses: list[JobStatus] | None = None,
        limit: int | None = None,
        job_ids: list[str] | None = None,
        trace_id: str | None = None,
        all_statuses: bool = False,
        ignore_recency: bool = False,
    ) -> int:
        """Reanalisa vagas já salvas com as regras atuais (sem candidatar).

        O estado antigo de qualificação é limpo antes de cada vaga voltar para
        a fila de análise. ``easy_apply`` já preenchido é preservado. ``job_ids``
        reanalisa IDs específicos e ``trace_id`` reanalisa apenas as vagas da
        mesma execução (rota alternativa, "ID de potência"). ``all_statuses``
        inclui rejeições e vencidas.

        Dados já existentes não passam pelo circuito de supressão de empresa:
        se a empresa estivesse suprimida, a vaga nunca teria entrado na tabela.
        A supressão continua ativa apenas para vagas recém-descobertas.
        ``ignore_recency`` desativa a janela de 6h apenas nesta reanálise (útil
        para auditar o histórico); a fila de candidaturas revalida a idade.
        """
        terminal = {
            JobStatus.SUBMITTED,
            JobStatus.FAILED,
            JobStatus.REQUIRES_HUMAN_ACTION,
            JobStatus.WAITING_DEVICE,
            JobStatus.PENDING_APPROVAL,
            JobStatus.APPLYING,
        }
        if trace_id:
            vagas = [
                job
                for job in self.repo.list_jobs(trace_id=trace_id, limit=limit)
                if job.status not in terminal
            ]
            log_event("reclassify_by_trace", trace_id=trace_id, count=len(vagas))
        elif job_ids:
            vagas = [
                job
                for job_id in job_ids
                if (job := self.repo.get_job(job_id)) is not None
                and job.status not in terminal
            ]
            if limit:
                vagas = vagas[:limit]
        elif all_statuses:
            vagas = [
                job
                for job in self.repo.list_jobs(limit=limit)
                if job.status not in terminal
            ]
        else:
            alvos = statuses or [JobStatus.DISCOVERED, JobStatus.QUALIFIED]
            vagas = self.repo.list_jobs(status=alvos, limit=limit)
        status_anterior = {job.id: job.status for job in vagas}
        for job in vagas:
            job.status = JobStatus.DISCOVERED
            job.has_restriction = False
            job.restriction_kinds = []
            job.restriction_details = ""
            job.qualification = None
            job.fit_assessment = None
            job.score = 0
            job.score_breakdown = {}
            self.repo.update_job(job)
        log_event("reclassify_queued", count=len(vagas))
        suppression = self.qualification.suppression
        previous_ignore_recency = self.qualification.ignore_recency
        self.qualification.suppression = None
        self.qualification.ignore_recency = previous_ignore_recency or ignore_recency
        try:
            processadas = self.qualification.run(job_ids=[job.id for job in vagas])
            memory = getattr(self, "qualification_memory", None)
            for job_id, anterior in status_anterior.items():
                atual = self.repo.get_job(job_id)
                if atual is not None and memory is not None:
                    memory.record_reclassification(anterior, atual)
            return processadas
        finally:
            self.qualification.suppression = suppression
            self.qualification.ignore_recency = previous_ignore_recency

    # ---- comandos de controle ----
    def pause(self) -> None:
        self.control.pause()
        log_event("execution_pause_requested")

    def resume(self) -> None:
        self.control.resume()
        log_event("execution_resume_requested")

    def stop(self) -> None:
        self.control.stop()
        log_event("execution_stop_requested")

    def status(self) -> dict:
        try:
            devices = self.adb.devices()
        except Exception as exc:
            devices = []
            log_event("device_probe_failed", error=str(exc))
        connected = next((d for d in devices if d.authorized), None)
        secrets_path = self.settings.resolve(self.settings.gmail.credentials_file)
        return {
            "device": (
                f"{connected.model or connected.serial} ({connected.serial})"
                if connected
                else "não conectado / não autorizado"
            ),
            "adb": self.adb.version() if devices else "disponível",
            "linkedin_instalado": (
                self.adb.is_installed(self.settings.device.linkedin_package)
                if connected
                else None
            ),
            "unlock_secret": "cadastrado" if self.adb.credentials.has_unlock_secret() else "ausente",
            "llm": self.llm.primary_name,
            "profile": (
                f"{self.profile.personal.full_name} — "
                f"{len(self.profile.experiences)} experiências"
            ),
            "gmail": "credencial de cliente presente" if secrets_path.exists() else "não configurado",
            "execution": self.control.status(),
            "stats": self.repo.stats(),
        }

    def report(self) -> dict:
        stats = self.repo.stats()
        executions = self.repo.list_executions(limit=10)
        daily = self.repo.get_daily_limit(limit=self.settings.limits.max_applications_per_day)
        return {
            "stats": stats,
            "daily": {
                "date": daily.date,
                "submitted": daily.submitted,
                "limit": daily.limit,
                "remaining": daily.remaining,
            },
            "executions": [execution.model_dump(mode="json") for execution in executions],
            "applications": [
                app.model_dump(mode="json")
                for app in self.repo.list_applications(limit=10)
            ],
        }

    # ---- fila Easy Apply (celular) ----
    def pending_device_jobs(self, limit: int | None = None) -> list:
        return self.device_apply.pending_jobs(limit=limit)

    def process_device_queue(self, max_jobs: int | None = None) -> tuple[int, int]:
        """Processa a fila quando o celular conecta; usa o trace de cada vaga."""
        self.control.check()
        submitted, pending = self.device_apply.process_queue(
            max_jobs=max_jobs, control_check=self.control.check
        )
        log_event("device_queue_processed", submitted=submitted, pending=pending)
        return submitted, pending

    def probe_easy_apply_logged(self, limit: int = 1) -> tuple[int, int]:
        """Sonda Easy Apply no navegador logado, uma vaga por vez (sem envio)."""
        yes, no = self.device_apply.probe_pending_jobs_logged(limit=limit)
        log_event("logged_probe_batch", com_easy_apply=yes, sem_easy_apply=no)
        return yes, no

    def complete_dependency(
        self,
        dependency_id: str | None = None,
        *,
        opportunity_id: str | None = None,
        kind: str | None = None,
        note: str = "",
    ):
        return self.dependencies.complete(
            dependency_id,
            opportunity_id=opportunity_id,
            kind=kind,
            note=note,
        )

    # ---- execução ----
    def _run_search_only(self) -> None:
        """Descobre vagas sem iniciar a qualificação em paralelo.

        Usado quando outra reclassificação já está consumindo a LLM/banco:
        apenas grava cards novos como ``DISCOVERED`` para análise posterior.
        """
        research = (
            self.browser_research
            if self.settings.search.source == "browser"
            else self.research
        )
        research.trace_id = self.current_trace_id
        self.research.trace_id = self.current_trace_id
        self.browser_research.trace_id = self.current_trace_id
        research.run()

    def _run_search_with_live_qualification(self) -> None:
        """Descobre vagas e qualifica em paralelo na LLM local.

        A fonte padrão é o browser público (páginas HTML, IDs reais). O celular
        fica reservado para a candidatura. Se a fonte browser falhar e o fallback
        estiver habilitado, a busca usa Appium. Qualificação usa só LLM + SQLite,
        numa thread que consome ``DISCOVERED`` conforme a busca persiste.
        """
        stop = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []

        research = (
            self.browser_research
            if self.settings.search.source == "browser"
            else self.research
        )
        research.trace_id = self.current_trace_id
        self.research.trace_id = self.current_trace_id
        self.browser_research.trace_id = self.current_trace_id
        self.qualification.trace_id = self.current_trace_id

        def worker() -> None:
            try:
                # Thread filha não herda o ContextVar do loop principal.
                set_trace_id(self.current_trace_id)
                self.qualification.run_live(
                    stop,
                    settle_seconds=5.0,  # tempo para o enrichment da descrição
                    abort_check=self.control.check,
                )
            except Exception as exc:  # nunca derruba a busca
                log_event("live_qualification_error", error=str(exc))
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(
            target=worker, name="qualification-worker", daemon=True
        )
        thread.start()
        log_event("live_qualification_started")
        try:
            # Nunca cair para o celular: descoberta é somente HTML público.
            research.run()
        finally:
            # A busca terminou: sinaliza o worker para drenar o que sobrou.
            stop.set()
            finished.wait(timeout=self.settings.browser.drain_timeout_seconds)
        log_event("live_qualification_finished", errors=len(errors))
        if errors:
            detail = f"qualificação: {errors[0]}"
            self._search_errors.append(detail)
            log_event("live_qualification_error_detail", error=str(errors[0]))

    def run(
        self,
        max_applications: int | None = None,
        phase: str = "all",
        qualify_live: bool = True,
    ) -> RunState:
        self.control.check()
        run_id = str(uuid4())
        # Um único ID correlaciona web -> banco -> LLM -> candidatura -> e-mail.
        trace_id = run_id
        self.current_trace_id = trace_id
        set_trace_id(trace_id)
        self.control.set_run(run_id)
        state = RunState(
            run_id=run_id, trace_id=trace_id, status=ExecutionStatus.RUNNING
        )
        history = ExecutionHistory(
            id=run_id,
            trace_id=trace_id,
            started_at=datetime.now(),
            status=ExecutionStatus.RUNNING.value,
        )
        self.repo.create_execution(history)
        started = datetime.now()
        log_event("execution_started", run_id=run_id, phase=phase)

        try:
            if phase in {"all", "search"}:
                self._search_errors = []
                if qualify_live:
                    self._run_search_with_live_qualification()
                else:
                    self._run_search_only()
                state.errors.extend(self._search_errors)
                state.jobs_discovered = self.repo.stats()["jobs_total"]
                self._checkpoint(state)
            if phase in {"all", "apply"}:
                self.qualification.run()
                # Revalida a janela mesmo para vagas qualificadas em rodadas anteriores.
                jobs = [
                    job
                    for job in self.repo.list_jobs(status=JobStatus.QUALIFIED)
                    if stale_reason(job, self.settings) is None
                ]
                state.jobs_analyzed = self.repo.stats()["jobs_total"]
                state.jobs_qualified = len(jobs)
                # Região (América do Norte > AU > NZ > Europa > Brasil) primeiro,
                # depois canal (simplificada -> portal conhecido -> externo),
                # depois publicação mais recente e score como desempate.
                channel_priority = {
                    name: index
                    for index, name in enumerate(self.settings.application.channel_priority)
                }
                region_priority = {
                    name: index
                    for index, name in enumerate(self.settings.application.region_priority)
                }
                jobs = sort_application_queue(jobs, channel_priority, region_priority)

                daily = self.repo.get_daily_limit(
                    limit=self.settings.limits.max_applications_per_day
                )
                limit = min(
                    max_applications or self.settings.limits.max_applications_per_run,
                    self.settings.limits.max_applications_per_run,
                    daily.remaining,
                )
                if limit <= 0:
                    raise LimitReached("limite diário de candidaturas atingido")

                device_online = self.device_apply.has_device()
                for job in jobs[:limit]:
                    self.control.check()
                    # Easy Apply exige sessão logada: sem celular, enfileira e
                    # mantém a vaga pronta para processar quando ele conectar.
                    if (
                        not device_online
                        and job.easy_apply is True
                        and job.channel == "easy_apply"
                    ):
                        queued = self.device_apply.queue(job)
                        log_event(
                            "easy_apply_queued",
                            external_id=job.external_id,
                            queued=queued,
                            trace_id=job.trace_id,
                        )
                        self._checkpoint(state)
                        continue
                    self.device.detect_block()
                    state.current_job = {
                        "company": job.company,
                        "title": job.title,
                        "location": job.location,
                        "score": job.score,
                        "status": "Preparando",
                    }
                    self._checkpoint(state)

                    approval = self.application.prepare(job)
                    if approval is None:
                        # Portal confirmou candidatura já existente: registrada
                        # pelo agente, sem aprovação e sem novo envio.
                        state.current_job["status"] = "Já candidaturada"
                        self._checkpoint(state)
                        continue
                    state.current_job["status"] = "Aguardando aprovação"
                    self._checkpoint(state)
                    if not self.application.confirm(approval):
                        self.repo.resolve_approval(approval.id, False)
                        job.status = JobStatus.REJECTED_BY_FILTER
                        self.repo.update_job(job)
                        self._checkpoint(state)
                        continue

                    if not self.repo.reserve_daily_slot(
                        self.settings.limits.max_applications_per_day
                    ):
                        raise LimitReached("limite diário de candidaturas atingido")
                    application = None
                    state.current_job["status"] = "Enviando"
                    self._checkpoint(state)
                    try:
                        application = self.application.submit(job, approval, confirmed=True)
                        if application is not None:
                            self.qualification_memory.record_outcome(job, "SUBMITTED")
                            self.validation.validate(job)
                            self.email.check(job)
                            state.applications_submitted += 1
                    except Exception as exc:
                        self.qualification_memory.record_outcome(job, "FAILED", str(exc))
                        if application is None:
                            self.repo.release_daily_slot()
                        raise
                    self._checkpoint(state)
            state.status = ExecutionStatus.COMPLETED

        except PausedError:
            state.status = ExecutionStatus.PAUSED
            log_event("execution_paused", run_id=run_id)
        except StoppedError:
            state.status = ExecutionStatus.STOPPED
            log_event("execution_stopped", run_id=run_id)
        except BlockedByPlatformError as exc:
            state.status = ExecutionStatus.STOPPED
            state.errors.append(str(exc))
            log_event("execution_blocked", run_id=run_id, reason=str(exc))
            if self.bus:
                self.bus.emit("platform.blocked", reason=str(exc))
        except (HumanActionRequired, LimitReached) as exc:
            state.status = ExecutionStatus.ERROR
            state.errors.append(str(exc))
            log_event("execution_needs_human", run_id=run_id, reason=str(exc))
            if self.bus:
                self.bus.emit("human.required", reason=str(exc))
        except Exception as exc:
            state.status = ExecutionStatus.ERROR
            state.errors.append(f"{type(exc).__name__}: {exc}")
            log_event("execution_error", run_id=run_id, error=str(exc))
        finally:
            history.status = state.status.value
            history.finished_at = datetime.now()
            history.jobs_discovered = state.jobs_discovered
            history.jobs_analyzed = state.jobs_analyzed
            history.jobs_qualified = state.jobs_qualified
            history.applications_submitted = state.applications_submitted
            history.errors = state.errors
            history.duration_seconds = (history.finished_at - started).total_seconds()
            self.repo.update_execution(history)
            self._checkpoint(state)
            self._send_notifications(state)
            clear_trace_id()
            self.device.close()
        return state

    def _send_notifications(self, state: RunState) -> None:
        """Dois e-mails, sempre best-effort: nunca derrubam a execução."""
        try:
            self.run_report.send(state)
        except Exception as exc:
            log_event("run_summary_email_error", error=str(exc))
        try:
            self.notifications.send_reminder()
        except Exception as exc:
            log_event("human_dependency_email_error", error=str(exc))

    def _checkpoint(self, state: RunState) -> None:
        self.repo.save_checkpoint(state.run_id, state.snapshot())
        if self.bus:
            self.bus.emit("state.changed", **state.snapshot())
