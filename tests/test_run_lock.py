"""Execução única do discovery: espera, heartbeat e reclaim de travessas órfãs."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from src.agents.discovery import DiscoveryAgent
from src.core.config import Settings
from src.core.state import ExecutionStatus
from src.execution.run_lock import RunLock, RunLockTimeout
from src.models.domain import ExecutionHistory
from src.storage.database import Repository, RunLockRow

NOW = datetime(2026, 11, 21, 18, 30, tzinfo=timezone(timedelta(hours=-3)))


def _settings(tmp_path) -> Settings:
    return Settings().model_copy(update={"root": tmp_path})


def test_lock_waits_until_previous_owner_releases(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    first = RunLock(
        repo,
        name="discovery",
        owner="first",
        stale_after_seconds=900,
        heartbeat_seconds=1,
        wait_seconds=30,
        sleep=lambda _: None,
    )
    releases: list[float] = []

    def release_on_wait(seconds: float) -> None:
        releases.append(seconds)
        repo.release_run_lock("discovery", "first")

    with first:
        with RunLock(
            repo,
            name="discovery",
            owner="second",
            stale_after_seconds=900,
            heartbeat_seconds=1,
            wait_seconds=30,
            sleep=release_on_wait,
        ) as acquisition:
            assert acquisition.owner == "second"

    assert acquisition.owner == "second"
    assert acquisition.reclaimed is False
    assert releases  # ficou em standby pelo menos um ciclo antes de entrar
    assert repo.get_run_lock("discovery") is None


def test_stale_lock_is_reclaimed_and_orphans_marked(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    old = datetime.now(timezone.utc) - timedelta(seconds=1200)
    with repo.Session() as session:
        with session.begin():
            session.add(
                RunLockRow(
                    name="discovery",
                    owner="dead-process",
                    acquired_at=old.isoformat(),
                    renewed_at=old.isoformat(),
                )
            )
    repo.create_execution(
        ExecutionHistory(
            id="orphan",
            trace_id="orphan",
            phase="discovery",
            started_at=old,
            status=ExecutionStatus.RUNNING.value,
        )
    )
    repo.save_discovery_watermark(
        "10:00", {"status": ExecutionStatus.RUNNING.value, "started_at": old.isoformat()}
    )

    lock = RunLock(
        repo,
        name="discovery",
        owner="new-process",
        stale_after_seconds=900,
        heartbeat_seconds=1,
        wait_seconds=5,
        sleep=lambda _: None,
    )
    with lock as acquisition:
        assert acquisition.reclaimed is True
        assert acquisition.previous_owner == "dead-process"
        assert repo.get_execution_by_trace("orphan") is not None
        assert (
            repo.get_execution_by_trace("orphan").status
            == ExecutionStatus.ABANDONED.value
        )
        assert (
            repo.get_discovery_watermark("10:00")["status"]
            == ExecutionStatus.ABANDONED.value
        )

    assert repo.get_run_lock("discovery") is None


def test_fresh_lock_is_not_stolen(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    with RunLock(
        repo,
        name="discovery",
        owner="alive",
        stale_after_seconds=900,
        heartbeat_seconds=1,
        wait_seconds=5,
        sleep=lambda _: None,
    ):
        intruder = RunLock(
            repo,
            name="discovery",
            owner="intruder",
            stale_after_seconds=900,
            heartbeat_seconds=1,
            wait_seconds=0.05,
            sleep=lambda _: None,
        )
        try:
            intruder.acquire()
        except RunLockTimeout:
            pass
        else:
            raise AssertionError("trava viva não pode ser roubada")
        assert repo.get_run_lock("discovery")["owner"] == "alive"


def test_heartbeat_keeps_lock_alive(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    with RunLock(
        repo,
        name="discovery",
        owner="beating",
        stale_after_seconds=900,
        heartbeat_seconds=0.02,
        wait_seconds=5,
        sleep=lambda _: None,
    ):
        before = repo.get_run_lock("discovery")["renewed_at"]
        time.sleep(0.15)
        after = repo.get_run_lock("discovery")["renewed_at"]
    assert after > before


def test_discovery_agent_releases_lock_and_allows_next_run(tmp_path, monkeypatch) -> None:
    repo = Repository(tmp_path / "agent.db")
    agent = DiscoveryAgent(_settings(tmp_path), repo=repo, browser=object())
    monkeypatch.setattr(agent.research, "run", lambda **_: 2)

    first = agent.run("first", trace_id="trace-1", now=NOW)
    assert first.jobs_new == 2
    assert repo.get_run_lock("discovery") is None

    second = agent.run("regular", trace_id="trace-2", now=NOW)
    assert second.jobs_new == 2
    assert repo.get_run_lock("discovery") is None


def test_discovery_agent_waits_for_active_external_lock(tmp_path, monkeypatch) -> None:
    repo = Repository(tmp_path / "agent.db")
    agent = DiscoveryAgent(_settings(tmp_path), repo=repo, browser=object())
    monkeypatch.setattr(agent.research, "run", lambda **_: 1)
    external = RunLock(
        repo,
        name="discovery",
        owner="external",
        stale_after_seconds=900,
        heartbeat_seconds=1,
        wait_seconds=10,
    )
    external.acquire()
    started = threading.Event()
    result: dict[str, object] = {}

    def target() -> None:
        started.set()
        result["value"] = agent.run("first", trace_id="queued", now=NOW)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    assert started.wait(2)
    time.sleep(0.2)
    assert "value" not in result  # ainda em standby
    repo.release_run_lock("discovery", "external")
    thread.join(timeout=5)
    assert "value" in result
    assert repo.get_run_lock("discovery") is None
