"""Fábrica e roteador do único LLM local autorizado (DGX)."""
from __future__ import annotations

import time
from typing import TypeVar

from pydantic import BaseModel

from ..core.config import Settings
from ..core.errors import LLMError
from ..core.events import EventBus
from ..core.logging import log_event
from .base import LLMBackend
from .openai_compatible import OpenAICompatibleBackend

T = TypeVar("T", bound=BaseModel)

# retry é do MESMO backend (DGX), nunca fallback para outro LLM/provedor.
_RETRY_BACKOFF_SECONDS = (2, 5)
_TRANSIENT_MARKS = ("timeout", "timed out", "connection", "temporarily", "503", "429")


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(mark in text for mark in _TRANSIENT_MARKS)


def build_backend(name: str, settings: Settings) -> LLMBackend:
    provider = settings.llm.providers.get(name)
    if provider is None:
        raise LLMError(f"Provider LLM '{name}' não configurado")
    # ProviderConfig.type só aceita "openai_compatible"; o único configurado é o DGX.
    return OpenAICompatibleBackend(
        name=name,
        base_url=provider.base_url,
        model=provider.model,
        api_key=provider.api_key or "local",
        timeout_seconds=provider.timeout_seconds,
        disable_thinking=provider.disable_thinking,
    )


class LLMRouter:
    """Usa somente o DGX. Nenhum fallback local e nenhuma API paga."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None = None,
        name: str | None = None,
    ) -> None:
        """Cria um roteador para uma tarefa.

        ``name=None`` usa o provider principal (Apply/desenvolvimento). Tarefas
        de runner informam ``settings.llm.runner`` explicitamente; ambos os
        providers precisam estar no mesmo DGX — nunca há fallback externo.
        """
        self.settings = settings
        self.bus = bus
        self.backends: list[LLMBackend] = []
        for provider_name in [name or settings.llm.primary]:
            if not provider_name:
                continue
            try:
                self.backends.append(build_backend(provider_name, settings))
            except LLMError as exc:
                log_event("llm_backend_invalido", backend=provider_name, error=str(exc))
        if not self.backends:
            raise LLMError("Nenhum backend LLM disponível")

    @property
    def primary_name(self) -> str:
        return self.backends[0].name

    def _run(self, operation: str, payload: dict, method: str) -> object:
        last: Exception | None = None
        max_attempts = max(1, self.settings.llm.max_retries + 1)
        for backend in self.backends:
            for attempt in range(1, max_attempts + 1):
                try:
                    result = getattr(backend, method)(**payload)
                    log_event(
                        "llm_ok", backend=backend.name, operation=operation, attempt=attempt
                    )
                    if self.bus:
                        self.bus.emit("llm.ok", backend=backend.name, operation=operation)
                    return result
                except Exception as exc:
                    last = exc
                    log_event(
                        "llm_falha",
                        backend=backend.name,
                        operation=operation,
                        attempt=attempt,
                        error=str(exc),
                    )
                    if attempt < max_attempts and _is_transient(exc):
                        time.sleep(_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)])
                        continue
                    break
        raise LLMError(f"Todos os backends falharam: {last}")

    def chat(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        return str(
            self._run(
                "chat",
                {
                    "messages": messages,
                    "temperature": self.settings.llm.temperature,
                    "json_mode": json_mode,
                    "max_tokens": min(self.settings.llm.max_output_tokens, 512),
                },
                "chat",
            )
        )

    def structured(
        self, messages: list[dict[str, str]], schema: type[T], *, temperature: float | None = None
    ) -> T:
        result = self._run(
            "structured",
            {
                "messages": messages,
                "schema": schema,
                "temperature": temperature or self.settings.llm.temperature,
            },
            "structured",
        )
        return result  # type: ignore[return-value]
