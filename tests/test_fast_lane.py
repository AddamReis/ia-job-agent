"""Fast lane determinístico: perfil simplificado de portal confiável, sem LLM."""
from __future__ import annotations

import pytest

from src.agents.fast_lane import FastLaneAgent
from src.core.config import Settings
from src.core.state import JobStatus
from src.models.domain import Company, JobOpportunity
from src.shared.seed import TRUSTED_PORTALS


class StubRepo:
    def __init__(self, company: Company, jobs: list[JobOpportunity]) -> None:
        self.company = company
        self.jobs = jobs
        self.updated: list[JobOpportunity] = []

    @staticmethod
    def normalize_company(name: str) -> str:
        return " ".join(
            (name or "").lower().replace(" llc", "").replace(" s/a", "").split()
        )

    def get_company_by_name(self, name: str) -> Company | None:
        if self.normalize_company(name) == self.normalize_company(self.company.name):
            return self.company
        return None

    def list_jobs(self, status=None, limit=None) -> list[JobOpportunity]:
        return list(self.jobs)

    def load_profile(self, schema):
        return schema(years_experience=7, skills={})

    def update_job(self, job: JobOpportunity) -> None:
        self.updated.append(job)


DESCRIPTION = (
    "5+ years of experience in Software Development and .NET Development. "
    "Proven expertise in C#, ASP.NET and SQL Server. "
    "100% remote work (from anywhere). Excellent compensation in USD. "
    "Advanced proficiency in English."
)


def _company(*, trusted: bool = True) -> Company:
    return Company(
        name="BairesDev",
        is_trusted_portal=trusted,
        trusted_portal_config=dict(TRUSTED_PORTALS["BairesDev"]) if trusted else {},
    )


def _job(title: str, *, location: str = "Brazil (Remote)", description: str = DESCRIPTION):
    return JobOpportunity(
        company="BairesDev",
        title=title,
        location=location,
        description=description,
    )


def _agent(jobs: list[JobOpportunity], *, trusted: bool = True) -> FastLaneAgent:
    return FastLaneAgent(StubRepo(_company(trusted=trusted), jobs), Settings.load())


def test_dotnet_full_stack_is_approved_without_llm() -> None:
    job = _job("Senior Full Stack Developer (ReactJS + .NET C# WebAPI) - Remote Work")
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "APPLY"
    assert result.score >= 70
    assert job.status == JobStatus.QUALIFIED
    assert job.channel == "known_portal"
    assert job.restriction_details == ""
    assert job.qualification is result


@pytest.mark.parametrize(
    "title",
    [
        "Senior Backend Engineer (Go) - Remote Work",
        "Senior Django Full Stack Engineer - Remote Work",
        "Senior Java Backend Engineer - Integrations - Remote Work",
        "Ingeniero PHP - Trabajo Remoto",
        "AI Full-stack Engineer (Next + Python) - Remote Work",
    ],
)
def test_off_stack_titles_are_rejected(title: str) -> None:
    job = _job(title)
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "SKIP"
    assert job.status == JobStatus.REJECTED_BY_FILTER
    assert job.restriction_details.startswith("análise:")
    assert result.hard_reject_reason


def test_non_remote_dotnet_job_is_rejected() -> None:
    job = _job(
        "Senior .NET Developer",
        location="São Paulo, Brazil (hybrid)",
        description=DESCRIPTION.replace("100% remote work (from anywhere).", "Hybrid work, 3 days in office."),
    )
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "SKIP"
    assert "remota" in (result.hard_reject_reason or "")


def test_explicit_brl_is_rejected_but_absent_currency_is_eligible() -> None:
    brl = _job("Senior .NET Developer")
    brl.description = DESCRIPTION.replace("compensation in USD", "compensation in BRL 20,000")
    result = _agent([brl]).qualify(brl, TRUSTED_PORTALS["BairesDev"])
    assert result.decision == "SKIP"
    assert "BRL" in (result.hard_reject_reason or "")

    without = _job("Senior .NET Developer")
    without.description = DESCRIPTION.replace("compensation in USD", "competitive compensation")
    result = _agent([without]).qualify(without, TRUSTED_PORTALS["BairesDev"])
    assert result.decision == "APPLY"


def test_foreign_language_requirement_is_rejected() -> None:
    job = _job("Senior .NET Developer")
    job.description = DESCRIPTION + " Fluent French is required for this role."
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "SKIP"
    assert "French" in (result.hard_reject_reason or "")


def test_company_must_be_trusted_portal() -> None:
    with pytest.raises(ValueError, match="não é portal confiável"):
        _agent([], trusted=False).load_config()


def test_company_suffix_is_normalized_after_lowercase() -> None:
    from src.storage.database import Repository

    assert Repository.normalize_company("BairesDev LLC") == "bairesdev"
    assert Repository.normalize_company("BairesDev S/A") == "bairesdev"


def test_run_filters_by_company_and_counts_verdicts() -> None:
    target = _job("Senior .NET Developer")
    other = JobOpportunity(
        company="Strider",
        title="Senior .NET Developer",
        location="Brazil (Remote)",
        description=DESCRIPTION,
    )
    rejected = _job("Senior Backend Engineer (Go) - Remote Work")

    counters = _agent([target, other, rejected]).run()

    assert counters == {"total": 2, "qualified": 1, "rejected": 1}
    assert target.status == JobStatus.QUALIFIED
    assert other.status == JobStatus.DISCOVERED
    assert rejected.status == JobStatus.REJECTED_BY_FILTER


def test_tech_lead_in_description_is_rejected() -> None:
    job = _job("Senior Full Stack Developer (ReactJS + .NET C# WebAPI)")
    job.description = DESCRIPTION + (
        " We are seeking a Full-Stack Tech Lead Developer. "
        "Lead smaller project teams and mentor junior developers."
    )
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "SKIP"
    assert "liderança" in (result.hard_reject_reason or "").lower()
    assert result.score == 0


def test_ten_years_required_is_rejected_but_five_is_allowed() -> None:
    senior = _job("Senior .NET Developer")
    senior.description = DESCRIPTION.replace(
        "5+ years of experience", "at least 10 years of professional experience"
    )
    result = _agent([senior]).qualify(senior, TRUSTED_PORTALS["BairesDev"])
    assert result.decision == "SKIP"
    assert "10+ anos" in (result.hard_reject_reason or "")

    normal = _job("Senior .NET Developer")
    result = _agent([normal]).qualify(normal, TRUSTED_PORTALS["BairesDev"])
    assert result.decision == "APPLY"


def test_empty_description_is_not_approved() -> None:
    job = _job("Senior Microsoft Teams & .NET Developer", description="")
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "SKIP"
    assert "sem descrição" in (result.hard_reject_reason or "").lower()
    assert result.score == 0


def test_lead_the_creation_is_not_people_leadership() -> None:
    """Posse técnica do código não é liderança de equipe."""
    job = _job("Senior Microsoft Teams & .NET Developer")
    job.description = DESCRIPTION.replace(
        "100% remote work (from anywhere).",
        "You will lead the creation of collaborative applications and serve as a "
        "key technical driver. 100% remote work (from anywhere).",
    )
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "APPLY"
    assert result.score == 100


def test_company_age_is_not_required_experience() -> None:
    job = _job("Senior .NET Developer")
    job.description = (
        DESCRIPTION
        + " BairesDev has been leading the way for over 15 years. We deliver "
        "cutting-edge solutions worldwide."
    )
    result = _agent([job]).qualify(job, TRUSTED_PORTALS["BairesDev"])

    assert result.decision == "APPLY"
