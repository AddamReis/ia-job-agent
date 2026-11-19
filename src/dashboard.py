"""Painel simples no terminal para acompanhar a execução em tempo real."""
from __future__ import annotations

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from .core.events import Event, EventBus
from .core.state import RunState


class Dashboard:
    def __init__(self, bus: EventBus, static: dict) -> None:
        self.bus = bus
        self.static = static
        self.state: RunState | None = None
        self.last_event: Event | None = None
        self.search_progress: dict = {}
        self.qualification_progress: dict = {}
        self.console = Console()
        self.live: Live | None = None
        bus.subscribe("*", self._on_event)

    def _on_event(self, event: Event) -> None:
        if event.type == "state.changed" and event.payload:
            try:
                self.state = RunState.from_snapshot(dict(event.payload))
            except Exception:
                pass
        elif event.type == "search.progress":
            self.search_progress = dict(event.payload)
        elif event.type == "qualification.progress":
            self.qualification_progress = dict(event.payload)
        self.last_event = event
        if self.live is not None:
            self.live.update(self.render())

    def render(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan")
        table.add_column()
        table.add_row("Device", str(self.static.get("device", "—")))
        table.add_row("LinkedIn", str(self.static.get("linkedin_instalado", "—")))
        table.add_row("CV", "Carregado" if self.static.get("profile") else "—")
        table.add_row("Gmail", str(self.static.get("gmail", "—")))
        table.add_row("LLM", str(self.static.get("llm", "—")))
        table.add_row("Execução", self.state.status.value if self.state else "IDLE")
        table.add_row("", "")
        table.add_row("Vagas descobertas", str(self.state.jobs_discovered if self.state else 0))
        table.add_row("Vagas analisadas", str(self.state.jobs_analyzed if self.state else 0))
        table.add_row("Vagas qualificadas", str(self.state.jobs_qualified if self.state else 0))
        table.add_row("Candidaturas enviadas", str(self.state.applications_submitted if self.state else 0))
        if self.search_progress:
            p = self.search_progress
            table.add_row("Busca", f"{p.get('current', 0)}/{p.get('total', 0)} "
                                   f"(novas={p.get('new', 0)}, total={p.get('cumulative', 0)})")
        if self.qualification_progress:
            p = self.qualification_progress
            table.add_row("Análise", f"{p.get('current', 0)}/{p.get('total', 0)} "
                                     f"— {p.get('company', '')} · {p.get('title', '')} "
                                     f"[{p.get('decision', '')}]")
        current = self.state.current_job if self.state else {}
        if current:
            table.add_row("", "")
            table.add_row("Vaga atual", str(current.get("title", "")))
            table.add_row("Empresa", str(current.get("company", "")))
            table.add_row("Local", str(current.get("location", "")))
            table.add_row("Compatibilidade", f"{current.get('score', 0)}/100")
            table.add_row("Status", str(current.get("status", "")))
        if self.last_event:
            table.add_row("", "")
            table.add_row("Último evento", self.last_event.type)
        return Panel(table, title="JOB APPLICATION AGENT", border_style="green")

    def start(self) -> None:
        self.live = Live(self.render(), console=self.console, refresh_per_second=2)
        self.live.start()

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None
