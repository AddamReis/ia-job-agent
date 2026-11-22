"""Outbox persistente usado no handoff DGX → agente local com Chrome CDP."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.storage.database import AgentEventRow, Repository


def test_publish_is_idempotent_and_reactivates_closed_event(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    kwargs = ("application", "job.qualified", "job-1", {"score": 90})

    assert repo.publish_agent_event(*kwargs) is True
    assert repo.publish_agent_event(*kwargs) is False

    eventos = repo.claim_agent_events("application")
    assert len(eventos) == 1
    assert eventos[0]["job_id"] == "job-1"
    assert eventos[0]["payload"] == {"score": 90}
    # Já reservado: outro consumidor não recebe o mesmo aviso.
    assert repo.claim_agent_events("application") == []

    assert repo.ack_agent_event(eventos[0]["id"]) is True
    assert repo.publish_agent_event(*kwargs) is True
    assert len(repo.claim_agent_events("application")) == 1


def test_failed_event_keeps_error_and_can_be_republished(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    repo.publish_agent_event("application", "job.qualified", "job-2")
    evento = repo.claim_agent_events("application")[0]

    assert repo.ack_agent_event(evento["id"], status="FAILED", error="CDP indisponível") is True
    with repo.session() as session:
        linha = session.get(AgentEventRow, evento["id"])
        assert linha.status == "FAILED"
        assert linha.error == "CDP indisponível"

    assert repo.publish_agent_event("application", "job.qualified", "job-2") is True
    with repo.session() as session:
        linha = session.get(AgentEventRow, evento["id"])
        assert linha.status == "PENDING"
        assert linha.error == ""


def test_claim_recovers_orphaned_processing_event(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    repo.publish_agent_event("application", "job.qualified", "job-3")
    evento = repo.claim_agent_events("application")[0]

    # Recém-reservado não é recuperado enquanto ainda está fresco.
    assert repo.claim_agent_events("application", stale_after_seconds=3600) == []

    antigo = (datetime.now() - timedelta(hours=1)).isoformat()
    with repo.session() as session:
        linha = session.get(AgentEventRow, evento["id"])
        linha.updated_at = antigo

    recuperados = repo.claim_agent_events("application", stale_after_seconds=3600)
    assert [evento["job_id"] for evento in recuperados] == ["job-3"]


def test_events_are_scoped_to_consumer(tmp_path) -> None:
    repo = Repository(tmp_path / "agent.db")
    repo.publish_agent_event("application", "job.qualified", "job-4")
    assert repo.claim_agent_events("classification") == []
    assert len(repo.claim_agent_events("application")) == 1
