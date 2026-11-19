"""Agente 6 — verificação de e-mails relacionados às candidaturas."""
from __future__ import annotations

import re

from rich.console import Console

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..models.domain import JobOpportunity
from ..storage.database import Repository
from ..execution.gmail.gmail_client import GmailClient, GmailMessage

_CODE_PATTERNS = [
    re.compile(r"\b\d{6}\b"),
    re.compile(r"\b[A-Z0-9]{6,8}\b"),
]
_SUBJECT_HINTS = (
    "application", "candidatura", "interview", "entrevista", "verification",
    "verifica", "confirm", "confirma", "assessment",
)


class EmailVerificationAgent:
    def __init__(
        self, gmail: GmailClient, repo: Repository, settings: Settings, bus: EventBus | None = None
    ) -> None:
        self.gmail = gmail
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self.console = Console()

    @staticmethod
    def extract_codes(text: str) -> list[str]:
        codes: list[str] = []
        for pattern in _CODE_PATTERNS:
            codes.extend(pattern.findall(text))
        return list(dict.fromkeys(codes))

    def check(self, job: JobOpportunity) -> list[GmailMessage]:
        if not self.settings.gmail.enabled:
            return []
        try:
            messages = self.gmail.search(f'"{job.company}"')
        except Exception as exc:
            log_event("gmail_indisponivel", error=str(exc))
            return []

        for message in messages:
            relevant = any(hint in message.subject.lower() for hint in _SUBJECT_HINTS)
            codes = self.extract_codes(message.body) if relevant else []
            log_event(
                "gmail_message",
                subject_length=len(message.subject),
                relevant=relevant,
                has_code=bool(codes),
            )
            if codes:
                # Código é apresentado ao usuário, nunca gravado em log ou banco.
                self.console.print(
                    f"[yellow]Código de verificação recebido ({job.company}): "
                    f"{codes[0]}[/yellow]"
                )
                if self.bus:
                    # O código é exibido ao usuário, nunca propagado em eventos/logs.
                    self.bus.emit("email.code_found", company=job.company)
        return messages
