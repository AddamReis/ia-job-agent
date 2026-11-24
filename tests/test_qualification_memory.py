"""Memória de classificação: feedback auditável sem mudar regras duras."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.agents.approval import ApprovalGate
from src.agents.qualification_memory import QualificationMemory
from src.core.config import Settings
from src.core.state import JobStatus
from src.llm.prompts import fit_messages
from src.models.domain import ApprovalRequest, JobOpportunity
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
    }
    data.update(overrides)
    return JobOpportunity(**data)


def test_human_rejection_is_remembered_as_negative(tmp_path) -> None:
    repo = _repo(tmp_path)
    job = _job(restriction_details="A vaga exige Java como linguagem principal.")
    repo.upsert_job(job)
    memory = QualificationMemory(repo)
    approval = ApprovalRequest(opportunity_id=job.id, trace_id="trace-1", company="Acme")

    assert memory.record_approval(approval, False) >= 1
    contexto = memory.context_for(_job(title="Java Developer", company="Outra"))
    assert "decisão humana/negativo" in contexto
    assert "stack:java" in contexto


def test_submission_outcome_is_remembered_as_positive(tmp_path) -> None:
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    memory = QualificationMemory(repo)

    assert memory.record_outcome(job, "SUBMITTED") >= 1
    contexto = memory.context_for(_job())
    assert "desfecho da candidatura/positivo" in contexto
    assert "candidatura enviada com sucesso" in contexto


def test_failed_application_is_attention_not_rejection(tmp_path) -> None:
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    memory = QualificationMemory(repo)

    assert memory.record_outcome(job, "FAILED", "CAPTCHA no portal") >= 1
    contexto = memory.context_for(_job())
    assert "desfecho da candidatura/atenção" in contexto
    assert "CAPTCHA" in contexto


def test_reclassification_flip_is_learned(tmp_path) -> None:
    repo = _repo(tmp_path)
    before = _job(status=JobStatus.REJECTED_BY_FILTER)
    after = _job(status=JobStatus.QUALIFIED)
    repo.upsert_job(after)
    memory = QualificationMemory(repo)

    assert memory.record_reclassification(before, after) >= 1
    assert "reclassificação/positivo" in memory.context_for(after)


def test_reclassification_accepts_status_only_before(tmp_path) -> None:
    repo = _repo(tmp_path)
    after = _job(status=JobStatus.QUALIFIED)
    repo.upsert_job(after)
    memory = QualificationMemory(repo)

    assert memory.record_reclassification(JobStatus.REJECTED_BY_FILTER, after) >= 1
    assert "reclassificação/positivo" in memory.context_for(after)


def test_memory_context_is_injected_into_prompt() -> None:
    mensagens = fit_messages(
        "Engineer", "Acme", "C# role", "{}", memory_context="- stack:java: negativo"
    )
    assert "MEMORIA DE CLASSIFICACAO" in mensagens[-1]["content"]
    assert "stack:java" in mensagens[-1]["content"]


def test_approval_gate_notifies_memory(monkeypatch) -> None:
    decisions = []
    approval = ApprovalRequest(company="Acme", title="Engineer")
    gate = ApprovalGate(
        Settings(), on_decision=lambda req, approved: decisions.append((req, approved))
    )
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert gate.request(approval) is False
    assert decisions == [(approval, False)]


def test_human_correction_comes_before_many_confirmations(tmp_path) -> None:
    repo = _repo(tmp_path)
    job = _job()
    repo.upsert_job(job)
    memory = QualificationMemory(repo)

    for _ in range(6):
        memory.record(job, source="human_validation", verdict="positive", reason="correto")
    memory.record(
        job,
        source="human_validation",
        verdict="negative",
        reason="Java é a linguagem principal; decisão deveria ser SKIP",
    )

    linhas = memory.context_for(job).splitlines()
    assert linhas
    assert "validação humana/negativo" in linhas[0]
    assert any("stack:java" in linha for linha in linhas)
    assert any("validação humana/positivo" in linha for linha in linhas[1:])
