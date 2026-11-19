"""Agente 7 — validação da candidatura enviada."""
from __future__ import annotations

from ..core.errors import ValidationError
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.android.appium_driver import AppiumDevice
from ..models.domain import JobOpportunity
from ..storage.database import Repository

_CONFIRMATIONS = (
    "application submitted",
    "candidatura enviada",
    "your application",
    "sua candidatura",
    "view application",
)


class ValidationAgent:
    def __init__(
        self, device: AppiumDevice, repo: Repository, bus: EventBus | None = None
    ) -> None:
        self.device = device
        self.repo = repo
        self.bus = bus

    def validate(self, job: JobOpportunity) -> bool:
        applications = [
            app for app in self.repo.list_applications() if app.opportunity_id == job.id
        ]
        problems: list[str] = []
        if len(applications) != 1:
            problems.append(f"registros de candidatura={len(applications)}")
        xml = self.device.page_source().lower()
        if not any(marker in xml for marker in _CONFIRMATIONS):
            problems.append("confirmação de envio não encontrada na interface")
        if applications:
            missing = [a.field for a in applications[0].answers if a.required and not a.value_redacted]
            if missing:
                problems.append(f"campos obrigatórios vazios: {missing[:5]}")

        if problems:
            job.status = JobStatus.FAILED
            self.repo.update_job(job)
            log_event("validation_failed", company=job.company, problems=problems)
            if self.bus:
                self.bus.emit("validation.failed", company=job.company, problems=problems)
            raise ValidationError("; ".join(problems))

        log_event("validation_ok", company=job.company, title=job.title)
        if self.bus:
            self.bus.emit("validation.ok", company=job.company, title=job.title)
        return True
