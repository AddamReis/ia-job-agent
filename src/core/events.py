"""Barramento de eventos em memória para o painel do terminal."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .logging import redact


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


Handler = Callable[[Event], None]


class EventBus:
    """Publicação síncrona e simples — sem fila persistente."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        safe = Event(
            type=event.type,
            payload=redact(event.payload) or {},  # type: ignore[arg-type]
            timestamp=event.timestamp,
        )
        for handler in list(self._handlers.get(safe.type, [])) + list(
            self._handlers.get("*", [])
        ):
            try:
                handler(safe)
            except Exception:  # painel nunca derruba a execução
                continue

    def emit(self, event_type: str, **payload: object) -> None:
        self.publish(Event(type=event_type, payload=dict(payload)))
