"""Supressão temporária: só geografia/visto e só após confirmação."""
from __future__ import annotations

from pathlib import Path

from src.agents.company_suppression import CompanySuppressionAgent
from src.execution.web.linkedin_logged import LinkedInLoggedBrowser
from src.agents.eligibility import EligibilityResult
from src.core.config import Settings
from src.models.domain import JobOpportunity
from src.models.schemas import ExtractedJobFacts
from src.storage.database import Repository


class FakeLLM:
    def structured(self, messages, schema):
        return ExtractedJobFacts()


class FakeBrowser:
    def __init__(self, urls: list[str], *, restriction: bool = True) -> None:
        self.urls = urls
        self.restriction = restriction
        self.detail_calls = 0

    def search_company_job_urls(
        self,
        company: str,
        *,
        limit: int = 3,
        company_url: str = "",
        job_url: str = "",
    ) -> list[str]:
        return self.urls[:limit]

    def read_job_detail(self, url: str) -> dict[str, str] | None:
        self.detail_calls += 1
        return {
            "url": url,
            "title": ".NET Engineer",
            "company": "Acme",
            "location": "Remote - New York",
            "description": "You must reside in New York.",
        }


class FakeEligibility:
    def __init__(self, restriction: bool) -> None:
        self.restriction = restriction

    def evaluate(self, job, facts):
        if self.restriction:
            return EligibilityResult(
                has_restriction=True,
                restriction_kinds=["residence"],
                details=["exige residência: New York"],
            )
        return EligibilityResult()


def _agent(tmp_path: Path, browser, restriction: bool = True) -> tuple[CompanySuppressionAgent, Repository]:
    settings = Settings.load()
    repo = Repository(tmp_path / "suppression.db")
    agent = CompanySuppressionAgent(
        settings,
        repo,
        FakeLLM(),  # type: ignore[arg-type]
        browser=browser,  # type: ignore[arg-type]
        eligibility=FakeEligibility(restriction),  # type: ignore[arg-type]
    )
    # Não dormir entre as leituras nos testes.
    settings.company_suppression.delay_ms = 0
    return agent, repo


def test_only_geography_and_visa_are_suppression_worthy() -> None:
    kinds = CompanySuppressionAgent.suppression_kinds(
        ["residence", "visa", "technology", "language", "seniority"]
    )
    assert kinds == ["residence", "visa"]


def test_company_is_suppressed_only_after_pattern_confirmation(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            "https://www.linkedin.com/jobs/view/111111111",
            "https://www.linkedin.com/jobs/view/222222222",
            "https://www.linkedin.com/jobs/view/333333333",
        ]
    )
    agent, repo = _agent(tmp_path, browser)
    job = JobOpportunity(
        company="Acme",
        title=".NET Engineer",
        location="Remote - New York",
        url="https://www.linkedin.com/jobs/view/999999999",
    )

    empresa = agent.maybe_suppress(job, ["residence"], "exige residência em Nova York")

    assert empresa is not None
    assert repo.is_company_suppressed("Acme")
    assert "residence" in empresa.suppression_reason
    assert browser.detail_calls >= 2


def test_unconfirmed_pattern_does_not_suppress(tmp_path: Path) -> None:
    browser = FakeBrowser(
        [
            "https://www.linkedin.com/jobs/view/111111111",
            "https://www.linkedin.com/jobs/view/222222222",
            "https://www.linkedin.com/jobs/view/333333333",
        ]
    )
    agent, repo = _agent(tmp_path, browser, restriction=False)
    job = JobOpportunity(company="Acme", title=".NET Engineer")

    assert agent.maybe_suppress(job, ["residence"], "residência") is None
    assert not repo.is_company_suppressed("Acme")


def test_technical_restriction_never_suppresses(tmp_path: Path) -> None:
    browser = FakeBrowser(["https://www.linkedin.com/jobs/view/111111111"])
    agent, repo = _agent(tmp_path, browser)
    job = JobOpportunity(company="Acme", title=".NET Engineer")

    assert agent.maybe_suppress(job, ["technology"], "Jenkins ausente") is None
    assert not repo.is_company_suppressed("Acme")
    assert browser.detail_calls == 0


def test_suppression_expires(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "agent.db")
    repo.suppress_company("Acme", days=-1, reason="teste")
    assert not repo.is_company_suppressed("Acme")


def test_company_job_ids_are_read_from_rendered_html() -> None:
    html = (
        '<a href="/jobs/search-results/?currentJobId=4491572876&amp;'
        'originToLandingJobPostings=4491572876%2C4491522105%2C4490679329">x</a>'
    )
    ids = LinkedInLoggedBrowser._job_ids_from_company_html(html)
    assert ids[:3] == ["4491572876", "4491522105", "4490679329"]


def test_company_slug_uses_linkedin_redirect_variant() -> None:
    slugs = LinkedInLoggedBrowser._company_slug("Compasso UOL")
    assert slugs[0] == "compasso-uol"
    assert "compass-uol" in slugs


def test_company_page_url_is_normalized() -> None:
    url = LinkedInLoggedBrowser._company_page_url(
        "https://www.linkedin.com/company/taskverse/life/?trk=x"
    )
    assert url == "https://www.linkedin.com/company/taskverse/jobs/"
