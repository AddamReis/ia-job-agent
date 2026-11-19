"""Compatibilidade tecnológica, classificação de canal e prioridade de envio."""
import threading
from datetime import datetime, timedelta

from src.agents.orchestrator import sort_application_queue
from src.agents.qualification import (
    JobQualificationAgent,
    classify_channel,
    currency_indicator,
    detect_region,
    normalize_technology,
    technologies_match,
)
from src.core.config import Settings
from src.core.events import EventBus
from src.core.state import JobStatus
from src.models.domain import CandidateProfile, JobOpportunity
from src.models.schemas import ExtractedJobFacts


class StubLLM:
    def __init__(self, facts: ExtractedJobFacts) -> None:
        self.facts = facts

    def structured(self, messages, schema):
        return self.facts


class StubRepo:
    def update_job(self, job: JobOpportunity) -> None:
        return None


def _agent(facts: ExtractedJobFacts) -> JobQualificationAgent:
    profile = CandidateProfile(
        years_experience=7,
        skills={"backend": [".NET", "C#", "SQL Server", "AWS"]},
    )
    return JobQualificationAgent(StubLLM(facts), profile, StubRepo(), Settings.load())


def _facts(required: list[str]) -> ExtractedJobFacts:
    return ExtractedJobFacts(
        required_skills=required,
        seniority="senior",
        years_required=5,
        salary_present=True,
        currency="USD",
        is_remote=True,
        location_eligible=True,
        language_requirements=["english"],
        summary="x",
    )


def _job(**overrides) -> JobOpportunity:
    data = {
        "title": ".NET Engineer",
        "company": "Example",
        "location": "Brazil (Remote)",
        "salary_raw": "USD 120,000",
        "description": ".NET and C# role.",
        "posted_at": datetime.now() - timedelta(hours=2),
        "posted_precision": "hour",
    }
    data.update(overrides)
    return JobOpportunity(**data)


def test_qualification_emits_progress_per_job():
    class ProgressRepo(StubRepo):
        def list_jobs(self, status, limit=None):
            return [
                _job(title="First", company="A"),
                _job(title="Second", company="B"),
            ]

    bus = EventBus()
    seen = []
    bus.subscribe("qualification.progress", lambda event: seen.append(event.payload))
    agent = JobQualificationAgent(
        StubLLM(_facts([".NET", "C#"])),
        CandidateProfile(years_experience=7, skills={"backend": [".NET", "C#"]}),
        ProgressRepo(),
        Settings.load(),
        bus,
    )
    agent.run()
    assert [event["current"] for event in seen] == [1, 2]
    assert all(event["total"] == 2 for event in seen)
    assert seen[-1]["title"] == "Second"


def test_live_qualification_drains_jobs_as_they_arrive():
    jobs = [_job(title="First", company="A"), _job(title="Second", company="B")]
    stop = threading.Event()

    class LiveRepo(StubRepo):
        def list_jobs(self, status, limit=None):
            return [job for job in jobs if job.status == JobStatus.DISCOVERED]

        def get_job(self, job_id):
            return next((job for job in jobs if job.id == job_id), None)

    bus = EventBus()
    seen = []

    def on_progress(event):
        seen.append(event.payload)
        if len(seen) == len(jobs):
            stop.set()  # simula o fim da busca depois que todas foram analisadas

    bus.subscribe("qualification.progress", on_progress)
    agent = JobQualificationAgent(
        StubLLM(_facts([".NET", "C#"])),
        CandidateProfile(years_experience=7, skills={"backend": [".NET", "C#"]}),
        LiveRepo(),
        Settings.load(),
        bus,
    )

    qualified = agent.run_live(stop, settle_seconds=0, poll_interval=0.01)

    assert qualified == 2
    assert [event["current"] for event in seen] == [1, 2]
    assert all(job.status != JobStatus.DISCOVERED for job in jobs)


def test_technology_mismatch_is_hard_rejected():
    job = _job()
    _agent(_facts(["Kubernetes", "Go", "React"])).qualify(job)
    assert job.qualification.decision == "SKIP"
    assert "tecnologias incompatíveis" in job.qualification.hard_reject_reason


def test_compatible_technologies_are_approved():
    job = _job()
    _agent(_facts([".NET", "C#", "AWS"])).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_classify_channel_uses_captured_card_marker():
    settings = Settings.load()
    # Marcador sozinho nao basta: a coluna booleana e a fonte autoritativa.
    assert (
        classify_channel(_job(company="Acme", apply_marker="Easy Apply"), settings)
        == "unknown"
    )
    assert (
        classify_channel(
            _job(company="Acme", apply_marker="Easy Apply", easy_apply=True), settings
        )
        == "easy_apply"
    )
    assert (
        classify_channel(
            _job(company="Acme", apply_marker="Apply on company website"), settings
        )
        == "external_portal"
    )
    # Empresa conhecida com marcador externo continua sendo portal conhecido.
    assert (
        classify_channel(
            _job(company="Strider", apply_marker="Apply on company website"), settings
        )
        == "known_portal"
    )
    # Marcador do cartão prevalece sobre a ausência de marcador.
    assert (
        classify_channel(
            _job(source="linkedin", url="linkedin://job/1",
                 apply_marker="Apply on company website"),
            settings,
        )
        == "external_portal"
    )


def test_classify_channel_uses_priority_order():
    settings = Settings.load()
    assert classify_channel(_job(company="BairesDev"), settings) == "known_portal"
    assert classify_channel(_job(company="Strider"), settings) == "known_portal"
    # URL do LinkedIn SEM marcador não prova Easy Apply (pode redirect externo).
    assert (
        classify_channel(_job(source="linkedin", url="linkedin://job/1"), settings)
        == "unknown"
    )
    assert (
        classify_channel(_job(description="Apply on company website"), settings)
        == "external_portal"
    )


def test_technology_aliases_normalize_to_same_canonical():
    for value in (".NET", "DotNet", "Dot Net", ".NET Core", ".Net Framework"):
        assert normalize_technology(value) == "net", value
    assert normalize_technology("C#") == "c"
    assert normalize_technology("C Sharp") == "c"


def test_technology_matching_handles_writing_variants():
    assert technologies_match("DotNet", ".NET")
    assert technologies_match("C#", "C Sharp")
    assert technologies_match("AWS Lambda", "AWS")
    assert technologies_match(".NET 8", ".NET")
    assert not technologies_match("C", "Cassandra")


def test_variant_writing_does_not_trigger_false_mismatch():
    job = _job()
    _agent(_facts(["DotNet", "C Sharp", "AWS Lambda"])).qualify(job)
    assert job.qualification.decision == "APPLY"


def test_queue_prioritizes_easy_apply_then_recency_then_score():
    now = datetime.now()
    jobs = [
        _job(title="External", channel="external_portal", posted_at=now - timedelta(hours=1), score=95),
        _job(title="Known", channel="known_portal", posted_at=now - timedelta(hours=3), score=70),
        _job(title="EasyOld", channel="easy_apply", posted_at=now - timedelta(hours=5), score=60),
        _job(title="EasyNew", channel="easy_apply", posted_at=now - timedelta(hours=2), score=90),
    ]
    priority = {"easy_apply": 0, "known_portal": 1, "external_portal": 2}
    ordered = sort_application_queue(jobs, priority)
    assert [job.title for job in ordered] == ["EasyNew", "EasyOld", "Known", "External"]


def test_region_priority_comes_before_channel():
    now = datetime.now()
    jobs = [
        _job(title="BrazilEasy", channel="easy_apply", region="brazil",
             posted_at=now - timedelta(hours=1), score=95),
        _job(title="EuropeExternal", channel="external_portal", region="europe",
             posted_at=now - timedelta(hours=5), score=60),
        _job(title="NorthEasy", channel="easy_apply", region="americas_north",
             posted_at=now - timedelta(hours=4), score=70),
    ]
    channels = {"easy_apply": 0, "known_portal": 1, "external_portal": 2}
    regions = {
        "americas_north": 0, "australia": 1, "new_zealand": 2,
        "europe": 3, "brazil": 4, "other": 5,
    }
    ordered = sort_application_queue(jobs, channels, regions)
    assert [job.title for job in ordered] == ["NorthEasy", "EuropeExternal", "BrazilEasy"]


def test_detect_region_by_location():
    assert detect_region("United States (Remote)") == "americas_north"
    assert detect_region("Toronto, Canada (Remote)") == "americas_north"
    assert detect_region("Sydney, Australia (Remote)") == "australia"
    assert detect_region("Auckland, New Zealand (Remote)") == "new_zealand"
    assert detect_region("Berlin, Germany (Remote)") == "europe"
    assert detect_region("São Paulo, Brazil (Remote)") == "brazil"
    assert detect_region("") == "other"


def test_currency_is_indicator_and_also_a_filter():
    assert currency_indicator(set(), False) == ("sem salário informado", False)
    assert currency_indicator({"USD"}, True) == ("USD", False)
    assert currency_indicator({"USD", "BRL"}, True)[1] is True

    settings = Settings.load()
    assert settings.qualification.reject.salary_currency_mismatch is True
    assert settings.search.currencies == ["USD", "EUR"]
    job = _job(salary_raw="R$ 20.000", description="Salário em BRL, plano de saúde.")
    _agent(_facts([".NET", "C#"])).qualify(job)
    assert "BRL" in job.currency_signal
    assert job.qualification.decision == "SKIP"
    assert "moeda não elegível" in job.qualification.hard_reject_reason
