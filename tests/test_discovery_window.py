"""Janela congelada, watermark e sobreposição do discovery-agent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.agents.research import _is_recent
from src.core.config import Settings
from src.models.domain import JobOpportunity
from src.shared.discovery_window import compute_window, normalize_slot

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=TZ)


def test_first_slot_uses_watermark_when_it_is_older() -> None:
    settings = Settings()
    last = datetime(2026, 11, 17, 18, 30, tzinfo=TZ)

    window = compute_window(settings, "first", now=NOW, last_started=last)

    assert window.slot == "10:00"
    assert window.source == "watermark"
    # 18:30 do dia anterior - 1h de sobreposição = 17:30.
    assert window.cutoff == datetime(2026, 11, 17, 17, 30, tzinfo=TZ)
    assert 16 < window.span_hours < 17
    assert window.time_filter == "day"


def test_regular_slot_uses_window_plus_buffer() -> None:
    settings = Settings()
    now = datetime(2026, 11, 18, 12, 30, tzinfo=TZ)

    window = compute_window(settings, "regular", now=now, last_started=None)

    assert window.slot == "12:30"
    assert window.source == "window"
    assert window.cutoff == datetime(2026, 11, 18, 8, 30, tzinfo=TZ)
    assert window.span_hours == 4


def test_watermark_does_not_shrink_the_configured_window() -> None:
    settings = Settings()
    recent = datetime(2026, 11, 18, 9, 55, tzinfo=TZ)

    window = compute_window(settings, "10:00", now=NOW, last_started=recent)

    assert window.source == "window"
    assert window.cutoff == datetime(2026, 11, 17, 21, 0, tzinfo=TZ)


def test_long_gap_widens_linkedin_filter_to_week() -> None:
    settings = Settings()
    last = NOW - timedelta(days=3)

    window = compute_window(settings, "first", now=NOW, last_started=last)

    assert window.time_filter == "week"
    assert window.span_hours <= 168
    assert window.cutoff >= NOW - timedelta(hours=168)


def test_reference_is_frozen_for_the_whole_execution() -> None:
    settings = Settings()
    first = compute_window(settings, "regular", now=NOW, last_started=None)
    second = compute_window(settings, "regular", now=NOW, last_started=None)

    assert first.reference_time == second.reference_time
    assert first.cutoff == second.cutoff


def test_recent_accepts_naive_posted_with_aware_cutoff() -> None:
    job = JobOpportunity(
        title="Dev",
        company="X",
        external_id="1",
        posted_at=NOW.replace(tzinfo=None) - timedelta(hours=1),
    )
    cutoff = NOW - timedelta(hours=3)

    assert _is_recent(job, cutoff, require_explicit=True) is True


def test_auto_slot() -> None:
    cfg = Settings().discovery_schedule
    assert normalize_slot(cfg, "auto", NOW) == "10:00"
    assert normalize_slot(cfg, "auto", datetime(2026, 11, 18, 15, 0, tzinfo=TZ)) == "15:00"
