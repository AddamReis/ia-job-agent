"""Agendador local para o discovery-agent (sem dependência externa).

O deploy no DGX Spark pode usar systemd/cron com ``discovery-run --slot ...``.
Este módulo existe para rodar o serviço como processo contínuo quando não houver
agendador do sistema disponível.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable

from ...core.logging import log_event
from ...shared.discovery_window import _parse_hhmm, local_now, local_timezone


class LocalScheduler:
    def __init__(
        self,
        timezone_name: str,
        slots: list[str],
        callback: Callable[[str], None],
        *,
        tick_seconds: int = 30,
    ) -> None:
        self.timezone_name = timezone_name
        self.slots = list(slots)
        self.callback = callback
        self.tick_seconds = tick_seconds
        self._tz = local_timezone(timezone_name)

    def next_run(self, now: datetime | None = None) -> tuple[datetime, str]:
        """Próxima execução e o slot correspondente, no timezone configurado."""
        current = now or local_now(self.timezone_name)
        candidates: list[tuple[datetime, str]] = []
        for slot in self.slots:
            hour_minute = _parse_hhmm(slot)
            candidate = datetime.combine(
                current.date(), hour_minute, tzinfo=current.tzinfo
            )
            if candidate <= current:
                candidate += timedelta(days=1)
            candidates.append((candidate, slot))
        return min(candidates, key=lambda item: item[0])

    def run_forever(self) -> None:
        log_event("discovery_scheduler_started", slots=self.slots, tz=self.timezone_name)
        while True:
            due, slot = self.next_run()
            while True:
                remaining = (due - local_now(self.timezone_name)).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(self.tick_seconds, max(0.2, remaining)))
            try:
                self.callback(slot)
            except Exception as exc:  # nunca derruba o agendador
                log_event("discovery_scheduler_slot_failed", slot=slot, error=str(exc))
            log_event("discovery_scheduler_done", slot=slot, next=str(self.next_run()[0]))
