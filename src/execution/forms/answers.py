"""Resolvedor de respostas: regras locais primeiro, LLM com perfil como evidência."""
from __future__ import annotations

from ...core.events import EventBus
from ...core.logging import field_summary, log_event
from ...llm.factory import LLMRouter
from ...llm.prompts import answer_messages
from ...models.domain import CandidateProfile
from ...models.schemas import AnswerDecision
from .field import FormField
from .mapper import resolve_local
from ..profile.builder import profile_context


class AnswerResolver:
    def __init__(
        self,
        profile: CandidateProfile,
        llm: LLMRouter,
        settings,
        bus: EventBus | None = None,
    ) -> None:
        self.profile = profile
        self.llm = llm
        self.settings = settings
        self.bus = bus
        self.extra_context = self._load_domains_doc()

    def _load_domains_doc(self) -> str:
        """Carrega o doc local de domínios como evidência, sem registrar o conteúdo."""
        path = getattr(self.settings.profile, "tech_domains_doc", "")
        if not path:
            return ""
        try:
            file = self.settings.resolve(path)
            if not file.exists():
                log_event("tech_domains_doc_missing")
                return ""
            text = file.read_text(encoding="utf-8", errors="replace")[:12000]
            log_event("tech_domains_doc_loaded", chars=len(text))
            return text
        except Exception as exc:
            log_event("tech_domains_doc_error", error=str(exc))
            return ""

    def resolve(self, field: FormField) -> AnswerDecision:
        value, source, needs_human, category = resolve_local(field, self.profile, self.settings)
        if not needs_human and value:
            return AnswerDecision(
                value=value, source=source, needs_human=False, reason="regra local com evidência"
            )

        if category == "sensitive" or category in {
            "legal", "work_authorization", "visa_sponsorship", "authentication",
            "captcha_mfa", "profile_changes", "salary_expectation",
        }:
            return AnswerDecision(
                value="",
                source="politica:acao_sensivel",
                needs_human=True,
                reason="ação sensível exige aprovação humana",
            )

        try:
            decision = self.llm.structured(
                answer_messages(
                    field.label or field.identifier,
                    profile_context(self.profile),
                    field.field_type,
                    extra_context=self.extra_context,
                ),
                AnswerDecision,
            )
        except Exception as exc:  # LLM indisponível não inventa resposta
            log_event("answer_llm_falha", error=str(exc))
            decision = AnswerDecision(needs_human=True, reason="LLM indisponível")

        if decision.needs_human or not decision.value.strip() or not decision.source.strip():
            decision.needs_human = True
            decision.value = ""
        log_event("answer_resolvida", **field_summary(field.label or field.identifier, decision.value))
        if self.bus:
            self.bus.emit(
                "field.resolved",
                field=field.label or field.identifier,
                needs_human=decision.needs_human,
            )
        return decision

