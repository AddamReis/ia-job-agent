"""Loop contínuo dos workers agênticos com trava persistente.

Cada rotina (classificação, aplicação) tem sua própria trava em ``run_locks``:
se outra instância estiver ativa, a nova fica em standby e tenta de novo no
próximo ciclo. Isso complementa a trava de ``discovery`` sem serializar agentes
diferentes entre si.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from ..core.logging import log_event
from ..execution.run_lock import RunLock, RunLockTimeout
from ..storage.database import Repository


def run_worker_loop(
    repo: Repository,
    *,
    name: str,
    callback: Callable[[], object],
    interval_seconds: float = 300.0,
    owner: str | None = None,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], object] | None = None,
) -> None:
    """Executa ``callback`` em ciclos, no máximo uma instância por ``name``."""
    stop = stop_event or threading.Event()
    wait = sleep or time.sleep
    dono = owner or f"{name}-{os.getpid()}"
    lock = RunLock(
        repo,
        name=name,
        owner=dono,
        wait_seconds=0,
        poll_seconds=0.1,
    )
    log_event("agent_worker_loop_started", name=name, interval=interval_seconds)
    while not stop.is_set():
        try:
            with lock:
                resultado = callback()
                log_event("agent_worker_cycle_done", name=name, resultado=resultado)
        except RunLockTimeout:
            log_event("agent_worker_standby", name=name, owner=dono)
        except Exception as exc:
            log_event(
                "agent_worker_cycle_failed",
                name=name,
                error=f"{type(exc).__name__}: {exc}",
            )
        if stop.wait(max(0.1, interval_seconds)):
            break
    log_event("agent_worker_loop_stopped", name=name)
