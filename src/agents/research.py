"""Agente 2 — pesquisa de vagas no LinkedIn via interface real."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..execution.android.accessibility import extract_job_cards, extract_job_description
from ..execution.android.appium_driver import AppiumDevice
from ..models.domain import JobOpportunity
from ..storage.database import Repository


def _is_recent(job: JobOpportunity, cutoff: datetime, require_explicit: bool) -> bool:
    """Só aceita idade explícita dentro da janela configurada.

    O discovery usa uma âncora com timezone (America/Sao_Paulo), enquanto o
    ``parse_posted`` produz datetimes locais ingênuos. Normaliza antes de
    comparar para não misturar valores aware/naive na mesma janela.
    """
    if job.posted_at is None:
        return not require_explicit
    posted = job.posted_at
    limit = cutoff
    if posted.tzinfo is None and limit.tzinfo is not None:
        limit = limit.replace(tzinfo=None)
    elif posted.tzinfo is not None and limit.tzinfo is None:
        posted = posted.replace(tzinfo=None)
    return posted >= limit


def _newest_first(jobs: list[JobOpportunity]) -> list[JobOpportunity]:
    return sorted(jobs, key=lambda job: job.posted_at or datetime.min, reverse=True)


class JobResearchAgent:
    def __init__(
        self, device: AppiumDevice, repo: Repository, settings: Settings, bus: EventBus | None = None
    ) -> None:
        self.device = device
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self.trace_id = ""

    def _enrich_description(self, card: JobOpportunity) -> None:
        """Abre o card e lê a descrição pela árvore de acessibilidade."""
        if not self.device.click_text(card.title):
            return
        try:
            (
                self.device.click_text("See more")
                or self.device.click_text("Show more")
                or self.device.click_text("Ver mais")
            )
            description = extract_job_description(self.device.page_source())
            if description:
                card.description = description[:14000]
                log_event("job_description_fetched", length=len(description))
            else:
                log_event("job_description_unavailable", reason="vazio")
        except Exception as exc:
            log_event("job_description_unavailable", error=str(exc))
        finally:
            try:
                self.device.press_back()
            except Exception:
                pass

    def search(self, query: str) -> int:
        """Retorna quantas vagas novas (após dedupe e janela de recência) foram persistidas."""
        self.device.detect_block()
        search_cfg = self.settings.search
        max_jobs = search_cfg.max_jobs_per_query
        cutoff = datetime.now() - timedelta(hours=search_cfg.recency_hours)

        def recent(cards: list[JobOpportunity]) -> list[JobOpportunity]:
            return [
                card for card in cards
                if _is_recent(card, cutoff, search_cfg.require_explicit_recency)
            ]

        xml = self.device.search(query)
        cards = recent(extract_job_cards(xml, max_jobs))
        found = {card.external_id: card for card in _newest_first(cards)}

        for _ in range(self.settings.device.max_scrolls_per_search):
            if len(found) >= max_jobs:
                break
            self.device.detect_block()
            before = len(found)
            self.device.scroll_forward()
            more = recent(
                extract_job_cards(self.device.page_source(), max_jobs)
            )
            for card in _newest_first(more):
                found.setdefault(card.external_id, card)
            if len(found) == before:
                break

        new_count = 0
        for card in _newest_first(list(found.values()))[:max_jobs]:
            card.trace_id = card.trace_id or self.trace_id
            card.source = "linkedin"
            card.url = card.url or f"linkedin://job/{card.external_id}"
            if not self.repo.upsert_job(card):
                continue
            self._enrich_description(card)
            if card.description:
                self.repo.update_job(card)
            new_count += 1
            if self.bus:
                self.bus.emit(
                    "job.discovered", company=card.company, title=card.title,
                    precision=card.posted_precision, trace_id=card.trace_id,
                )
        log_event("search_finished", query_length=len(query), found=len(found), new=new_count)
        return new_count

    def run(self) -> int:
        self.device.open_linkedin()
        queries = self.settings.search.queries
        total = 0
        for index, query in enumerate(queries, start=1):
            found = self.search(query)
            total += found
            if self.bus:
                self.bus.emit(
                    "search.progress",
                    current=index,
                    total=len(queries),
                    new=found,
                    cumulative=total,
                    trace_id=self.trace_id,
                )
        return total
