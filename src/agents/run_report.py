"""Monta e envia o e-mail de resumo de uma execução.

Três tabelas, nesta ordem:

1. Resumo por termo/localidade: encontradas, elegíveis, cadastro automático
   concluído, falhas e pendentes de ação humana.
2. Pendências humanas: o que falta fazer em cada vaga (vídeo, câmera, texto).
3. Falhas: motivo de cada candidatura que não concluiu.

O e-mail é gerado a partir do banco (histórico), não apenas do estado em memória,
para refletir também o que ficou pendente de rodadas anteriores.
"""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime

from ..core.config import Settings
from ..core.logging import log_event
from ..core.state import JobStatus, RunState
from ..execution.gmail.gmail_client import GmailClient
from ..models.domain import ApplicationDependency, JobOpportunity
from ..storage.database import Repository

_STYLE = """
<style>
body { font-family: Arial, sans-serif; color: #222; font-size: 13px; }
h2 { margin: 22px 0 8px; font-size: 16px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 18px; }
th { background: #1f4e79; color: white; text-align: left; padding: 6px 8px; }
td { border: 1px solid #ccc; padding: 5px 8px; vertical-align: top; }
tr:nth-child(even) td { background: #f5f8fb; }
.small { color: #666; font-size: 11px; }
.pending { background: #fff3cd !important; }
.failed { background: #f8d7da !important; }
</style>
"""


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _link(url: str, label: str = "abrir") -> str:
    if not url:
        return "—"
    safe = _esc(url)
    return f'<a href="{safe}">{_esc(label)}</a>'


def _table(headers: list[str], rows: list[list[str]], css_class: str = "") -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        f'<tr class="{css_class}">' + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class RunReportAgent:
    def __init__(
        self,
        gmail: GmailClient,
        repo: Repository,
        settings: Settings,
    ) -> None:
        self.gmail = gmail
        self.repo = repo
        self.settings = settings

    def build_html(self, state: RunState, jobs: list[JobOpportunity]) -> str:
        counts = self._count_jobs(jobs)
        deps = self.repo.list_dependencies(status="pending")
        by_opportunity: dict[str, list[ApplicationDependency]] = defaultdict(list)
        for dep in deps:
            by_opportunity[dep.opportunity_id].append(dep)

        # --- Tabela 1: resumo por termo/localidade ---
        summary_rows: list[list[str]] = []
        for key, group in sorted(counts.items()):
            query, location = key
            summary_rows.append(
                [
                    _esc(query or "(sem termo)"),
                    _esc(location or "(todas)"),
                    str(group["found"]),
                    str(group["eligible"]),
                    str(group["submitted"]),
                    str(group["failed"]),
                    str(group["pending"]),
                ]
            )
        totals = {k: sum(g[k] for g in counts.values()) for k in
                  ("found", "eligible", "submitted", "failed", "pending")}
        summary_rows.append(
            [
                "<b>TOTAL</b>", "<b>—</b>",
                *(f"<b>{totals[k]}</b>" for k in
                  ("found", "eligible", "submitted", "failed", "pending")),
            ]
        )
        table1 = _table(
            ["Termo", "Localidade", "Encontradas", "Elegíveis",
             "Cadastro automático", "Falhas", "Pendente ação sua"],
            summary_rows,
        )

        # --- Tabela 2: pendências humanas ---
        pending_rows: list[list[str]] = []
        for dep in deps:
            if dep.owner != "human":
                continue
            job = self.repo.get_job(dep.opportunity_id)
            pending_rows.append(
                [
                    _esc(dep.company),
                    _esc(dep.title),
                    _esc(dep.description),
                    _link(
                        (job.external_apply_url if job else "")
                        or (job.url if job else "")
                        or dep.url,
                        "link direto",
                    ),
                ]
            )
        table2 = _table(
            ["Empresa", "Vaga", "O que falta fazer", "Link"],
            pending_rows,
            css_class="pending",
        ) if pending_rows else "<p>Nenhuma pendência de ação humana.</p>"

        # --- Tabela 3: falhas ---
        failed_rows: list[list[str]] = []
        for job in jobs:
            if job.status not in {JobStatus.FAILED, JobStatus.REJECTED_BY_FILTER}:
                continue
            reason = job.apply_failure_reason or (
                job.qualification.hard_reject_reason if job.qualification else ""
            ) or "—"
            failed_rows.append(
                [
                    _esc(job.company),
                    _esc(job.title),
                    _esc(reason),
                    _link(job.external_apply_url or job.url),
                ]
            )
        table3 = _table(
            ["Empresa", "Vaga", "Motivo", "Link"],
            failed_rows,
            css_class="failed",
        ) if failed_rows else "<p>Nenhuma falha registrada.</p>"

        # --- Seção extra: fila que depende do celular logado ---
        waiting = [job for job in jobs if job.status == JobStatus.WAITING_DEVICE]
        waiting_rows = [
            [
                _esc(job.trace_id or "—"),
                _esc(job.company),
                _esc(job.title),
                _link(job.url, "abrir vaga"),
            ]
            for job in waiting
        ]
        waiting_block = (
            "<h2>4. Aguardando conexão do celular (Easy Apply)</h2>"
            + (
                _table(
                    ["Trace ID", "Empresa", "Vaga", "Link"],
                    waiting_rows,
                    css_class="pending",
                )
                + "<p class='small'>Conecte o celular e rode "
                "<code>agent device-apply</code> para processar a fila.</p>"
                if waiting_rows
                else "<p>Nenhuma candidatura aguardando o celular.</p>"
            )
        )

        errors = "".join(f"<li>{_esc(e)}</li>" for e in state.errors)
        errors_block = f"<p><b>Erros da execução:</b></p><ul>{errors}</ul>" if errors else ""
        trace_id = state.trace_id or state.run_id

        return "".join(
            [
                "<html><body>", _STYLE,
                f"<h2>Resumo da execução — {_esc(trace_id)}</h2>",
                f'<p class="small"><b>Trace ID:</b> <code>{_esc(trace_id)}</code><br/>'
                f"Iniciada: {_esc(state.started_at)} · "
                f"Status: {_esc(state.status.value)} · "
                f"Descobertas: {state.jobs_discovered} · "
                f"Analisadas: {state.jobs_analyzed} · "
                f"Qualificadas: {state.jobs_qualified} · "
                f"Enviadas: {state.applications_submitted}</p>",
                "<h2>1. Resultado por termo e localidade</h2>", table1,
                "<h2>2. Pendências de ação sua</h2>", table2,
                "<h2>3. Falhas e não elegíveis</h2>", table3,
                waiting_block,
                errors_block,
                "</body></html>",
            ]
        )

    @staticmethod
    def _count_jobs(jobs: list[JobOpportunity]) -> dict[tuple[str, str], dict[str, int]]:
        counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"found": 0, "eligible": 0, "submitted": 0, "failed": 0, "pending": 0}
        )
        for job in jobs:
            key = (job.search_query, job.search_location)
            group = counts[key]
            group["found"] += 1
            if job.status in {
                JobStatus.QUALIFIED, JobStatus.PENDING_APPROVAL,
                JobStatus.WAITING_DEVICE, JobStatus.APPLYING,
                JobStatus.SUBMITTED, JobStatus.REQUIRES_HUMAN_ACTION,
            }:
                group["eligible"] += 1
            if job.status in {JobStatus.SUBMITTED, JobStatus.REQUIRES_HUMAN_ACTION} or job.has_applied:
                group["submitted"] += 1
            if job.status == JobStatus.FAILED:
                group["failed"] += 1
            if job.status == JobStatus.REQUIRES_HUMAN_ACTION:
                group["pending"] += 1
        return counts

    def send(self, state: RunState) -> bool:
        if not self.settings.gmail.enabled or not self.settings.gmail.send_run_summary:
            return False
        trace_id = state.trace_id or state.run_id
        jobs = self.repo.list_jobs()
        html_body = self.build_html(state, jobs)
        subject = (
            f"[Job Agent][trace={trace_id[:8]}] "
            f"Resumo da execução {datetime.now():%d/%m %H:%M} — "
            f"{state.applications_submitted} candidaturas enviadas"
        )
        try:
            message_id = self.gmail.send(
                to=self.settings.gmail.alert_recipient,
                subject=subject,
                body=html_body,
                html=True,
                trace_id=trace_id,
            )
        except Exception as exc:
            log_event("run_summary_email_failed", error=str(exc))
            return False
        self.repo.record_notification(
            kind="run_summary",
            subject=subject,
            message_id=message_id,
            trace_ids=[trace_id],
        )
        log_event(
            "run_summary_email_sent",
            run_id=state.run_id,
            trace_id=trace_id,
            message_id=message_id,
        )
        return True
