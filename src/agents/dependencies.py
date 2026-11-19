"""Agente 8 — dependências de candidatura.

Depois da análise/classificação, identifica etapas que ainda precisam ser feitas
e separa claramente o que é automático (quiz, código) do que exige o humano
(vídeo de apresentação, teste de câmera, autorização de trabalho).

Nenhuma dependência é marcada como concluída aqui: a conclusão exige feedback
explícito — do portal, do agente ou do operador.
"""
from __future__ import annotations

import re
from datetime import datetime

from ..core.config import Settings
from ..core.events import EventBus
from ..core.logging import log_event
from ..models.domain import ApplicationDependency, JobOpportunity
from ..storage.database import Repository

_VIDEO = re.compile(
    r"self[ -]?introduc|video interview|record(?:ing)? a video|video response|"
    r"v[íi]deo de apresenta|gravar um v[íi]deo|selfie video|video question",
    re.I,
)
_CAMERA = re.compile(
    r"camera check|camera test|webcam check|teste de c[âa]mera|verificar c[âa]mera",
    re.I,
)
_AUTHORIZATION = re.compile(
    r"work authorization|right to work|visa sponsorship|autoriza[çc][ãa]o de trabalho",
    re.I,
)
_QUIZ = re.compile(
    r"assessment|quiz|multiple choice|skill test|technical test|teste t[ée]cnico|"
    r"question[áa]rio|multiple-choice|cognitive test",
    re.I,
)
_CODE = re.compile(
    r"coding test|take[ -]?home|code challenge|technical challenge|"
    r"desafio t[ée]cnico|exerc[íi]cio t[ée]cnico|implemente|pull request",
    re.I,
)


class DependencyAgent:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.bus = bus

    def _ensure_company(self, job: JobOpportunity) -> None:
        if not job.company.strip():
            return
        # A gravação da vaga já vincula o company_id; isto é só fallback para
        # registros antigos ou repositórios customizados.
        company = self.repo.ensure_company(job.company)
        if company is None:
            return
        if not job.company_id:
            self.repo.link_company_to_job(job.id, company.id)
            job.company_id = company.id

    def classify(self, job: JobOpportunity) -> list[ApplicationDependency]:
        """Cria dependências a partir da descrição; idempotente por (vaga, tipo)."""
        self._ensure_company(job)
        text = " ".join([job.title, job.description or ""])

        specs: list[tuple[str, str, str, str]] = []
        if _VIDEO.search(text) or _CAMERA.search(text):
            kind = "video_self_intro" if _VIDEO.search(text) else "camera_test"
            specs.append(
                (
                    kind,
                    "human",
                    "Gravar vídeo de apresentação / validar câmera na plataforma — "
                    "depende exclusivamente de você.",
                )
            )
        if _AUTHORIZATION.search(text):
            specs.append(
                (
                    "work_authorization",
                    "human",
                    "Informar autorização de trabalho / visto / sponsorship — "
                    "decisão exclusivamente humana.",
                )
            )
        if _QUIZ.search(text):
            specs.append(
                (
                    "technical_test_quiz",
                    "agent",
                    "Teste técnico de perguntas e respostas — pode ser feito pelo "
                    "agente lendo a tela no celular.",
                )
            )
        elif _CODE.search(text):
            specs.append(
                (
                    "technical_test_code",
                    "agent",
                    "Desafio de código / implementação — pode ser feito pelo agente "
                    "com base no perfil local.",
                )
            )

        created: list[ApplicationDependency] = []
        for kind, owner, description in specs:
            dependency = ApplicationDependency(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                kind=kind,  # type: ignore[arg-type]
                owner=owner,  # type: ignore[arg-type]
                description=description,
                url=job.external_apply_url or job.url,
            )
            if not self.repo.add_dependency(dependency):
                continue
            created.append(dependency)
            log_event(
                "dependency_created",
                kind=kind,
                owner=owner,
                external_id=job.external_id,
            )
            if self.bus:
                self.bus.emit(
                    "dependency.created",
                    company=job.company,
                    title=job.title,
                    kind=kind,
                    owner=owner,
                )
        return created

    def pending_human(self) -> list[ApplicationDependency]:
        return self.repo.list_dependencies(owner="human", status="pending")

    def complete(
        self,
        dependency_id: str | None = None,
        *,
        opportunity_id: str | None = None,
        kind: str | None = None,
        note: str = "",
    ) -> ApplicationDependency | None:
        """Marca uma dependência como concluída após feedback explícito.

        É o "check" usado quando o humano termina o vídeo/candidatura na
        plataforma. ``dependency_id`` identifica a etapa; como alternativa,
        informe ``opportunity_id`` e ``kind``.
        """
        dependency = None
        if dependency_id:
            for item in self.repo.list_dependencies():
                if item.id == dependency_id:
                    dependency = item
                    break
        elif opportunity_id and kind:
            for item in self.repo.list_dependencies(
                opportunity_id=opportunity_id, kind=kind  # type: ignore[arg-type]
            ):
                dependency = item
                break
        if dependency is None:
            return None
        if dependency.status == "done":
            return dependency
        dependency.status = "done"
        dependency.completed_at = datetime.now()
        if note:
            dependency.description = (
                f"{dependency.description} — check manual: {note}".strip()
            )
        self.repo.update_dependency(dependency)
        log_event(
            "dependency_completed",
            dependency_id=dependency.id,
            kind=dependency.kind,
            opportunity_id=dependency.opportunity_id,
            trace_id=dependency.trace_id,
            owner=dependency.owner,
        )
        if self.bus:
            self.bus.emit(
                "dependency.completed",
                company=dependency.company,
                kind=dependency.kind,
                opportunity_id=dependency.opportunity_id,
            )
        return dependency

    def list_pending_by_opportunity(self, opportunity_id: str) -> list[ApplicationDependency]:
        return self.repo.list_dependencies(
            opportunity_id=opportunity_id, status="pending"
        )
