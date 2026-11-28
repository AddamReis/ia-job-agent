"""Base multiplataforma: dedupe por fonte, URL canônica e watermarks separados."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.agents.browser_apply import BrowserApplyAgent
from src.agents.discovery import DiscoveryAgent
from src.core.config import Settings
from src.core.state import ExecutionStatus
from src.models.domain import JobOpportunity
from src.storage.database import Repository, dedupe_hash

NOW = datetime(2026, 11, 27, 10, 0, tzinfo=timezone(timedelta(hours=-3)))


class FakeSource:
    def __init__(self, count: int) -> None:
        self.count = count
        self.trace_id = ""

    def run(self, **kwargs) -> int:
        self.received = kwargs
        return self.count


def test_dedupe_hash_preserves_legacy_linkedin_and_separates_new_sources() -> None:
    args = ("Empresa", "Developer", "Remoto", "1234567890", "https://linkedin.com/jobs/view/1234567890")
    assert dedupe_hash(*args, source="linkedin") == dedupe_hash(*args)
    assert dedupe_hash(*args, source="") == dedupe_hash(*args)
    assert dedupe_hash(*args, source="jobgether") != dedupe_hash(*args)

    # IDs iguais em plataformas diferentes não colidem.
    linkedin = dedupe_hash("A", "Dev", "BR", "abc123", "", "linkedin")
    jobgether = dedupe_hash("A", "Dev", "BR", "abc123", "", "jobgether")
    assert linkedin != jobgether


def test_job_url_uses_canonical_url_by_source() -> None:
    url = "https://jobgether.com/offer/6b08c14a1870ab7f4ccec84a-senior-.net-developer"
    job = JobOpportunity(source="jobgether", external_id="6b08c14a1870ab7f4ccec84a", url=url)
    assert BrowserApplyAgent.job_url(job) == url

    applied = job.model_copy(
        update={"external_apply_url": "https://somniosoftware.recruitee.com/o/senior-net-developer/c/new"}
    )
    assert BrowserApplyAgent.job_url(applied) == (
        "https://somniosoftware.recruitee.com/o/senior-net-developer/c/new"
    )

    linkedin = JobOpportunity(external_id="1234567890", url="https://www.linkedin.com/jobs/view/1234567890")
    assert BrowserApplyAgent.job_url(linkedin) == "https://www.linkedin.com/jobs/view/1234567890"


def test_discovery_aggregates_sources_with_separate_watermarks(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    settings = Settings().model_copy(update={"root": tmp_path})
    linkedin, jobgether = FakeSource(3), FakeSource(2)
    agent = DiscoveryAgent(
        settings,
        repo=repo,
        browser=object(),
        sources=[("linkedin", linkedin), ("jobgether", jobgether)],
    )

    result = agent.run("first", trace_id="trace-multi", now=NOW)

    assert result.jobs_new == 5
    assert result.sources == {"linkedin": 3, "jobgether": 2}
    assert linkedin.received["reference_time"] == NOW
    assert jobgether.received["reference_time"] == NOW
    assert linkedin.trace_id == "trace-multi"
    assert jobgether.trace_id == "trace-multi"

    legacy = repo.get_discovery_watermark("10:00")
    new = repo.get_discovery_watermark("10:00:jobgether")
    assert legacy is not None and new is not None
    assert legacy["status"] == ExecutionStatus.COMPLETED.value
    assert legacy["jobs_new"] == 3
    assert new["jobs_new"] == 2
    assert new["source"] == "jobgether"
    assert "fontes=jobgether=2 linkedin=3" in result.describe()
