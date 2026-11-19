"""Testes da descoberta em páginas públicas (sem rede e sem navegador)."""
from __future__ import annotations

import json
from pathlib import Path

from src.agents.browser_research import BrowserResearchAgent
from src.agents.qualification import classify_channel
from src.core.config import Settings
from src.core.events import EventBus
from src.execution.web.linkedin_public import (
    build_search_url,
    detect_block,
    extract_apply_markers,
    extract_card_ages,
    extract_criteria,
    extract_description,
    extract_detail_posted,
    extract_job_id,
    parse_item_list,
)
from src.models.domain import JobOpportunity

URL = "https://www.linkedin.com/jobs/view/senior-net-at-acme-123456789"
SEARCH_HTML = f"""
<script type="application/ld+json">{json.dumps({
    "@context": "https://schema.org",
    "@type": "ItemList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "url": URL,
         "name": "Senior .NET Engineer", "disambiguatingDescription": "Acme",
         "description": "Remote"}
    ],
})}</script>
<li><a href="{URL}">Senior .NET Engineer</a>
    <time class="h-card__listdate--new">2 hours ago</time></li>
"""
DETAIL_HTML = """
<span class="posted-time-ago__text posted-time-ago__text--new">1 hour ago</span>
<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-20
     relative overflow-hidden"><p>Experiencia com .NET, C#, Azure e SQL Server.</p></div>
<ul class="description__job-criteria-list">
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Seniority level</h3>
    <span class="description__job-criteria-text">Mid-Senior level</span>
  </li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Employment type</h3>
    <span class="description__job-criteria-text">Full-time</span>
  </li>
</ul>
"""


class StubBrowser:
    def close(self) -> None:
        pass

    def search_page(self, query: str, location: str = "", start: int = 0) -> str:
        return SEARCH_HTML

    def job_page(self, url: str) -> str:
        return DETAIL_HTML


class StubRepo:
    def __init__(self, suppressed: set[str] | None = None) -> None:
        self.saved: list[JobOpportunity] = []
        self.suppressed = suppressed or set()

    def upsert_job(self, job: JobOpportunity) -> bool:
        self.saved.append(job)
        return True

    def update_job(self, job: JobOpportunity) -> None:
        pass

    def is_company_suppressed(self, name: str) -> bool:
        return name in self.suppressed


def _cards_html(titles: list[str], age: str = "2 hours ago") -> str:
    """Página pública sintética com N cards, IDs distintos e idade explícita."""
    items: list[dict[str, object]] = []
    cards: list[str] = []
    for index, title in enumerate(titles, start=1):
        job_id = str(4_000_000_000 + index)
        url = f"https://www.linkedin.com/jobs/view/role-at-acme-{job_id}"
        items.append({
            "@type": "ListItem",
            "position": index,
            "url": url,
            "name": title,
            "disambiguatingDescription": "Acme",
            "description": "Remote",
        })
        cards.append(f'<li><a href="{url}">{title}</a><time>{age}</time></li>')
    return (
        '<script type="application/ld+json">'
        + json.dumps({
            "@context": "https://schema.org",
            "@type": "ItemList",
            "itemListElement": items,
        })
        + "</script>"
        + "".join(cards)
    )


def _settings_for_test(tmp_path: Path) -> Settings:
    base = Settings.load()
    return base.model_copy(update={
        "search": base.search.model_copy(
            update={"queries": [".NET Engineer"], "locations": ["Brazil"]}
        ),
        "storage": base.storage.model_copy(
            update={"control_file": str(tmp_path / "control.json")}
        ),
    })


def test_all_recent_jobs_are_saved_without_a_query_cap(tmp_path: Path) -> None:
    """Toda vaga compatível entra: não existe 'N mais novas por query'."""
    class ManyBrowser(StubBrowser):
        def search_page(self, query: str, location: str = "", start: int = 0) -> str:
            return _cards_html([f".NET Engineer {index}" for index in range(40)])

    repo = StubRepo()
    agent = BrowserResearchAgent(ManyBrowser(), repo, _settings_for_test(tmp_path))  # type: ignore[arg-type]

    assert agent.run() == 40
    assert len(repo.saved) == 40
    assert len({job.external_id for job in repo.saved}) == 40


def test_focused_java_and_frontend_titles_are_discarded_early(tmp_path: Path) -> None:
    """Pré-filtro barato: nem busca descrição para Java/FrontEnd focados."""
    titles = [
        "Senior Java Developer",
        "Senior Frontend Developer",
        ".NET Engineer",
        "Full Stack Developer (React + .NET C# WebAPI)",
    ]

    class CountingBrowser(StubBrowser):
        def __init__(self) -> None:
            self.descriptions = 0

        def search_page(self, query: str, location: str = "", start: int = 0) -> str:
            return _cards_html(titles)

        def job_page(self, url: str) -> str:
            self.descriptions += 1
            return DETAIL_HTML

    browser = CountingBrowser()
    repo = StubRepo()
    agent = BrowserResearchAgent(browser, repo, _settings_for_test(tmp_path))  # type: ignore[arg-type]

    assert agent.run() == 2
    assert browser.descriptions == 2  # Java/FrontEnd nem abriram a página
    assert {job.title for job in repo.saved} == {
        ".NET Engineer",
        "Full Stack Developer (React + .NET C# WebAPI)",
    }


def test_old_jobs_are_not_saved_as_expired(tmp_path: Path) -> None:
    """Fora da janela não é compatível: não ocupa banco nem tempo do robô."""
    class OldBrowser(StubBrowser):
        def search_page(self, query: str, location: str = "", start: int = 0) -> str:
            return _cards_html([".NET Engineer"], age="3 days ago")

    repo = StubRepo()
    agent = BrowserResearchAgent(OldBrowser(), repo, _settings_for_test(tmp_path))  # type: ignore[arg-type]

    assert agent.run() == 0
    assert repo.saved == []


def test_card_ages_are_not_swapped_between_neighbors() -> None:
    html = """
    <li><a href="https://www.linkedin.com/jobs/view/job-at-a-111111111">A</a>
        <time>2 hours ago</time></li>
    <li><a href="https://www.linkedin.com/jobs/view/job-at-b-222222222">B</a>
        <time>5 hours ago</time></li>
    """
    assert extract_card_ages(html) == {
        "111111111": "2 hours ago",
        "222222222": "5 hours ago",
    }


def test_config_feature_flag_is_not_a_block() -> None:
    # HTML real traz este atributo na pagina normal; nao e um desafio CAPTCHA.
    assert detect_block(
        '<meta id="config" data-recaptcha-v3-integration-lix-value="enabled" '
        'data-is-bot="false">'
    ) is False
    assert detect_block('<div class="captcha-challenge">Verify you are a human</div>')


def test_search_url_uses_public_24h_filter() -> None:
    url = build_search_url(".NET Engineer", "Brazil")
    assert "f_TPR=r86400" in url
    assert "keywords=.NET+Engineer" in url or "keywords=.NET%20Engineer" in url


def test_json_ld_exposes_real_job_id() -> None:
    cards = parse_item_list(SEARCH_HTML)
    assert len(cards) == 1
    assert extract_job_id(cards[0]["url"]) == "123456789"


def test_age_and_apply_marker_extraction() -> None:
    assert extract_card_ages(SEARCH_HTML) == {"123456789": "2 hours ago"}
    assert extract_apply_markers(
        SEARCH_HTML.replace("<li>", '<li><span>Easy Apply</span>')
    ) == {"123456789": "Easy Apply"}


def test_detail_extraction() -> None:
    assert ".NET" in extract_description(DETAIL_HTML)
    assert extract_detail_posted(DETAIL_HTML) == "1 hour ago"
    seniority, employment = extract_criteria(DETAIL_HTML)
    assert seniority == "Mid-Senior level"
    assert employment == "Full-time"


def test_agent_saves_only_real_id_and_description(tmp_path: Path) -> None:
    base = Settings.load()
    settings = base.model_copy(
        update={
            "search": base.search.model_copy(
                update={"queries": [".NET Engineer"], "locations": ["Brazil"]}
            ),
            "storage": base.storage.model_copy(
                update={"control_file": str(tmp_path / "control.json")}
            ),
        }
    )
    repo = StubRepo()
    bus = EventBus()
    events: list[dict] = []
    bus.subscribe("search.progress", lambda event: events.append(event.payload))
    bus.subscribe("job.discovered", lambda event: events.append(event.payload))

    agent = BrowserResearchAgent(StubBrowser(), repo, settings, bus)  # type: ignore[arg-type]
    assert agent.run() == 1

    job = repo.saved[0]
    assert job.external_id == "123456789"
    assert job.url == URL
    assert job.posted_raw == "1 hour ago"
    assert job.posted_precision == "hour"
    assert ".NET" in job.description
    # Sem marcador explícito no HTML público, o canal permanece desconhecido.
    assert classify_channel(job, settings) == "unknown"
    assert any(event.get("current") == 1 for event in events)


def test_suppressed_company_job_is_not_saved(tmp_path: Path) -> None:
    base = Settings.load()
    settings = base.model_copy(
        update={
            "search": base.search.model_copy(
                update={"queries": [".NET Engineer"], "locations": ["Brazil"]}
            ),
            "storage": base.storage.model_copy(
                update={"control_file": str(tmp_path / "control.json")}
            ),
        }
    )
    repo = StubRepo(suppressed={"Acme"})
    agent = BrowserResearchAgent(StubBrowser(), repo, settings)  # type: ignore[arg-type]

    assert agent.run() == 0
    assert repo.saved == []


def test_progress_events_and_status_snapshot(tmp_path: Path) -> None:
    base = Settings.load()
    search = base.search.model_copy(
        update={"queries": [".NET Engineer"], "locations": ["Brazil"]}
    )
    settings = base.model_copy(
        update={
            "search": search,
            "storage": base.storage.model_copy(
                update={"control_file": str(tmp_path / "control.json")}
            ),
        }
    )
    bus = EventBus()
    events: list[tuple[str, dict]] = []
    for name in ("search.started", "browser.location", "search.progress"):
        bus.subscribe(
            name, lambda event, name=name: events.append((name, event.payload))
        )
    agent = BrowserResearchAgent(StubBrowser(), StubRepo(), settings, bus)  # type: ignore[arg-type]
    assert agent.run() == 1

    kinds = [name for name, _ in events]
    assert kinds == [
        "search.started",
        "browser.location",
        "search.progress",
    ]
    started = events[0][1]
    assert started["current"] == 1 and started["total"] == 1
    assert started["query"] == ".NET Engineer"
    location = events[1][1]
    assert location["location"] == "Brazil"
    assert location["cards"] == 1 and location["recent"] == 1
    assert location["location_index"] == 1 and location["location_total"] == 1

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["query_index"] == 1
    assert status["recent_unique"] == 1
    assert status["new_saved"] == 1
    assert "updated_at" in status


def test_agent_rejects_job_without_explicit_age(tmp_path: Path) -> None:
    no_age = SEARCH_HTML.replace("2 hours ago", "recently")
    base = Settings.load()
    settings = base.model_copy(
        update={
            "storage": base.storage.model_copy(
                update={"control_file": str(tmp_path / "control.json")}
            )
        }
    )

    class NoAgeBrowser(StubBrowser):
        def search_page(self, query: str, location: str = "", start: int = 0) -> str:
            return no_age

    repo = StubRepo()
    agent = BrowserResearchAgent(NoAgeBrowser(), repo, settings)  # type: ignore[arg-type]
    assert agent.run() == 0
    assert repo.saved == []
