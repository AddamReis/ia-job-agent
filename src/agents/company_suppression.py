"""Supressão temporária de empresas por padrão geográfico/visto.

Circuito de duas pontas:

1. Na análise, só restrições de residência/localização/visto/autorização
   tornam a empresa candidata a supressão. Requisito técnico NUNCA entra aqui.
2. Antes de marcar, outras vagas da empresa são lidas no LinkedIn logado para
   confirmar o mesmo padrão.
3. Na busca pública, ``Repository.is_company_suppressed`` impede o salvamento
   de novas vagas dessa empresa até o prazo expirar.

Não é blacklist: depois de ``days`` a empresa volta a ser avaliada.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from ..core.config import Settings
from ..core.logging import log_event
from ..execution.web.linkedin_logged import LinkedInLoggedBrowser
from ..llm.factory import LLMRouter
from ..llm.prompts import extraction_messages
from ..models.domain import Company, JobOpportunity
from ..models.schemas import ExtractedJobFacts
from ..storage.database import Repository
from .eligibility import EligibilityAgent

# Só estes tipos de restrição podem gerar supressão de empresa.
SUPPRESSION_KINDS = frozenset(
    {"visa", "work_authorization", "residence", "location", "company_policy"}
)


def _normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def _same_company(left: str, right: str) -> bool:
    a, b = _normalize_company(left), _normalize_company(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool(ta & tb)


def _same_job(left: str, right: str) -> bool:
    def job_id(url: str) -> str:
        match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})", url or "", re.I)
        return match.group(1) if match else ""

    a, b = job_id(left), job_id(right)
    return bool(a and b and a == b)


@dataclass
class CompanyPatternCheck:
    """Resultado da conferência em outras vagas da mesma empresa."""

    company: str
    checked: int = 0
    matching: int = 0
    urls: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    confirmed: bool = False


class CompanySuppressionAgent:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        llm: LLMRouter,
        browser: LinkedInLoggedBrowser | None = None,
        eligibility: EligibilityAgent | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.llm = llm
        self._browser_override = browser
        self._local = threading.local()
        self.eligibility = eligibility or EligibilityAgent(settings, llm, repo)

    @property
    def browser(self) -> LinkedInLoggedBrowser:
        """Cliente CDP por thread (a API sync do Playwright não é reentrante).

        A qualificação roda em worker thread; reutilizar a instância criada na
        thread principal gera ``Sync API inside asyncio loop``. Com uma instância
        por thread a leitura das vagas da empresa funciona no mesmo modelo do
        probe de Easy Apply.
        """
        if self._browser_override is not None:
            return self._browser_override
        atual = getattr(self._local, "browser", None)
        if atual is None:
            atual = LinkedInLoggedBrowser()
            atual.connect()
            self._local.browser = atual
        return atual

    @staticmethod
    def suppression_kinds(kinds: list[str] | set[str]) -> list[str]:
        """Filtra só restrições elegíveis para supressão (nunca técnicas)."""
        return sorted(SUPPRESSION_KINDS & set(kinds))

    def _job_hint(self, company_name: str, target_url: str = "") -> str:
        """Vaga conhecida da empresa para descobrir o link /company/ no LinkedIn."""
        if target_url:
            return target_url
        alvo = _normalize_company(company_name)
        for job in self.repo.list_jobs():
            if _normalize_company(job.company) == alvo and job.url:
                return job.url
        return ""

    def check(
        self,
        company_name: str,
        *,
        target_kinds: list[str] | set[str] | None = None,
        target_url: str = "",
        limit: int | None = None,
    ) -> CompanyPatternCheck:
        """Confirma o padrão em outras vagas recentes da empresa no LinkedIn."""
        cfg = self.settings.company_suppression
        limit = max(1, limit or cfg.sample_jobs)
        kinds_alvo = set(target_kinds or [])

        resultado = CompanyPatternCheck(company=company_name)
        candidatas = self.browser.search_company_job_urls(
            company_name,
            limit=limit,
            job_url=self._job_hint(company_name, target_url),
        )
        for url in candidatas:
            if resultado.checked >= limit:
                break
            if target_url and _same_job(url, target_url):
                continue
            time.sleep(cfg.delay_ms / 1000)
            detail = self.browser.read_job_detail(url)
            if detail is None:
                continue
            # As URLs vêm da página de vagas da própria empresa. A marca pode
            # mudar (ex.: "Compasso UOL" virou "AI/R" no LinkedIn), então aceite
            # também quando o texto da vaga menciona a empresa consultada.
            descricao = detail.get("description", "")
            mesma_empresa = _same_company(detail.get("company", ""), company_name) or (
                bool(detail.get("company_url"))
                and _same_company(f"{detail.get('company', '')} {descricao[:3000]}", company_name)
            )
            if not mesma_empresa:
                continue

            job = JobOpportunity(
                title=detail.get("title", ""),
                company=detail.get("company") or company_name,
                location=detail.get("location", ""),
                url=url,
                description=detail.get("description", ""),
                status="DISCOVERED",
            )
            try:
                facts = self.llm.structured(
                    extraction_messages(
                        job.title,
                        job.company,
                        job.description or job.title,
                        location=job.location,
                    ),
                    ExtractedJobFacts,
                )
            except Exception as exc:
                log_event(
                    "company_check_llm_failed",
                    company=company_name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            evaluation = self.eligibility.evaluate(job, facts)
            encontrados = self.suppression_kinds(evaluation.restriction_kinds)
            casa = bool(set(encontrados) & kinds_alvo) if kinds_alvo else bool(encontrados)
            resultado.checked += 1
            resultado.urls.append(url)
            rotulo = ",".join(encontrados) or "sem restrição"
            if evaluation.details:
                rotulo += f" ({'; '.join(evaluation.details)[:140]})"
            resultado.details.append(f"{url} — {'+' if casa else '-'} {rotulo}")
            if casa:
                resultado.matching += 1

        minimo = max(cfg.min_matching, 1)
        resultado.confirmed = (
            resultado.checked > 0
            and resultado.matching >= minimo
            and resultado.matching >= (resultado.checked + 1) // 2
        )
        log_event(
            "company_pattern_checked",
            company=company_name,
            checked=resultado.checked,
            matching=resultado.matching,
            confirmed=resultado.confirmed,
        )
        return resultado

    def maybe_suppress(
        self,
        job: JobOpportunity,
        restriction_kinds: list[str],
        restriction_details: str = "",
    ) -> Company | None:
        """Marca a empresa apenas se o padrão se repetir em outras vagas."""
        cfg = self.settings.company_suppression
        if not cfg.enabled or not job.company:
            return None
        kinds = self.suppression_kinds(restriction_kinds)
        if not kinds:
            # Idioma, tecnologia, senioridade etc. NUNCA geram supressão.
            return None
        if self.repo.is_company_suppressed(job.company):
            return self.repo.get_company_by_name(job.company)

        check = self.check(
            job.company,
            target_kinds=kinds,
            target_url=job.url,
        )
        if not check.confirmed:
            log_event(
                "company_suppression_not_confirmed",
                company=job.company,
                checked=check.checked,
                matching=check.matching,
                kinds=kinds,
            )
            return None

        motivo = (
            f"restrição {','.join(kinds)} confirmada em "
            f"{check.matching}/{check.checked} vagas da empresa"
        )
        if restriction_details:
            motivo += f"; vaga atual: {restriction_details[:300]}"
        empresa = self.repo.suppress_company(
            job.company, days=cfg.days, reason=motivo
        )
        log_event(
            "company_suppressed",
            company=job.company,
            days=cfg.days,
            kinds=kinds,
            matching=check.matching,
            checked=check.checked,
        )
        return empresa
