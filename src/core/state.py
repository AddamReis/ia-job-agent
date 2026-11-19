"""Máquina de estados, limites e controles pause/resume/stop."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import PausedError, StoppedError


class JobStatus(str, Enum):
    # Descoberta: vaga salva da busca pública, ainda sem análise.
    DISCOVERED = "DISCOVERED"
    # Análise: LLM local já extraiu fatos e calculou score.
    ANALYZED = "ANALYZED"
    # Elegível: condiz com o perfil e está habilitada para candidatura.
    QUALIFIED = "QUALIFIED"
    # Candidata a aprovação humana antes de qualquer envio.
    PENDING_APPROVAL = "PENDING_APPROVAL"
    # Easy Apply aprovada, mas impossível sem sessão logada: aguarda o celular.
    # A página pública do LinkedIn não permite candidatura simplificada.
    WAITING_DEVICE = "WAITING_DEVICE"
    # Automação em andamento (simplificada ou portal externo — mesmo status).
    APPLYING = "APPLYING"
    # Candidatura enviada com sucesso, qualquer que seja o canal.
    SUBMITTED = "SUBMITTED"
    # Falha na tentativa de envio.
    FAILED = "FAILED"
    # Enviada/aberta, mas depende de ação humana (vídeo, documento, formulário).
    # É uma candidatura parcial, não um erro.
    REQUIRES_HUMAN_ACTION = "REQUIRES_HUMAN_ACTION"
    # Não passou nos filtros/elegibilidade/score. Mantido no banco para histórico.
    REJECTED_BY_FILTER = "REJECTED_BY_FILTER"
    # Histórico: fora da janela de análise (6h). Nunca é excluído do banco.
    EXPIRED = "EXPIRED"


class ExecutionStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


_ALLOWED: dict[JobStatus, set[JobStatus]] = {
    JobStatus.DISCOVERED: {JobStatus.ANALYZED, JobStatus.REJECTED_BY_FILTER},
    JobStatus.ANALYZED: {JobStatus.QUALIFIED, JobStatus.REJECTED_BY_FILTER},
    JobStatus.QUALIFIED: {
        JobStatus.PENDING_APPROVAL,
        JobStatus.WAITING_DEVICE,
        JobStatus.APPLYING,
        JobStatus.REQUIRES_HUMAN_ACTION,
        JobStatus.REJECTED_BY_FILTER,
    },
    JobStatus.PENDING_APPROVAL: {
        JobStatus.APPLYING,
        JobStatus.WAITING_DEVICE,
        JobStatus.REJECTED_BY_FILTER,
        JobStatus.REQUIRES_HUMAN_ACTION,
    },
    JobStatus.WAITING_DEVICE: {
        JobStatus.APPLYING,
        JobStatus.REQUIRES_HUMAN_ACTION,
        JobStatus.FAILED,
        JobStatus.REJECTED_BY_FILTER,
    },
    JobStatus.APPLYING: {
        JobStatus.SUBMITTED,
        JobStatus.FAILED,
        JobStatus.REQUIRES_HUMAN_ACTION,
    },
    JobStatus.SUBMITTED: set(),
    JobStatus.FAILED: {
        JobStatus.REQUIRES_HUMAN_ACTION,
        JobStatus.APPLYING,
        JobStatus.WAITING_DEVICE,  # permite reenfileirar após falha transitória
    },
    JobStatus.REQUIRES_HUMAN_ACTION: {JobStatus.SUBMITTED},
    JobStatus.REJECTED_BY_FILTER: set(),
    # Histórico é terminal: nunca volta a ser analisado automaticamente.
    JobStatus.EXPIRED: set(),
}


def can_transition(source: JobStatus, target: JobStatus) -> bool:
    return target in _ALLOWED.get(source, set())


@dataclass
class RunState:
    run_id: str
    # Mesmo valor de run_id por padrão: usado como trace ID ponta a ponta.
    trace_id: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: ExecutionStatus = ExecutionStatus.IDLE
    jobs_discovered: int = 0
    jobs_analyzed: int = 0
    jobs_qualified: int = 0
    applications_submitted: int = 0
    errors: list[str] = field(default_factory=list)
    current_job: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["trace_id"] = self.trace_id or self.run_id
        return data

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "RunState":
        data = dict(data)
        data["status"] = ExecutionStatus(data.get("status", "IDLE"))
        state = cls(**data)
        state.trace_id = state.trace_id or state.run_id
        return state


class ControlFlags:
    """Controle entre processos (CLI pause/stop -> loop em execução)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"paused": False, "stopped": False, "run_id": None}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"paused": False, "stopped": False, "run_id": None}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set_run(self, run_id: str | None) -> None:
        data = self._read()
        data.update({"run_id": run_id, "paused": False, "stopped": False})
        self._write(data)

    def pause(self) -> None:
        data = self._read()
        data["paused"] = True
        data["stopped"] = False
        self._write(data)

    def resume(self) -> None:
        data = self._read()
        data["paused"] = False
        self._write(data)

    def stop(self) -> None:
        data = self._read()
        data["stopped"] = True
        data["paused"] = False
        self._write(data)

    def status(self) -> dict[str, Any]:
        return self._read()

    def check(self) -> None:
        data = self._read()
        if data.get("stopped"):
            raise StoppedError("Execução interrompida pelo usuário")
        if data.get("paused"):
            raise PausedError("Execução pausada pelo usuário")
