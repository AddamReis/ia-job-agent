"""Pastas, busca, ordenação e paginação da PWA."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

import src.web.review_app as review
from src.core.state import JobStatus
from src.models.domain import ApprovalRequest, JobOpportunity
from src.models.schemas import JobFitAssessment, RequirementVerdict


class StubRepo:
    """ Espelho em memória da consulta paginada, para testar a camada HTTP. """

    def __init__(self, jobs, approvals=(), errors=()) -> None:
        self.jobs = {job.id: job for job in jobs}
        self.approvals = list(approvals)
        self.errors = list(errors)

    def _bucket(self, job):
        failed = [
            event for event in self.failed_agent_events(review.AUTOMATION_CONSUMERS)
            if event["job_id"] == job.id
        ]
        return review._bucket(
            job,
            70,
            automation_error=failed[0]["error"] if failed else "",
        )

    def query_review_jobs(
        self, *, bucket="approval", query="", sort="newer", page=1, limit=10,
        automation_consumers=(),
    ):
        term = " ".join((query or "").split()).lower()
        jobs = list(self.jobs.values())
        if term:
            jobs = [
                job for job in jobs
                if term in json.dumps(job.model_dump(mode="json"), ensure_ascii=False).lower()
            ]
        counts = {key: 0 for key in review.BUCKET_KEYS}
        for job in jobs:
            counts[self._bucket(job)] += 1
        counts["all"] = len(jobs)
        if bucket not in review.BUCKET_KEYS:
            bucket = "approval"
        jobs = [job for job in jobs if self._bucket(job) == bucket]
        jobs.sort(key=lambda job: job.created_at, reverse=sort != "older")
        total = len(jobs)
        pages = (total + limit - 1) // limit
        page = max(1, page)
        return {
            "items": jobs[(page - 1) * limit:page * limit],
            "counts": counts,
            "bucket": bucket,
            "query": term,
            "sort": "older" if sort == "older" else "newer",
            "page": page,
            "limit": limit,
            "total": total,
            "pages": pages,
        }

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


def _job(
    job_id: str,
    status: JobStatus,
    *,
    has_applied: bool = False,
    created_at: str = "2026-11-23T10:00:00",
    title: str | None = None,
    description: str = "100% remote .NET role",
) -> JobOpportunity:
    return JobOpportunity(
        id=job_id,
        company="Acme",
        title=title or f"Vaga {job_id}",
        location="Remote",
        description=description,
        status=status,
        has_applied=has_applied,
        created_at=datetime.fromisoformat(created_at),
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

    assert result["order"] == ["approval", "pending", "submitted", "rejected", "errors", "all"]
    assert result["bucket"] == "approval"
    assert result["counts"] == {
        "approval": 1, "pending": 0, "submitted": 1, "rejected": 1, "errors": 1, "all": 4
    }
    assert [item["id"] for item in result["items"]] == ["wait"]
    assert result["labels"]["approval"] == "Aguardando Aprovação"
    assert result["labels"]["all"] == "Todas"


def test_unclassified_jobs_have_own_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        _job("discovered", JobStatus.DISCOVERED),
        _job("analyzed", JobStatus.ANALYZED),
        _job("ready", JobStatus.QUALIFIED),
    ]
    monkeypatch.setattr(review, "_repo", lambda: StubRepo(jobs))

    result = review.job_index()

    assert result["counts"] == {
        "approval": 1, "pending": 2, "submitted": 0, "rejected": 0, "errors": 0, "all": 3
    }
    assert [item["id"] for item in result["items"]] == ["ready"]
    assert result["order"] == ["approval", "pending", "submitted", "rejected", "errors", "all"]


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


def test_pagination_sort_and_text_query(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [
        _job(f"vaga-{i}", JobStatus.QUALIFIED,
             created_at=f"2026-11-23T10:{i:02d}:00",
             title=f"Analyzer {i}" if i % 2 else f"Developer {i}")
        for i in range(12)
    ]
    monkeypatch.setattr(review, "_repo", lambda: StubRepo(jobs))

    first = review.job_index("approval", query="developer", sort="newer", page=1)
    second = review.job_index("approval", query="developer", sort="newer", page=2)

    assert first["limit"] == 10
    assert first["total"] == 6
    assert first["pages"] == 1
    assert [item["id"] for item in first["items"]] == [
        "vaga-10", "vaga-8", "vaga-6", "vaga-4", "vaga-2", "vaga-0"
    ]
    assert second["items"] == []
    assert second["has_prev"] is True

    paged = review.job_index("approval", sort="older", page=2, limit=5)
    assert [item["id"] for item in paged["items"]] == [
        "vaga-5", "vaga-6", "vaga-7", "vaga-8", "vaga-9"
    ]
    assert paged["has_next"] is True
    assert paged["counts"]["all"] == 12


def test_missing_core_badge_only_for_real_core_technologies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job("ref", JobStatus.REJECTED_BY_FILTER)
    job.fit_assessment = JobFitAssessment(
        core_language="Java",
        language_fit="misaligned",
        requirement_verdicts=[
            RequirementVerdict(requirement="Java 17 e Spring Boot", verdict="core_missing"),
            RequirementVerdict(requirement="Next.js e React", verdict="core_missing"),
            RequirementVerdict(requirement="Docker e Jenkins", verdict="platform_or_tool"),
            RequirementVerdict(requirement="5 anos de experiência", verdict="core_missing"),
        ],
    )

    # Plataformas e requisitos não tecnológicos não viram badge.
    assert review.missing_core_technologies(job) == ["Next.js", "Java", "React"]

    monkeypatch.setattr(review, "_repo", lambda: StubRepo([job]))
    item = review.job_index("rejected")["items"][0]
    assert item["missing_core"] == ["Next.js", "Java", "React"]


def test_service_worker_is_network_first_and_versioned() -> None:
    """Cache antigo não pode prender o navegador em uma versão anterior do filtro."""
    source = (review.STATIC_DIR / "sw.js").read_text(encoding="utf-8")

    assert "const CACHE = 'review-pwa-v5'" in source
    fetch_at = source.index("fetch(event.request)")
    cached_at = source.index("caches.match(event.request)")
    assert fetch_at < cached_at
    assert "pathname.startsWith('/api/')" in source
