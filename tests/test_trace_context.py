"""O trace ID da execução deve chegar aos logs estruturados."""
from __future__ import annotations

from loguru import logger

from src.core.logging import clear_trace_id, log_event, set_trace_id


def test_log_event_injects_trace_id() -> None:
    captured: list[dict] = []
    handler = logger.add(lambda message: captured.append(dict(message.record["extra"])))
    set_trace_id("trace-abcdef12")
    try:
        log_event("trace_probe", company="Acme")
    finally:
        clear_trace_id()
        logger.remove(handler)

    assert captured
    assert captured[-1]["trace_id"] == "trace-abcdef12"
    assert captured[-1]["company"] == "Acme"
