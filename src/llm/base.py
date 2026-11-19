"""Contrato comum dos backends de LLM local."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from ..core.errors import LLMError

T = TypeVar("T", bound=BaseModel)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json(text: str) -> str:
    """Extrai um objeto JSON de respostas com ou sem markdown fence."""
    fenced = _JSON_FENCE.search(text)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError("Resposta do modelo não continha JSON válido")
    return candidate[start : end + 1]


class LLMBackend(ABC):
    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Retorna o texto da resposta do modelo."""

    def structured(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        temperature: float = 0.1,
    ) -> T:
        schema_hint = {
            "role": "user",
            "content": (
                "Responda APENAS com um objeto JSON válido, sem texto fora do JSON, "
                "seguindo rigorosamente este schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            ),
        }
        raw = self.chat(
            [*messages, schema_hint],
            temperature=temperature,
            json_mode=True,
            max_tokens=2048,
        )
        try:
            payload = json.loads(extract_json(raw))
            return schema.model_validate(payload)
        except Exception as exc:
            raise LLMError(f"Falha ao validar resposta estruturada: {exc}") from exc
