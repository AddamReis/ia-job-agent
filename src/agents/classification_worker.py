"""Classificação em streaming, com rede de segurança limitada.

A descoberta publica ``job.discovered`` com o ``job_id`` assim que a vaga é
gravada; ``StreamingClassifier`` classifica imediatamente num pool limitado e
publica o veredito no outbox. Isso preserva o timing da vaga: a análise não
espera o fim do slot nem acumula centenas de registros.

O worker contínuo (``classification-serve``) é apenas rede de segurança: pega
só resíduos recentes, com teto e SLA. Nunca reprocessa o histórico — vaga fora
da janela útil vira ``EXPIRED`` em vez de ocupar a fila.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait
from datetime import datetime
from typing import Any

from ..core.config import Settings
from ..core.events import Event, EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..models.domain import Company, JobOpportunity, QualificationResult
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


def runner_healthy(settings: Settings) -> bool:
    """Checa o provider de classificação antes de encher a fila de erros.

    Qualquer resposta HTTP (até 401) prova que o servidor está vivo; só
    falha de conexão/timeout interrompe o fluxo. O endpoint de modelos é
    barato e evita o erro em lote que já derrubou 214 vagas de uma vez.
    """
    cfg = settings.classification
    name = settings.llm.runner or settings.llm.primary
    provider = settings.llm.providers.get(name)
    if provider is None:
        return False
    url = provider.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {provider.api_key}"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=cfg.health_check_timeout_seconds
        ) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _age_seconds(job: JobOpportunity, now: datetime | None = None) -> float:
    reference = now or datetime.now()
    entrada = job.created_at or job.updated_at or job.posted_at
    if entrada is None:
        return 0.0
    return max(0.0, (reference - entrada).total_seconds())


def _empty_counters() -> dict[str, int]:
    return {"total": 0, "qualified": 0, "rejected": 0, "errors": 0, "known_portal": 0}


class ClassificationWorker:
    """Classifica vagas ``DISCOVERED`` sem qualificação, em paralelo limitado."""

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

    def pending(
        self,
        limit: int | None = None,
        *,
        max_age_minutes: int | None = None,
    ) -> list[JobOpportunity]:
        """Fila pendente, opcionalmente limitada por idade da descoberta.

        Busca sem limit ANTES de ordenar: limitar primeiro deixaria empresas
        conhecidas de fora da janela só por terem chegado depois.
        """
        candidatas = self.repo.list_jobs(status=JobStatus.DISCOVERED)
        pendentes = [job for job in candidatas if job.qualification is None]
        if max_age_minutes is not None:
            teto = max_age_minutes * 60
            agora = datetime.now()
            pendentes = [job for job in pendentes if _age_seconds(job, agora) <= teto]
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

        # Recência vale para as duas rotas: vaga antiga é histórico, nunca
        # aprovação determinística nem pelo LLM local.
        stale = stale_reason(job, self.settings)
        if stale:
            job.status = JobStatus.EXPIRED
            job.score = 0
            job.qualification = QualificationResult(
                score=0,
                decision="SKIP",
                reasons=[stale],
                hard_reject_reason=stale,
            )
            self.repo.update_job(job)
            log_event("classification_skipped_stale", job_id=job.id, reason=stale)
            return {
                "job_id": job.id,
                "decision": "SKIP",
                "lane": lane,
                "known_portal": known,
                "reason": stale,
            }

        if prioridade == 0 and config:
            lane = "fast_lane"
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

    def _publish_result(self, result: dict[str, Any]) -> None:
        if result.get("decision") != "APPLY":
            return
        # Handoff persistente: a DGX não tem o Chrome CDP logado. O worker
        # local de aplicação consome este aviso mesmo após restart.
        self.repo.publish_agent_event(
            "application",
            "job.qualified",
            result["job_id"],
            {
                "company": result.get("company", ""),
                "title": result.get("title", ""),
                "score": result.get("score"),
                "lane": result.get("lane"),
                "known_portal": bool(result.get("known_portal")),
            },
        )

    def _classify_one(self, job: JobOpportunity) -> dict[str, Any] | None:
        """Revalida o status no banco antes de classificar (streaming + safety net)."""
        atual = self.repo.get_job(job.id)
        if atual is None or atual.status != JobStatus.DISCOVERED:
            return None
        if atual.qualification is not None:
            return None
        result = self._classify(atual)
        result["title"] = atual.title
        self._publish_result(result)
        return result

    def _run_batch(self, candidatas: list[JobOpportunity]) -> dict[str, int]:
        counters = _empty_counters()
        counters["total"] = len(candidatas)
        if not candidatas:
            return counters
        pool_size = min(self.settings.classification.workers, len(candidatas))
        log_event("classification_worker_started", total=len(candidatas), workers=pool_size)
        if len(candidatas) > self.settings.classification.max_pending_alert:
            log_event(
                "classification_queue_alert",
                total=len(candidatas),
                limit=self.settings.classification.max_pending_alert,
            )
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures: dict[Future, JobOpportunity] = {
                pool.submit(self._classify_one, job): job for job in candidatas
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    counters["errors"] += 1
                    log_event(
                        "classification_worker_error",
                        job_id=job.id,
                        company=job.company,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                if result is None:
                    continue
                if result.get("known_portal"):
                    counters["known_portal"] += 1
                if result["decision"] == "APPLY":
                    counters["qualified"] += 1
                else:
                    counters["rejected"] += 1
        log_event("classification_worker_finished", **counters)
        return counters

    def run(self, limit: int | None = None) -> dict[str, int]:
        """Classifica o backlog pendente (comando explícito ``classification-run``)."""
        return self._run_batch(self.pending(limit))

    def run_safety_net(self) -> dict[str, int]:
        """Rede de segurança: só resíduos recentes, com teto, SLA e health check."""
        cfg = self.settings.classification
        counters = _empty_counters()
        if not cfg.safety_net_enabled:
            log_event("classification_safety_net_disabled")
            return counters
        if not runner_healthy(self.settings):
            log_event(
                "classification_safety_net_unhealthy",
                runner=self.settings.llm.runner or self.settings.llm.primary,
            )
            return counters
        agora = datetime.now()
        candidatas = [
            job for job in self.repo.list_jobs(status=JobStatus.DISCOVERED)
            if job.qualification is None
        ]
        expiradas = 0
        for job in candidatas:
            if _age_seconds(job, agora) <= cfg.stale_after_hours * 3600:
                continue
            motivo = "fora da janela de classificação (SLA)"
            job.status = JobStatus.EXPIRED
            job.score = 0
            job.qualification = QualificationResult(
                score=0,
                decision="SKIP",
                reasons=[motivo],
                hard_reject_reason=motivo,
            )
            self.repo.update_job(job)
            expiradas += 1
        log_event("classification_sla_expired", count=expiradas)
        residuos = self.pending(limit=cfg.safety_net_limit, max_age_minutes=cfg.safety_net_max_age_minutes)
        counters = self._run_batch(residuos)
        counters["expired"] = expiradas
        return counters


class StreamingClassifier:
    """Classifica cada vaga no instante em que a descoberta a persiste."""

    def __init__(
        self,
        worker: ClassificationWorker,
        bus: EventBus | None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.worker = worker
        self.bus = bus
        cfg = worker.settings.classification
        self.enabled = cfg.stream_enabled if enabled is None else enabled
        self._pool = ThreadPoolExecutor(
            max_workers=cfg.workers, thread_name_prefix="classify-stream"
        )
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()
        self.counters = _empty_counters()
        self._healthy = True
        self._health_checked_at = 0.0
        if bus is not None:
            bus.subscribe("job.discovered", self.on_event)
        log_event("streaming_classifier_started", enabled=self.enabled, workers=cfg.workers)

    def _runner_available(self) -> bool:
        agora = time.monotonic()
        if agora - self._health_checked_at < 30.0:
            return self._healthy
        self._healthy = runner_healthy(self.worker.settings)
        self._health_checked_at = agora
        if not self._healthy:
            log_event(
                "streaming_classifier_unhealthy",
                runner=self.worker.settings.llm.runner or self.worker.settings.llm.primary,
            )
        return self._healthy

    def on_event(self, event: Event) -> None:
        job_id = str((event.payload or {}).get("job_id") or "")
        if not job_id:
            log_event("streaming_classifier_missing_job_id", event=event.type)
            return
        self.submit(job_id)

    def submit(self, job_id: str) -> None:
        if not self.enabled or not self._runner_available():
            return
        with self._lock:
            if job_id in self._futures:
                return
            future = self._pool.submit(self._run_one, job_id)
            self._futures[job_id] = future
        future.add_done_callback(lambda done, ident=job_id: self._finished(ident, done))

    def _run_one(self, job_id: str) -> dict[str, Any] | None:
        job = self.worker.repo.get_job(job_id)
        if job is None:
            return None
        return self.worker._classify_one(job)

    def _finished(self, job_id: str, future: Future) -> None:
        with self._lock:
            self._futures.pop(job_id, None)
        try:
            result = future.result()
        except Exception as exc:
            with self._lock:
                self.counters["errors"] += 1
            log_event(
                "streaming_classifier_error",
                job_id=job_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        if result is None:
            return
        with self._lock:
            self.counters["total"] += 1
            if result.get("known_portal"):
                self.counters["known_portal"] += 1
            if result["decision"] == "APPLY":
                self.counters["qualified"] += 1
            else:
                self.counters["rejected"] += 1

    def drain(self, timeout_seconds: float = 900.0) -> bool:
        """Espera a drenagem da fila no fim da descoberta; False se estourar o SLA."""
        with self._lock:
            futures = list(self._futures.values())
        if not futures:
            return True
        done, pendentes = wait(futures, timeout=timeout_seconds)
        log_event(
            "streaming_classifier_drained",
            done=len(done),
            pending=len(pendentes),
            counters=dict(self.counters),
        )
        return not pendentes

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=False)
