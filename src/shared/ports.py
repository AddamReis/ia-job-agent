"""Portas (interfaces) da arquitetura hexagonal.

Agentes dependem destes contratos, nunca de SQLAlchemy/Playwright/OpenAI.
As implementações ficam em ``src/adapters`` e ``src/storage``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .domain import Company, JobOpportunity


class Clock(Protocol):
    """Relógio injetável — permite testar janelas sem depender de agora."""

    def now(self) -> datetime: ...


class JobSource(Protocol):
    """Fonte externa de vagas: HTML público, sem login."""

    def run(
        self,
        *,
        reference_time: datetime | None = None,
        cutoff: datetime | None = None,
        time_filter: str = "day",
    ) -> int: ...


class JobRepository(Protocol):
    """Persistência mínima necessária aos agentes."""

    def upsert_job(self, job: JobOpportunity) -> bool: ...

    def update_job(self, job: JobOpportunity) -> None: ...

    def list_trusted_portals(self) -> list[Company]: ...

    def get_discovery_watermark(self, slot: str) -> dict[str, Any] | None: ...

    def save_discovery_watermark(self, slot: str, data: dict[str, Any]) -> None: ...


class PortalRegistry(Protocol):
    """Portais confiáveis são dados da empresa, não código."""

    def list_trusted(self) -> list[Company]: ...

    def filters_for(self, company: str) -> dict[str, Any] | None: ...


class LLMPort(Protocol):
    """Inferência local.DGX é o único adaptador permitido."""

    def structured(self, messages: list[dict[str, str]], schema: type) -> Any: ...


__all__ = ["Clock", "JobSource", "JobRepository", "PortalRegistry", "LLMPort"]
