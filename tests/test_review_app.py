"""Pastas da PWA: ordem, contadores e precedência de erros de automação."""
from __future__ import annotations

import pytest

import src.web.review_app as review
from src.core.state import JobStatus
from src.models.domain import ApprovalRequest, JobOpportunity


class StubRepo:
    def __init__(self, jobs, approvals=(), errors=()) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.approvals = list(approvals)
        self.errors = list(errors)

    def list_jobs(self, limit: int = 5000):
        return list(self.jobs.values())[:limit]

    def list_approvals(self):
        return list(self.approvals)

    def failed_agent_events(self, consumers=None, job_id=None):
        return [
            event
            for event in self.errors
            if (not consumers or event["consumer"] in consumers)
            and (not job_id or event["job_id"] == job_id)
        ]

    def get_job(self, job_id: str):
        return self.jobs.get(job_id)

    def get_approval_for_job(self, job_id: str):
        return next(
            (item for item in self.approvals if item.opportunity_id == job_id), None
        )

    def get_review_decision_for_job(self, job_id: str):
        return None


def _job(job_id: str, status: JobStatus, *, has_applied: bool = False) -> JobOpportunity:
    return JobOpportunity(
        id=job_id,
        company="Acme",
        title=f"Vaga {job_id}",
        location="Remote",
        description="100% remote .NET role",
        status=status,
        has_applied=has_applied,
    )


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review, "_settings", lambda: review.Settings.load())


def test_bucket_order_counts_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        _job("wait", JobStatus.PENDING_APPROVAL),
        _job("sent", JobStatus.SUBMITTED, has_applied=True),
        _job("old", JobStatus.EXPIRED),
        _job("bad", JobStatus.FAILED),
    ]
    approval = ApprovalRequest(
        opportunity_id="wait", company="Acme", title="Vaga wait", status="PENDING"
    )
    monkeypatch.setattr(
        review,
        "_repo",
        lambda: StubRepo(jobs, [approval], [{"job_id": "bad", "consumer": "apply_submit",
                                              "event_type": "review.approved",
                                              "error": "CAPTCHA solicitado"}]),
    )

    result = review.job_index()

    assert result["order"] == ["approval", "submitted", "rejected", "errors", "all"]
    assert result["bucket"] == "approval"
    assert result["counts"] == {
        "approval": 1, "submitted": 1, "rejected": 1, "errors": 1, "all": 4
    }
    assert [item["id"] for item in result["items"]] == ["wait"]
    assert result["labels"]["approval"] == "Aguardando Aprovação"
    assert result["labels"]["all"] == "Todas"


def test_failed_automation_wins_over_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("sent", JobStatus.SUBMITTED, has_applied=True)
    monkeypatch.setattr(
        review,
        "_repo",
        lambda: StubRepo(
            [job],
            errors=[{"job_id": "sent", "consumer": "application",
                     "event_type": "prepare", "error": "CDP indisponível"}],
        ),
    )

    result = review.job_index("errors")

    assert [item["id"] for item in result["items"]] == ["sent"]
    assert result["counts"]["errors"] == 1
    assert result["counts"]["submitted"] == 0


def test_detail_reports_automation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job("bad", JobStatus.APPLYING)
    monkeypatch.setattr(
        review,
        "_repo",
        lambda: StubRepo(
            [job],
            errors=[{"job_id": "bad", "consumer": "apply_submit",
                     "event_type": "review.approved", "error": "plataforma pediu MFA"}],
        ),
    )

    detail = review.job_detail("bad")

    assert detail is not None
    assert detail["bucket"] == "errors"
    assert detail["automation_error"] == "plataforma pediu MFA"
    assert detail["can_check"] is False
