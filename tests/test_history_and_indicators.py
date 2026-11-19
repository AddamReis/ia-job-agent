"""Histórico preservado, status de candidatura e indicadores salariais."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.agents.qualification import JobQualificationAgent
from src.core.config import Settings
from src.core.state import JobStatus
from src.models.domain import (
    CandidateProfile,
    JobOpportunity,
)
from src.models.schemas import ExtractedJobFacts
from src.storage.database import Repository


def _job(**overrides) -> JobOpportunity:
    data = {
        "title": ".NET Engineer",
        "company": "Example",
        "location": "Remote",
        "url": "https://www.linkedin.com/jobs/view/111222333",
        "external_id": "111222333",
        "posted_at": datetime.now(),
        "posted_raw": "1 hour ago",
        "posted_precision": "hour",
        "description": "Remote .NET role. Salary 90,000 USD per year.",
    }
    data.update(overrides)
    return JobOpportunity(**data)


def test_expire_marks_history_without_deleting(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "agent.db")
    old = _job(posted_at=datetime.now() - timedelta(hours=10))
    repo.upsert_job(old)
    fresh = _job(
        external_id="999888777",
        dedupe_hash="fresh",
        posted_at=datetime.now(),
    )
    repo.upsert_job(fresh)

    expired = repo.expire_stale_jobs(datetime.now() - timedelta(hours=6))
    assert expired == [old.id]
    assert repo.get_job(old.id) is not None, "vaga antiga NÃO pode ser excluída"
    assert repo.get_job(old.id).status == JobStatus.EXPIRED
    assert repo.get_job(fresh.id).status == JobStatus.DISCOVERED


def test_applied_flag_columns_survive_update(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "agent.db")
    job = _job()
    repo.upsert_job(job)
    job.has_applied = True
    job.applied_at = datetime(2026, 11, 17, 18, 0, 0)
    job.status = JobStatus.SUBMITTED
    repo.update_job(job)
    reread = repo.get_job(job.id)
    assert reread is not None
    assert reread.has_applied
    assert reread.applied_at == datetime(2026, 11, 17, 18, 0, 0)

    # Reencontrar a vaga em outro termo não pode apagar a flag de candidatura.
    repo.upsert_job(_job())
    reread = repo.get_job(job.id)
    assert reread is not None
    assert reread.has_applied
    assert reread.status == JobStatus.SUBMITTED


def test_salary_indicator_hourly_and_yearly() -> None:
    facts = ExtractedJobFacts(
        required_skills=[".NET"],
        seniority="mid",
        years_required=3,
        salary_present=True,
        salary_min=60,
        salary_max=60,
        currency="USD",
        salary_period="hourly",
    )
    job = _job(salary_raw="")
    JobQualificationAgent._apply_salary_indicator(job, facts)
    assert job.salary_basis == "hourly"
    assert job.salary_disclosed
    assert "USD" in job.salary_text

    facts.salary_period = "per year"
    facts.salary_min = 90000
    facts.salary_max = 90000
    job2 = _job(salary_raw="")
    JobQualificationAgent._apply_salary_indicator(job2, facts)
    assert job2.salary_basis == "yearly"
