"""Entrypoint isolado do discovery: janela persistida e watermark por slot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.agents.discovery import DiscoveryAgent
from src.core.config import Settings
from src.core.state import ExecutionStatus
from src.storage.database import Repository

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone(timedelta(hours=-3)))


def _settings(tmp_path) -> Settings:
    return Settings().model_copy(update={"root": tmp_path})


def test_run_uses_frozen_reference_and_records_watermark(tmp_path, monkeypatch) -> None:
    repo = Repository(tmp_path / "agent.db")
    agent = DiscoveryAgent(_settings(tmp_path), repo=repo, browser=object())
    received: dict = {}

    def fake_run(**kwargs) -> int:
        received.update(kwargs)
        return 3

    monkeypatch.setattr(agent.research, "run", fake_run)

    result = agent.run("first", trace_id="trace-1", now=NOW)

    assert result.jobs_new == 3
    assert received["reference_time"] == NOW
    assert received["cutoff"] == result.cutoff
    assert received["time_filter"] == "day"

    watermark = repo.get_discovery_watermark("10:00")
    assert watermark is not None
    assert watermark["status"] == ExecutionStatus.COMPLETED.value
    assert watermark["jobs_new"] == 3
    assert watermark["trace_id"] == "trace-1"

    execution = repo.list_executions(limit=1)[0]
    assert execution.phase == "discovery"
    assert execution.trace_id == "trace-1"


def test_dry_run_does_not_call_research(tmp_path, monkeypatch) -> None:
    repo = Repository(tmp_path / "agent.db")
    agent = DiscoveryAgent(_settings(tmp_path), repo=repo, browser=object())

    def fail(**kwargs) -> int:
        raise AssertionError("dry-run não deve buscar")

    monkeypatch.setattr(agent.research, "run", fail)

    result = agent.run("regular", now=NOW, dry_run=True)

    assert result.jobs_new == 0
    assert result.status == ExecutionStatus.COMPLETED.value
