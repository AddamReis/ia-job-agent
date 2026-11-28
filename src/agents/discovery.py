"""Serviço isolado de população da base (discovery-agent).

Responsabilidade única: varrer fontes públicas e gravar vagas ``DISCOVERED``.
Não chama LLM, não mexe no Chrome logado e não candidata ninguém. Pode rodar no
DGX Spark, em VM ou localmente — a porta de entrada é ``run(slot=...)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import configure_logging, log_event, set_trace_id
from ..core.state import ExecutionStatus
from ..execution.run_lock import RunLock
from ..execution.web.linkedin_public import LinkedInPublicBrowser
from ..models.domain import ExecutionHistory
from ..shared.discovery_window import DiscoveryWindow, compute_window
from ..storage.database import Repository
from .browser_research import BrowserResearchAgent
from .jobgether_research import JobgetherResearchAgent


@dataclass(frozen=True)
class DiscoveryResult:
    slot: str
    trace_id: str
    reference_time: datetime
    cutoff: datetime
    time_filter: str
    window_source: str
    jobs_new: int
    status: str
    # Vagas novas por plataforma. O LinkedIn mantém a chave de watermark
    # histórica (só o slot) para não invalidar execuções já gravadas.
    sources: dict[str, int] | None = None

    def describe(self) -> str:
        por_fonte = (
            " ".join(f"{name}={count}" for name, count in sorted((self.sources or {}).items()))
            or "—"
        )
        return (
            f"slot={self.slot} novas={self.jobs_new} "
            f"janela={self.cutoff.isoformat(timespec='seconds')} -> "
            f"{self.reference_time.isoformat(timespec='seconds')} "
            f"origem={self.window_source} filtro={self.time_filter} "
            f"fontes={por_fonte} trace={self.trace_id}"
        )


class DiscoveryAgent:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None = None,
        *,
        repo: Repository | None = None,
        browser: LinkedInPublicBrowser | None = None,
        sources: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus or EventBus()
        configure_logging(settings.resolve(settings.storage.log_dir))
        self.repo = repo or Repository(
            settings.resolve(settings.storage.database),
            known_portals=settings.application.known_portals,
        )
        self.browser = browser or LinkedInPublicBrowser(settings)
        self.research = BrowserResearchAgent(
            self.browser, self.repo, settings, self.bus
        )
        self.sources: list[tuple[str, Any]] = sources if sources is not None else [
            ("linkedin", self.research)
        ]
        if sources is None and settings.sources.jobgether.enabled:
            self.sources.append(
                (
                    "jobgether",
                    JobgetherResearchAgent(self.repo, settings, self.bus),
                )
            )

    @staticmethod
    def watermark_key(slot: str, source: str = "linkedin") -> str:
        """LinkedIn preserva a chave antiga; fontes novas ganham sufixo."""
        if not source or source == "linkedin":
            return slot
        return f"{slot}:{source}"

    def window(
        self,
        slot: str = "auto",
        now: datetime | None = None,
        source: str = "linkedin",
    ) -> DiscoveryWindow:
        """Resolve o slot e aplica watermark + sobreposição, por plataforma."""
        cfg = self.settings.discovery_schedule
        resolved = slot
        if slot in {"auto", "", None}:  # type: ignore[comparison-overlap]
            from ..shared.discovery_window import normalize_slot

            resolved = normalize_slot(cfg, slot, now)
        watermark = self.repo.get_discovery_watermark(
            self.watermark_key(resolved, source)
        )
        last_started = None
        if watermark and watermark.get("status") == ExecutionStatus.COMPLETED.value:
            try:
                last_started = datetime.fromisoformat(str(watermark["started_at"]))
            except (KeyError, ValueError, TypeError):
                last_started = None
        return compute_window(self.settings, resolved, now=now, last_started=last_started)

    def run(
        self,
        slot: str = "auto",
        *,
        trace_id: str | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> DiscoveryResult:
        trace = trace_id or str(uuid4())
        set_trace_id(trace)
        self.research.trace_id = trace
        for _name, agent in self.sources:
            if hasattr(agent, "trace_id"):
                agent.trace_id = trace
        cfg = self.settings.discovery_schedule
        lock = RunLock(
            self.repo,
            name=cfg.lock_name,
            owner=trace,
            stale_after_seconds=cfg.lock_stale_after_seconds,
            heartbeat_seconds=cfg.lock_heartbeat_seconds,
            wait_seconds=cfg.lock_wait_seconds,
            poll_seconds=cfg.lock_poll_seconds,
        )

        # A janela é calculada SÓ depois de adquirir a trava: uma rodada em
        # standby usa o instante em que realmente começa, não o pedido.
        with lock:
            # Uma única âncora de tempo para todas as fontes. Cada plataforma
            # tem watermark próprio (o LinkedIn conserva a chave histórica).
            first_name = self.sources[0][0] if self.sources else "linkedin"
            window = self.window(slot, now, first_name)
            windows = {
                name: (
                    window
                    if name == first_name
                    else self.window(window.slot, window.reference_time, name)
                )
                for name, _agent in self.sources
            }
            log_event(
                "discovery_started",
                slot=window.slot,
                trace_id=trace,
                window=window.describe(),
                sources=list(windows),
            )
            if self.bus:
                self.bus.emit("discovery.started", slot=window.slot, trace_id=trace)

            history = ExecutionHistory(
                id=trace,
                trace_id=trace,
                phase="discovery",
                started_at=window.reference_time,
                status=ExecutionStatus.RUNNING.value,
            )
            self.repo.create_execution(history)
            for name, source_window in windows.items():
                self.repo.save_discovery_watermark(
                    self.watermark_key(window.slot, name),
                    {
                        "status": ExecutionStatus.RUNNING.value,
                        "started_at": source_window.reference_time.isoformat(),
                        "cutoff": source_window.cutoff.isoformat(),
                        "trace_id": trace,
                        "source": name,
                    },
                )

            jobs_new = 0
            per_source: dict[str, int] = {}
            status = ExecutionStatus.COMPLETED.value
            try:
                if not dry_run:
                    # Serializado de propósito: CDP é único, DGX é único e uma
                    # rodada em standby não deve disputar banco com a seguinte.
                    for name, agent in self.sources:
                        source_window = windows[name]
                        count = agent.run(
                            reference_time=source_window.reference_time,
                            cutoff=source_window.cutoff,
                            time_filter=source_window.time_filter,
                        )
                        per_source[name] = count
                        jobs_new += count
                        log_event(
                            "discovery_source_finished",
                            slot=window.slot,
                            source=name,
                            jobs_new=count,
                            trace_id=trace,
                        )
            except Exception as exc:
                status = ExecutionStatus.ERROR.value
                history.errors.append(str(exc))
                log_event("discovery_failed", trace_id=trace, error=str(exc))
                raise
            finally:
                finished = datetime.now()
                history.finished_at = finished
                history.status = status
                history.jobs_discovered = jobs_new
                history.duration_seconds = (
                    finished - window.reference_time.replace(tzinfo=None)
                ).total_seconds()
                self.repo.update_execution(history)
                for name, source_window in windows.items():
                    self.repo.save_discovery_watermark(
                        self.watermark_key(window.slot, name),
                        {
                            "status": status,
                            "started_at": source_window.reference_time.isoformat(),
                            "finished_at": finished.isoformat(),
                            "cutoff": source_window.cutoff.isoformat(),
                            "time_filter": source_window.time_filter,
                            "window_source": source_window.source,
                            "trace_id": trace,
                            "source": name,
                            "jobs_new": per_source.get(name, 0),
                            "errors": history.errors,
                        },
                    )
                log_event(
                    "discovery_finished",
                    slot=window.slot,
                    trace_id=trace,
                    jobs_new=jobs_new,
                    sources=per_source,
                    status=status,
                )
                if self.bus:
                    self.bus.emit(
                        "discovery.finished",
                        jobs_new=jobs_new,
                        sources=per_source,
                        status=status,
                    )

            return DiscoveryResult(
                slot=window.slot,
                trace_id=trace,
                reference_time=window.reference_time,
                cutoff=window.cutoff,
                time_filter=window.time_filter,
                window_source=window.source,
                jobs_new=jobs_new,
                status=status,
                sources=per_source,
            )


__all__ = ["DiscoveryAgent", "DiscoveryResult"]
