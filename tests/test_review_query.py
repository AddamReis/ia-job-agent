"""Consultas paginadas da PWA resolvidas no banco (SQLite)."""
from __future__ import annotations

from datetime import datetime

from src.core.state import JobStatus
from src.models.domain import JobAnalysis, JobOpportunity, QualificationResult
from src.storage.database import JobRow, Repository


def _add_job(
    repo: Repository,
    job_id: str,
    status: JobStatus,
    *,
    created_at: str = "2026-11-23T10:00:00",
    title: str | None = None,
    description: str = "vaga remota",
    has_applied: bool = False,
    qualification: QualificationResult | None = None,
    analysis: JobAnalysis | None = None,
) -> JobOpportunity:
    job = JobOpportunity(
        id=job_id,
        company="Acme",
        title=title or f"Vaga {job_id}",
        location="Remote",
        description=description,
        status=status,
        has_applied=has_applied,
        qualification=qualification,
        analysis=analysis,
    )
    assert repo.upsert_job(job) is True
    repo.update_job(job)
    with repo.session() as session:
        row = session.get(JobRow, job.id)
        assert row is not None
        row.created_at = created_at
    return job


def test_query_review_jobs_text_order_and_pagination(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    for i in range(12):
        _add_job(
            repo,
            f"vaga-{i}",
            JobStatus.QUALIFIED,
            created_at=f"2026-11-23T10:{i:02d}:00",
            title=f"Developer {i}" if i % 2 == 0 else f"Analyzer {i}",
        )

    newer = repo.query_review_jobs(query="developer", sort="newer", page=1, limit=5)
    assert [job.id for job in newer["items"]] == [
        "vaga-10", "vaga-8", "vaga-6", "vaga-4", "vaga-2"
    ]
    assert newer["total"] == 6
    assert newer["pages"] == 2
    assert newer["counts"] == {
        "approval": 6, "pending": 0, "submitted": 0, "rejected": 0, "errors": 0, "all": 6
    }

    page2 = repo.query_review_jobs(query="developer", sort="newer", page=2, limit=5)
    assert [job.id for job in page2["items"]] == ["vaga-0"]

    older = repo.query_review_jobs(sort="older", page=1, limit=5)
    assert [job.id for job in older["items"]] == [
        "vaga-0", "vaga-1", "vaga-2", "vaga-3", "vaga-4"
    ]
    assert older["total"] == 12

    # Texto digitado com curinga de LIKE deve ser tratado literalmente.
    assert repo.query_review_jobs(query="%")["total"] == 0


def test_query_review_jobs_has_own_pending_classification_bucket(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    _add_job(repo, "descoberta", JobStatus.DISCOVERED)
    _add_job(repo, "analisada", JobStatus.ANALYZED)
    _add_job(repo, "pronta", JobStatus.QUALIFIED)

    result = repo.query_review_jobs()

    assert [job.id for job in result["items"]] == ["pronta"]
    assert result["counts"] == {
        "approval": 1, "pending": 2, "submitted": 0, "rejected": 0, "errors": 0, "all": 3
    }
    assert [job.id for job in repo.query_review_jobs(bucket="pending")["items"]] == [
        "analisada", "descoberta"
    ]
    assert [job.id for job in repo.query_review_jobs(bucket="all")["items"]] == [
        "analisada", "descoberta", "pronta"
    ]


def test_query_review_jobs_buckets_search_in_analysis_and_error_precedence(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    _add_job(repo, "wait", JobStatus.QUALIFIED, created_at="2026-11-23T10:00:00")
    _add_job(
        repo, "sent", JobStatus.SUBMITTED, created_at="2026-11-23T10:01:00",
        has_applied=True,
    )
    _add_job(repo, "old", JobStatus.EXPIRED, created_at="2026-11-23T10:02:00")
    _add_job(
        repo, "skip", JobStatus.QUALIFIED, created_at="2026-11-23T10:03:00",
        qualification=QualificationResult(decision="SKIP", score=20),
    )
    _add_job(repo, "bad", JobStatus.FAILED, created_at="2026-11-23T10:04:00")
    _add_job(
        repo, "analise", JobStatus.PENDING_APPROVAL, created_at="2026-11-23T10:05:00",
        analysis=JobAnalysis(summary="Resumo com Kubernetes e Jenkins"),
    )

    repo.publish_agent_event("apply_submit", "review.approved", "sent")
    repo.ack_agent_event(
        "apply_submit|review.approved|sent", status="FAILED", error="CAPTCHA solicitado"
    )

    result = repo.query_review_jobs()
    assert [job.id for job in result["items"]] == ["analise", "wait"]
    assert result["counts"] == {
        "approval": 2, "pending": 0, "submitted": 0, "rejected": 2, "errors": 2, "all": 6
    }

    assert [job.id for job in repo.query_review_jobs(bucket="errors")["items"]] == [
        "bad", "sent"
    ]
    assert [job.id for job in repo.query_review_jobs(bucket="rejected")["items"]] == [
        "skip", "old"
    ]
    assert [job.id for job in repo.query_review_jobs(bucket="submitted")["items"]] == []

    found = repo.query_review_jobs(query="kubernetes")
    assert [job.id for job in found["items"]] == ["analise"]
    assert found["counts"]["approval"] == 1
    assert found["counts"]["all"] == 1
