"""Banco configurável por URL (Postgres no DGX) e cópia direta de dados."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from scripts.migrate_data import copy_database
from src.models.domain import JobOpportunity
from src.storage.database import Repository


def test_repository_honors_environment_url(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target.db"
    monkeypatch.setenv("AI_JOB_DATABASE_URL", f"sqlite:///{target}")

    repo = Repository(tmp_path / "ignored.db")

    assert Path(repo.engine.url.database) == target
    assert not (tmp_path / "ignored.db").exists()


def test_copy_database_moves_all_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_JOB_DATABASE_URL", raising=False)
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    source = Repository(source_path)
    source.seed_known_companies()
    source.upsert_job(
        JobOpportunity(
            title="Software Engineer",
            company="BairesDev",
            external_id="copy-1",
            posted_at=datetime.now() - timedelta(hours=1),
        )
    )

    counts = copy_database(
        f"sqlite:///{source_path}", f"sqlite:///{target_path}"
    )

    assert counts["job_opportunities"] == 1
    target = Repository(target_path)
    assert target.stats()["jobs_total"] == 1
    assert [company.name for company in target.list_trusted_portals()] == [
        "BairesDev",
        "Strider",
    ]
