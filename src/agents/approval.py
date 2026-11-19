"""Portão de aprovação humana para ações sensíveis e envios."""
from __future__ import annotations

from typing import Callable

from rich.console import Console
from rich.panel import Panel

from ..core.config import Settings
from ..core.events import EventBus
from ..models.domain import ApprovalRequest


class ApprovalGate:
    def __init__(
        self,
        settings: Settings,
        bus: EventBus | None = None,
        on_decision: Callable[[ApprovalRequest, bool], None] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.console = Console()
        self.on_decision = on_decision

    def request(self, approval: ApprovalRequest) -> bool:
        """Sempre exige decisão humana explícita antes de qualquer envio.

        O modo 'autonomous' e a ausência de ``before_submit`` nunca podem
        burlar a aprovação: nenhuma candidatura é enviada sem confirmação.
        """
        sensitive = bool(approval.sensitive_actions)

        lines = [
            f"Empresa: {approval.company}",
            f"Cargo: {approval.title}",
            f"Compatibilidade: {approval.score}/100" if approval.score else "",
            f"Campos preparados: {len(approval.answers)}",
        ]
        if sensitive:
            lines.append("AÇÕES SENSÍVEIS: " + ", ".join(approval.sensitive_actions))
        self.console.print(
            Panel("\n".join(line for line in lines if line), title="APROVAÇÃO NECESSÁRIA")
        )
        if self.bus:
            self.bus.emit("approval.requested", approval_id=approval.id, sensitive=sensitive)
        try:
            answer = input("Autorizar envio? [s/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        approved = answer in {"s", "sim", "y", "yes"}
        if self.bus:
            self.bus.emit("approval.resolved", approved=approved)
        if self.on_decision is not None:
            self.on_decision(approval, approved)
        return approved
