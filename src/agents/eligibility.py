"""Agente de elegibilidade — barreiras que tornam a vaga inviável.

Avalia, no momento da análise, se há restrições que impedem a candidatura:

- visto/sponsorship exigido e indisponível no perfil;
- autorização de trabalho em país onde o candidato não pode trabalhar;
- idioma exigido fora de accepted_languages (inglês/português/espanhol OK,
  francês/japonês etc. barram). NÍVEL não importa: fluent/C1/C2/native não barra;
- exigência de residência em região onde o candidato não mora/pode morar.

Quando a descrição não traz informação suficiente e existe link público da
empresa, o agente consulta essa página e alimenta a base de empresas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.config import Settings
from ..core.logging import log_event
from ..llm.factory import LLMRouter
from ..llm.prompts import company_eligibility_messages
from ..models.domain import Company, JobOpportunity
from ..models.schemas import (
    CompanyEligibilityFacts,
    ExtractedJobFacts,
    LanguageRequirement,
)
from ..storage.database import Repository

# Níveis de idioma (fluent/C1/C2/native) NÃO são critério — ver evaluate().
# Mantido apenas o aliases para normalizar a IDENTIDADE do idioma.

_LANGUAGE_ALIASES = {
    "english": "english", "inglês": "english", "ingles": "english",
    "portuguese": "portuguese", "português": "portuguese", "portugues": "portuguese",
    "spanish": "spanish", "espanhol": "spanish", "espagnol": "spanish",
    "french": "french", "français": "french", "francais": "french",
    "german": "german", "deutsch": "german", "alemão": "german",
    "italian": "italian", "italiano": "italian",
    "japanese": "japanese", "japonês": "japanese",
    "chinese": "chinese", "mandarin": "chinese",
}

_REGION_ALIASES = {
    "brazil": ("brazil", "brasil", "latam", "latin america"),
    "europe": ("europe", "europa", "eu", "european union"),
    "americas_north": ("united states", "usa", "u.s.", "canada", "north america"),
    "australia": ("australia",),
    "new_zealand": ("new zealand", "nova zelandia"),
    "worldwide": ("worldwide", "anywhere", "global", "any country", "all countries",
                  "open to all", "remote anywhere"),
}

# Cidade/UF brasileira no card (ex.: "Manaus, AM") precisa resolver para o
# Brasil; senão a autorização de trabalho é confundida com barreira de visto.
_BRAZIL_UFS = {
    "ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms",
    "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc",
    "se", "sp", "to",
}
_BRAZIL_PLACES = (
    "acre", "alagoas", "amazonas", "amapá", "amapa", "bahia", "ceará", "ceara",
    "distrito federal", "espírito santo", "espirito santo", "goiás", "goias",
    "maranhão", "maranhao", "minas gerais", "mato grosso do sul", "mato grosso",
    "pará", "para", "paraíba", "paraiba", "pernambuco", "piauí", "piaui",
    "paraná", "parana", "rio de janeiro", "rio grande do norte",
    "rio grande do sul", "santa catarina", "sergipe", "são paulo", "sao paulo",
    "tocantins", "manaus", "brasília", "brasilia",
)
_UF_RE = re.compile(r"\b(?:" + "|".join(sorted(_BRAZIL_UFS, key=len, reverse=True)) + r")\b")

# Visto oferecido pela empresa é informação neutra: não é barreira do candidato.
_SPONSORSHIP_OFFER_RE = re.compile(
    r"(?:sponsorship|visa|work permit|relocation)[^.\n]{0,60}"
    r"(?:provided|available|offered|supported|assisted|covered|arranged|package|support)"
    r"|(?:we|the company|our company|our team)[^.\n]{0,60}"
    r"(?:sponsor|sponsors|sponsored|visa|relocation|relocate)"
    r"|(?:offer|offers|provide|provides|arrange|assist with|help with)[^.\n]{0,60}"
    r"(?:visa|sponsorship|relocation)"
    r"|patroc[ií]nio de visto|visto fornecido|fornecemos (?:o )?visto"
    r"|oferecemos (?:o )?visto|mudan[cç]a custeada|custea(?:mos)? a mudan[cç]a"
    r"|aux[ií]lio de mudan[cç]a|reembolso de mudan[cç]a",
    re.IGNORECASE,
)
_SPONSORSHIP_DENIAL_RE = re.compile(
    r"(?:no|not|never|without|cannot|can't|do not|does not|will not|won't)[^.\n]{0,40}"
    r"(?:sponsor|sponsorship|visa|relocation)"
    r"|(?:sponsor|sponsorship|visa)[^.\n]{0,40}(?:not available|unavailable|not provided)"
    r"|sem (?:visto|patroc[ií]nio)"
    r"|n[aã]o (?:fornecemos|oferecemos|aceitamos) (?:visto|patroc[ií]nio)"
    r"|(?:exige|necessita|precisa) (?:j[aá] possuir|de) visto",
    re.IGNORECASE,
)


@dataclass
class EligibilityResult:
    has_restriction: bool = False
    restriction_kinds: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    needs_company_check: bool = False

    @property
    def summary(self) -> str:
        return " | ".join(self.details)


def _normalize_language(value: str) -> str:
    key = (value or "").strip().lower()
    return _LANGUAGE_ALIASES.get(key, key)


def _matches_region(text: str, region: str) -> bool:
    lowered = (text or "").lower()
    key = (region or "").strip().lower()
    for mark in _REGION_ALIASES.get(key, (key,)):
        # Siglas curtas (UF, "eu", "usa") exigem fronteira de palavra para não
        # casar dentro de "Amazon", "Sistemas", etc.
        if len(mark) <= 3 and mark.isalpha():
            if re.search(rf"\b{re.escape(mark)}\b", lowered):
                return True
        elif mark in lowered:
            return True
    if key == "brazil":
        return bool(_UF_RE.search(lowered)) or any(p in lowered for p in _BRAZIL_PLACES)
    return False


def _any_region(text: str, regions: list[str]) -> bool:
    return any(_matches_region(text, region) for region in regions)


def is_brazilian_location(location: str) -> bool:
    """Detecta Brasil por país, UF, estado ou cidade (sem contar 'LATAM')."""
    lowered = (location or "").lower()
    return (
        "brazil" in lowered
        or "brasil" in lowered
        or bool(_UF_RE.search(lowered))
        or any(place in lowered for place in _BRAZIL_PLACES)
    )


def work_authorization_authorized(location: str, settings: Settings) -> bool:
    """True quando a localização da vaga é coberta pelo perfil do candidato."""
    return _any_region(location, settings.eligibility.work_authorization_countries)


class EligibilityAgent:
    def __init__(
        self,
        settings: Settings,
        llm: LLMRouter | None = None,
        repo: Repository | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.repo = repo

    def evaluate(
        self,
        job: JobOpportunity,
        facts: ExtractedJobFacts,
        company: Company | None = None,
    ) -> EligibilityResult:
        cfg = self.settings.eligibility
        result = EligibilityResult()

        # 1) Visto/sponsorship exigido e indisponível. Patrocínio/relocação
        #    oferecido pela empresa é informação neutra, não barreira.
        description = job.description or ""
        sponsorship_offered = bool(
            _SPONSORSHIP_OFFER_RE.search(description)
        ) and not _SPONSORSHIP_DENIAL_RE.search(description)
        if (
            facts.visa_sponsorship_required
            and not sponsorship_offered
            and not cfg.visa_sponsorship_available
        ):
            result.restriction_kinds.append("visa")
            result.details.append(
                "exige patrocínio de visto/sponsorship, indisponível no perfil"
            )

        # 2) Autorização de trabalho no país da vaga.
        if facts.work_authorization_required:
            location = job.location or ""
            authorized = cfg.work_authorization_countries
            if not _any_region(location, authorized):
                result.restriction_kinds.append("work_authorization")
                result.details.append(
                    f"exige autorização de trabalho em '{location or 'país da vaga'}' "
                    f"sem cobertura no perfil"
                )

        # 3) Idiomas exigidos — APENAS a identidade do idioma importa.
        # Nível (fluent/C1/C2/native) NÃO barra: inglês técnico é suficiente
        # na prática. Só idiomas fora de accepted_languages viram restrição.
        accepted = {_normalize_language(x) for x in cfg.accepted_languages}
        requirements: list[LanguageRequirement] = list(facts.language_details)
        if not requirements:
            requirements = [
                LanguageRequirement(language=lang) for lang in facts.language_requirements
            ]
        for req in requirements:
            lang = _normalize_language(req.language)
            if not lang or lang in accepted:
                continue
            result.restriction_kinds.append("language")
            result.details.append(
                f"exige idioma fora do perfil: {req.language or lang}"
            )

        # 4) Residência obrigatória em região incompatível.
        residence = facts.residence_restriction or ""
        if residence and not _any_region(residence, cfg.residence_countries + cfg.residence_regions):
            # "Worldwide" na própria restrição significa que não há barreira.
            if not _matches_region(residence, "worldwide"):
                result.restriction_kinds.append("residence")
                result.details.append(f"exige residência: {residence}")

        # 5) Países explicitamente aceitos — se não inclui o Brasil e não é worldwide.
        eligible = facts.eligible_regions or []
        if eligible and not facts.worldwide_remote:
            allowed = cfg.residence_countries + cfg.residence_regions
            if not any(_any_region(" ".join(eligible), [a]) for a in allowed) and not any(
                _matches_region(r, "worldwide") for r in eligible
            ):
                result.restriction_kinds.append("location")
                result.details.append(
                    "aceita apenas candidatos de: " + ", ".join(eligible)
                )

        result.has_restriction = bool(result.details)

        # Informação insuficiente + link público -> tentar enriquecer a empresa.
        unclear = (
            facts.confidence < 0.5
            and not facts.visa_sponsorship_required
            and not residence
            and not eligible
            and not result.details
        )
        result.needs_company_check = bool(
            unclear and cfg.enrich_from_company_page and job.external_apply_url
        )
        return result

    def enrich_company(
        self, job: JobOpportunity, company: Company | None
    ) -> Company | None:
        """Consulta a página pública da empresa e atualiza a base de empresas."""
        if not job.external_apply_url or self.llm is None or self.repo is None:
            return company
        if company is None:
            company = self.repo.get_company_by_name(job.company)
        if company is None or not job.company:
            return company
        from ..execution.web.linkedin_public import extract_visible_text, LinkedInPublicBrowser

        browser = LinkedInPublicBrowser(self.settings)
        try:
            html = browser.public_page(job.external_apply_url)
        except Exception as exc:
            log_event("company_page_fetch_failed", company=job.company, error=str(exc))
            return company
        finally:
            browser.close()

        text = extract_visible_text(html)
        if not text:
            return company
        facts = self.llm.structured(
            company_eligibility_messages(job.company, text, job.title),
            CompanyEligibilityFacts,
        )
        if facts.hiring_regions:
            company.hiring_regions = sorted(set(company.hiring_regions + facts.hiring_regions))
        if facts.languages:
            company.languages = sorted(set(company.languages + facts.languages))
        if facts.visa_policy:
            company.visa_policy = facts.visa_policy
        if facts.notes:
            company.eligibility_notes = facts.notes
        self.repo.upsert_company(company)
        log_event(
            "company_enriched",
            company=job.company,
            regions=len(company.hiring_regions),
            languages=len(company.languages),
        )
        return company

    def apply_company_policy(
        self, result: EligibilityResult, company: Company | None
    ) -> EligibilityResult:
        """Usa o conhecimento da empresa quando a vaga é omissa."""
        if company is None:
            return result
        cfg = self.settings.eligibility
        regions = company.hiring_regions
        if regions and not any(_matches_region(r, "worldwide") for r in regions):
            allowed = cfg.residence_countries + cfg.residence_regions
            if not any(_any_region(" ".join(regions), [a]) for a in allowed):
                kinds = set(result.restriction_kinds)
                if "company_policy" not in kinds:
                    result.restriction_kinds.append("company_policy")
                    result.details.append(
                        f"{company.name} contrata apenas em: {', '.join(regions)}"
                    )
        accepted = {_normalize_language(x) for x in cfg.accepted_languages}
        for lang in company.languages:
            normalized = _normalize_language(lang)
            if normalized and normalized not in accepted:
                if "company_language" not in result.restriction_kinds:
                    result.restriction_kinds.append("company_language")
                    result.details.append(
                        f"{company.name} exige idioma fora do perfil: {lang}"
                    )
        result.has_restriction = bool(result.details)
        return result
