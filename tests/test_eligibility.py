"""Testes das barreiras de elegibilidade e do enriquecimento da empresa."""
from __future__ import annotations

from pathlib import Path

from src.agents.eligibility import EligibilityAgent, EligibilityResult
from src.agents.qualification import JobQualificationAgent
from src.core.config import Settings
from src.core.state import JobStatus
from src.models.domain import (
    CandidateProfile,
    Company,
    JobOpportunity,
)
from src.models.schemas import (
    CompanyEligibilityFacts,
    ExtractedJobFacts,
    LanguageRequirement,
)
from src.storage.database import Repository


def _settings(tmp_path: Path | None = None) -> Settings:
    base = Settings.load()
    if tmp_path is None:
        return base
    return base.model_copy(
        update={
            "storage": base.storage.model_copy(
                update={
                    "database": str(tmp_path / "agent.db"),
                    "control_file": str(tmp_path / "control.json"),
                }
            )
        }
    )


def _agent(settings: Settings | None = None) -> EligibilityAgent:
    return EligibilityAgent(settings or _settings())


def _job(**overrides) -> JobOpportunity:
    from datetime import datetime

    data = {
        "title": ".NET Engineer",
        "company": "Example",
        "location": "Remote - Brazil",
        "url": "https://www.linkedin.com/jobs/view/123456789",
        "external_id": "123456789",
        "external_apply_url": "https://careers.example.com/apply/1",
        "posted_at": datetime.now(),
        "posted_raw": "1 hour ago",
        "posted_precision": "hour",
    }
    data.update(overrides)
    return JobOpportunity(**data)


def _facts(**overrides) -> ExtractedJobFacts:
    data = {
        "required_skills": [".NET"],
        "seniority": "mid",
        "years_required": 3,
        "is_remote": True,
        "language_requirements": ["english"],
        "confidence": 0.9,
    }
    data.update(overrides)
    return ExtractedJobFacts(**data)


def test_visa_sponsorship_required_blocks() -> None:
    result = _agent().evaluate(_job(location="Remote - United States"),
                              _facts(visa_sponsorship_required=True))
    assert result.has_restriction
    assert "visa" in result.restriction_kinds
    assert "sponsorship" in result.summary.lower()


def test_company_offered_visa_sponsorship_is_neutral() -> None:
    """Quando a empresa oferece visto/relocação, não há restrição nem desconto."""
    job = _job(
        description="We will sponsor your visa and provide a relocation package."
    )
    result = _agent().evaluate(
        job,
        _facts(visa_sponsorship_required=True, visa_sponsorship_mentioned=True),
    )
    assert not result.has_restriction
    assert "visa" not in result.restriction_kinds


def test_company_paid_relocation_in_portuguese_is_neutral() -> None:
    job = _job(description="Oferecemos patrocínio de visto e mudança custeada pela empresa.")
    result = _agent().evaluate(job, _facts(visa_sponsorship_required=True))
    assert not result.has_restriction
    assert "visa" not in result.restriction_kinds


def test_no_sponsorship_available_still_blocks_when_explicitly_required() -> None:
    job = _job(description="No visa sponsorship available for this role.")
    result = _agent().evaluate(job, _facts(visa_sponsorship_required=True))
    assert "visa" in result.restriction_kinds


def test_french_required_blocks_but_spanish_passes() -> None:
    agent = _agent()
    french = agent.evaluate(
        _job(location="Remote - France"),
        _facts(language_details=[LanguageRequirement(language="French", level="fluent")]),
    )
    assert french.has_restriction
    assert "language" in french.restriction_kinds
    assert "French" in french.summary

    spanish = agent.evaluate(
        _job(location="Remote - Spain"),
        _facts(language_details=[LanguageRequirement(language="Spanish", level="intermediate")]),
    )
    assert not spanish.has_restriction


def test_native_level_does_not_block_known_language() -> None:
    """Nível fluent/C1/C2/native nunca barra: só a identidade do idioma importa."""
    result = _agent().evaluate(
        _job(),
        _facts(language_details=[
            LanguageRequirement(language="English", level="fluent", native_required=True)
        ]),
    )
    assert not result.has_restriction

    espanhol = _agent().evaluate(
        _job(),
        _facts(language_details=[
            LanguageRequirement(language="Spanish", level="native", native_required=True)
        ]),
    )
    assert not espanhol.has_restriction


def test_residence_in_europe_blocks() -> None:
    result = _agent().evaluate(
        _job(location="Remote"),
        _facts(residence_restriction="You must be based in Europe"),
    )
    assert result.has_restriction
    assert "residence" in result.restriction_kinds


def test_worldwide_remote_passes() -> None:
    result = _agent().evaluate(
        _job(location="Italy"),
        _facts(worldwide_remote=True, eligible_regions=["Worldwide"]),
    )
    assert not result.has_restriction


def test_eligible_brazil_does_not_create_location_restriction() -> None:
    """Config 'Brazil' e texto 'Brasil' precisam casar (case/alias)."""
    result = _agent().evaluate(_job(), _facts(eligible_regions=["Brasil"]))
    assert not result.has_restriction
    assert "location" not in result.restriction_kinds


def test_eligible_foreign_country_creates_location_restriction() -> None:
    result = _agent().evaluate(
        _job(), _facts(eligible_regions=["United States"])
    )
    assert result.has_restriction
    assert "location" in result.restriction_kinds


def test_brazilian_city_and_uf_do_not_create_work_authorization_restriction() -> None:
    """'Manaus, AM' é Brasil: texto genérico de autorização não é barreira."""
    result = _agent().evaluate(
        _job(location="Manaus, AM"), _facts(work_authorization_required=True)
    )
    assert not result.has_restriction
    assert "work_authorization" not in result.restriction_kinds


def test_foreign_location_still_blocks_work_authorization() -> None:
    result = _agent().evaluate(
        _job(location="Remote - Germany"), _facts(work_authorization_required=True)
    )
    assert result.has_restriction
    assert "work_authorization" in result.restriction_kinds


def test_company_policy_blocks_when_job_is_omitted() -> None:
    agent = _agent()
    company = Company(name="EuropeOnly", hiring_regions=["Europe"], languages=["French"])
    result = agent.apply_company_policy(EligibilityResult(), company)
    assert result.has_restriction
    assert "company_policy" in result.restriction_kinds
    assert "company_language" in result.restriction_kinds


def test_restriction_is_persisted_and_causes_skip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    facts = _facts(visa_sponsorship_required=True)

    class LLM:
        def structured(self, messages, schema):
            return facts

        def chat(self, messages, **kwargs):
            return "{}"

    class StubRepo:
        def get_company_by_name(self, name):
            return None

        def update_job(self, job):
            return None

    agent = JobQualificationAgent(
        LLM(),  # type: ignore[arg-type]
        CandidateProfile(years_experience=7, skills={"backend": [".NET"]}),
        StubRepo(),  # type: ignore[arg-type]
        settings,
        eligibility=EligibilityAgent(settings),
    )
    job = _job(location="Remote - United States")
    agent.qualify(job)
    assert job.status == JobStatus.REJECTED_BY_FILTER
    assert job.has_restriction
    assert job.qualification is not None
    assert "elegibilidade" in (job.qualification.hard_reject_reason or "")
