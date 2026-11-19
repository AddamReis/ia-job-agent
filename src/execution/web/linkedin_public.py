"""Leitura das páginas públicas do LinkedIn Jobs.

Usa somente HTML público (search e view), sem login, sem API privada e sem
qualquer forma de evasão. O Chrome instalado é conduzido pelo Playwright em
modo headless; o parsing é feito sobre o HTML entregue pela própria página.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import time
from urllib.parse import urlencode

from ...core.config import Settings
from ...core.errors import BlockedByPlatformError

# O ID costuma ser seguido por aspas (href/JSON-LD), não apenas por / ? # ou fim.
_JOB_ID = re.compile(
    r"/jobs/view/(?:[^/?\"'<>]*-)?(\d{6,})(?=[/?#\"'<>\\s}]|$)", re.I
)
_JSON_LD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
_TIME = re.compile(r"<time\b[^>]*>(.*?)</time>", re.S | re.I)
_LI_BLOCK = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_MARKUP = re.compile(
    r'<div class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', re.S | re.I
)
_CRITERION = re.compile(
    r'<li class="description__job-criteria-item">(.*?)</li>', re.S | re.I
)
_ANCHOR = re.compile(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_AGE_HINT = re.compile(r"\b(ago|now|há|agora)\b", re.I)

# Filtro remoto aplicado JÁ na leitura pública: o agente só aceita 100% remoto.
_REMOTE_RE = re.compile(
    r"\b(remote|remoto|work from home|wfh|anywhere|distributed|telecommut|teletrabalho)\b",
    re.I,
)
_ONSITE_RE = re.compile(
    r"\b(hybrid|h[íi]brido|on-?site|presencial|in[- ]office|office[- ]based)\b",
    re.I,
)
# "hybrid cloud/app" é tecnologia, não modelo de trabalho.
_TECH_HYBRID_RE = re.compile(
    r"\bhybrid\s+(?:cloud|app|mobile|application|framework|integration|database)\b",
    re.I,
)


def is_remote_public(*texts: str) -> bool:
    """True somente com sinal explícito de remoto e nenhum sinal de presencial.

    Usado na camada pública antes de salvar a vaga. ``is_remote`` do LLM e a
    checagem da tela do celular são as duas validações seguintes.
    """
    text = " ".join(t or "" for t in texts)
    cleaned = _TECH_HYBRID_RE.sub(" ", text)
    if _ONSITE_RE.search(cleaned):
        return False
    return bool(_REMOTE_RE.search(cleaned))

# Sinais de que o link externo leva a um cadastro/candidatura (e não a uma
# página institucional, de privacidade ou rede social).
_APPLY_HINTS = re.compile(
    r"apply|applic|career|job|opening|position|vacanc|recruit|talent|"
    r"candidat|inscri|form|register|sign.?up|workday|greenhouse|lever|"
    r"icims|smartrecruiters|bamboohr|myworkdayjobs|ultipro|successfactors",
    re.I,
)
_NOT_APPLY = re.compile(
    r"privacy|polic|terms|cookie|linkedin\.com|facebook|instagram|twitter|"
    r"youtube|mailto:|tel:|#",
    re.I,
)

# Padrões específicos de uma página de bloqueio/verificação real. Não usar
# substring simples: o HTML normal traz `data-recaptcha-v3-integration-lix-value`
# (feature flag) e links para /authwall, o que geraria falso positivo.
_BLOCK_PATTERNS = (
    re.compile(r'data-is-bot=["\']true["\']', re.I),
    re.compile(r'name=["\']pageKey["\']\s+content=["\']d_authwall', re.I),
    re.compile(r"\bunusual traffic\b", re.I),
    re.compile(r"\bverify you(?: are|'re) a human\b", re.I),
    re.compile(r"\baccess denied\b", re.I),
    re.compile(r"<title[^>]*>[^<]*\bcaptcha\b", re.I),
    re.compile(
        r"<(?:div|section|iframe)[^>]*(?:class|id)=[\"'][^\"']*\bcaptcha\b", re.I
    ),
    re.compile(r"\bpágina não encontrada\b|\bpage not found\b", re.I),
)


def _clean(value: str) -> str:
    """Remove tags/entidades e normaliza espaços sem perder acentos."""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(?:p|li|div|h\d)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html_lib.unescape(value).split())


def extract_visible_text(page_html: str) -> str:
    """Texto visível de uma página pública externa, para análise pelo LLM."""
    if not page_html:
        return ""
    value = re.sub(
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", page_html,
        flags=re.S | re.I,
    )
    return _clean(value)


def build_search_url(
    query: str, location: str = "", start: int = 0, time_filter: str = "day"
) -> str:
    """URL pública de busca com filtro nativo configurável (padrão: 24h)."""
    native = {"day": "r86400", "week": "r604800"}
    params: dict[str, str | int] = {
        "keywords": query,
        "f_TPR": native.get(time_filter, native["day"]),
        "start": start,
    }
    if location:
        params["location"] = location
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def extract_job_id(url: str) -> str:
    """Extrai o ID numérico real de qualquer URL /jobs/view/... ."""
    match = _JOB_ID.search(url or "")
    return match.group(1) if match else ""


def parse_item_list(page_html: str) -> list[dict[str, str]]:
    """Lê o ItemList JSON-LD entregue na busca pública (titulo/empresa/local/url)."""
    for block in _JSON_LD.findall(page_html or ""):
        try:
            data = json.loads(html_lib.unescape(block.strip()))
        except (ValueError, TypeError):
            continue
        nodes = data.get("itemListElement", []) if isinstance(data, dict) else []
        if data.get("@type") == "ItemList" and nodes:
            return [
                {
                    "url": str(node.get("url", "")),
                    "title": str(node.get("name", "")),
                    "company": str(node.get("disambiguatingDescription", "")),
                    "location": str(node.get("description", "")),
                }
                for node in nodes
                if isinstance(node, dict) and node.get("url")
            ]
    return []


def extract_card_ages(page_html: str) -> dict[str, str]:
    """Mapeia ID da vaga -> idade explícita ('16 minutes ago', '2 hours ago').

    Cada ``<li>`` de resultado traz o link da vaga e o ``<time>`` no mesmo bloco.
    Usar esse bloco (em vez de uma janela de caracteres) evita atribuir a idade
    de um card vizinho a outro ID — os cards têm ~3 KB cada.
    """
    ages: dict[str, str] = {}
    for match in _LI_BLOCK.finditer(page_html or ""):
        block = match.group(1)
        ids = _JOB_ID.findall(block)
        if not ids:
            continue
        age = _TIME.search(block)
        if age is None:
            continue
        raw = _clean(age.group(1))
        if raw and _AGE_HINT.search(raw):
            ages.setdefault(ids[0], raw)
    return ages


def extract_apply_markers(page_html: str) -> dict[str, str]:
    """Detecta 'Easy Apply' no HTML do card, quando a página pública informa."""
    markers: dict[str, str] = {}
    for match in _LI_BLOCK.finditer(page_html or ""):
        block = match.group(1)
        ids = _JOB_ID.findall(block)
        if not ids:
            continue
        text = _clean(block).lower()
        if "easy apply" in text:
            markers[ids[0]] = "Easy Apply"
    return markers


def extract_description(page_html: str) -> str:
    """Extrai o texto da descrição publicada na página da vaga."""
    match = _MARKUP.search(page_html or "")
    return _clean(match.group(1)) if match else ""


def extract_external_links(page_html: str) -> list[dict[str, str]]:
    """Links externos citados na descrição, com texto-âncora.

    Heurística conservadora: só devolve links que parecem de candidatura/cadastro.
    Nunca inventa URL — se a descrição não traz link, a lista é vazia.
    """
    markup = _MARKUP.search(page_html or "")
    if not markup:
        return []
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _ANCHOR.finditer(markup.group(1)):
        url = html_lib.unescape(match.group(1)).strip()
        text = _clean(match.group(2))[:160]
        if not url.lower().startswith("http") or _NOT_APPLY.search(url):
            continue
        if not _APPLY_HINTS.search(f"{url} {text}"):
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append({"url": url, "text": text})
    return links


def extract_detail_posted(page_html: str) -> str:
    match = re.search(
        r'class="posted-time-ago__text[^"]*"[^>]*>(.*?)</span>',
        page_html or "",
        re.S | re.I,
    )
    return _clean(match.group(1)) if match else ""


def extract_criteria(page_html: str) -> tuple[str, str]:
    """Retorna (seniority, employment_type) do bloco de critérios da vaga."""
    seniority = employment = ""
    for match in _CRITERION.finditer(page_html or ""):
        block = match.group(1)
        heading = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S | re.I)
        value = re.search(
            r'<span class="description__job-criteria-text[^"]*"[^>]*>(.*?)</span>',
            block,
            re.S | re.I,
        )
        if not heading or not value:
            continue
        label = _clean(heading.group(1))
        text = _clean(value.group(1))
        if label == "Seniority level":
            seniority = text
        elif label == "Employment type":
            employment = text
    return seniority, employment


def detect_block(page_html: str) -> bool:
    text = page_html or ""
    return any(pattern.search(text) for pattern in _BLOCK_PATTERNS)


class LinkedInPublicBrowser:
    """Driver de navegação das páginas públicas, com ritmo conservador."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Janela nativa da busca; o discovery-agent ajusta por slot.
        self.time_filter = "day"
        self._playwright = None
        self._browser = None
        self._page = None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        cfg = self.settings.browser
        self._playwright = sync_playwright().start()
        launch: dict[str, object] = {"headless": cfg.headless}
        if cfg.source:
            launch["channel"] = cfg.source
        self._browser = self._playwright.chromium.launch(**launch)
        context = self._browser.new_context(locale="en-US")
        self._page = context.new_page()
        self._page.set_default_timeout(cfg.timeout_ms)

    def _get(
        self,
        url: str,
        wait_selector: str | None = None,
        check_block: bool = True,
        wait_timeout_ms: int | None = None,
    ) -> str:
        self._ensure()
        cfg = self.settings.browser
        time.sleep(max(0, cfg.delay_ms) / 1000.0)
        self._page.goto(url, wait_until="domcontentloaded", timeout=cfg.timeout_ms)
        if wait_selector:
            try:
                # ``script`` e elementos de descrição podem existir mas estar
                # ocultos; esperar "visible" custava o timeout inteiro por página.
                self._page.wait_for_selector(
                    wait_selector,
                    state="attached",
                    timeout=wait_timeout_ms or cfg.timeout_ms,
                )
            except Exception:
                pass
        content = self._page.content() or ""
        if check_block and detect_block(content):
            raise BlockedByPlatformError("LinkedIn bloqueou o acesso público")
        return content

    def search_page(
        self, query: str, location: str = "", start: int = 0, time_filter: str | None = None
    ) -> str:
        filtro = time_filter or getattr(self, "time_filter", "day")
        return self._get(
            build_search_url(query, location, start, filtro),
            'script[type="application/ld+json"]',
        )

    def job_page(self, url: str) -> str:
        # Nem toda vaga pública expõe esse seletor; esperar 30s por ele
        # transformava cada detalhe em uma pausa longa. 3s bastam para o HTML
        # já carregado pelo domcontentloaded.
        return self._get(url, "div.show-more-less-html__markup", wait_timeout_ms=3000)

    def public_page(self, url: str) -> str:
        """Página pública externa (site da empresa), sem detecção do LinkedIn."""
        return self._get(url, check_block=False)

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None
