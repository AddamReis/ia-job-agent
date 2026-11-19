"""Lembrete consolidado diário de pendências humanas e erros de candidatura.

Envia UM e-mail por dia (a partir do horário configurado) com três seções:
pendências de ação exclusivamente humana, erros de candidatura e vagas Easy
Apply aguardando a conexão do celular. O resumo da execução já mostra o estado
do momento; este lembrete cobre o que ficou para trás no fim do dia, sem repetir
um e-mail por vaga nem a cada rodada. Cada linha e o cabeçalho da mensagem
carregam o trace ID da execução de origem.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.gmail.gmail_client import GmailClient
from ..models.domain import ApplicationDependency, JobOpportunity
from ..storage.database import Repository

MAX_DESCRIPTION = 1500
_DAILY_REMINDER_KEY = "daily_reminder"

try:  # zoneinfo faz parte da stdlib; tzdata pode faltar em Windows muito antigo.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _link(url: str, label: str = "abrir") -> str:
    if not url:
        return "—"
    return f'<a href="{_esc(url)}">{_esc(label)}</a>'


class NotificationAgent:
    def __init__(
        self,
        gmail: GmailClient,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.gmail = gmail
        self.repo = repo
        self.settings = settings
        self.bus = bus

    def _now(self) -> datetime:
        """Hora local configurada, usada para a janela do fim do dia."""
        try:
            tz = ZoneInfo(self.settings.app.timezone) if ZoneInfo else None
        except Exception:
            tz = None
        return datetime.now(tz) if tz else datetime.now()

    def _pending_rows(self, now: datetime) -> tuple[list[list[str]], list[ApplicationDependency]]:
        deps = self.repo.list_dependencies(owner="human", status="pending")
        rows: list[list[str]] = []
        now_naive = now.replace(tzinfo=None)
        for dep in deps:
            job = self.repo.get_job(dep.opportunity_id)
            description = (job.description if job else "") or ""
            if len(description) > MAX_DESCRIPTION:
                description = description[:MAX_DESCRIPTION] + "..."

            due_text = "—"
            if dep.due_at:
                due = dep.due_at.replace(tzinfo=None) if dep.due_at.tzinfo else dep.due_at
                due_text = due.strftime("%d/%m %H:%M")
                if due < now_naive:
                    due_text += " <b>⚠ vencido</b>"
                elif due <= now_naive + timedelta(
                    hours=self.settings.gmail.reminder_before_due_hours
                ):
                    due_text += " <b>⚠ próximo</b>"

            link = (
                (job.external_apply_url if job else "")
                or (job.url if job else "")
                or dep.url
                or ""
            )
            rows.append(
                [
                    _esc(dep.trace_id or (job.trace_id if job else "") or "—"),
                    _esc(dep.company),
                    _esc(dep.title),
                    _esc(dep.description),
                    due_text,
                    _link(link, "abrir cadastro/teste"),
                    f"<div class='desc'>{_esc(description)}</div>",
                ]
            )
        return rows, deps

    def _error_rows(self) -> tuple[list[list[str]], list[JobOpportunity]]:
        failed = [
            job for job in self.repo.list_jobs()
            if job.status in {JobStatus.FAILED, JobStatus.REQUIRES_HUMAN_ACTION}
            and (job.apply_failure_reason or job.status == JobStatus.FAILED)
        ]
        rows: list[list[str]] = []
        for job in failed:
            reason = job.apply_failure_reason or "falha no envio da candidatura"
            rows.append(
                [
                    _esc(job.trace_id or "—"),
                    _esc(job.company),
                    _esc(job.title),
                    _esc(reason),
                    job.updated_at.strftime("%d/%m %H:%M"),
                    _link(job.external_apply_url or job.url),
                ]
            )
        return rows, failed

    def _waiting_rows(self) -> tuple[list[list[str]], list[JobOpportunity]]:
        waiting = self.repo.list_jobs(status=JobStatus.WAITING_DEVICE)
        rows = [
            [
                _esc(job.trace_id or "—"),
                _esc(job.company),
                _esc(job.title),
                _link(job.url, "abrir vaga"),
            ]
            for job in waiting
        ]
        return rows, waiting

    @staticmethod
    def _table(headers: list[str], rows: list[list[str]], color: str) -> str:
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
        return (
            f"<table><thead><tr style='background:{color};color:#fff'>{head}</tr>"
            f"</thead><tbody>{body}</tbody></table>"
        )

    def _build_html(
        self,
        pending_rows: list[list[str]],
        error_rows: list[list[str]],
        now: datetime,
        traces: list[str],
        waiting_rows: list[list[str]] | None = None,
    ) -> str:
        pendencia_table = self._table(
            ["Trace ID", "Empresa", "Vaga", "O que falta fazer", "Prazo", "Link", "Descrição"],
            pending_rows,
            "#8a6d00",
        ) if pending_rows else "<p>Nenhuma pendência de ação sua.</p>"
        erro_table = self._table(
            ["Trace ID", "Empresa", "Vaga", "Motivo do erro", "Quando", "Link"],
            error_rows,
            "#8b2020",
        ) if error_rows else "<p>Nenhum erro pendente.</p>"
        waiting_rows = waiting_rows or []
        waiting_table = self._table(
            ["Trace ID", "Empresa", "Vaga", "Link"],
            waiting_rows,
            "#1f4e79",
        ) if waiting_rows else "<p>Nenhuma candidatura aguardando o celular.</p>"
        trace_block = (
            "<p><b>Execuções incluídas (trace IDs):</b><br/>"
            + "<br/>".join(f"<code>{_esc(trace)}</code>" for trace in traces)
            + "</p>"
        ) if traces else "<p><b>Execuções incluídas:</b> sem trace registrado.</p>"
        return "".join(
            [
                "<html><body>",
                "<style>body{font-family:Arial,sans-serif;font-size:13px;color:#222}"
                "table{border-collapse:collapse;width:100%;margin-bottom:20px}"
                "th{text-align:left;padding:6px 8px}"
                "td{border:1px solid #ccc;padding:5px 8px;vertical-align:top}"
                ".desc{max-height:110px;overflow:auto;color:#555;font-size:11px}"
                "h2{font-size:16px;margin:18px 0 6px}</style>",
                f"<h1 style='font-size:18px'>Lembrete diário do Job Agent — "
                f"{_esc(now.strftime('%d/%m %H:%M'))}</h1>",
                trace_block,
                f"<h2>1. Pendências de ação sua ({len(pending_rows)})</h2>",
                "<p>Etapas que dependem exclusivamente de você "
                "(vídeo, câmera, documento, visto ou formulário). "
                "Nada foi marcado como concluído automaticamente.</p>",
                pendencia_table,
                f"<h2>2. Erros de candidatura ({len(error_rows)})</h2>",
                erro_table,
                f"<h2>3. Aguardando conexão do celular ({len(waiting_rows)})</h2>",
                "<p>Easy Apply aprovadas que exigem sessão logada no app. "
                "Conecte o celular e rode <code>agent device-apply</code>.</p>",
                waiting_table,
                "<p class='small'>Este é um único e-mail por dia; "
                "o resumo de cada execução é enviado separadamente.</p>",
                "</body></html>",
            ]
        )

    def _should_send(self, now: datetime, has_content: bool, force: bool) -> bool:
        cfg = self.settings.gmail
        if not cfg.enabled or not cfg.send_human_reminder:
            return False
        if not has_content:
            return False
        if force:
            return True
        # Só no fim do dia: primeiro job a partir de reminder_hour envia.
        if now.hour < cfg.reminder_hour:
            return False
        # Um por dia, mesmo que a rotina rode várias vezes.
        return self.repo.last_notification_date(_DAILY_REMINDER_KEY) != now.date()

    def send_reminder(self, now: datetime | None = None, force: bool = False) -> bool:
        """Envia o lembrete diário. ``force`` serve para teste/manual."""
        cfg = self.settings.gmail
        current = now or self._now()
        pending_rows, deps = self._pending_rows(current)
        error_rows, failed = self._error_rows()
        waiting_rows, waiting = self._waiting_rows()
        if not self._should_send(current, bool(deps or failed or waiting), force):
            return False

        traces = sorted(
            {dep.trace_id for dep in deps if dep.trace_id}
            | {job.trace_id for job in failed if job.trace_id}
            | {job.trace_id for job in waiting if job.trace_id}
        )
        trace_header = ",".join(traces) if traces else "sem-trace"
        extra = f",+{len(traces) - 1}" if len(traces) > 1 else ""
        trace_short = (traces[0][:8] + extra) if traces else "sem-trace"
        waiting_note = f" · {len(waiting)} aguardando celular" if waiting else ""
        subject = (
            f"[Lembrete][trace={trace_short}] {len(deps)} pendência(s) e "
            f"{len(failed)} erro(s) no Job Agent{waiting_note}"
        )
        try:
            message_id = self.gmail.send(
                to=cfg.alert_recipient,
                subject=subject,
                body=self._build_html(
                    pending_rows, error_rows, current, traces, waiting_rows
                ),
                html=True,
                trace_id=trace_header,
            )
        except Exception as exc:
            log_event("consolidated_reminder_failed", error=str(exc))
            return False

        # Marca o dia somente após sucesso: se o Gmail falhar, a próxima
        # execução do dia ainda tenta reenviar.
        self.repo.mark_notification_sent(_DAILY_REMINDER_KEY, current)
        self.repo.record_notification(
            kind="daily_reminder",
            subject=subject,
            message_id=message_id,
            trace_ids=traces,
            sent_at=current,
        )
        for dep in deps:
            dep.last_notified_at = current
            self.repo.update_dependency(dep)
        log_event(
            "consolidated_reminder_sent",
            pending=len(deps),
            errors=len(failed),
            waiting_device=len(waiting),
            trace_ids=trace_header,
            message_id=message_id,
            sent_at=current.isoformat(),
        )
        if self.bus:
            self.bus.emit(
                "human.reminder_sent",
                pending=len(deps),
                errors=len(failed),
                waiting_device=len(waiting),
                trace_ids=trace_header,
            )
        return True

    # Alias para código antigo; agora é o lembrete diário consolidado.
    def send_pending(self, only_new: bool = True, **kwargs) -> int:
        return 1 if self.send_reminder(**kwargs) else 0
