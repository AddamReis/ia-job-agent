"""Loop de correção: revisão humana reclassifica e registra antes/depois."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.agents.orchestrator import Orchestrator
from src.agents.qualification_memory import QualificationMemory
from src.core.state import JobStatus
from src.models.domain import JobOpportunity, QualificationResult
from src.storage.database import Repository


def _repo(tmp_path):
    return Repository(tmp_path / "agent.db")


def _job(**overrides) -> JobOpportunity:
    data = {
        "title": ".NET Engineer",
        "company": "Acme",
        "location": "Brazil (Remote)",
        "external_id": "4491234567",
        "status": JobStatus.QUALIFIED,
        "posted_at": datetime.now() - timedelta(hours=2),
        "score": 82,
        "qualification": QualificationResult(score=82, decision="APPLY"),
    }
    data.update(overrides)
    return JobOpportunity(**data)


def _stub(repo, monkeypatch, result: int = 1) -> Orchestrator:
    """Orquestrador sem iniciar DGX/ADB: só o método de validação é exercitado."""
    orchestrator = object.__new__(Orchestrator)
    orchestrator.repo = repo
    orchestrator.qualification_memory = QualificationMemory(repo)
    monkeypatch.setattr(orchestrator, "reclassify_jobs", lambda **kwargs: result)
    return orchestrator


def test_wrong_validation_reclassifies_and_records_diff(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    orchestrator = _stub(repo, monkeypatch)

    resultado = orchestrator.validate_classification(
        job.id, correct=False, reason="Java é core; deveria ser SKIP"
    )

    assert resultado["reclassified"] is True
    assert resultado["feedback"] >= 1
    atual = repo.get_job(job.id)
    assert atual is not None
    assert atual.validation is not None
    assert atual.validation.correct is False
    assert atual.validation.before_status == "QUALIFIED"
    assert atual.validation.before_decision == "APPLY"
    assert atual.validation.before_score == 82
    assert atual.validation.reclassified is True
    assert "Java" in atual.validation.reason
    assert "validação humana/negativo" in QualificationMemory(repo).context_for(job)


def test_correct_validation_does_not_reclassify(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    chamados: list[dict] = []
    orchestrator = object.__new__(Orchestrator)
    orchestrator.repo = repo
    orchestrator.qualification_memory = QualificationMemory(repo)
    orchestrator.reclassify_jobs = lambda **kwargs: chamados.append(kwargs) or 1  # type: ignore[method-assign]

    resultado = orchestrator.validate_classification(
        job.id, correct=True, reason="conferido na PWA"
    )

    assert resultado["reclassified"] is False
    assert chamados == []
    atual = repo.get_job(job.id)
    assert atual is not None
    assert atual.validation is not None
    assert atual.validation.correct is True
    assert atual.validation.before_status == atual.validation.after_status
    assert atual.validation.before_decision == atual.validation.after_decision


def test_validation_survives_reload_without_column_change(tmp_path, monkeypatch) -> None:
    """O campo vive no JSON da vaga; não precisa de ALTER TABLE."""
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    orchestrator = _stub(repo, monkeypatch)

    orchestrator.validate_classification(job.id, correct=False, reason="erro de stack")

    atual = repo.get_job(job.id)
    assert atual is not None
    assert atual.validation is not None
    assert atual.validation.reason == "erro de stack"


def test_unknown_job_raises_value_error(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    orchestrator = _stub(repo, monkeypatch)
    try:
        orchestrator.validate_classification("inexistente", correct=False)
    except ValueError as exc:
        assert "Vaga não encontrada" in str(exc)
    else:
        raise AssertionError("esperava ValueError")
