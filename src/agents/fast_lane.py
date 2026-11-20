"""Fast lane determinístico para portais confiáveis (BairesDev, Strider).

Sem LLM, sem rede e sem CDP: usa apenas ``Company.trusted_portal_config`` e os
dados já salvos da vaga (título, localização, descrição). O resultado é gravado
no mesmo contrato do classificador completo (``QualificationResult``), para que
as fases seguintes vejam a vaga como QUALIFIED ou REJECTED_BY_FILTER.

O título é a fonte autoritativa da função principal: .NET/C# precisa aparecer
nele. Java, FrontEnd ou AI no título reprovam antes de qualquer outra checagem,
mesmo que a descrição cite .NET como tecnologia complementar. Nenhuma
candidatura é submetida aqui.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.config import Settings
from ..core.logging import log_event
from ..core.state import JobStatus
from ..models.domain import Company, JobOpportunity, QualificationResult
from ..shared.stack_filter import is_off_stack_title, mentions_dotnet
from ..storage.database import Repository
from .qualification import (
    classify_channel,
    detect_currencies,
    detect_region,
    is_remote_location,
)

# Pesos determinísticos — somam 100 quando todos os checks passam.
_WEIGHTS: dict[str, int] = {
    "stack": 30,
    "role": 25,
    "remote": 20,
    "currency": 15,
    "language": 10,
}

# Papéis aceitos pelo perfil. Chaves são os valores usados em
# ``trusted_portal_config["roles"]``; valores desconhecidos caem em substring.
_ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "backend": re.compile(r"\bback[\s-]?end\b", re.I),
    "full stack": re.compile(r"\bfull[\s-]?stack\b", re.I),
    "software developer": re.compile(
        r"\b(?:software\s+)?developer\b|\bdesarrollador\b|\bdesenvolvedor\b", re.I
    ),
    "software engineer": re.compile(
        r"\b(?:software\s+)?engineer\b|\bingenier[oa]\b|\bengenheiro\b", re.I
    ),
}

_REMOTE_DESCRIPTION_RE = re.compile(
    r"100\s*%\s*(?:remote|remoto)|fully\s+remote|trabalho\s+remoto|trabajo\s+remoto",
    re.I,
)

# Apenas a identidade do idioma importa. Nível (fluent, C1, native) não é lido.
_FOREIGN_LANGUAGE_RE = re.compile(
    r"\b(?:french|français|german|deutsch|mandarin|chinese|japanese|korean|"
    r"russian|dutch|polish|italian)\b",
    re.I,
)
_REQUIREMENT_MARKER_RE = re.compile(
    r"\b(?:proficien\w*|fluen\w*|knowledge|level|required|mandatory|"
    r"speak\w*|excellent|advanced|require\w*)\b",
    re.I,
)


def _stack_token_in_title(token: str, title: str) -> bool:
    """ Casa um token configurado ('.NET', 'C#', ...) no título da vaga. """
    normalized = (token or "").strip().lower()
    if normalized in {".net", "c#", "dotnet", "csharp"}:
        return mentions_dotnet(title)
    pattern = re.compile(re.escape(token), re.I)
    return bool(pattern.search(title or ""))


def _role_in_title(role: str, title: str) -> bool:
    lowered = (role or "").strip().lower()
    pattern = _ROLE_PATTERNS.get(lowered)
    if pattern is not None:
        return bool(pattern.search(title or ""))
    return bool(lowered) and lowered in (title or "").lower()


def _foreign_language_required(description: str) -> str | None:
    """ Retorna o idioma estrangeiro exigido, ignorando menções informais. """
    for sentence in re.split(r"[.\n]", description or ""):
        if not _REQUIREMENT_MARKER_RE.search(sentence):
            continue
        match = _FOREIGN_LANGUAGE_RE.search(sentence)
        if match:
            return match.group(0)
    return None


class FastLaneAgent:
    """Classificação simplificada e determinística de um portal confiável."""

    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        company_name: str = "BairesDev",
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.company_name = company_name

    def load_config(self) -> tuple[Company, dict[str, Any]]:
        company = self.repo.get_company_by_name(self.company_name)
        if company is None:
            raise ValueError(f"Empresa '{self.company_name}' não encontrada no banco.")
        if not company.is_trusted_portal or not company.trusted_portal_config:
            raise ValueError(
                f"'{self.company_name}' não é portal confiável "
                "(is_trusted_portal/trusted_portal_config vazios)."
            )
        return company, company.trusted_portal_config

    def qualify(self, job: JobOpportunity, config: dict[str, Any]) -> QualificationResult:
        """ Aplica os filtros do perfil e grava o veredito na vaga. """
        title = job.title or ""
        description = job.description or ""
        checks: dict[str, tuple[bool, str]] = {}

        # 1) Stack — o título define a função principal.
        if is_off_stack_title(title):
            checks["stack"] = (
                False,
                "Título focado em Java/FrontEnd/Inteligência Artificial, fora do perfil.",
            )
        else:
            tokens = [str(item) for item in (config.get("stack") or [])]
            if not tokens or any(_stack_token_in_title(token, title) for token in tokens):
                checks["stack"] = (True, "Título com .NET/C# como core.")
            else:
                checks["stack"] = (False, "Título sem .NET/C# como core.")

        # 2) Função / senioridade
        roles = [str(item).lower() for item in (config.get("roles") or [])]
        hit = next((role for role in roles if _role_in_title(role, title)), None)
        if roles and hit is None:
            checks["role"] = (
                False,
                "Função do título fora do perfil "
                "(backend/full stack/software developer/software engineer).",
            )
        else:
            checks["role"] = (True, f"Função compatível: {hit or 'perfil amplo'}.")

        # 3) Remoto — híbrido/presencial nunca passa.
        if config.get("remote_only", True):
            remote = is_remote_location(job.location, title) or bool(
                _REMOTE_DESCRIPTION_RE.search(description)
            )
            checks["remote"] = (
                (True, "100% remoto.")
                if remote
                else (False, "Vaga não é 100% remota (híbrido/presencial).")
            )
        else:
            checks["remote"] = (True, "Remoto não exigido pelo perfil.")

        # 4) Moeda — só USD/EUR; outra moeda explícita reprova; ausência é elegível.
        accepted_currencies = {
            str(item).upper() for item in (config.get("currencies") or [])
        }
        detected = detect_currencies(f"{job.salary_raw or ''} {description}")
        if accepted_currencies and detected and not (detected & accepted_currencies):
            checks["currency"] = (
                False,
                f"Moeda explícita {', '.join(sorted(detected))} fora do perfil "
                f"({', '.join(sorted(accepted_currencies))}).",
            )
        else:
            checks["currency"] = (True, "Moeda compatível ou não informada.")

        # 5) Idioma — apenas identidade; nível nunca barra.
        accepted_languages = {
            str(item).lower() for item in self.settings.eligibility.accepted_languages
        }
        configured = str(config.get("language") or "").lower()
        if configured:
            accepted_languages.add(configured)
        foreign = _foreign_language_required(description)
        if foreign:
            checks["language"] = (
                False,
                f"Idioma exigido fora dos aceitos: {foreign}.",
            )
        else:
            checks["language"] = (True, "Idioma compatível.")

        score = sum(_WEIGHTS[key] for key, (ok, _) in checks.items() if ok)
        breakdown = {
            key: (_WEIGHTS[key] if checks[key][0] else 0) for key in _WEIGHTS
        }
        reasons = [message for ok, message in checks.values() if ok]
        failures = [message for ok, message in checks.values() if not ok]
        decision = "SKIP" if failures else "APPLY"
        result = QualificationResult(
            score=score,
            breakdown=breakdown,
            decision=decision,
            reasons=reasons,
            hard_reject_reason="; ".join(failures) if failures else None,
        )

        job.score = score
        job.score_breakdown = breakdown
        job.qualification = result
        job.region = detect_region(job.location)
        job.channel = classify_channel(job, self.settings)
        job.status = (
            JobStatus.REJECTED_BY_FILTER if failures else JobStatus.QUALIFIED
        )
        # Stack/função não são barreiras de elegibilidade da empresa: não marcam
        # supressão. Só o motivo legível vai para a coluna de inspeção.
        job.has_restriction = False
        job.restriction_kinds = []
        job.restriction_details = (
            f"análise: {result.hard_reject_reason}" if failures else ""
        )
        self.repo.update_job(job)

        log_event(
            "fast_lane_qualified",
            company=job.company,
            title=job.title,
            score=score,
            decision=decision,
            reasons=reasons,
            failures=failures,
        )
        return result

    def run(
        self,
        statuses: list[JobStatus] | None = None,
        limit: int | None = None,
    ) -> dict[str, int]:
        """ Classifica todas as vagas do portal confiável nos statuses dados. """
        company, config = self.load_config()
        statuses = statuses or [JobStatus.DISCOVERED]
        candidatas = self.repo.list_jobs(status=statuses, limit=limit)
        alvo = self.repo.normalize_company(company.name)
        vagas = [
            job
            for job in candidatas
            if self.repo.normalize_company(job.company) == alvo
        ]

        counters = {
            "total": len(vagas),
            "qualified": 0,
            "rejected": 0,
        }
        log_event(
            "fast_lane_started",
            company=company.name,
            config=config,
            total=len(vagas),
            statuses=[status.value for status in statuses],
        )
        for job in vagas:
            result = self.qualify(job, config)
            if result.decision == "APPLY":
                counters["qualified"] += 1
            else:
                counters["rejected"] += 1
        log_event("fast_lane_finished", company=company.name, **counters)
        return counters
