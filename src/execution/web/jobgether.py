"""Fonte pública de vagas da Jobgether.

A API ``/api/v1/jobs`` é HTTP simples, não autenticada e não exige browser.
A listagem NÃO traz a descrição: ela é lida do ``JobPosting`` em JSON-LD na
página do offer e gravada no banco já na descoberta. Assim a classificação
posterior não precisa abrir a vaga na web novamente.

Nada aqui toca o Chrome logado, o CDP ou credenciais.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ...core.config import JobgetherSourceConfig
from ...core.logging import log_event

_API_PATH = "/api/v1/jobs"
_JSONLD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_DATA_OFFER = re.compile(r"data-offer=[\"']([^\"']+)", re.I)
_EXTERNAL_APPLY_HOSTS = (
    "recruitee.com",
    "greenhouse.io",
    "lever.co",
    "workable.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "bamboohr.com",
    "teamtailor.com",
)
_LOCATION_ALIASES = {
    "latam": "latam",
    "latin-america": "latam",
    "latinamerica": "latam",
    "usa": "united-states",
    "us": "united-states",
    "uk": "united-kingdom",
    "england": "united-kingdom",
    "newzealand": "new-zealand",
}


@dataclass(frozen=True)
class JobgetherCard:
    """Item da listagem (sem descrição — ela vem do offer)."""

    id: str
    title: str
    company: str
    url: str
    location: str
    remote: str
    contract_type: str
    experience: str
    salary_range: str
    posted_at: datetime | None
    job_functions: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobgetherDetail:
    """Detalhe do offer: descrição completa e link real de candidatura."""

    description: str = ""
    apply_url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    posted_at: datetime | None = None
    employment_type: str = ""
    languages: tuple[str, ...] = ()


def location_slug(value: str) -> str:
    """Converte um nome de localidade amigável no slug da API Jobgether."""
    text = unicodedata.normalize("NFKD", (value or "").strip()).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return _LOCATION_ALIASES.get(text, text)


def effective_locations(cfg: JobgetherSourceConfig, fallback: list[str]) -> list[str]:
    """Usa os slugs configurados ou deriva-os das localidades globais."""
    if cfg.locations:
        return [slug for slug in (location_slug(item) for item in cfg.locations) if slug]
    return [slug for slug in (location_slug(item) for item in fallback) if slug]


def build_search_params(
    keyword: str,
    locations: list[str],
    cfg: JobgetherSourceConfig,
    *,
    page: int = 1,
) -> dict[str, Any]:
    """Parâmetros GET da API v1. Nunca inclui hybrid quando proibido."""
    params: dict[str, Any] = {
        "keyword": keyword,
        "sort": cfg.sort,
        "page": max(1, page),
        "limit": min(25, max(1, cfg.page_size)),
    }
    if locations:
        params["locations"] = ",".join(locations)
    if cfg.remote_type:
        params["remoteType"] = cfg.remote_type
    if cfg.include_hybrid:
        params["includeHybrid"] = "true"
    if cfg.contract_types:
        params["contractType"] = ",".join(cfg.contract_types)
    return params


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_search(payload: dict[str, Any]) -> tuple[list[JobgetherCard], bool]:
    """Devolve (cards, hasMore). Campos ausentes nunca derrubam a varredura."""
    cards: list[JobgetherCard] = []
    for item in payload.get("jobs") or []:
        if not isinstance(item, dict):
            continue
        offer_id = str(item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not offer_id and not url:
            continue
        functions = item.get("jobFunctions") or []
        cards.append(
            JobgetherCard(
                id=offer_id,
                title=str(item.get("title") or "").strip(),
                company=str(item.get("company") or "").strip(),
                url=url,
                location=str(item.get("location") or "").strip(),
                remote=str(item.get("remote") or "").strip(),
                contract_type=str(item.get("contractType") or "").strip(),
                experience=str(item.get("experience") or "").strip(),
                salary_range=str(item.get("salaryRange") or "").strip(),
                posted_at=_parse_datetime(item.get("postedAt")),
                job_functions=tuple(str(fn).strip() for fn in functions if fn),
            )
        )
    pagination = payload.get("pagination") or {}
    has_more = bool(pagination.get("hasMore")) if isinstance(pagination, dict) else False
    return cards, has_more


def _iter_jsonld(page_html: str) -> Iterator[dict[str, Any]]:
    for block in _JSONLD.findall(page_html or ""):
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        if not isinstance(nodes, list):
            continue
        for node in nodes:
            if isinstance(node, dict):
                yield node


def _clean_offer(value: str) -> str:
    """Texto da descrição preservando quebras de parágrafo e acentos."""
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"</(?:p|li|h\d|ul|ol|div)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [
        re.sub(r"\s+", " ", html_lib.unescape(line)).strip()
        for line in value.split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _find_apply_url(node: dict[str, Any]) -> str:
    for key, value in node.items():
        if "apply" in str(key).lower() and isinstance(value, str) and value.startswith("http"):
            return value.strip()
    return ""


def extract_apply_url(page_html: str) -> str:
    """URL real de candidatura (Recruitee/Greenhouse/etc.), nunca a do Jobgether."""
    for raw in _DATA_OFFER.findall(page_html or ""):
        try:
            payload = json.loads(html_lib.unescape(raw))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            url = _find_apply_url(payload)
            if url:
                return url
    for href in re.findall(r'href=["\'](https?://[^"\']+)["\']', page_html or "", re.I):
        url = html_lib.unescape(href).strip()
        if any(host in url for host in _EXTERNAL_APPLY_HOSTS):
            return url
    return ""


def parse_offer(page_html: str) -> JobgetherDetail:
    """Lê ``JobPosting.description`` e metadados do offer."""
    posting: dict[str, Any] | None = None
    for node in _iter_jsonld(page_html):
        types = node.get("@type")
        if isinstance(types, list):
            is_posting = any(str(item).lower() == "jobposting" for item in types)
        else:
            is_posting = str(types or "").lower() == "jobposting"
        if is_posting and node.get("description"):
            posting = node
            break
    if posting is None:
        return JobgetherDetail(apply_url=extract_apply_url(page_html))

    description = posting.get("description")
    if isinstance(description, dict):
        description = str(description.get("text") or "")
    organization = posting.get("hiringOrganization") or {}
    company = (
        organization.get("name", "")
        if isinstance(organization, dict)
        else str(organization)
    )
    job_location = posting.get("jobLocation") or {}
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else {}
    if isinstance(job_location, dict):
        address = job_location.get("address") or {}
        place = (
            address.get("addressLocality", "")
            if isinstance(address, dict)
            else str(address)
        )
        country = (
            address.get("addressCountry", "")
            if isinstance(address, dict)
            else ""
        )
        if isinstance(country, dict):
            country = country.get("name", "")
        location_text = ", ".join(str(part).strip() for part in (place, country) if part)
    else:
        location_text = str(job_location)
    languages = posting.get("inLanguage") or ()
    if isinstance(languages, (str, dict)):
        languages = (str(languages),)

    return JobgetherDetail(
        description=_clean_offer(str(description or "")),
        apply_url=extract_apply_url(page_html),
        title=str(posting.get("title") or "").strip(),
        company=str(company or "").strip(),
        location=location_text.strip()[:200],
        posted_at=_parse_datetime(posting.get("datePosted")),
        employment_type=str(posting.get("employmentType") or "").strip(),
        languages=tuple(str(item) for item in languages if item),
    )


class JobgetherPublicSource:
    """Cliente HTTP da API pública. Injete ``client`` nos testes (sem rede)."""

    def __init__(
        self,
        cfg: JobgetherSourceConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.cfg = cfg
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=cfg.timeout_seconds,
            headers={"User-Agent": "local-ai-job-agent/1.0 (+job discovery)"},
            follow_redirects=True,
        )

    def search_page(
        self, keyword: str, locations: list[str], *, page: int = 1
    ) -> tuple[list[JobgetherCard], bool]:
        url = self.cfg.base_url.rstrip("/") + _API_PATH
        response = self.client.get(
            url, params=build_search_params(keyword, locations, self.cfg, page=page)
        )
        response.raise_for_status()
        return parse_search(response.json())

    def offer(self, url: str) -> JobgetherDetail:
        response = self.client.get(url)
        response.raise_for_status()
        detail = parse_offer(response.text)
        if not detail.description:
            log_event("jobgether_offer_without_description", url=url)
        return detail

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


__all__ = [
    "JobgetherCard",
    "JobgetherDetail",
    "JobgetherPublicSource",
    "build_search_params",
    "effective_locations",
    "extract_apply_url",
    "location_slug",
    "parse_offer",
    "parse_search",
]
