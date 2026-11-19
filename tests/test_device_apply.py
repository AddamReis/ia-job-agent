"""Fila Easy Apply que aguarda o celular e check manual de dependências."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.agents.dependencies import DependencyAgent
from src.agents.device_apply import DeviceApplicationAgent
from src.core.config import Settings
from src.core.state import can_transition, JobStatus
from src.models.domain import (
    ApplicationDependency,
    ApprovalRequest,
    CandidateProfile,
    JobOpportunity,
)
from src.storage.database import Repository


class StubGate:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls: list[ApprovalRequest] = []

    def request(self, approval: ApprovalRequest) -> bool:
        self.calls.append(approval)
        return self.approved


class StubADB:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.opened: list[str] = []

    def has_device(self) -> bool:
        return self.connected

    def require_device(self):
        return None

    def unlock_if_needed(self) -> bool:
        return False

    def open_url(self, url: str, *, label: str = "") -> None:
        self.opened.append(url)

    def push_file(self, local_path: str, remote_name: str) -> str:
        return f"/sdcard/Download/{remote_name}"


class StubDevice:
    def page_source(self) -> str:
        return "<hierarchy/>"

    def click_text(self, text: str) -> bool:
        return False

    def detect_block(self) -> None:
        return None

    def fill_visible_inputs(self, values: list[str]) -> int:
        return len(values)


class StubResolver:
    def resolve(self, field):
        class Decision:
            needs_human = False
            value = "answer"

        return Decision()


def _settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    return base.model_copy(
        update={
            "storage": base.storage.model_copy(
                update={"database": str(tmp_path / "agent.db")}
            )
        }
    )


def _job(**overrides) -> JobOpportunity:
    data = {
        "company": "Acme",
        "title": ".NET Engineer",
        "external_id": "4123456789",
        "url": "https://www.linkedin.com/jobs/view/4123456789",
        "trace_id": "trace-device-01",
        "status": JobStatus.QUALIFIED,
        "channel": "easy_apply",
        "easy_apply": True,
        "easy_apply_source": "public_html",
        "region": "americas_north",
        "posted_at": datetime.now() - timedelta(hours=1),
        "posted_precision": "hour",
        "score": 88,
    }
    data.update(overrides)
    return JobOpportunity(**data)


def _agent(tmp_path: Path, *, approved: bool = True, connected: bool = True):
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    gate = StubGate(approved)
    adb = StubADB(connected)
    agent = DeviceApplicationAgent(
        StubDevice(), adb, CandidateProfile(), StubResolver(), gate,
        repo, settings,
    )
    return agent, repo, gate, adb


def test_waiting_device_state_allows_apply_and_blocks_stale_analysis() -> None:
    assert can_transition(JobStatus.QUALIFIED, JobStatus.WAITING_DEVICE)
    assert can_transition(JobStatus.PENDING_APPROVAL, JobStatus.WAITING_DEVICE)
    assert can_transition(JobStatus.WAITING_DEVICE, JobStatus.APPLYING)
    assert can_transition(JobStatus.WAITING_DEVICE, JobStatus.FAILED)
    assert not can_transition(JobStatus.WAITING_DEVICE, JobStatus.QUALIFIED)
    assert not can_transition(JobStatus.SUBMITTED, JobStatus.WAITING_DEVICE)


def test_job_url_uses_real_linkedin_id() -> None:
    url = DeviceApplicationAgent.job_url(
        _job(external_id="9876543210", url="linkedin://job/synthetic")
    )
    assert url == "https://www.linkedin.com/jobs/view/9876543210"
    assert "synthetic" not in url


def test_queue_persists_approval_and_waits_for_device(tmp_path: Path) -> None:
    agent, repo, gate, _ = _agent(tmp_path)
    job = _job()
    repo.upsert_job(job)

    assert agent.queue(job) is True
    assert job.status == JobStatus.WAITING_DEVICE
    saved = repo.get_job(job.id)
    assert saved is not None and saved.status == JobStatus.WAITING_DEVICE
    approval = repo.get_approval_for_job(job.id)
    assert approval is not None and approval.status == "APPROVED"
    assert gate.calls and gate.calls[0].opportunity_id == job.id
    assert [item.id for item in agent.pending_jobs()] == [job.id]


def test_queue_rejected_approval_does_not_enter_queue(tmp_path: Path) -> None:
    agent, repo, _, _ = _agent(tmp_path, approved=False)
    job = _job()
    repo.upsert_job(job)
    assert agent.queue(job) is False
    assert job.status == JobStatus.REJECTED_BY_FILTER
    assert agent.pending_jobs() == []


def test_process_queue_skips_when_phone_is_disconnected(tmp_path: Path) -> None:
    agent, repo, _, adb = _agent(tmp_path, connected=False)
    job = _job()
    repo.upsert_job(job)
    assert agent.queue(job) is True

    submitted, pending = agent.process_queue()
    assert submitted == 0
    assert pending == 1
    assert adb.opened == []


def test_expiration_keeps_waiting_device_jobs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    job = _job(status=JobStatus.WAITING_DEVICE, posted_at=datetime.now() - timedelta(days=3))
    repo.upsert_job(job)
    stale = repo.expire_stale_jobs(datetime.now())
    assert job.id not in stale
    assert repo.get_job(job.id).status == JobStatus.WAITING_DEVICE


def test_dependency_check_marks_done(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    job = _job()
    repo.upsert_job(job)
    dep = ApplicationDependency(
        trace_id=job.trace_id,
        opportunity_id=job.id,
        company=job.company,
        title=job.title,
        kind="video_self_intro",
        owner="human",
        description="Gravar vídeo de 15s",
    )
    repo.add_dependency(dep)
    agent = DependencyAgent(repo, settings)

    done = agent.complete(dep.id, note="vídeo enviado no app")
    assert done is not None
    assert done.status == "done"
    assert done.completed_at is not None
    assert repo.list_dependencies(opportunity_id=job.id, status="done")
    assert repo.list_dependencies(opportunity_id=job.id, status="pending") == []

    by_kind = agent.complete(opportunity_id=job.id, kind="video_self_intro")
    assert by_kind is not None and by_kind.status == "done"  # idempotente
