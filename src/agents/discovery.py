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
from ..execution.web.linkedin_public import LinkedInPublicBrowser
from ..models.domain import ExecutionHistory
from ..shared.discovery_window import DiscoveryWindow, compute_window
from ..storage.database import Repository
from .browser_research import BrowserResearchAgent


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

    def describe(self) -> str:
        return (
            f"slot={self.slot} novas={self.jobs_new} "
            f"janela={self.cutoff.isoformat(timespec='seconds')} -> "
            f"{self.reference_time.isoformat(timespec='seconds')} "
            f"origem={self.window_source} filtro={self.time_filter} "
            f"trace={self.trace_id}"
        )


class DiscoveryAgent:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None = None,
        *,
        repo: Repository | None = None,
        browser: LinkedInPublicBrowser | None = None,
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

    def window(self, slot: str = "auto", now: datetime | None = None) -> DiscoveryWindow:
        """Resolve o slot e aplica watermark + sobreposição."""
        cfg = self.settings.discovery_schedule
        resolved = slot
        if slot in {"auto", "", None}:  # type: ignore[comparison-overlap]
            from ..shared.discovery_window import normalize_slot

            resolved = normalize_slot(cfg, slot, now)
        watermark = self.repo.get_discovery_watermark(resolved)
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
        window = self.window(slot, now)
        trace = trace_id or str(uuid4())
        set_trace_id(trace)
        self.research.trace_id = trace
        log_event("discovery_started", slot=window.slot, trace_id=trace, window=window.describe())
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
        self.repo.save_discovery_watermark(
            window.slot,
            {
                "status": ExecutionStatus.RUNNING.value,
                "started_at": window.reference_time.isoformat(),
                "cutoff": window.cutoff.isoformat(),
                "trace_id": trace,
            },
        )

        jobs_new = 0
        status = ExecutionStatus.COMPLETED.value
        try:
            if not dry_run:
                jobs_new = self.research.run(
                    reference_time=window.reference_time,
                    cutoff=window.cutoff,
                    time_filter=window.time_filter,
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
            self.repo.save_discovery_watermark(
                window.slot,
                {
                    "status": status,
                    "started_at": window.reference_time.isoformat(),
                    "finished_at": finished.isoformat(),
                    "cutoff": window.cutoff.isoformat(),
                    "time_filter": window.time_filter,
                    "window_source": window.source,
                    "trace_id": trace,
                    "jobs_new": jobs_new,
                    "errors": history.errors,
                },
            )
            log_event(
                "discovery_finished",
                slot=window.slot,
                trace_id=trace,
                jobs_new=jobs_new,
                status=status,
            )
            if self.bus:
                self.bus.emit("discovery.finished", jobs_new=jobs_new, status=status)

        return DiscoveryResult(
            slot=window.slot,
            trace_id=trace,
            reference_time=window.reference_time,
            cutoff=window.cutoff,
            time_filter=window.time_filter,
            window_source=window.source,
            jobs_new=jobs_new,
            status=status,
        )


__all__ = ["DiscoveryAgent", "DiscoveryResult"]
