"""Rotinas separadas de classificação e aplicação, com aprovação obrigatória."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.agents.application_worker import (
    ApplicationWorker,
    portal_knowledge,
    sort_for_application,
)
from src.agents.classification_worker import (
    ClassificationWorker,
    sort_for_classification,
)
from src.core.config import Settings
from src.core.events import EventBus
from src.core.state import JobStatus
from src.execution.web.site_patterns import SitePatternStore
from src.models.domain import ApprovalRequest, Company, JobOpportunity, QualificationResult
from src.shared.seed import TRUSTED_PORTALS


def _settings() -> Settings:
    return Settings.load()


def _trusted() -> Company:
    return Company(
        name="BairesDev",
        is_trusted_portal=True,
        trusted_portal_config=dict(TRUSTED_PORTALS["BairesDev"]),
    )


def _job(company: str, title: str, *, status: JobStatus = JobStatus.DISCOVERED, **kwargs):
    description = kwargs.pop(
        "description",
        "5+ years of .NET development. 100% remote work. "
        "Proven expertise in C#, ASP.NET and SQL Server.",
    )
    return JobOpportunity(
        company=company,
        title=title,
        location="Brazil (Remote)",
        description=description,
        status=status,
        posted_at=datetime.now() - timedelta(hours=1),
        posted_precision="hour",
        **kwargs,
    )


class StubRepo:
    def __init__(self, jobs: list[JobOpportunity], companies: list[Company] | None = None):
        self.jobs = {job.id: job for job in jobs}
        self.companies = companies or []
        self.updated: list[JobOpportunity] = []
        self.applications: set[str] = set()
        self.approvals: dict[str, ApprovalRequest] = {}
        self.events: dict[str, dict] = {}

    @staticmethod
    def normalize_company(name: str) -> str:
        return " ".join((name or "").lower().replace(" llc", "").split())

    def list_jobs(self, status=None, limit=None) -> list[JobOpportunity]:
        jobs = [
            job for job in self.jobs.values()
            if status is None or job.status in (status if isinstance(status, list) else [status])
        ]
        return jobs[:limit] if limit else jobs

    def list_companies(self) -> list[Company]:
        return list(self.companies)

    def get_company_by_name(self, name: str) -> Company | None:
        normalizado = self.normalize_company(name)
        return next(
            (c for c in self.companies if self.normalize_company(c.name) == normalizado),
            None,
        )

    def get_job(self, job_id: str) -> JobOpportunity | None:
        return self.jobs.get(job_id)

    def update_job(self, job: JobOpportunity) -> None:
        self.updated.append(job)

    def has_application_for(self, job_id: str) -> bool:
        return job_id in self.applications

    def get_approval_for_job(self, job_id: str) -> ApprovalRequest | None:
        return self.approvals.get(job_id)

    @staticmethod
    def agent_event_id(consumer: str, event_type: str, job_id: str) -> str:
        return f"{consumer}|{event_type}|{job_id}"

    def publish_agent_event(self, consumer, event_type, job_id, payload=None) -> bool:
        event_id = self.agent_event_id(consumer, event_type, job_id)
        existing = self.events.get(event_id)
        if existing and existing["status"] not in {"ACK", "FAILED"}:
            return False
        self.events[event_id] = {
            "id": event_id,
            "consumer": consumer,
            "event_type": event_type,
            "job_id": job_id,
            "payload": payload or {},
            "status": "PENDING",
            "error": "",
        }
        return True

    def claim_agent_events(self, consumer, limit=20, *, stale_after_seconds=900):
        claimed = []
        for event in self.events.values():
            if event["consumer"] != consumer or event["status"] != "PENDING":
                continue
            event["status"] = "PROCESSING"
            claimed.append(dict(event))
            if len(claimed) >= limit:
                break
        return claimed

    def ack_agent_event(self, event_id, *, status="ACK", error="") -> bool:
        if event_id not in self.events:
            return False
        self.events[event_id]["status"] = status
        self.events[event_id]["error"] = error
        return True


class RecordingQualification:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _process_one(self, job: JobOpportunity) -> tuple[str, bool]:
        self.calls.append(job.id)
        job.status = JobStatus.QUALIFIED
        job.score = 88
        job.qualification = QualificationResult(
            score=88, breakdown={}, decision="APPLY", reasons=[]
        )
        return "APPLY", True


class RecordingFastLane:
    instances: list["RecordingFastLane"] = []

    def __init__(self, repo, settings, company_name="BairesDev") -> None:
        self.company_name = company_name
        self.calls: list[JobOpportunity] = []
        RecordingFastLane.instances.append(self)

    def qualify(self, job: JobOpportunity, config: dict) -> QualificationResult:
        self.calls.append(job)
        job.status = JobStatus.QUALIFIED
        job.score = 100
        result = QualificationResult(score=100, breakdown={}, decision="APPLY", reasons=[])
        job.qualification = result
        return result


class RecordingApplyAgent:
    def __init__(self) -> None:
        self.prepared: list[JobOpportunity] = []

    def prepare(self, job: JobOpportunity) -> ApprovalRequest:
        self.prepared.append(job)
        return ApprovalRequest(opportunity_id=job.id, company=job.company)

    def submit(self, *args, **kwargs):
        raise AssertionError("worker de aplicação NUNCA deve chamar submit")


def test_classification_prioritizes_trusted_portal(monkeypatch: pytest.MonkeyPatch) -> None:
    trusted = _job("BairesDev", "Senior .NET Developer")
    other = _job("Acme", "Senior .NET Developer")
    empresas = {RepositoryNormalize(c.name): c for c in [_trusted()]}

    ordenadas = sort_for_classification(
        [other, trusted], empresas, {RepositoryNormalize("BairesDev")}
    )

    assert [job.id for job in ordenadas] == [trusted.id, other.id]


def RepositoryNormalize(name: str) -> str:
    return StubRepo.normalize_company(name)


def test_classification_worker_uses_fast_lane_for_trusted_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingFastLane.instances.clear()
    monkeypatch.setattr("src.agents.classification_worker.FastLaneAgent", RecordingFastLane)
    job = _job("BairesDev", "Senior .NET Developer")
    repo = StubRepo([job], [_trusted()])
    bus = EventBus()
    events: list[str] = []
    bus.subscribe("*", lambda event: events.append(event.type))
    qualification = RecordingQualification()

    worker = ClassificationWorker(repo, _settings(), qualification, bus)
    counters = worker.run()

    assert counters == {
        "total": 1, "qualified": 1, "rejected": 0, "errors": 0, "known_portal": 1
    }
    assert qualification.calls == []
    assert len(RecordingFastLane.instances) == 1
    assert RecordingFastLane.instances[0].calls == [job]
    assert "job.qualified" in events
    assert "job.classified" in events
    # Handoff persistente para a máquina local com CDP.
    event_id = repo.agent_event_id("application", "job.qualified", job.id)
    assert repo.events[event_id]["status"] == "PENDING"
    assert repo.events[event_id]["payload"]["known_portal"] is True


def test_classification_worker_sends_other_companies_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job("Acme", "Senior .NET Developer")
    repo = StubRepo([job], [_trusted()])
    qualification = RecordingQualification()
    worker = ClassificationWorker(repo, _settings(), qualification)

    counters = worker.run()

    assert counters["qualified"] == 1
    assert qualification.calls == [job.id]


def test_application_worker_only_wakes_on_apply_events() -> None:
    bus = EventBus()
    repo = StubRepo([])
    worker = ApplicationWorker(repo, _settings(), RecordingApplyAgent(), bus)

    bus.emit("job.classified", job_id="skip-1", decision="SKIP")
    bus.emit("job.qualified", job_id="apply-1", decision="APPLY")
    assert worker.notified == {"apply-1"}

    worker.run()
    assert worker.notified == set()


def test_application_preflight_blocks_ineligible_currency() -> None:
    job = _job(
        "BairesDev",
        "Senior .NET Developer",
        status=JobStatus.QUALIFIED,
        description="100% remote. Salary CHF 120000. C# and .NET.",
    )
    repo = StubRepo([job], [_trusted()])
    worker = ApplicationWorker(repo, _settings(), RecordingApplyAgent())

    assert worker.preflight(job) == "moeda não elegível: CHF"


def test_application_worker_prepares_and_never_submits() -> None:
    job = _job("BairesDev", "Senior .NET Developer", status=JobStatus.QUALIFIED)
    repo = StubRepo([job], [_trusted()])
    agent = RecordingApplyAgent()
    bus = EventBus()
    events: list[str] = []
    bus.subscribe("*", lambda event: events.append(event.type))
    worker = ApplicationWorker(repo, _settings(), agent, bus)

    counters = worker.run()

    assert counters["prepared"] == 1
    assert counters["errors"] == 0
    assert agent.prepared == [job]
    assert "application.prepared" in events
    assert job.status == JobStatus.QUALIFIED  # o prepare é quem muda para PENDING_APPROVAL


def test_application_prioritizes_known_portal() -> None:
    settings = _settings()
    patterns = SitePatternStore(settings.resolve("data/site_patterns.json"))
    known = _job("BairesDev", "Senior .NET Developer", status=JobStatus.QUALIFIED)
    unknown = _job("Acme", "Senior .NET Developer", status=JobStatus.QUALIFIED)

    ordenadas = sort_for_application(
        [unknown, known], settings, patterns, set(), {RepositoryNormalize(c.name): c for c in [_trusted()]}
    )

    assert [job.id for job in ordenadas] == [known.id, unknown.id]
    portal_known = portal_knowledge(known, _trusted(), settings, patterns)
    assert portal_known["known_portal"] is True
    assert portal_known["route"] == "known_portal"


def test_application_consumes_and_acks_outbox_event() -> None:
    job = _job("BairesDev", "Senior .NET Developer", status=JobStatus.QUALIFIED)
    repo = StubRepo([job], [_trusted()])
    repo.publish_agent_event("application", "job.qualified", job.id, {"score": 100})
    agent = RecordingApplyAgent()
    worker = ApplicationWorker(repo, _settings(), agent)

    counters = worker.run()

    event_id = repo.agent_event_id("application", "job.qualified", job.id)
    assert counters["events"] == 1
    assert counters["prepared"] == 1
    assert agent.prepared == [job]
    assert repo.events[event_id]["status"] == "ACK"


def test_outbox_event_for_job_out_of_queue_is_dropped() -> None:
    repo = StubRepo([])
    repo.publish_agent_event("application", "job.qualified", "vaga-antiga")
    worker = ApplicationWorker(repo, _settings(), RecordingApplyAgent())

    counters = worker.run()

    event_id = repo.agent_event_id("application", "job.qualified", "vaga-antiga")
    assert counters["events"] == 1
    assert counters["total"] == 0
    assert repo.events[event_id]["status"] == "ACK"
