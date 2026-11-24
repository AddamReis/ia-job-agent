"""Retenção de 30 dias das evidências em disco, com limpeza lazy na PWA."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import src.web.review_app as review
from src.core.config import Settings
from src.models.domain import Application, ApplicationEvidence, JobOpportunity
from src.storage.evidence_store import cleanup_expired, is_expired, retention_cutoff


def _utime(path: Path, when: datetime) -> None:
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def test_cleanup_expired_removes_old_images_and_empty_albums(tmp_path: Path) -> None:
    agora = datetime.now()
    base = tmp_path / "data/evidence"
    antigo = base / "job-1"
    antigo.mkdir(parents=True)
    velha = antigo / "step_01.webp"
    velha.write_bytes(b"antiga")
    _utime(velha, agora - timedelta(days=31))
    # Arquivo que não é imagem nunca é apagado pela varredura.
    nota = antigo / "nota.txt"
    nota.write_bytes(b"manter")

    vazio = base / "job-2"
    vazio.mkdir()
    sozinha = vazio / "confirmation.jpg"
    sozinha.write_bytes(b"antiga")
    _utime(sozinha, agora - timedelta(days=45))

    recente = base / "job-3"
    recente.mkdir()
    nova = recente / "step_01.webp"
    nova.write_bytes(b"nova")
    _utime(nova, agora - timedelta(hours=2))

    resultado = cleanup_expired(base, retention_days=30, now=agora)

    assert resultado == {"files_removed": 2, "bytes_removed": 12, "dirs_removed": 1}
    assert not velha.exists()
    assert nota.exists() and antigo.is_dir()
    assert not sozinha.exists() and not vazio.exists()
    assert nova.exists()


def test_cleanup_expired_can_be_scoped_to_one_job(tmp_path: Path) -> None:
    agora = datetime.now()
    base = tmp_path / "data/evidence"
    for job_id in ("job-1", "job-2"):
        album = base / job_id
        album.mkdir(parents=True)
        arquivo = album / "step_01.webp"
        arquivo.write_bytes(b"x")
        _utime(arquivo, agora - timedelta(days=60))

    resultado = cleanup_expired(base, retention_days=30, now=agora, job_id="job-1")

    assert resultado["files_removed"] == 1
    assert not (base / "job-1" / "step_01.webp").exists()
    assert (base / "job-2" / "step_01.webp").exists()


def test_retention_zero_disables_cleanup(tmp_path: Path) -> None:
    album = tmp_path / "data/evidence/job-1"
    album.mkdir(parents=True)
    arquivo = album / "step_01.webp"
    arquivo.write_bytes(b"antiga")
    _utime(arquivo, datetime.now() - timedelta(days=365))

    assert cleanup_expired(album.parent, retention_days=0)["files_removed"] == 0
    assert arquivo.exists()


def test_expired_metadata_is_hidden_but_legacy_without_date_is_kept() -> None:
    agora = datetime.now()
    cutoff = retention_cutoff(30, agora)
    antiga = ApplicationEvidence(
        step=1, path="data/evidence/job-1/a.webp", created_at=agora - timedelta(days=31)
    )
    nova = ApplicationEvidence(
        step=1, path="data/evidence/job-1/b.webp", created_at=agora - timedelta(days=2)
    )
    legada = {"path": "data/evidence/job-1/c.png"}

    assert is_expired(antiga, cutoff=cutoff, retention_days=30) is True
    assert is_expired(nova, cutoff=cutoff, retention_days=30) is False
    assert is_expired(legada, cutoff=cutoff, retention_days=30) is False
    assert is_expired(antiga, cutoff=cutoff, retention_days=0) is False


def test_job_detail_hides_expired_evidence_and_removes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agora = datetime.now()
    job = JobOpportunity(id="job-1", company="Acme", title="Dev")
    antiga = ApplicationEvidence(
        step=1,
        path="data/evidence/job-1/antiga.webp",
        created_at=agora - timedelta(days=31),
    )
    nova = ApplicationEvidence(
        step=2,
        path="data/evidence/job-1/nova.webp",
        created_at=agora - timedelta(days=1),
    )
    application = Application(opportunity_id=job.id, evidence=[antiga, nova])
    arquivo_antigo = tmp_path / antiga.path
    arquivo_antigo.parent.mkdir(parents=True)
    arquivo_antigo.write_bytes(b"antiga")
    _utime(arquivo_antigo, antiga.created_at)

    class Repo:
        def get_job(self, job_id: str):
            return job if job_id == job.id else None

        def get_approval_for_job(self, _job_id: str):
            return None

        def get_review_decision_for_job(self, _job_id: str):
            return None

        def failed_agent_events(self, *_args, **_kwargs):
            return []

        def get_application_for_job(self, _job_id: str):
            return application

    monkeypatch.setattr(review, "_repo", lambda: Repo())
    monkeypatch.setattr(review, "_settings", lambda: Settings(root=tmp_path))

    detail = review.job_detail("job-1")

    assert detail is not None
    assert [item["path"] for item in detail["evidence"]] == [nova.path]
    assert not arquivo_antigo.exists()


def test_daily_sweep_removes_expired_evidence_without_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agora = datetime.now()
    album = tmp_path / "data/evidence/job-1"
    album.mkdir(parents=True)
    antiga = album / "step_01.webp"
    antiga.write_bytes(b"antiga")
    _utime(antiga, agora - timedelta(days=31))

    class Stop:
        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls >= 2

    stop = Stop()
    review.daily_evidence_cleanup(
        Settings(root=tmp_path), interval_seconds=86400, sleep=lambda _s: None, stop=stop
    )

    assert not antiga.exists()
    assert not album.exists()


def test_config_exposes_retention_and_cdp_standby() -> None:
    settings = Settings()
    assert settings.application.evidence_retention_days == 30
    assert settings.application.evidence_format == "webp"
    assert settings.application.evidence_quality == 82
    assert settings.application.cdp_wait_seconds == 1800
    assert settings.application.cdp_poll_seconds == 5.0
