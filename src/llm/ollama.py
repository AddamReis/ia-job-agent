"""Backend para Ollama (fallback leve)."""
from __future__ import annotations

import httpx

from ..core.errors import LLMError
from .base import LLMBackend


class OllamaBackend(LLMBackend):
    def __init__(self, name: str, base_url: str, model: str, timeout_seconds: int = 60) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        options: dict[str, object] = {"temperature": temperature}
        if max_tokens:
            options["num_predict"] = max_tokens
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if json_mode:
            payload["format"] = "json"
        payload["think"] = False
        try:
            response = httpx.post(
                f"{self.base_url}/api/chat", json=payload, timeout=self.timeout_seconds
            )
            response.raise_for_status()
            return response.json()["message"]["content"]
        except Exception as exc:
            raise LLMError(f"[{self.name}] {type(exc).__name__}: {exc}") from exc
