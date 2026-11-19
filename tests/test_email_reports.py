"""E-mails: resumo da execução e lembrete diário de pendências/erros."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.agents.notifications import NotificationAgent
from src.agents.run_report import RunReportAgent
from src.core.config import Settings
from src.core.state import ExecutionStatus, JobStatus, RunState
from src.models.domain import ApplicationDependency, JobOpportunity
from src.storage.database import Repository


class StubGmail:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, to, subject, body, html=False, trace_id=""):
        self.calls.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "html": html,
                "trace_id": trace_id,
            }
        )
        return "msg-1"


def _settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    return base.model_copy(
        update={
            "storage": base.storage.model_copy(
                update={
                    "database": str(tmp_path / "agent.db"),
                    "control_file": str(tmp_path / "control.json"),
                }
            )
        }
    )


def _job(**overrides) -> JobOpportunity:
    data = {
        "title": ".NET Engineer",
        "company": "BairesDev",
        "location": "Remote - Brazil",
        "url": "https://www.linkedin.com/jobs/view/123456789",
        "external_id": "123456789",
        "search_query": ".NET Engineer",
        "search_location": "Brazil",
        "posted_at": datetime.now(),
        "posted_raw": "1 hour ago",
        "posted_precision": "hour",
        "trace_id": "trace-12345678",
    }
    data.update(overrides)
    return JobOpportunity(**data)


def _state() -> RunState:
    return RunState(
        run_id="run-1",
        trace_id="trace-12345678",
        status=ExecutionStatus.COMPLETED,
    )


def test_run_summary_has_three_tables_counts_and_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    repo.upsert_job(_job(status=JobStatus.QUALIFIED))
    repo.upsert_job(_job(external_id="2", dedupe_hash="h2", status=JobStatus.REQUIRES_HUMAN_ACTION))
    repo.upsert_job(_job(external_id="3", dedupe_hash="h3", status=JobStatus.FAILED,
                         apply_failure_reason="CAPTCHA"))
    gmail = StubGmail()
    agent = RunReportAgent(gmail, repo, settings)  # type: ignore[arg-type]
    assert agent.send(_state())
    call = gmail.calls[0]
    assert call["html"] is True
    assert call["trace_id"] == "trace-12345678"
    assert "trace=trace-12" in call["subject"]
    body = call["body"]
    assert "Resultado por termo e localidade" in body
    assert "Pendências de ação sua" in body
    assert "Falhas e não elegíveis" in body
    assert "CAPTCHA" in body
    assert ".NET Engineer" in body
    assert "trace-12345678" in body

    logs = repo.list_notifications(trace_id="trace-12345678")
    assert len(logs) == 1
    assert logs[0]["kind"] == "run_summary"


def test_reminder_is_daily_and_only_after_configured_hour(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    job = _job()
    repo.upsert_job(job)
    dep = ApplicationDependency(
        trace_id=job.trace_id,
        opportunity_id=job.id,
        company=job.company,
        title=job.title,
        kind="video_self_intro",
        owner="human",
        description="Gravar vídeo de 15 segundos",
    )
    repo.add_dependency(dep)
    gmail = StubGmail()
    agent = NotificationAgent(gmail, repo, settings)  # type: ignore[arg-type]

    # De manhã não envia: o lembrete é no fim do dia.
    assert agent.send_reminder(now=datetime(2026, 11, 17, 10, 0)) is False
    assert gmail.calls == []

    # Às 19h envia uma vez.
    assert agent.send_reminder(now=datetime(2026, 11, 17, 19, 0)) is True
    assert len(gmail.calls) == 1
    assert "1 pendência" in gmail.calls[0]["subject"]
    assert "Gravar vídeo" in gmail.calls[0]["body"]
    assert gmail.calls[0]["trace_id"] == "trace-12345678"
    assert "trace-12345678" in gmail.calls[0]["body"]

    # Outras execuções no mesmo dia não reenviam.
    assert agent.send_reminder(now=datetime(2026, 11, 17, 23, 0)) is False
    assert len(gmail.calls) == 1

    # No dia seguinte, volta a enviar.
    assert agent.send_reminder(now=datetime(2026, 11, 18, 19, 0)) is True
    assert len(gmail.calls) == 2

    logs = repo.list_notifications(trace_id="trace-12345678")
    assert len(logs) == 2
    assert {log["kind"] for log in logs} == {"daily_reminder"}


def test_both_emails_show_waiting_device_queue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    repo.upsert_job(_job(status=JobStatus.WAITING_DEVICE))
    gmail = StubGmail()

    assert RunReportAgent(gmail, repo, settings).send(_state())
    assert "Aguardando conexão do celular" in gmail.calls[-1]["body"]
    assert "device-apply" in gmail.calls[-1]["body"]

    agent = NotificationAgent(gmail, repo, settings)  # type: ignore[arg-type]
    assert agent.send_reminder(now=datetime(2026, 11, 17, 19, 0)) is True
    assert "Aguardando conexão do celular" in gmail.calls[-1]["body"]
    assert "1 aguardando celular" in gmail.calls[-1]["subject"]
    assert "trace-12345678" in gmail.calls[-1]["body"]


def test_reminder_includes_errors_and_trace(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    repo.upsert_job(_job(status=JobStatus.FAILED, apply_failure_reason="CAPTCHA"))
    gmail = StubGmail()
    agent = NotificationAgent(gmail, repo, settings)  # type: ignore[arg-type]
    assert agent.send_reminder(now=datetime(2026, 11, 17, 19, 0)) is True
    assert "1 erro" in gmail.calls[0]["subject"]
    assert "CAPTCHA" in gmail.calls[0]["body"]
    assert "Trace ID" in gmail.calls[0]["body"]
    assert "trace-12345678" in gmail.calls[0]["body"]
