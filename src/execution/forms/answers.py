"""Resolvedor de respostas: regras locais primeiro, LLM com perfil como evidência."""
from __future__ import annotations

import re

from ...core.events import EventBus
from ...core.logging import field_summary, log_event
from ...llm.factory import LLMRouter
from ...llm.prompts import answer_messages, ui_action_messages
from ...models.domain import CandidateProfile
from ...models.schemas import AnswerDecision, UiActionDecision
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
        # Dados pessoais sensíveis nunca são inferidos pelo LLM. O mapper já os
        # sinaliza; esta barreira garante que nenhuma resposta do modelo os
        # reintroduza (ex.: "Year" com ano de nascimento inventado).
        if category == "sensitive" or source == "politica:acao_sensivel":
            return AnswerDecision(
                value="",
                source="politica:acao_sensivel",
                needs_human=True,
                reason="ação sensível exige aprovação humana",
            )
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

    def interpret_action(
        self,
        intent: str,
        dialog_text: str,
        buttons: list[str],
        memory_context: str = "",
    ) -> UiActionDecision:
        """Escolhe, com o LLM da DGX, o botão seguro para o estado atual.

        O modelo não tem permissão para inventar rótulos: a validação final em
        ``form_dom`` exige que a ação escolhida exista entre os botões visíveis.
        """
        try:
            decision = self.llm.structured(
                ui_action_messages(intent, dialog_text, buttons, memory_context),
                UiActionDecision,
            )
        except Exception as exc:
            log_event("ui_action_llm_falha", intent=intent, error=str(exc)[:200])
            return UiActionDecision(needs_human=True, reason="LLM indisponível")

        labels = {str(item).strip() for item in buttons if str(item).strip()}
        chosen = str(decision.action or "").strip()
        if decision.needs_human or not chosen or chosen.lower() == "none":
            return UiActionDecision(needs_human=True, reason=decision.reason or "sem ação segura")
        if chosen not in labels:
            log_event("ui_action_label_rejeitada", intent=intent, action=chosen[:80])
            return UiActionDecision(needs_human=True, reason="botão não está visível")

        sensitive = re.compile(
            r"captcha|verifica|mfa|senha|password|pagamento|payment|termos|privacidade|"
            r"autoriza[cç][aã]o de trabalho|visto|sponsorship|contrato|"
            r"linkedin|indeed|google|facebook|apple|oauth|sign in|iniciar sess|acceder",
            re.I,
        )
        if sensitive.search(chosen):
            return UiActionDecision(needs_human=True, reason="ação sensível")

        log_event(
            "ui_action_interpretada",
            intent=intent,
            action=chosen[:80],
            source=str(decision.source or "llm")[:40],
        )
        return decision

