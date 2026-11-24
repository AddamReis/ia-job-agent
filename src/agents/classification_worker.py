"""Worker de classificação — consulta o banco e classifica vagas pendentes.

Rotina SEPARADA da descoberta e da aplicação. A fonte da verdade é o banco:
toda vaga ``DISCOVERED`` sem qualificação entra na fila. A ordem prioriza
empresas com cadastro de portal confiável (``is_trusted_portal`` +
``trusted_portal_config``), depois empresas com conta conhecida e só então as
demais. Portais confiáveis usam o fast lane determinístico (sem LLM); os demais
usam o classificador com DGX.

O barramento em memória anuncia o resultado no mesmo processo; para o handoff
entre a DGX e a máquina local com CDP, o veredito ``APPLY`` também entra no
outbox ``agent_events`` (consumer ``application``). Nenhuma candidatura é
preparada ou enviada aqui.
"""
from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..models.domain import Company, JobOpportunity
from ..storage.database import Repository
from .fast_lane import FastLaneAgent
from .qualification import JobQualificationAgent, stale_reason


def company_priority(
    job: JobOpportunity,
    companies: dict[str, Company],
    known_portals: set[str],
) -> tuple[int, bool]:
    """Prioridade de classificação: portal confiável > conta conhecida > demais.

    Devolve ``(prioridade, conhecida)``. A flag do banco manda: o nome configurado
    em ``application.known_portals`` entra apenas como segundo nível, para não
    sobrepor um ajuste manual feito na tabela ``companies``.
    """
    company = companies.get(Repository.normalize_company(job.company))
    trusted = bool(
        company is not None
        and company.is_trusted_portal
        and bool(company.trusted_portal_config)
    )
    known = bool(
        trusted
        or (company is not None and company.has_account)
        or Repository.normalize_company(job.company) in known_portals
    )
    if trusted:
        return 0, True
    return (1 if known else 2), known


def sort_for_classification(
    jobs: list[JobOpportunity],
    companies: dict[str, Company],
    known_portals: set[str],
) -> list[JobOpportunity]:
    """Empresa conhecida primeiro; dentro do grupo, inclusão mais recente.

    A data que manda é ``created_at`` (entrada no banco), não ``posted_at``
    (publicação no portal). Isso mantém a fila estável mesmo quando o card
    descoberto traz uma data de publicação imprecisa ou ausente.
    """

    def key(job: JobOpportunity) -> tuple[int, float, str]:
        prioridade, _ = company_priority(job, companies, known_portals)
        inclusao = job.created_at or job.posted_at
        return (
            prioridade,
            -(inclusao.timestamp() if inclusao is not None else 0),
            (job.company or "").casefold(),
        )

    return sorted(jobs, key=key)


class ClassificationWorker:
    """Consome ``DISCOVERED`` sem qualificação e grava o veredito no banco."""

    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        qualification: JobQualificationAgent,
        bus: EventBus | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.qualification = qualification
        self.bus = bus
        self.known_portals = {
            Repository.normalize_company(portal)
            for portal in settings.application.known_portals
        }

    def pending(self, limit: int | None = None) -> list[JobOpportunity]:
        # Busca sem limit ANTES de ordenar: limitar primeiro deixaria empresas
        # conhecidas de fora da janela só por terem chegado depois.
        candidatas = self.repo.list_jobs(status=JobStatus.DISCOVERED)
        pendentes = [job for job in candidatas if job.qualification is None]
        companies = {
            Repository.normalize_company(company.name): company
            for company in self.repo.list_companies()
        }
        ordenadas = sort_for_classification(pendentes, companies, self.known_portals)
        return ordenadas[:limit] if limit else ordenadas

    def _classify(self, job: JobOpportunity) -> dict[str, Any]:
        companies = {
            Repository.normalize_company(company.name): company
            for company in self.repo.list_companies()
        }
        prioridade, known = company_priority(job, companies, self.known_portals)
        company = companies.get(Repository.normalize_company(job.company))
        config = company.trusted_portal_config if company is not None else {}
        lane = "llm"

        if prioridade == 0 and config:
            lane = "fast_lane"
            # Recência também vale no fast lane: vaga antiga é histórico, não
            # aprovação determinística.
            stale = stale_reason(job, self.settings)
            if stale:
                job.status = JobStatus.EXPIRED
                job.score = 0
                job.qualification = None
                self.repo.update_job(job)
                log_event("classification_skipped_stale", job_id=job.id, reason=stale)
                return {
                    "job_id": job.id,
                    "decision": "SKIP",
                    "lane": lane,
                    "known_portal": known,
                    "reason": stale,
                }
            result = FastLaneAgent(self.repo, self.settings, company.name).qualify(job, config)
            decision, score = result.decision, result.score
        else:
            decision, _ = self.qualification._process_one(job)
            score = job.score or 0

        details = {
            "job_id": job.id,
            "decision": decision,
            "score": score,
            "lane": lane,
            "known_portal": known,
            "company": job.company,
        }
        log_event("classification_worker_classified", **details)
        if self.bus is not None:
            self.bus.emit("job.classified", **details)
            if decision == "APPLY":
                # O classificador LLM já emite job.qualified; o fast lane não.
                # Eventual duplicata é inofensiva porque o worker de aplicação
                # usa o status do banco como fonte da verdade.
                if lane == "fast_lane":
                    self.bus.emit(
                        "job.qualified",
                        job_id=job.id,
                        company=job.company,
                        title=job.title,
                        score=score,
                        decision=decision,
                    )
        return details

    def run(self, limit: int | None = None) -> dict[str, int]:
        candidatas = self.pending(limit)
        counters = {
            "total": len(candidatas),
            "qualified": 0,
            "rejected": 0,
            "errors": 0,
            "known_portal": 0,
        }
        log_event("classification_worker_started", total=len(candidatas))
        for job in candidatas:
            try:
                result = self._classify(job)
            except Exception as exc:
                counters["errors"] += 1
                log_event(
                    "classification_worker_error",
                    job_id=job.id,
                    company=job.company,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if result.get("known_portal"):
                counters["known_portal"] += 1
            if result["decision"] == "APPLY":
                counters["qualified"] += 1
                # Handoff persistente: a DGX não tem o Chrome CDP logado. O
                # worker local de aplicação consume este aviso mesmo após
                # restart, consultando depois o status real no banco.
                self.repo.publish_agent_event(
                    "application",
                    "job.qualified",
                    job.id,
                    {
                        "company": job.company,
                        "title": job.title,
                        "score": result.get("score"),
                        "lane": result.get("lane"),
                        "known_portal": bool(result.get("known_portal")),
                    },
                )
            else:
                counters["rejected"] += 1
        log_event("classification_worker_finished", **counters)
        return counters
