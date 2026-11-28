"""Descoberta na Jobgether: descrição completa salva já na coleta."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from src.agents.jobgether_research import JobgetherResearchAgent
from src.core.config import Settings
from src.execution.web.jobgether import (
    JobgetherCard,
    JobgetherDetail,
    build_search_params,
    location_slug,
    parse_offer,
    parse_search,
)
from src.models.domain import JobOpportunity
from src.storage.database import Repository, dedupe_hash

NOW = datetime(2026, 11, 27, 20, 0, tzinfo=timezone(timedelta(hours=-3)))

OFFER_HTML = """
<html><body>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Senior .NET Developer",
 "hiringOrganization":{"name":"Somnio Software"},
 "datePosted":"2026-11-27T18:00:00Z",
 "employmentType":"FULL_TIME",
 "inLanguage":"English",
 "description":"&lt;p&gt;About the role&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Solid .NET and C# experience&lt;/li&gt;&lt;li&gt;100% remote from LATAM&lt;/li&gt;&lt;/ul&gt;"}
</script>
<div data-offer="{&quot;applyUrl&quot;:&quot;https://somniosoftware.recruitee.com/o/senior-net-developer/c/new&quot;,&quot;isEasyApply&quot;:false}"></div>
</body></html>
"""


def _card() -> JobgetherCard:
    return JobgetherCard(
        id="6b08c14a1870ab7f4ccec84a",
        title="Senior .NET Developer",
        company="Somnio Software",
        url="https://jobgether.com/offer/6b08c14a1870ab7f4ccec84a-senior-.net-developer",
        location="Latin America",
        remote="Full Remote",
        contract_type="Full time",
        experience="Senior (5-10 years)",
        salary_range="",
        posted_at=NOW,
    )


class FakeSource:
    def __init__(self, detail: JobgetherDetail, *, cards: list[JobgetherCard] | None = None) -> None:
        self.detail = detail
        self.cards = cards if cards is not None else [_card()]
        self.offer_calls: list[str] = []
        self.search_calls: list[dict[str, Any]] = []

    def search_page(self, keyword, locations, *, page=1):
        self.search_calls.append({"keyword": keyword, "locations": locations, "page": page})
        return list(self.cards), False

    def offer(self, url):
        self.offer_calls.append(url)
        return self.detail

    def close(self) -> None:
        return None


def _settings(tmp_path) -> Settings:
    settings = Settings().model_copy(update={"root": tmp_path})
    settings.sources.jobgether.enabled = True
    settings.sources.jobgether.keywords = [".NET Developer"]
    settings.sources.jobgether.locations = ["latam"]
    settings.sources.jobgether.max_pages = 1
    return settings


def test_location_slug_and_search_params_use_public_api_contract() -> None:
    assert location_slug("Latin America") == "latam"
    assert location_slug("United States") == "united-states"
    settings = _settings(None)  # type: ignore[arg-type]
    params = build_search_params(
        ".NET Developer", ["latam"], settings.sources.jobgether, page=2
    )
    assert params["page"] == 2
    assert params["locations"] == "latam"
    assert params["remoteType"] == "full-remote"
    assert params["sort"] == "date"
    assert "includeHybrid" not in params


def test_parse_search_does_not_invent_description() -> None:
    cards, has_more = parse_search(
        {
            "jobs": [
                {
                    "id": "abc123",
                    "title": "Developer",
                    "company": "Acme",
                    "url": "https://jobgether.com/offer/abc123-developer",
                    "postedAt": "2026-11-27T18:00:00.000Z",
                    "salaryRange": "90000-120000 USD",
                }
            ],
            "pagination": {"page": 1, "hasMore": True},
        }
    )
    assert has_more is True
    assert cards[0].salary_range == "90000-120000 USD"
    assert cards[0].posted_at is not None
    assert not hasattr(cards[0], "description")


def test_parse_offer_reads_full_description_and_real_apply_url() -> None:
    detail = parse_offer(OFFER_HTML)
    assert "About the role" in detail.description
    assert "100% remote from LATAM" in detail.description
    assert detail.title == "Senior .NET Developer"
    assert detail.company == "Somnio Software"
    assert detail.apply_url == (
        "https://somniosoftware.recruitee.com/o/senior-net-developer/c/new"
    )


def test_research_saves_full_description_from_discovery(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    detail = parse_offer(OFFER_HTML)
    fake = FakeSource(detail)
    agent = JobgetherResearchAgent(repo, _settings(tmp_path), source=fake)

    saved = agent.run(reference_time=NOW, cutoff=NOW - timedelta(hours=6))

    assert saved == 1
    assert len(fake.offer_calls) == 1
    jobs = repo.list_jobs()
    assert len(jobs) == 1
    job = repo.get_job(jobs[0].id)
    assert job is not None
    assert job.source == "jobgether"
    assert job.external_id == "6b08c14a1870ab7f4ccec84a"
    assert "100% remote from LATAM" in (job.description or "")
    assert job.external_apply_url.startswith("https://somniosoftware.recruitee.com/")
    assert job.external_apply_source == "jobgether"
    # A classificação posterior pode ler tudo do banco, sem nova navegação.
    assert fake.offer_calls == [_card().url]


def test_known_job_does_not_pay_another_offer_request(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    known = JobOpportunity(
        title=_card().title,
        company=_card().company,
        location=_card().location,
        url=_card().url,
        external_id=_card().id,
        source="jobgether",
    )
    known.dedupe_hash = dedupe_hash(
        known.company, known.title, known.location,
        known.external_id, known.url, known.source,
    )
    assert repo.upsert_job(known) is True

    fake = FakeSource(JobgetherDetail(description="nunca lida"))
    agent = JobgetherResearchAgent(repo, _settings(tmp_path), source=fake)

    assert agent.run(reference_time=NOW, cutoff=NOW - timedelta(hours=6)) == 0
    assert fake.offer_calls == []


def test_gone_offer_is_skipped_without_aborting_the_source(tmp_path) -> None:
    from dataclasses import replace

    repo = Repository(tmp_path / "agent.db")
    gone = replace(_card(), id="6b0a2ee51870ab7f4cd47b7e", url="https://jobgether.com/offer/6b0a2ee51870ab7f4cd47b7e-netsuite")
    usable = replace(
        gone,
        id="6b0a20ce1870ab7f4cd42c89",
        title="Senior/Staff Software Engineer, Security",
        url="https://jobgether.com/offer/6b0a20ce1870ab7f4cd42c89-security",
    )

    class FlakySource(FakeSource):
        def offer(self, url):
            if url == gone.url:
                self.offer_calls.append(url)
                raise RuntimeError("Client error 410 Gone")
            return super().offer(url)

    fake = FlakySource(parse_offer(OFFER_HTML), cards=[gone, usable])
    agent = JobgetherResearchAgent(repo, _settings(tmp_path), source=fake)

    assert agent.run(reference_time=NOW, cutoff=NOW - timedelta(hours=6)) == 1
    assert fake.offer_calls == [gone.url, usable.url]
    jobs = [job for job in repo.list_jobs() if job.source == "jobgether"]
    assert len(jobs) == 1
    assert jobs[0].external_id == usable.id
