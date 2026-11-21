"""Trava persistente de execução única para buscas de vagas.

Uma rodada por serviço: se outra estiver ativa, a nova fica em standby e entra
em execução assim que a anterior libera a trava. Heartbeat periódico distingue
processo vivo de restart que deixou a rodada órfã.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..core.logging import log_event
from ..storage.database import Repository


class RunLockTimeout(RuntimeError):
    """A execução anterior não liberou a trava dentro do limite de espera."""


@dataclass(frozen=True)
class RunLockAcquisition:
    name: str
    owner: str
    reclaimed: bool
    previous_owner: str | None


class RunLock:
    def __init__(
        self,
        repo: Repository,
        *,
        name: str,
        owner: str,
        stale_after_seconds: int = 900,
        heartbeat_seconds: int = 30,
        wait_seconds: int = 3600,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo
        self.name = name
        self.owner = owner
        self.stale_after_seconds = stale_after_seconds
        self.heartbeat_seconds = max(0.01, heartbeat_seconds)
        self.wait_seconds = max(0.0, wait_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self.sleep = sleep
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._acquisition: RunLockAcquisition | None = None

    def acquire(self) -> RunLockAcquisition:
        deadline = time.monotonic() + self.wait_seconds
        waited = False
        while True:
            now = datetime.now(timezone.utc)
            acquired, reclaimed, previous_owner = self.repo.try_acquire_run_lock(
                self.name,
                self.owner,
                stale_after_seconds=self.stale_after_seconds,
                now=now,
            )
            if acquired:
                if reclaimed:
                    cutoff = now - timedelta(seconds=self.stale_after_seconds)
                    executions = self.repo.mark_orphan_executions(cutoff)
                    watermarks = self.repo.mark_orphan_watermarks(cutoff)
                    log_event(
                        "run_lock_reclaimed",
                        name=self.name,
                        owner=self.owner,
                        previous_owner=previous_owner,
                        orphan_executions=executions,
                        orphan_watermarks=watermarks,
                    )
                self._acquisition = RunLockAcquisition(
                    name=self.name,
                    owner=self.owner,
                    reclaimed=reclaimed,
                    previous_owner=previous_owner,
                )
                log_event(
                    "run_lock_acquired",
                    name=self.name,
                    owner=self.owner,
                    waited=waited,
                )
                self._start_heartbeat()
                return self._acquisition

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log_event(
                    "run_lock_timeout",
                    name=self.name,
                    owner=self.owner,
                    previous_owner=previous_owner,
                    wait_seconds=self.wait_seconds,
                )
                raise RunLockTimeout(
                    f"Trava '{self.name}' ocupada por {previous_owner or 'outro processo'} "
                    f"após {self.wait_seconds}s de espera"
                )
            waited = True
            log_event(
                "run_lock_waiting",
                name=self.name,
                owner=self.owner,
                previous_owner=previous_owner,
                remaining_seconds=int(remaining),
            )
            self.sleep(min(self.poll_seconds, remaining))

    def _start_heartbeat(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"run-lock-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                if not self.repo.renew_run_lock(self.name, self.owner):
                    log_event(
                        "run_lock_lost",
                        name=self.name,
                        owner=self.owner,
                    )
                    return
            except Exception as exc:  # never derruba a busca por falha de log
                log_event(
                    "run_lock_heartbeat_failed",
                    name=self.name,
                    owner=self.owner,
                    error=str(exc),
                )

    @property
    def acquisition(self) -> RunLockAcquisition | None:
        return self._acquisition

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        released = self.repo.release_run_lock(self.name, self.owner)
        log_event(
            "run_lock_released",
            name=self.name,
            owner=self.owner,
            released=released,
        )

    def __enter__(self) -> RunLockAcquisition:
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = ["RunLock", "RunLockAcquisition", "RunLockTimeout"]
