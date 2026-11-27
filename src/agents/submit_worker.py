"""Consumidor do check aprovado na PWA — autoriza o envio no Chrome logado.

O operador decide na plataforma intermediária; este worker apenas consome o
aviso persistido pelo check e chama ``BrowserApplyAgent.submit``. Antes disso
ele repete as revalidações determinísticas (remoto, moeda, recência, score e
decisão de classificação). Se a plataforma pedir CAPTCHA/MFA/verificação, o
``submit`` lança ``BlockedByPlatformError`` e o evento é encerrado como FAILED:
não há retry automático nem tentativa de burla.
"""
from __future__ import annotations

from typing import Any

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.errors import JobUnavailable
from ..core.state import JobStatus
from ..models.domain import ApprovalRequest, JobOpportunity
from ..storage.database import Repository
from .browser_apply import BrowserApplyAgent
from .qualification import (
    currency_rejection_reason,
    detect_currencies,
    explicit_currency_codes,
    expire_stale_jobs,
    is_remote_location,
    stale_reason,
)

CONSUMER = "apply_submit"
EVENT_TYPE = "review.approved"


def submit_preflight(
    job: JobOpportunity,
    approval: ApprovalRequest,
    settings: Settings,
    repo: Repository,
) -> str | None:
    """Revalidações determinísticas feitas novamente no processo de envio."""
    if approval.status != "APPROVED":
        return "aprovação não está APPROVED"
    # QUALIFIED entra quando o operador autoriza direto pela PWA: o próprio
    # submit() prepara o formulário e envia, sem etapa anterior de approve.
    if job.status not in {
        JobStatus.QUALIFIED,
        JobStatus.PENDING_APPROVAL,
        JobStatus.APPLYING,
    }:
        return f"vaga em status {job.status.value}; esperava QUALIFIED/PENDING_APPROVAL"
    if repo.has_application_for(job.id):
        return "já existe candidatura registrada"

    stale = stale_reason(job, settings)
    if stale:
        return f"fora da janela de recência: {stale}"
    if job.analysis is not None and job.analysis.is_remote is False:
        return "a vaga deixou de ser 100% remota"
    if settings.search.remote_only and not is_remote_location(
        job.location_normalized or job.location,
        job.title,
        job.description or "",
    ):
        return "remoto não confirmado na revalidação"

    permitidas = {str(item).upper() for item in settings.search.currencies}
    texto = " ".join(
        filter(
            None,
            [job.salary_raw, job.salary_text, job.description or "", job.currency_signal],
        )
    )
    codigos = detect_currencies(texto) | explicit_currency_codes(job.currency, job.currency_signal)
    motivo_moeda = currency_rejection_reason(codigos, permitidas)
    if motivo_moeda:
        return motivo_moeda

    if (job.score or 0) < settings.qualification.min_score:
        return "score abaixo do mínimo na revalidação"
    qualification = job.qualification
    if qualification is not None and qualification.decision != "APPLY":
        return "classificação atual não é APPLY"
    return None


class SubmitWorker:
    """Envia somente vagas cujo check foi aprovado na PWA."""

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

    def run(self, limit: int | None = None) -> dict[str, int]:
        # A vaga pode envelhecer enquanto esperava o clique na PWA. Antes de
        # consumir o outbox, encerra o que saiu da janela para que a fila e os
        # contadores da PWA deixem de exibi-la como autorizável.
        expire_stale_jobs(self.repo, self.settings)
        eventos = self.repo.claim_agent_events(CONSUMER, limit=max(20, limit or 0))
        counters = {
            "events": len(eventos),
            "submitted": 0,
            "blocked": 0,
            "unavailable": 0,
            "errors": 0,
        }
        log_event("submit_worker_started", events=len(eventos))
        for evento in eventos:
            event_id = str(evento["id"])
            job_id = str(evento.get("job_id") or "")
            payload = evento.get("payload") or {}
            job = self.repo.get_job(job_id) if job_id else None
            approval_id = str(payload.get("approval_id") or "")
            decision_id = str(payload.get("review_decision_id") or "")
            approval = (
                self.repo.get_approval(approval_id)
                if approval_id
                else self.repo.get_approval_for_job(job_id)
            )
            if job is None or approval is None or approval.opportunity_id != job_id:
                self.repo.ack_agent_event(
                    event_id, status="FAILED", error="vaga ou aprovação não encontrada"
                )
                counters["errors"] += 1
                continue

            # A varredura acima já marcou a vaga EXPIRED; ainda assim o evento
            # deve registrar o motivo real da reprovação, não só o status novo.
            stale = stale_reason(job, self.settings)
            if stale:
                motivo = f"fora da janela de recência: {stale}"
                self.repo.ack_agent_event(event_id, status="FAILED", error=motivo)
                counters["blocked"] += 1
                log_event(
                    "submit_worker_blocked",
                    job_id=job_id,
                    approval_id=approval.id,
                    reason=motivo,
                )
                continue

            bloqueio = submit_preflight(job, approval, self.settings, self.repo)
            if bloqueio:
                self.repo.ack_agent_event(event_id, status="FAILED", error=bloqueio)
                counters["blocked"] += 1
                log_event(
                    "submit_worker_blocked",
                    job_id=job_id,
                    approval_id=approval.id,
                    reason=bloqueio,
                )
                continue

            try:
                application = self.apply_agent.submit(job, approval, confirmed=True)
            except JobUnavailable as exc:
                # Vaga encerrada no portal não é falha do agente: encerra o evento
                # sem deixar a vaga na aba Erros da PWA.
                self.repo.ack_agent_event(event_id, status="ACK", error=str(exc)[:2000])
                counters["unavailable"] += 1
                log_event(
                    "submit_worker_unavailable",
                    job_id=job_id,
                    reason=str(exc)[:200],
                )
                continue
            except Exception as exc:
                self.repo.ack_agent_event(
                    event_id, status="FAILED", error=f"{type(exc).__name__}: {exc}"
                )
                counters["errors"] += 1
                log_event(
                    "submit_worker_error",
                    job_id=job_id,
                    approval_id=approval.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            if application is None:
                self.repo.ack_agent_event(
                    event_id, status="FAILED", error="envio não confirmado pelo portal"
                )
                counters["blocked"] += 1
                continue

            anexar = getattr(self.repo, "attach_evidence_to_review_decision", None)
            if decision_id and anexar is not None:
                try:
                    anexar(
                        decision_id,
                        [item.model_dump(mode="json") for item in application.evidence],
                    )
                except Exception as exc:
                    # A candidatura já está gravada; a PWA também lê evidências
                    # diretamente do Application. Não transforma sucesso em erro.
                    log_event(
                        "submit_worker_evidence_attach_failed",
                        job_id=job_id,
                        decision_id=decision_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )

            self.repo.ack_agent_event(event_id)
            counters["submitted"] += 1
            log_event(
                "submit_worker_submitted",
                job_id=job_id,
                approval_id=approval.id,
                company=job.company,
            )
            if self.bus is not None:
                self.bus.emit(
                    "application.submitted",
                    job_id=job_id,
                    company=job.company,
                    title=job.title,
                )
        log_event("submit_worker_finished", **counters)
        return counters
