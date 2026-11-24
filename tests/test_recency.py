"""Filtro de janela de recência e ordenação por publicação mais recente."""
from datetime import datetime, timedelta, timezone

from src.agents.qualification import stale_reason
from src.agents.research import _is_recent, _newest_first
from src.core.config import Settings
from src.execution.android.accessibility import parse_posted
from src.models.domain import JobOpportunity


def _job(minutes_ago: int) -> JobOpportunity:
    return JobOpportunity(
        title="Engineer",
        external_id=f"job-{minutes_ago}",
        posted_at=datetime.now() - timedelta(minutes=minutes_ago),
        posted_precision="hour",
    )


def test_is_recent_respects_six_hour_cutoff_and_explicit_recency():
    cutoff = datetime.now() - timedelta(hours=6)
    assert _is_recent(_job(60), cutoff, require_explicit=True)
    assert not _is_recent(_job(480), cutoff, require_explicit=True)

    unknown = JobOpportunity(title="Engineer", posted_at=None)
    assert not _is_recent(unknown, cutoff, require_explicit=True)
    assert _is_recent(unknown, cutoff, require_explicit=False)


def test_newest_first_orders_by_posted_at_desc():
    jobs = [_job(300), _job(10), _job(120)]
    ordered = _newest_first(jobs)
    assert [job.posted_at for job in ordered] == sorted(
        (job.posted_at for job in jobs), reverse=True
    )


def test_parse_posted_distinguishes_hours_from_days():
    posted, precision = parse_posted("Posted 5 hours ago")
    assert precision == "hour"
    assert posted is not None and posted >= datetime.now() - timedelta(hours=6)

    posted, precision = parse_posted("Posted 2 days ago")
    assert precision == "day"
    assert posted is not None and posted < datetime.now() - timedelta(hours=6)


def test_stale_reason_accepts_timezone_aware_posted_at_from_postgres() -> None:
    settings = Settings.load()
    recente = JobOpportunity(
        title="Engineer",
        posted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        posted_precision="hour",
    )
    antiga = JobOpportunity(
        title="Engineer",
        posted_at=datetime.now(timezone.utc) - timedelta(days=5),
        posted_precision="day",
    )

    assert stale_reason(recente, settings) is None
    assert "janela" in (stale_reason(antiga, settings) or "")
