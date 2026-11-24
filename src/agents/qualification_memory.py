"""Memória local e auditável da classificação.

Não altera pesos, limiares nem barreiras duras. Apenas transforma decisões
humanas e desfechos de candidatura em contexto curto, rastreável e injetado no
DGX na próxima classificação. Nada é enviado para fora da máquina.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..core.state import JobStatus
from ..models.domain import ApprovalRequest, JobOpportunity, QualificationFeedback
from ..storage.database import Repository

_SOURCE_LABEL = {
    "human_approval": "decisão humana",
    "application_outcome": "desfecho da candidatura",
    "reclassification": "reclassificação",
    "human_validation": "validação humana",
}
_VERDICT_LABEL = {
    "positive": "positivo",
    "negative": "negativo",
    "attention": "atenção",
}
_GENERIC_TITLE_TOKENS = {
    "engineer", "developer", "software", "senior", "junior", "mid", "pl", "jr",
    "remote", "full", "stack", "backend", "frontend", "dev", "the", "and",
}
_TOPIC_PATTERNS = (
    (r"\bjava\b(?!script)", "stack:java"),
    (r"javascript|typescript|\bnode\.?js\b", "stack:javascript_typescript"),
    (r"\bpython\b", "stack:python"),
    (r"\bphp\b", "stack:php"),
    (r"\bruby\b", "stack:ruby"),
    (r"\bgo(lang)?\b", "stack:golang"),
    (r"\bflutter\b|\bdart\b", "stack:flutter"),
    (r"fluent|c1|c2|native|ingles|espanhol|idioma|language", "idioma"),
    (r"\bbrl\b|r\$|reais", "moeda:brl"),
    (r"\busd\b|us\$|\beur\b|€", "moeda:aceita"),
    (r"visado|visto|sponsorship|work authorization|autorização", "visto"),
    (r"hibrid|hybrid|presencial|onsite|on-site", "modalidade"),
)


class QualificationMemory:
    """Aprende com correções humanas e com o que aconteceu após o envio."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    @staticmethod
    def _title_topics(title: str) -> list[str]:
        tokens = [
            token
            for token in re.findall(r"[a-z0-9.+#]+", (title or "").lower())
            if len(token) >= 3 and token not in _GENERIC_TITLE_TOKENS
        ]
        if not tokens:
            return []
        return ["title:" + "+".join(sorted(set(tokens))[:3])]

    @classmethod
    def topics_for(cls, job: JobOpportunity, reason: str = "") -> list[str]:
        text = " ".join([job.title or "", job.restriction_details or "", reason or ""]).lower()
        topics: list[str] = []
        for pattern, topic in _TOPIC_PATTERNS:
            if re.search(pattern, text, re.I) and topic not in topics:
                topics.append(topic)
        company = Repository.normalize_company(job.company or "")
        if company:
            topics.append(f"company:{company}")
        topics.extend(cls._title_topics(job.title))
        return list(dict.fromkeys(topics))[:4]

    def _detail(self, job: JobOpportunity, reason: str) -> str:
        base = f"{job.company or '?'} — {job.title or '?'}".strip(" —")
        if reason:
            base = f"{base}; {reason}"
        return re.sub(r"\s+", " ", base).strip()[:220]

    def record(
        self,
        job: JobOpportunity,
        *,
        source: str,
        verdict: str,
        reason: str = "",
        trace_id: str = "",
    ) -> int:
        topics = self.topics_for(job, reason) or ["global"]
        detail = self._detail(job, reason)
        gravados = 0
        for topic in topics:
            feedback = QualificationFeedback(
                trace_id=trace_id or job.trace_id,
                opportunity_id=job.id,
                source=source,  # type: ignore[arg-type]
                verdict=verdict,  # type: ignore[arg-type]
                topic=topic,
                detail=detail,
            )
            if self.repo.record_qualification_feedback(feedback):
                gravados += 1
        return gravados

    def record_approval(self, approval: ApprovalRequest, approved: bool) -> int:
        job = self.repo.get_job(approval.opportunity_id)
        if job is None:
            return 0
        return self.record(
            job,
            source="human_approval",
            verdict="positive" if approved else "negative",
            reason="envio aprovado pelo operador" if approved else "envio negado pelo operador",
            trace_id=approval.trace_id,
        )

    def record_outcome(self, job: JobOpportunity, status: str, reason: str = "") -> int:
        status = str(getattr(status, "value", status)).upper()
        if status == "SUBMITTED":
            return self.record(
                job,
                source="application_outcome",
                verdict="positive",
                reason="candidatura enviada com sucesso",
            )
        if status in {"FAILED", "REQUIRES_HUMAN_ACTION"}:
            return self.record(
                job,
                source="application_outcome",
                verdict="attention",
                reason=reason or "candidatura exigiu intervenção ou falhou no portal",
            )
        return 0

    def record_reclassification(
        self, before: JobOpportunity | JobStatus, after: JobOpportunity
    ) -> int:
        # A reclassificação em lote guarda apenas o status anterior (enum),
        # enquanto outros chamadores passam a vaga completa. Aceitar os dois.
        original = getattr(before, "status", before)
        anterior = getattr(original, "value", original)
        atual = getattr(after.status, "value", after.status)
        if anterior == JobStatus.REJECTED_BY_FILTER.value and atual == JobStatus.QUALIFIED.value:
            return self.record(
                after,
                source="reclassification",
                verdict="positive",
                reason="reclassificação aprovou a vaga",
            )
        if anterior == JobStatus.QUALIFIED.value and atual == JobStatus.REJECTED_BY_FILTER.value:
            return self.record(
                after,
                source="reclassification",
                verdict="negative",
                reason=after.restriction_details or "reclassificação reprovou a vaga",
            )
        return 0

    def record_validation(
        self, job: JobOpportunity, *, correct: bool, reason: str = ""
    ) -> int:
        """Registra a revisão humana de uma classificação já gravada.

        Diferente de ``record_reclassification``, aqui o sinal vem de alguém
        conferindo o veredito — não do próprio modelo se autoavalizando.
        """
        return self.record(
            job,
            source="human_validation",
            verdict="positive" if correct else "negative",
            reason=reason
            or ("classificação confirmada na validação" if correct else "classificação corrigida na validação"),
        )

    def context_for(self, job: JobOpportunity, limit: int = 8) -> str:
        """Devolve contexto curto, priorizando sinais parecidos com a vaga atual."""
        itens = self.repo.list_qualification_feedback(limit=300)
        if not itens:
            return ""
        relevantes = set(self.topics_for(job))
        groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "score": 0.0, "detail": ""}
        )
        for item in itens:
            score = 2.0 + {"negative": 2.0, "positive": 1.0, "attention": 1.0}.get(
                item.verdict, 1.0
            )
            if item.topic in relevantes:
                score += 6.0
            if item.topic == "global":
                score += 0.5
            group = groups[(item.topic, item.verdict, item.source)]
            group["count"] += 1
            group["score"] = max(group["score"], score)
            if not group["detail"]:
                group["detail"] = item.detail
        ordered = sorted(
            groups.items(),
            key=lambda pair: (pair[1]["score"] + min(pair[1]["count"], 5), pair[0][0]),
            reverse=True,
        )
        linhas: list[str] = []
        for (topic, verdict, source), group in ordered[:limit]:
            origem = _SOURCE_LABEL.get(source, source)
            linhas.append(
                f"- [{origem}/{_VERDICT_LABEL.get(verdict, verdict)} {group['count']}x] "
                f"{topic}: {group['detail'][:180]}"
            )
        return "\n".join(linhas)[:3200]
