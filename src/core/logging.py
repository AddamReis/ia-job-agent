"""Logging estruturado com redação de informações sensíveis."""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "cookie", "code", "verification_code", "otp",
    "credential", "private_key",
}

_PATTERNS = [
    re.compile(
        r"(?i)\b(authorization|token|api[_-]?key|password|passwd|secret|cookie|code)\b\s*[:=]\s*\S+"
    ),
    re.compile(r"\b\d{6}\b"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
]

_configured = False

# Correlation ID da execução atual (equivalente a um trace ID do OpenTelemetry).
# ContextVar isola execuções/threads sem passar o ID manualmente em cada chamada.
_trace_id: ContextVar[str] = ContextVar("job_agent_trace_id", default="")


def set_trace_id(trace_id: str) -> None:
    """Associa todos os logs seguintes a esta execução."""
    _trace_id.set(trace_id)


def get_trace_id() -> str:
    return _trace_id.get()


def clear_trace_id() -> None:
    _trace_id.set("")


def redact(value: object) -> object:
    """Remove segredos de strings e dicionários antes de virarem log."""
    if isinstance(value, dict):
        return {
            key: ("<REDACTED>" if str(key).lower() in _SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in _PATTERNS:
            result = pattern.sub("<REDACTED>", result)
        return result
    return value


def configure_logging(log_dir: Path) -> None:
    """Configura sink JSONL em arquivo. Nunca registra valores de campos sensíveis."""
    global _configured
    if _configured:
        return
    logger.remove()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"agent-{datetime.now().strftime('%Y-%m-%d')}.jsonl"

    def sink(message) -> None:
        record = message.record
        payload = {
            "ts": record["time"].astimezone(timezone.utc).isoformat(),
            "level": record["level"].name,
            "event": redact(record["message"]),
            "module": record["name"],
            "line": record["line"],
        }
        extra = redact(record["extra"])
        if extra:
            payload["context"] = extra
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    logger.add(sink, level="DEBUG")
    _configured = True


def log_event(event: str, **context: object) -> None:
    """Registra um evento de domínio com trace ID e contexto redacionado."""
    trace_id = get_trace_id()
    if trace_id and "trace_id" not in context:
        context["trace_id"] = trace_id
    logger.info(event, **context)


def field_summary(name: str, value: object) -> dict[str, object]:
    """Resumo seguro de um campo preenchido: nome + booleano, nunca o valor."""
    return {"field": redact(name), "filled": bool(value)}
