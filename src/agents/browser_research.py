"""Agente 2 (browser) — descoberta em páginas públicas do LinkedIn Jobs.

Mesmo contrato do ``JobResearchAgent`` (``search``/``run`` e os mesmos eventos),
mas sem tocar no celular: lê o JSON-LD da busca pública, usa o ID real da vaga
e busca a descrição na página pública antes de passar para a qualificação.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..core.config import Settings
from ..core.events import EventBus
from ..core.geo import normalize_location
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.android.accessibility import parse_posted
from ..execution.web.linkedin_public import (
    LinkedInPublicBrowser,
    extract_apply_markers,
    extract_card_ages,
    extract_criteria,
    extract_description,
    extract_detail_posted,
    extract_external_links,
    extract_job_id,
    is_remote_public,
    parse_item_list,
)
from ..models.domain import JobOpportunity
from ..storage.database import Repository
from .research import _is_recent, _newest_first


class BrowserResearchAgent:
    def __init__(
        self,
        browser: LinkedInPublicBrowser,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.browser = browser
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self.trace_id = ""
        self._status_path = Path(settings.resolve(settings.storage.control_file)).with_name(
            "status.json"
        )

    def _publish_status(self, **fields: object) -> None:
        """Snapshot atômico do progresso, consultável fora do terminal.

        Escreve em ``data/status.json`` com rename atômico para que um leitor
        nunca veja JSON pela metade. Nunca inclui termos de busca completos nem
        dados sensíveis — apenas índices, contagens e a localidade.
        """
        snapshot = {
            "phase": "search",
            "trace_id": self.trace_id,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            **fields,
        }
        tmp = self._status_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._status_path)
        except OSError:
            pass  # status é best-effort; nunca derruba a busca

    def _card_to_job(
        self,
        card: dict[str, str],
        age: str,
        marker: str,
        query: str = "",
        location: str = "",
        reference_time: datetime | None = None,
    ) -> JobOpportunity | None:
        job_id = extract_job_id(card.get("url", ""))
        if not job_id:
            return None
        posted_at, precision = parse_posted(age, reference_time)
        if posted_at is None and self.settings.search.require_explicit_recency:
            return None
        return JobOpportunity(
            trace_id=self.trace_id,
            title=card.get("title", "").strip(),
            company=card.get("company", "").strip(),
            location=card.get("location", "").strip(),
            location_normalized=normalize_location(card.get("location", "")),
            url=card.get("url", ""),
            external_id=job_id,
            source="linkedin",
            posted_raw=age,
            posted_at=posted_at,
            posted_precision=precision,
            apply_marker=marker,
            # A API pública NÃO informa Easy Apply. Só o celular logado revela
            # o botão — deixa NULL até a análise no app.
            easy_apply=None,
            easy_apply_source="",
            search_query=query,
            search_location=location,
        )

    def _enrich_description(
        self, job: JobOpportunity, reference_time: datetime | None = None
    ) -> bool:
        """Carrega a descrição pública. Retorna False se não houver texto."""
        try:
            detail = self.browser.job_page(job.url)
        except Exception as exc:
            log_event("job_description_unavailable", reason="page", error=str(exc))
            return False
        description = extract_description(detail)
        if not description:
            log_event("job_description_unavailable", reason="vazio", external_id=job.external_id)
            return False
        detail_age = extract_detail_posted(detail)
        if detail_age:
            posted_at, precision = parse_posted(detail_age, reference_time)
            if posted_at is not None:
                job.posted_raw = detail_age
                job.posted_at = posted_at
                job.posted_precision = precision
        seniority, employment = extract_criteria(detail)
        if seniority and seniority not in job.title:
            job.description = f"Seniority level: {seniority}\n{description}"
        else:
            job.description = description
        job.employment_type = employment
        links = extract_external_links(detail)
        if links:
            job.external_apply_url = links[0]["url"]
            job.external_apply_source = links[0]["text"]
            log_event(
                "external_apply_link_found",
                external_id=job.external_id,
                links=len(links),
            )
        log_event("job_description_fetched", length=len(description), external_id=job.external_id)
        return True

    def search(
        self,
        query: str,
        query_index: int = 1,
        query_total: int = 1,
        saved_total: int = 0,
        *,
        reference_time: datetime | None = None,
        cutoff: datetime | None = None,
        time_filter: str = "day",
    ) -> int:
        """Persista apenas vagas com idade explícita dentro da janela configurada.

        Emite progresso no início de cada termo e após cada localidade, além de
        gravar um snapshot atômico em ``data/status.json``. Assim o operador vê
        em que passo está sem esperar o termo inteiro terminar.
        """
        search_cfg = self.settings.search
        # Âncora congelada pela execução: nunca datetime.now() dentro do loop.
        reference = reference_time or datetime.now()
        cutoff = cutoff or reference - timedelta(hours=search_cfg.recency_hours)
        # O filtro nativo é propriedade do adaptador web; fakes de teste sem
        # esse atributo continuam funcionando com o padrão "day".
        if hasattr(self.browser, "time_filter"):
            self.browser.time_filter = time_filter
        locations = [loc.strip() for loc in search_cfg.locations if loc.strip()] or [""]
        found: dict[str, JobOpportunity] = {}

        if self.bus:
            self.bus.emit(
                "search.started",
                current=query_index,
                total=query_total,
                query=query,
                locations=len(locations),
            )
        self._publish_status(
            query_index=query_index,
            query_total=query_total,
            query=query,
            location_index=0,
            location_total=len(locations),
            location="",
            page=0,
            cards_last=0,
            recent_unique=0,
            new_saved=saved_total,
        )

        for loc_index, location in enumerate(locations, start=1):
            for page in range(max(1, self.settings.browser.max_pages_per_query)):
                start = page * 60
                try:
                    page_html = self.browser.search_page(query, location, start)
                except Exception as exc:
                    log_event(
                        "browser_search_failed",
                        query_length=len(query),
                        location=location,
                        start=start,
                        error=str(exc),
                    )
                    continue
                cards = parse_item_list(page_html)
                ages = extract_card_ages(page_html)
                markers = extract_apply_markers(page_html)
                for card in cards:
                    card_id = extract_job_id(card.get("url", ""))
                    job = self._card_to_job(
                        card,
                        ages.get(card_id, ""),
                        markers.get(card_id, ""),
                        query=query,
                        location=location,
                        reference_time=reference,
                    )
                    if job is None:
                        continue  # sem ID real ou sem idade explícita: rejeitada
                    found.setdefault(job.external_id, job)
                log_event(
                    "browser_search_page",
                    query_length=len(query),
                    location=location,
                    cards=len(cards),
                    recent=len(found),
                )
                if self.bus:
                    self.bus.emit(
                        "browser.location",
                        current=query_index,
                        total=query_total,
                        query=query,
                        location=location or "(all)",
                        location_index=loc_index,
                        location_total=len(locations),
                        page=page + 1,
                        cards=len(cards),
                        recent=len(found),
                        new_saved=saved_total,
                    )
                self._publish_status(
                    query_index=query_index,
                    query_total=query_total,
                    query=query,
                    location_index=loc_index,
                    location_total=len(locations),
                    location=location or "(all)",
                    page=page + 1,
                    cards_last=len(cards),
                    recent_unique=len(found),
                    new_saved=saved_total,
                )
                if not cards:
                    break

        candidates = _newest_first(list(found.values()))[: search_cfg.max_jobs_per_query]
        new_count = 0
        for job in candidates:
            # Empresa com supressao TEMPORARIA comprovada (ex.: so aceita
            # candidatos de NJ/NY/Charlotte) nao e salva. O filtro atua AQUI,
            # antes de gravar — a busca publica continua normal.
            if self.repo.is_company_suppressed(job.company):
                log_event(
                    "company_suppressed_discarded",
                    company=job.company,
                    title=job.title,
                    external_id=job.external_id,
                )
                continue
            # Fora da janela de 6h: mantém no banco como EXPIRED (nunca exclui).
            if not _is_recent(job, cutoff, search_cfg.require_explicit_recency):
                job.status = JobStatus.EXPIRED
                self.repo.upsert_job(job)
                continue
            self._enrich_description(job, reference)
            # Filtro remoto na própria fonte pública: sem descrição ou sem
            # evidência de 100% remoto, a vaga nem entra no banco.
            if not is_remote_public(job.title, job.location, job.description):
                log_event(
                    "public_job_not_remote_discarded",
                    external_id=job.external_id,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                )
                continue
            if not self.repo.upsert_job(job):
                continue
            if job.description:
                self.repo.update_job(job)
            new_count += 1
            if self.bus:
                self.bus.emit(
                    "job.discovered",
                    company=job.company,
                    title=job.title,
                    precision=job.posted_precision,
                    trace_id=job.trace_id,
                )
        log_event(
            "search_finished",
            query_length=len(query),
            found=len(found),
            new=new_count,
            source="browser",
        )
        self._publish_status(
            query_index=query_index,
            query_total=query_total,
            query=query,
            location_index=len(locations),
            location_total=len(locations),
            location="(concluído)",
            page=0,
            cards_last=len(found),
            recent_unique=len(found),
            new_saved=saved_total + new_count,
        )
        return new_count

    def run(
        self,
        *,
        reference_time: datetime | None = None,
        cutoff: datetime | None = None,
        time_filter: str = "day",
    ) -> int:
        queries = self.settings.search.queries
        total = 0
        try:
            for index, query in enumerate(queries, start=1):
                found = self.search(
                    query,
                    index,
                    len(queries),
                    total,
                    reference_time=reference_time,
                    cutoff=cutoff,
                    time_filter=time_filter,
                )
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
        finally:
            self.browser.close()
        return total
