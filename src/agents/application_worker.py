"""Worker de aplicação — consome vagas QUALIFIED e prepara a candidatura.

Rotina SEPARADA da classificação. Ela recebe o aviso ``job.qualified``
(ou ``job.classified`` com ``decision=APPLY``), mas a fila real é o banco: toda
vaga ``QUALIFIED`` sem candidatura/aprovação é elegível. Antes de abrir o Chrome
logado, o worker revalida recência, remoto e moeda e decide se o portal é
conhecido.

Conhecimento/portal conocido combina quatro fontes, sem criar um agente por
site: ``Company.is_trusted_portal``, ``Company.has_account``,
``application.known_portals`` e as rotas aprendidas em ``SitePatternStore``
(mecanismo local de aprendizado — "Hermes" no vocabulário do produto). O envio
simplificado é a rota ``EASY_APPLY`` ("Zaply" no vocabulário do produto).

Este worker NUNCA clica em Submit. Ele para em ``PENDING_APPROVAL``; o envio
continua exclusivo do fluxo com ``APPROVED`` humano persistido.
"""
from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..core.events import Event, EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.web.site_patterns import SitePatternStore, domain_of
from ..models.domain import Company, JobOpportunity
from ..storage.database import Repository
from .browser_apply import ApplyRoute, BrowserApplyAgent
from .qualification import detect_currencies, is_remote_location, stale_reason


def portal_knowledge(
    job: JobOpportunity,
    company: Company | None,
    settings: Settings,
    patterns: SitePatternStore,
) -> dict[str, Any]:
    """Descreve o que o agente já sabe sobre o portal antes de abri-lo."""
    dominio = domain_of(job.external_apply_url or job.url)
    cfg = settings.application.portal_auth.get(dominio)
    known_portals = {
        Repository.normalize_company(portal) for portal in settings.application.known_portals
    }
    normalized = Repository.normalize_company(job.company)
    learned = dominio in patterns.load().get("sites", {})
    known = bool(
        (company is not None and (company.is_trusted_portal or company.has_account))
        or normalized in known_portals
        or cfg is not None
        or learned
    )
    if job.easy_apply is True:
        route = ApplyRoute.EASY_APPLY
    elif known:
        route = ApplyRoute.KNOWN_PORTAL
    else:
        route = ApplyRoute.EXTERNAL_SIGNUP
    return {
        "known_portal": known,
        "route": route.value,
        "domain": dominio,
        "learned_route": learned,
        "trusted_portal": bool(company is not None and company.is_trusted_portal),
        "has_account": bool(company is not None and company.has_account),
    }


def sort_for_application(
    jobs: list[JobOpportunity],
    settings: Settings,
    patterns: SitePatternStore,
    notified: set[str],
    companies: dict[str, Company] | None = None,
) -> list[JobOpportunity]:
    """Portal conhecido primeiro, depois avisos do evento, score e recência."""
    empresas = companies or {}

    def knowledge(job: JobOpportunity) -> dict[str, Any]:
        company = empresas.get(Repository.normalize_company(job.company))
        return portal_knowledge(job, company, settings, patterns)

    def key(job: JobOpportunity) -> tuple[int, int, int, float]:
        known = 0 if knowledge(job)["known_portal"] else 1
        return (
            known,
            0 if job.id in notified else 1,
            -(job.score or 0),
            -(job.posted_at.timestamp() if job.posted_at else 0),
        )

    return sorted(jobs, key=key)


class ApplicationWorker:
    """Prepara candidaturas e para no portão de aprovação humana."""

    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        apply_agent: BrowserApplyAgent,
        bus: EventBus | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.apply_agent = apply_agent
        self.bus = bus
        self.patterns = SitePatternStore(settings.resolve("data/site_patterns.json"))
        self.notified: set[str] = set()
        if bus is not None:
            bus.subscribe("job.qualified", self.on_event)
            bus.subscribe("job.classified", self.on_event)

    def on_event(self, event: Event) -> None:
        """O evento só acorda o worker; o status do banco decide o processamento."""
        payload = event.payload
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            return
        if event.type == "job.classified" and payload.get("decision") != "APPLY":
            return
        self.notified.add(job_id)
        log_event(
            "application_worker_notified",
            event_type=event.type,
            job_id=job_id,
            company=payload.get("company", ""),
        )

    def preflight(self, job: JobOpportunity) -> str | None:
        """Rechecks determinísticos. Devolve o motivo do bloqueio, se houver."""
        if self.repo.has_application_for(job.id):
            return "já existe candidatura registrada para esta vaga"
        approval = self.repo.get_approval_for_job(job.id)
        if approval is not None and approval.status in {"PENDING", "APPROVED"}:
            return f"aprovação {approval.status} já registrada; nada a preparar"
        stale = stale_reason(job, self.settings)
        if stale:
            job.status = JobStatus.EXPIRED
            self.repo.update_job(job)
            return f"fora da janela de recência: {stale}"
        if job.analysis is not None and job.analysis.is_remote is False:
            return "a vaga deixou de ser 100% remota"
        if self.settings.search.remote_only and not is_remote_location(job.location, job.title):
            return "remoto não confirmado na revalidação"
        detectadas = detect_currencies(f"{job.salary_raw or ''} {job.description or ''}")
        permitidas = {str(item).upper() for item in self.settings.search.currencies}
        if detectadas and not (detectadas & permitidas):
            return f"moeda não elegível: {', '.join(sorted(detectadas))}"
        return None

    def prepare_one(self, job: JobOpportunity) -> dict[str, Any]:
        bloqueio = self.preflight(job)
        if bloqueio:
            log_event("application_worker_skipped", job_id=job.id, reason=bloqueio)
            return {"job_id": job.id, "result": "skipped", "reason": bloqueio}
        company = self.repo.get_company_by_name(job.company)
        knowledge = portal_knowledge(job, company, self.settings, self.patterns)
        log_event("application_worker_portal", job_id=job.id, **knowledge)
        approval = self.apply_agent.prepare(job)
        if approval is None:
            return {"job_id": job.id, "result": "already_applied", **knowledge}
        details = {
            "job_id": job.id,
            "result": "prepared",
            "approval_id": approval.id,
            **knowledge,
        }
        log_event("application_worker_prepared", **details)
        if self.bus is not None:
            self.bus.emit(
                "application.prepared",
                job_id=job.id,
                company=job.company,
                title=job.title,
                route=knowledge["route"],
                known_portal=knowledge["known_portal"],
            )
        return details

    def run(self, limit: int | None = None) -> dict[str, int]:
        # O outbox é o handoff entre processos: a classificação pode ter rodado
        # na DGX, sem acesso ao Chrome CDP deste operador.
        eventos = self.repo.claim_agent_events("application", limit=max(50, limit or 0))
        evento_por_job: dict[str, str] = {}
        for evento in eventos:
            job_id = str(evento.get("job_id") or "")
            if not job_id:
                continue
            evento_por_job[job_id] = str(evento["id"])
            self.notified.add(job_id)
            log_event(
                "application_worker_event_claimed",
                event_type=evento.get("event_type", ""),
                job_id=job_id,
            )

        candidatas = self.repo.list_jobs(status=JobStatus.QUALIFIED)
        companies = {
            Repository.normalize_company(company.name): company
            for company in self.repo.list_companies()
        }
        ordenadas = sort_for_application(
            candidatas, self.settings, self.patterns, self.notified, companies
        )
        if limit:
            ordenadas = ordenadas[:limit]
        avisadas = len(self.notified)
        self.notified.clear()

        # Aviso para vaga que saiu da fila (ex.: rejeitada depois do evento) é
        # encerrado de imediato; o status do banco continua sendo a verdade.
        candidatas_ids = {job.id for job in candidatas}
        for job_id, evento_id in evento_por_job.items():
            if job_id not in candidatas_ids:
                self.repo.ack_agent_event(evento_id, status="ACK", error="fora da fila QUALIFIED")
                log_event("application_worker_event_dropped", job_id=job_id)

        counters = {
            "total": len(ordenadas),
            "prepared": 0,
            "already_applied": 0,
            "skipped": 0,
            "errors": 0,
            "known_portal": 0,
            "notified": avisadas,
            "events": len(eventos),
        }
        log_event("application_worker_started", **counters)
        for job in ordenadas:
            evento_id = evento_por_job.get(job.id)
            try:
                result = self.prepare_one(job)
            except Exception as exc:
                if evento_id:
                    self.repo.ack_agent_event(
                        evento_id, status="FAILED", error=f"{type(exc).__name__}: {exc}"
                    )
                counters["errors"] += 1
                log_event(
                    "application_worker_error",
                    job_id=job.id,
                    company=job.company,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if evento_id:
                self.repo.ack_agent_event(evento_id)
            if result.get("known_portal"):
                counters["known_portal"] += 1
            counters[result["result"]] += 1
        log_event("application_worker_finished", **counters)
        return counters
