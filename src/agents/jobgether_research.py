"""Descoberta na Jobgether com o mesmo contrato do research do LinkedIn.

A descrição completa vem do JSON-LD do offer e é gravada junto da vaga. A
classificação posterior lê só o banco. Filtros baratos e dedupe rodam ANTES da
busca do detalhe, para não pagar uma navegação por vaga já conhecida.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..core.config import Settings
from ..core.events import EventBus
from ..core.geo import normalize_location
from ..core.logging import log_event
from ..execution.web.jobgether import (
    JobgetherPublicSource,
    effective_locations,
)
from ..models.domain import JobOpportunity
from ..shared.stack_filter import is_off_stack_title
from ..storage.database import Repository, dedupe_hash
from .research import _is_recent
from ..execution.web.linkedin_public import is_remote_public

SOURCE = "jobgether"


class JobgetherResearchAgent:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
        *,
        source: JobgetherPublicSource | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self.trace_id = ""
        self.cfg = settings.sources.jobgether
        self.source = source or JobgetherPublicSource(self.cfg)
        self.locations = effective_locations(self.cfg, settings.search.locations)

    def _card_to_job(self, card, keyword: str, location: str) -> JobOpportunity | None:
        if not card.url and not card.id:
            return None
        posted = card.posted_at
        if posted is None and self.settings.search.require_explicit_recency:
            return None
        precision = "unknown"
        if posted is not None:
            precision = "hour" if (posted.hour or posted.minute or posted.second) else "day"
        return JobOpportunity(
            trace_id=self.trace_id,
            title=card.title,
            company=card.company,
            location=card.location,
            location_normalized=normalize_location(card.location),
            url=card.url,
            external_id=card.id,
            source=SOURCE,
            posted_at=posted,
            posted_precision=precision,  # type: ignore[arg-type]
            posted_raw=posted.isoformat() if posted else "",
            salary_raw=card.salary_range,
            employment_type=card.contract_type,
            search_query=keyword,
            search_location=location,
        )

    def run(
        self,
        *,
        reference_time: datetime | None = None,
        cutoff: datetime | None = None,
        time_filter: str = "day",
    ) -> int:
        """Varre as queries configuradas e devolve o total de vagas novas."""
        reference = reference_time or datetime.now()
        cutoff = cutoff or reference - timedelta(hours=self.settings.search.recency_hours)
        require_explicit = self.settings.search.require_explicit_recency
        seen: set[str] = set()
        saved = 0
        details = 0
        discarded = {"off_stack": 0, "suppressed": 0, "outside_window": 0, "known": 0, "offer_failed": 0, "no_description": 0, "not_remote": 0}

        try:
            for keyword in self.cfg.keywords:
                for location in self.locations or [""]:
                    for page in range(1, max(1, self.cfg.max_pages) + 1):
                        try:
                            cards, has_more = self.source.search_page(
                                keyword, [location] if location else [], page=page
                            )
                        except Exception as exc:
                            log_event(
                                "jobgether_search_failed",
                                keyword_length=len(keyword),
                                location=location,
                                page=page,
                                error=str(exc),
                            )
                            break
                        log_event(
                            "jobgether_search_page",
                            keyword_length=len(keyword),
                            location=location,
                            page=page,
                            found=len(cards),
                        )
                        for card in cards:
                            key = card.id or card.url
                            if key in seen:
                                continue
                            seen.add(key)
                            job = self._card_to_job(card, keyword, location)
                            if job is None:
                                continue
                            if is_off_stack_title(job.title):
                                discarded["off_stack"] += 1
                                continue
                            if self.repo.is_company_suppressed(job.company):
                                discarded["suppressed"] += 1
                                continue
                            if not _is_recent(job, cutoff, require_explicit):
                                discarded["outside_window"] += 1
                                continue

                            # Dedupe ANTES do detalhe: vaga já gravada não paga
                            # uma navegação ao offer na sobreposição de janelas.
                            job.dedupe_hash = dedupe_hash(
                                job.company, job.title, job.location,
                                job.external_id, job.url, job.source,
                            )
                            if self.repo.exists_by_dedupe_hash(job.dedupe_hash):
                                discarded["known"] += 1
                                continue
                            if details >= self.cfg.max_details_per_slot:
                                log_event("jobgether_detail_budget_reached", limit=self.cfg.max_details_per_slot)
                                continue

                            try:
                                # Offer pode ter sido removido (410) entre a
                                # listagem e o detalhe. Uma vaga caducada não
                                # pode abortar toda a varredura da fonte.
                                detail = self.source.offer(card.url)
                            except Exception as exc:
                                discarded["offer_failed"] += 1
                                log_event(
                                    "jobgether_offer_failed",
                                    external_id=card.id,
                                    error=str(exc),
                                )
                                continue
                            details += 1
                            if not detail.description:
                                discarded["no_description"] += 1
                                continue
                            job.description = detail.description
                            job.title = detail.title or job.title
                            job.company = detail.company or job.company
                            job.employment_type = detail.employment_type or job.employment_type
                            if detail.posted_at is not None:
                                job.posted_at = detail.posted_at
                            if not _is_recent(job, cutoff, require_explicit):
                                discarded["outside_window"] += 1
                                continue
                            if not is_remote_public(job.title, job.location, job.description):
                                discarded["not_remote"] += 1
                                continue
                            job.external_apply_url = detail.apply_url
                            job.external_apply_source = SOURCE if detail.apply_url else ""

                            if self.repo.upsert_job(job):
                                saved += 1
                                if self.bus:
                                    self.bus.emit(
                                        "job.discovered",
                                        job_id=job.id,
                                        company=job.company,
                                        title=job.title,
                                        precision=job.posted_precision,
                                        trace_id=job.trace_id,
                                        source=SOURCE,
                                    )
                        if not has_more:
                            break
        finally:
            self.source.close()

        log_event(
            "jobgether_run_finished",
            trace_id=self.trace_id,
            new=saved,
            details=details,
            discarded=discarded,
        )
        return saved


__all__ = ["JobgetherResearchAgent", "SOURCE"]
