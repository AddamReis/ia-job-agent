"""Backend para o DGX Spark (API compatível com OpenAI)."""
from __future__ import annotations

from ..core.errors import LLMError
from .base import LLMBackend


class OpenAICompatibleBackend(LLMBackend):
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "local",
        timeout_seconds: int = 300,
        disable_thinking: bool = True,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.disable_thinking = disable_thinking
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMError("openai não instalado") from exc
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout_seconds,
            )
        return self._client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        try:
            kwargs: dict[str, object] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if self.disable_thinking:
                # Qwen3 em vLLM entra em modo reasoning e pode travar a resposta.
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
            response = self._get_client().chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise LLMError(f"[{self.name}] {type(exc).__name__}: {exc}") from exc
