"""Agente 3 — qualificação com extração via LLM e score determinístico."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Callable

from ..core.config import Settings
from ..core.errors import LLMError
from ..core.events import EventBus
from ..core.geo import normalize_location
from ..core.logging import log_event
from ..core.state import JobStatus
from ..llm.factory import LLMRouter
from ..llm.prompts import extraction_messages, fit_messages
from ..execution.profile.builder import profile_context
from .company_suppression import CompanySuppressionAgent
from .qualification_memory import QualificationMemory
from .eligibility import (
    _normalize_language,
    is_brazilian_location,
    work_authorization_authorized,
)
from ..models.domain import (
    ApplicationChannel,
    CandidateProfile,
    JobAnalysis,
    JobOpportunity,
    QualificationResult,
)
from ..models.schemas import ExtractedJobFacts, JobFitAssessment
from ..shared.stack_filter import (
    is_ai_focused,
    mentions_ai,
    mentions_dotnet,
    mentions_frontend,
    mentions_java,
)
from ..execution.web.linkedin_public import is_remote_public
from ..storage.database import Repository

_REMOTE_RE = re.compile(
    r"\b(remote|remoto|work from home|wfh|anywhere|distributed|telecommut|teletrabalho)\b",
    re.IGNORECASE,
)
_ONSITE_RE = re.compile(
    r"\b(hybrid|h[íi]brido|on-?site|presencial|in office|office based)\b",
    re.IGNORECASE,
)
# Usos técnicos de "hybrid" (hybrid cloud, hybrid app) NÃO são modelo de trabalho.
_TECH_HYBRID_RE = re.compile(
    r"\bhybrid\s+(?:cloud|app|mobile|application|framework|integration|database)\b",
    re.IGNORECASE,
)
_CURRENCY_PATTERNS = {
    "USD": re.compile(r"(?<!r)\bUSD\b|US\s*\$|(?<![rR])\$", re.IGNORECASE),
    "EUR": re.compile(r"\bEUR\b|€|euros?", re.IGNORECASE),
    "GBP": re.compile(r"\bGBP\b|£|pounds?", re.IGNORECASE),
    "BRL": re.compile(r"\bBRL\b|R\$|reais?", re.IGNORECASE),
    "CHF": re.compile(r"\bCHF\b|francos?|francs?", re.IGNORECASE),
    "JPY": re.compile(r"\bJPY\b|\byen\b", re.IGNORECASE),
    "CAD": re.compile(r"\bCAD\b|CA\$", re.IGNORECASE),
    "AUD": re.compile(r"\bAUD\b|AUS\$", re.IGNORECASE),
}
_ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def explicit_currency_codes(*values: str | None) -> set[str]:
    """Códigos ISO explícitos já extraídos pelo LLM (ex.: ``currency=CHF``)."""
    codes = {
        value.strip().upper()
        for value in values
        if value and _ISO_CURRENCY_RE.fullmatch(value.strip().upper())
    }
    return codes


def is_remote_location(location: str, title: str = "") -> bool:
    """Só considera 100% remoto; híbrido/presencial nunca passa.

    "Hybrid cloud" é tecnologia e não veto: usa a mesma limpeza da validação
    pública para não descartar vagas remotas falsamente.
    """
    text = f"{location} {title}"
    cleaned = _TECH_HYBRID_RE.sub(" ", text)
    if _ONSITE_RE.search(cleaned):
        return False
    return bool(_REMOTE_RE.search(cleaned))


def detect_currencies(text: str) -> set[str]:
    """Detecta moedas explícitas sem confundir R$ com USD."""
    return {
        code for code, pattern in _CURRENCY_PATTERNS.items() if pattern.search(text or "")
    }


def is_national_location(location: str, countries: list[str]) -> bool:
    lowered = (location or "").lower()
    return any((country or "").lower() in lowered for country in countries)


# LATAM primeiro, depois as demais regiões já definidas.
_REGION_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        "latam",
        ("latam", "latin america", "america latina", "américa latina",
         "mexico", "méxico", "colombia", "argentina", "chile", "peru",
         "uruguay", "paraguay", "bolivia", "ecuador", "venezuela",
         "costa rica", "panama", "guatemala", "dominican"),
    ),
    ("australia", ("australia", "sydney", "melbourne", "australian")),
    ("new_zealand", ("new zealand", "nova zelandia", "auckland", "wellington")),
    (
        "americas_north",
        ("united states", "united states of america", "u.s.a", "usa", "canada",
         "united canada", "new york", "san francisco", "seattle", "austin",
         "boston", "chicago", "toronto", "vancouver", "montreal", "us remote",
         "north america", "america do norte"),
    ),
    (
        "europe",
        ("united kingdom", "uk", "england", "london", "ireland", "dublin",
         "germany", "berlin", "munich", "netherlands", "amsterdam", "portugal",
         "lisbon", "spain", "madrid", "barcelona", "france", "paris", "italy",
         "milan", "switzerland", "sweden", "stockholm", "norway", "denmark",
         "finland", "poland", "austria", "belgium", "europe", "europa", "eu remote"),
    ),
    ("brazil", ("brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro")),
]


def detect_region(location: str) -> str:
    """Infere a região a partir da localização informada na vaga."""
    lowered = (location or "").lower()
    # Cidade/UF brasileira (ex.: "Manaus, AM") precede o fallback "other", mas
    # termos amplos como "LATAM" continuam caindo na sua própria região.
    if is_brazilian_location(location):
        return "brazil"
    for region, marks in _REGION_PATTERNS:
        if any(mark in lowered for mark in marks):
            return region
    return "other"


def currency_indicator(currencies: set[str], salary_present: bool) -> tuple[str, bool]:
    """Indicador informativo de moeda, gravado na vaga para auditoria."""
    if not currencies:
        return ("sem salário informado" if not salary_present else "moeda não informada", False)
    ordered = sorted(currencies)
    multi = len(ordered) > 1
    return ("/".join(ordered), multi)


# Marcadores fortes de português: evita confundir vaga espanhola com brasileira.
_PT_POSTING_MARKERS = (
    "você", "currículo", "curriculo", "salário", "remuneração",
    "desenvolvedor", "desenvolvedora", "português", "portugues", "brasil",
)


def is_national_or_portuguese_posting(location: str, text: str) -> bool:
    """Vaga do Brasil ou escrita em português exige evidência de USD/EUR."""
    if is_brazilian_location(location):
        return True
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PT_POSTING_MARKERS)


def currency_rejection_reason(
    currencies: set[str], allowed: set[str], national: bool = False
) -> str | None:
    """Moeda é critério somente quando há moeda explícita na vaga.

    - USD/EUR explícitos (ou presentes em faixa multi-moeda): aprovado.
    - Qualquer outra moeda explícita (BRL, CHF, GBP...): reprovado.
    - Sem salário ou sem moeda informada: neutro. Salario fica FORA do score e
      a ausência de faixa não é evidência de moeda inelegível.

    ``national`` permanece no contrato por compatibilidade, mas não reprova
    vagas brasileiras/PT-BR apenas por omissão salarial.
    """
    del national  # mantido apenas para compatibilidade de chamada
    if currencies & allowed:
        return None
    if currencies:
        return (
            f"salário informado em {', '.join(sorted(currencies))} — "
            f"moeda não elegível ({'/'.join(sorted(allowed))})"
        )
    return None


_TECH_ALIASES = {
    "dotnet": "net",
    "netcore": "net",
    "netframework": "net",
    "aspnetcore": "aspnet",
    "csharp": "c",
    "c": "c",
}


def normalize_technology(value: str) -> str:
    """Canonicaliza `.NET`, `DotNet`, `Dot Net`, `.NET Core`, `C#`, `C Sharp`."""
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9#]", "", text).replace("#", "sharp")
    return _TECH_ALIASES.get(text, text)


def technologies_match(skill: str, known: str) -> bool:
    a, b = normalize_technology(skill), normalize_technology(known)
    if not a or not b:
        return False
    if a == b:
        return True
    # Evita que "C" case com "Cassandra": só usa substring em termos longos.
    if len(a) < 3 or len(b) < 3:
        return False
    return a in b or b in a


def stale_reason(job: JobOpportunity, settings: Settings) -> str | None:
    """Só classifica como EXPIRED após ``expired_after_hours`` (config: 96h).

    A janela de descoberta (``recency_hours``) continua em 6h. Como a
    classificação leva segundos, uma vaga publicada há 1–3 dias ainda vale
    análise; ``EXPIRED``/passado o prazo fica reservado ao histórico defasado.
    """
    search_cfg = settings.search
    window = search_cfg.expired_after_hours or search_cfg.recency_hours
    if window <= 0 or not settings.qualification.reject.stale_posting:
        return None
    # Postgres devolve ``posted_at`` timezone-aware; SQLite, naive. Usar o mesmo
    # tzinfo da vaga evita TypeError de comparação na revalidação dos workers.
    agora = (
        datetime.now(job.posted_at.tzinfo)
        if job.posted_at is not None and job.posted_at.tzinfo is not None
        else datetime.now()
    )
    cutoff = agora - timedelta(hours=window)
    if (
        job.posted_at is None
        or job.posted_at < cutoff
        or (search_cfg.require_explicit_recency and job.posted_precision == "unknown")
    ):
        return f"publicação fora da janela de {window} horas"
    return None


def job_side_language_reason(assessment: "JobFitAssessment") -> str:
    """Motivo de reprovação semântica sem explicar o que o candidato já tem.

    O perfil é conhecido: o campo de detalhe deve dizer apenas o que a VAGA
    exige e o perfil não cobre. O texto completo (cross-check) continua no
    ``fit_assessment`` e nos reasons da qualificação.
    """
    core = (assessment.core_language or "").strip()
    if core:
        stack = list(dict.fromkeys(item.strip() for item in assessment.core_stack if item.strip()))
        reason = f"A vaga exige {core} como linguagem principal (core)"
        if stack:
            reason += ": " + ", ".join(stack[:5])
        return reason

    # Sem core explícito, aproveita a frase do LLM mas corta a parte que fala
    # do candidato (já conhecida pelo operador).
    reason = (assessment.language_reason or "").strip()
    reason = re.split(
        r"\s+[—–-]\s*(?:o|a|os|as)?\s*(?:candidato|perfil|ele|ela)\b",
        reason,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return reason[:180] or "a vaga exige stack diferente da especialização"


# Veto de CORE: usa o mesmo pré-filtro determinístico do discovery. Java,
# FrontEnd e AI só reprovam quando são o foco; .NET/C# no título/core protege.
def _dotnet_core_signals(
    job: "JobOpportunity",
    facts: "ExtractedJobFacts",
    assessment: "JobFitAssessment",
) -> tuple[bool, bool]:
    """Sinal forte (título/core) e fraco (requisitos) de que .NET é o core.

    O sinal forte vence: título 'Java Developer' com .NET em required_skills
    continua sendo vaga Java e deve ser recusada.
    """
    core_stack = list(assessment.core_stack or [])
    explicit = bool(
        mentions_dotnet(job.title or "")
        or mentions_dotnet(assessment.core_language or "")
        or (core_stack and mentions_dotnet(str(core_stack[0])))
    )
    required = any(mentions_dotnet(str(skill)) for skill in facts.required_skills)
    return explicit, required


def _core_matches(
    job: "JobOpportunity", assessment: "JobFitAssessment", matcher: Callable[[str], bool]
) -> bool:
    """Sinaliza que Java/FrontEnd é o core, não mera citação na descrição."""
    core_stack = list(assessment.core_stack or [])
    return bool(
        matcher(job.title or "")
        or matcher(assessment.core_language or "")
        or (core_stack and matcher(str(core_stack[0])))
    )


def stack_mismatch_reason(
    job: "JobOpportunity",
    facts: "ExtractedJobFacts",
    assessment: "JobFitAssessment | None",
) -> str | None:
    """Veto semântico de core: Java/FrontEnd/AI focados reprovam; .NET protege.

    O score não participa: é o core da vaga que não bate com a especialização.
    Se .NET/C# é o core, Java/React/AI citados como complemento não geram veto —
    nem quando o analista marcou 'misaligned' por engano. No título, porém, o
    que aparece primeiro define a função principal: 'AI Engineer (.NET)' é
    focada em AI, enquanto 'Senior .NET Engineer with AI integration' não é.
    """
    if assessment is None:
        return None
    # AI no título vem antes da proteção .NET: o prefixo indica a função.
    if is_ai_focused(job.title or ""):
        return (
            "A vaga é focada em Inteligência Artificial/ML como função principal "
            "do título, sem C#/.NET como core."
        )
    explicit_dotnet, required_dotnet = _dotnet_core_signals(job, facts, assessment)
    if explicit_dotnet:
        return None
    if _core_matches(job, assessment, mentions_java):
        return "A vaga exige Java como linguagem principal (core), sem C#/.NET."
    if _core_matches(job, assessment, mentions_frontend):
        return (
            "A vaga é focada em FrontEnd (React/Angular/Vue/UI como core), "
            "sem C#/.NET."
        )
    if _core_matches(job, assessment, mentions_ai):
        return (
            "A vaga é focada em Inteligência Artificial/ML (core), sem C#/.NET."
        )
    if required_dotnet:
        return None
    # Fatos extraídos da descrição cobrem títulos genéricos ("Software
    # Engineer") em que o analista não preencheu core_language/core_stack.
    if any(mentions_java(str(skill)) for skill in facts.required_skills):
        return "A vaga exige Java como linguagem principal (core), sem C#/.NET."
    if any(mentions_frontend(str(skill)) for skill in facts.required_skills):
        return (
            "A vaga é focada em FrontEnd (React/Angular/Vue/UI como core), "
            "sem C#/.NET."
        )
    if any(mentions_ai(str(skill)) for skill in facts.required_skills):
        return "A vaga exige Inteligência Artificial/ML como core, sem C#/.NET."
    if assessment.language_fit == "misaligned":
        return job_side_language_reason(assessment)
    return None


_EASY_MARKS = ("easy apply", "candidatura rápida", "candidatura rapida")
_EXTERNAL_MARKS = (
    "apply on company", "company website", "site da empresa", "apply externally",
)

# Liderança/gestão explícita no título. Mentoria no texto NÃO entra aqui:
# isso é avaliado pelo LLM com evidência em `explicit_leadership_role`.
_LEADERSHIP_TITLE_RE = re.compile(
    r"\b(?:manager|head\s+of|director|vice\s+president|vp|"
    r"tech(?:nical)?\s+lead|team\s+lead|lead)\b",
    re.IGNORECASE,
)


def _required_domains_missing(
    facts: "ExtractedJobFacts", settings: Settings
) -> list[str]:
    """Domínios EXIGIDos pela vaga que não estão cobertos pelo perfil.

    Usa as restrições da vaga como critério: só o que é exigência enters here.
    Diferenciais ficam em `preferred_domain_experience` e nunca são retornados.
    """
    known = [item.strip().lower() for item in settings.profile.domain_experience if item.strip()]
    missing: list[str] = []
    for raw in facts.required_domain_experience:
        domain = (raw or "").strip().lower()
        if not domain:
            continue
        domain_tokens = set(re.findall(r"[a-z]+", domain))
        covered = any(
            domain in item
            or item in domain
            or bool(domain_tokens & set(re.findall(r"[a-z]+", item)))
            for item in known
        )
        if not covered:
            missing.append(raw.strip())
    return missing


def _is_known_portal(text: str, settings: Settings) -> bool:
    return any(
        portal and portal.lower() in text
        for portal in settings.application.known_portals
    )


def classify_channel(job: JobOpportunity, settings: Settings) -> ApplicationChannel:
    """Canal derivado do fato explícito ``easy_apply`` — nunca inferido.

    A tri-estado ``easy_apply`` é a única fonte autoritativa para o canal do
    LinkedIn: True somente com o botão visto na sessão logada (celular ou
    Chrome), False quando visto e ausente, None quando ainda não analisado.
    HTML público, texto, empresa e URL nunca transformam uma vaga em Easy Apply.
    """
    if job.easy_apply is True:
        return "easy_apply"

    text = " ".join(
        [job.company, job.title, job.description or "", job.url or ""]
    ).lower()
    marker = (job.apply_marker or "").lower()

    # Portais externos têm evidência própria e não dependem do bool de Easy Apply.
    if marker and any(mark in marker for mark in _EXTERNAL_MARKS):
        return "known_portal" if _is_known_portal(text, settings) else "external_portal"
    if _is_known_portal(text, settings):
        return "known_portal"
    if any(mark in text for mark in _EXTERNAL_MARKS):
        return "external_portal"
    return "unknown"


class JobQualificationAgent:
    def __init__(
        self,
        llm: LLMRouter,
        profile: CandidateProfile,
        repo: Repository,
        settings: Settings,
        bus: EventBus | None = None,
        dependencies: "DependencyAgent | None" = None,
        eligibility: "EligibilityAgent | None" = None,
        suppression: CompanySuppressionAgent | None = None,
        memory: QualificationMemory | None = None,
    ) -> None:
        self.llm = llm
        self.profile = profile
        self.repo = repo
        self.settings = settings
        self.bus = bus
        self.dependencies = dependencies
        self.eligibility = eligibility
        self.suppression = suppression
        self.memory = memory
        self.trace_id = ""
        # Preenchido quando uma rodada em lote é interrompida por falha da LLM.
        self.last_error = ""
        # Usado apenas pela reclassificação explícita com --ignore-recency:
        # analisa o histórico da base sem aplicar a janela de 6h. A fila de
        # candidaturas continua revalidando a idade antes de qualquer envio.
        self.ignore_recency = False
        # Modo auditoria: marca cada vaga processada individualmente, sem
        # esperar o fim do lote (útil para acompanhar e retomar execuções).
        self.audit_mode = False

    def _stale_reason(self, job: JobOpportunity) -> str | None:
        if self.ignore_recency:
            return None
        return stale_reason(job, self.settings)

    @staticmethod
    def _apply_salary_indicator(job: JobOpportunity, facts: ExtractedJobFacts) -> None:
        """Salva um indicador salarial textual (hora/ano/mês) na vaga.

        É informativo: nunca entra no score nem motiva rejeição. Preserva o
        texto original publicado para auditoria e comparação futura.
        """
        period = (facts.salary_period or "").strip().lower()
        if "hour" in period or "hora" in period:
            basis = "hourly"
        elif "year" in period or "ano" in period or "annual" in period:
            basis = "yearly"
        elif "month" in period or "mês" in period or "mes" in period:
            basis = "monthly"
        else:
            basis = "unknown"

        parts = [p.strip() for p in (job.salary_raw or "").split() if p.strip()]
        lo = facts.salary_min
        hi = facts.salary_max
        amount = (
            f"{int(lo):,}".replace(",", ".")
            if lo is not None and lo == hi
            else (
                f"{int(lo):,}".replace(",", ".") + "-" + f"{int(hi):,}".replace(",", ".")
                if lo is not None and hi is not None
                else ""
            )
        )
        currency = facts.currency or job.currency or ""
        text = " ".join(parts) or " ".join(
            p for p in [f"{currency} {amount}".strip(), basis] if p
        )
        # Normaliza o separador decimal para formato legível (10.000 = dez mil).
        if amount and currency and not job.salary_raw:
            text = f"{currency} {amount} ({basis})"
        job.salary_basis = basis
        job.salary_text = text.strip()
        job.salary_disclosed = bool(text.strip()) and (
            facts.salary_present or lo is not None or hi is not None
        )
        if facts.currency and not job.currency:
            job.currency = facts.currency
        if lo is not None and job.salary_min is None:
            job.salary_min = lo
        if hi is not None and job.salary_max is None:
            job.salary_max = hi

    def _score(
        self,
        job: JobOpportunity,
        facts: ExtractedJobFacts,
        assessment: JobFitAssessment | None = None,
    ) -> QualificationResult:
        weights = self.settings.qualification.weights
        breakdown: dict[str, int] = {}
        reasons: list[str] = []

        # Nível sem especificação é prioridade; sênior continua elegível, mas
        # fica atrás na ordem de candidatura (não é motivo de rejeição).
        job.level_priority = {
            "unknown": 0, "mid": 1, "senior": 2, "junior": 3,
            "lead": 4, "principal": 4,
        }.get(facts.seniority, 0)
        if facts.seniority == "senior":
            reasons.append("nível sênior: elegível, não prioritário")
        elif facts.seniority == "unknown":
            reasons.append("sem nível explícito: prioridade")

        leadership_title = bool(_LEADERSHIP_TITLE_RE.search(job.title or ""))
        leadership_explicit = leadership_title or facts.explicit_leadership_role
        if leadership_explicit:
            evidence = facts.leadership_evidence.strip()
            reasons.append(
                "liderança explícita" + (f": {evidence[:100]}" if evidence else "")
            )

        missing_domains = _required_domains_missing(facts, self.settings)
        if missing_domains:
            reasons.append("setor exigido ausente no perfil: " + ", ".join(missing_domains[:4]))

        tech = [known.lower() for known in self.profile.all_technologies()]
        required = [s.strip().lower() for s in facts.required_skills if s.strip()]
        tech_ratio: float | None = None
        if assessment is not None and assessment.technical_fit > 0:
            # Analista semântico: plataformas não descontam, conceitos
            # equivalentes atendem e a linguagem core é julgada pelo LLM.
            tech_ratio = max(0.0, min(1.0, assessment.technical_fit))
            breakdown["technical"] = round(weights.technical * tech_ratio)
            reasons.append(f"análise semântica: {tech_ratio:.0%} de aderência técnica")
            if assessment.core_language:
                reasons.append(
                    f"linguagem core: {assessment.core_language} ({assessment.language_fit})"
                )
            for verdict in assessment.requirement_verdicts[:8]:
                if verdict.verdict == "platform_or_tool":
                    reasons.append(f"sem desconto (plataforma/ferramenta): {verdict.requirement}")
                elif verdict.verdict == "equivalent_concept":
                    reasons.append(f"conceito equivalente atendido: {verdict.requirement}")
            if assessment.reasoning:
                reasons.append("cross-check: " + assessment.reasoning[:280])
        elif required:
            # Fallback determinístico (usado se o LLM falhar).
            hits = sum(
                1 for skill in required
                if any(technologies_match(skill, known) for known in tech)
            )
            tech_ratio = hits / len(required)
            breakdown["technical"] = round(weights.technical * tech_ratio)
            reasons.append(f"tecnologias (fallback): {hits}/{len(required)} requisitos")
        else:
            breakdown["technical"] = round(weights.technical * 0.6)
            reasons.append("requisitos técnicos não informados")

        seniority = facts.seniority
        if seniority in {"senior", "lead"}:
            breakdown["seniority"] = weights.seniority
        elif seniority == "principal":
            breakdown["seniority"] = round(weights.seniority * 0.7)
        elif seniority == "mid":
            breakdown["seniority"] = round(weights.seniority * 0.8)
        elif seniority == "junior":
            breakdown["seniority"] = round(weights.seniority * 0.4)
        else:
            breakdown["seniority"] = round(weights.seniority * 0.6)

        years = facts.years_required
        if years is None:
            breakdown["experience"] = round(weights.experience * 0.6)
            reasons.append("anos de experiência não informados")
        elif years <= self.profile.years_experience:
            breakdown["experience"] = weights.experience
        elif years <= self.profile.years_experience + 2:
            breakdown["experience"] = round(weights.experience * 0.6)
        else:
            breakdown["experience"] = round(weights.experience * 0.2)

        search_cfg = self.settings.search
        reject_cfg = self.settings.qualification.reject
        # Dupla validação: o texto público (título/local/descrição) TEM que
        # comprovar remoto integral. Um `is_remote=True` do LLM jamais
        # sobrescreve "Hybrid"/"On-site" presente no card ou na descrição.
        remote_public = is_remote_public(
            job.title, job.location, job.description or ""
        )
        remote_by_location = is_remote_location(job.location, job.title)
        remote_ok = remote_public and (
            facts.is_remote is True or (
                facts.is_remote is None and remote_by_location
            )
        )

        # Moeda explícita vale o que está no card/descrição. Só se não houver
        # nada explícito usamos o ISO extraído pelo LLM como fallback.
        salary_text = " ".join([job.salary_raw or "", job.description or ""])
        currencies = detect_currencies(salary_text)
        if not currencies:
            currencies = detect_currencies(
                " ".join(p for p in [facts.currency or "", job.currency or ""] if p)
            )
            # O extrator pode devolver um ISO válido que ainda não tem regex
            # própria (ex.: CHF). Só entra como fallback: se o texto já trouxe
            # moeda explícita (ex.: BRL), ela prevalece sobre o campo do LLM.
            currencies |= explicit_currency_codes(facts.currency, job.currency)
        allowed_currencies = {code.upper() for code in search_cfg.currencies}
        currency_reject = currency_rejection_reason(
            currencies,
            allowed_currencies,
            is_national_or_portuguese_posting(
                job.location, f"{job.title} {job.description or ''}"
            ),
        )

        # A moeda continua FORA do score (salário é informativo), mas passou a
        # ser critério: USD/EUR, com evidência explícita em vaga PT-BR/BR.
        signal, multi = currency_indicator(currencies, facts.salary_present)
        job.currency_signal = signal
        job.multi_currency = multi

        target_locations = [loc.lower() for loc in search_cfg.locations]
        job_location = (job.location or "").lower()
        if remote_ok:
            breakdown["international"] = weights.international
        elif facts.location_eligible:
            breakdown["international"] = weights.international
        elif any(loc in job_location for loc in target_locations):
            breakdown["international"] = weights.international
        elif job_location:
            breakdown["international"] = round(weights.international * 0.3)
        else:
            breakdown["international"] = round(weights.international * 0.5)

        # Nível de idioma (fluent/C1/C2/native) NUNCA desconta: apenas a
        # identidade do idioma importa. Francês/alemão etc. já viram restrição
        # no EligibilityAgent; aqui só não recebem os pontos de idioma.
        accepted_langs = {
            _normalize_language(x) for x in self.settings.eligibility.accepted_languages
        }
        langs = [_normalize_language(lang) for lang in facts.language_requirements]
        if not langs or all(lang in accepted_langs for lang in langs if lang):
            breakdown["language"] = weights.language
        else:
            breakdown["language"] = 0

        score = min(100, sum(breakdown.values()))

        # Veto de core calculado à parte: se não houver conflito de stack, a
        # cadeia abaixo continua avaliando tecnologia, remoto, moeda, etc.
        stack_reject = stack_mismatch_reason(job, facts, assessment)
        reject_reason: str | None = None
        stale = self._stale_reason(job)
        if stale:
            reject_reason = stale
        elif leadership_explicit and reject_cfg.explicit_leadership_role:
            reject_reason = "cargo de liderança explícita (tech lead/manager)"
        elif (
            job.has_restriction
            and reject_cfg.eligibility_restriction
        ):
            reject_reason = f"restrições de elegibilidade: {job.restriction_details}"
        elif missing_domains and reject_cfg.missing_required_domain_experience:
            reject_reason = (
                "exige experiência em setor ausente no perfil: "
                + ", ".join(missing_domains)
            )
        elif stack_reject:
            # Java/FrontEnd focados reprovam; em vaga .NET são complementares.
            reject_reason = stack_reject
        elif (
            tech_ratio is not None
            and reject_cfg.technology_mismatch
            and tech_ratio < self.settings.qualification.min_technology_match
        ):
            reject_reason = f"tecnologias incompatíveis ({tech_ratio:.0%} dos requisitos)"
        elif search_cfg.remote_only and reject_cfg.non_remote and not remote_ok:
            reject_reason = (
                "vaga híbrida/presencial ou sem evidência pública de remoto integral"
            )
        elif reject_cfg.salary_currency_mismatch and currency_reject:
            reject_reason = currency_reject
        elif (
            not search_cfg.allow_national_remote
            and is_national_location(job.location, search_cfg.national_countries)
        ):
            reject_reason = "vaga nacional desabilitada na configuração"
        elif (
            facts.work_authorization_required
            and reject_cfg.work_authorization_required
            and not work_authorization_authorized(job.location, self.settings)
        ):
            # Texto genérico ("autorizado a trabalhar no país da vaga") não é
            # barreira quando o país/cidade é coberto pelo perfil (ex.: Manaus/AM).
            reject_reason = "exige autorização de trabalho não confirmada"
        elif (
            facts.is_remote is False
            and facts.location_eligible is False
            and reject_cfg.onsite_in_unsupported_country
        ):
            reject_reason = "presencial em país não elegível"
        # Não existe mais veto por nível de idioma: fluent/C1/C2/native não
        # barram. Idiomas fora de accepted_languages já viraram restrição no
        # EligibilityAgent; aqui apenas não pontuam.

        decision = "SKIP" if reject_reason or score < self.settings.qualification.min_score else "APPLY"
        if reject_reason:
            reasons.append(reject_reason)
        return QualificationResult(
            score=score,
            breakdown=breakdown,
            decision=decision,
            reasons=reasons,
            hard_reject_reason=reject_reason,
        )

    def _assess_fit(
        self, job: JobOpportunity, facts: ExtractedJobFacts, memory_context: str = ""
    ) -> JobFitAssessment | None:
        """Analista semântico: cruza vaga com perfil completo (CV + LinkedIn).

        Usa o mesmo DGX, mas com prompt e schema próprios. Se falhar, o score
        cai para o fallback determinístico — nunca outro LLM, nunca silêncio.
        """
        try:
            assessment = self.llm.structured(
                fit_messages(
                    title=job.title,
                    company=job.company,
                    description=job.description or job.title,
                    profile_json=profile_context(self.profile),
                    extracted_facts=facts.model_dump_json(),
                    memory_context=memory_context,
                ),
                JobFitAssessment,
            )
            # Defesa contra backend/fake que ignore o schema pedido: nunca
            # transformar um ExtractedJobFacts em assessment.
            if not isinstance(assessment, JobFitAssessment):
                return None
        except Exception as exc:
            log_event(
                "fit_assessment_failed",
                external_id=job.external_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None
        log_event(
            "fit_assessment_ok",
            external_id=job.external_id,
            core_language=assessment.core_language,
            language_fit=assessment.language_fit,
            technical_fit=assessment.technical_fit,
        )
        return assessment

    def qualify(self, job: JobOpportunity) -> JobOpportunity:
        # Retrofill: vagas descobertas antes deste campo não têm o valor gravado.
        if not job.location_normalized:
            job.location_normalized = normalize_location(job.location)
        memory_context = self.memory.context_for(job) if self.memory is not None else ""
        job.memory_context = memory_context
        if memory_context:
            log_event(
                "classification_memory_used",
                external_id=job.external_id,
                chars=len(memory_context),
            )
        facts = self.llm.structured(
            extraction_messages(
                job.title,
                job.company,
                job.description or job.title,
                location=job.location,
                salary_raw=job.salary_raw,
                memory_context=memory_context,
            ),
            ExtractedJobFacts,
        )
        job.analysis = JobAnalysis.model_validate(facts.model_dump())
        self._apply_salary_indicator(job, facts)
        job.status = JobStatus.ANALYZED

        # Analista de currículo: substitui o checklist cego por cross-check
        # semântico. Persistimos o veredito para auditoria da decisão.
        assessment = self._assess_fit(job, facts, memory_context)
        job.fit_assessment = assessment

        # Easy Apply NÃO é sondado na classificação. Só vagas que passaram no
        # score e nas barreiras de elegibilidade abrem o Chrome logado, e a
        # detecção acontece na MESMA abertura usada para preparar a candidatura
        # (BrowserApplyAgent). Assim, vagas reprovadas nunca consomem CDP.
        job.channel = classify_channel(job, self.settings)
        job.region = detect_region(job.location)

        if self.eligibility is not None:
            company = self.repo.get_company_by_name(job.company)
            evaluation = self.eligibility.evaluate(job, facts, company)
            if evaluation.needs_company_check:
                company = self.eligibility.enrich_company(job, company)
                if company is not None:
                    evaluation = self.eligibility.apply_company_policy(evaluation, company)
            job.has_restriction = evaluation.has_restriction
            job.restriction_kinds = evaluation.restriction_kinds
            job.restriction_details = evaluation.summary
            if evaluation.has_restriction:
                log_event(
                    "eligibility_restriction_found",
                    kinds=evaluation.restriction_kinds,
                    details=evaluation.details,
                    external_id=job.external_id,
                )
                # Ponta 1 da supressão: só restrições geográficas/visto podem
                # marcar a empresa, e somente depois de conferir outras vagas.
                if self.suppression is not None:
                    try:
                        self.suppression.maybe_suppress(
                            job, evaluation.restriction_kinds, evaluation.summary
                        )
                    except Exception as exc:
                        log_event(
                            "company_suppression_error",
                            company=job.company,
                            error=f"{type(exc).__name__}: {exc}",
                        )

        result = self._score(job, facts, assessment)
        job.score = result.score
        job.score_breakdown = result.breakdown
        job.qualification = result
        if result.decision == "SKIP" and result.hard_reject_reason and not job.has_restriction:
            # Não é barreira de elegibilidade (não marca empresa), mas precisa
            # explicar na mesma coluna por que o score alto não virou aprovação.
            job.restriction_details = f"análise: {result.hard_reject_reason}"
        job.status = (
            JobStatus.REJECTED_BY_FILTER if result.decision == "SKIP" else JobStatus.QUALIFIED
        )
        if self.audit_mode:
            job.audit_classified_at = datetime.now()
        self.repo.update_job(job)
        if result.decision == "APPLY" and self.dependencies is not None:
            try:
                self.dependencies.classify(job)
            except Exception as exc:  # dependências nunca derrubam a qualificação
                log_event("dependency_classification_error", error=str(exc))
        log_event(
            "job_qualified", company=job.company, title=job.title,
            score=result.score, decision=result.decision,
        )
        if self.bus:
            # job_id é o contrato do worker de aplicação: o evento acorda o
            # agente, mas a fonte da verdade continua sendo o banco.
            self.bus.emit(
                "job.qualified", job_id=job.id, company=job.company, title=job.title,
                score=result.score, decision=result.decision,
            )
        return job

    def _process_one(self, job: JobOpportunity) -> tuple[str, bool]:
        """Qualifica uma vaga (triagem barata de recência + LLM local).

        Retorna ``(decisão, aprovada)``. Usado tanto no modo em lote quanto no
        modo incremental em background.
        """
        # Vagas legadas/sem trace recebem o ID da execução que as analisou.
        job.trace_id = job.trace_id or self.trace_id
        stale = self._stale_reason(job)
        if stale:
            # Triagem barata: fora da janela de 6h nem chega à LLM local.
            # Vaga antiga é histórico, não rejeição de perfil.
            job.status = JobStatus.EXPIRED
            job.channel = classify_channel(job, self.settings)
            job.region = detect_region(job.location)
            job.score = 0
            job.qualification = QualificationResult(
                score=0,
                decision="SKIP",
                reasons=[stale],
                hard_reject_reason=stale,
            )
            if self.audit_mode:
                job.audit_classified_at = datetime.now()
            self.repo.update_job(job)
        else:
            self.qualify(job)
        decision = job.qualification.decision if job.qualification else "SKIP"
        return decision, job.status == JobStatus.QUALIFIED

    def _emit_progress(
        self, current: int, total: int, job: JobOpportunity, decision: str, qualified: int
    ) -> None:
        if self.bus:
            self.bus.emit(
                "qualification.progress",
                current=current,
                total=total,
                company=job.company,
                title=job.title,
                decision=decision,
                qualified=qualified,
            )

    def run(self, limit: int | None = None, job_ids: list[str] | None = None) -> int:
        """Analisa a fila ``DISCOVERED`` ou apenas os IDs informados.

        ``job_ids`` é usado pela reclassificação para não arrastar vagas que já
        estavam na fila mas não eram alvo da rodada (ex.: reclassificar só um
        Trace ID).
        """
        if job_ids is not None:
            jobs = [
                job
                for job_id in job_ids
                if (job := self.repo.get_job(job_id)) is not None
            ]
            if limit:
                jobs = jobs[:limit]
        else:
            jobs = self.repo.list_jobs(status=JobStatus.DISCOVERED, limit=limit)
        total = len(jobs)
        count = 0
        consecutive_errors = 0
        self.last_error = ""
        for index, job in enumerate(jobs, start=1):
            try:
                decision, approved = self._process_one(job)
                consecutive_errors = 0
            except LLMError as exc:
                # Falha transitória da DGX não deve abortar toda a rodada:
                # a vaga fica DISCOVERED para a próxima execução. Depois de
                # duas falhas seguidas, paramos para não girar em falso.
                consecutive_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                log_event(
                    "qualification_llm_interrompida",
                    company=job.company,
                    title=job.title,
                    error=self.last_error,
                    consecutive=consecutive_errors,
                )
                if consecutive_errors >= 2:
                    self.last_error += (
                        f" — abortado em {index}/{total}; vagas restantes seguem DISCOVERED"
                    )
                    break
                decision, approved = "ERROR", False
            if approved:
                count += 1
            self._emit_progress(index, total, job, decision, count)
        return count

    def run_live(
        self,
        stop_event: object,
        settle_seconds: float = 2.0,
        poll_interval: float = 0.5,
        abort_check: Callable[[], None] | None = None,
    ) -> int:
        """Consome vagas descobertas em background enquanto a busca roda.

        A busca salva a vaga como ``DISCOVERED`` e depois enriquece a descrição
        pela acessibilidade. Para não qualificar com descrição incompleta, só
        processamos vagas cujo ``updated_at`` já tem ``settle_seconds``. Quando
        ``stop_event`` é sinalizado (fim da busca), o restante é drenado sem
        espera — a escrita da descrição já terminou nesse ponto.

        ``stop_event`` é qualquer objeto com ``.wait(timeout)`` e ``.is_set()``
        (``threading.Event`` na prática), mantido genérico para facilitar testes.
        ``abort_check`` permite honrar pause/stop: se lançar, o restante fica
        ``DISCOVERED`` para uma próxima rodada.
        """
        count = 0
        processed = 0
        total_seen = 0
        draining = False
        consecutive_errors = 0
        self.last_error = ""
        while True:
            if abort_check is not None:
                try:
                    abort_check()
                except Exception:
                    return count
            pending = self.repo.list_jobs(status=JobStatus.DISCOVERED)
            total_seen = max(total_seen, processed + len(pending))
            now = datetime.now()
            ready = (
                pending
                if draining
                else [
                    job
                    for job in pending
                    if (now - job.updated_at).total_seconds() >= settle_seconds
                ]
            )
            if not ready:
                if draining:
                    break
                if stop_event.wait(poll_interval):  # type: ignore[attr-defined]
                    draining = True
                continue
            for job in ready:
                # Outra instância pode ter processado a vaga nesse intervalo.
                current = self.repo.get_job(job.id)
                if current is None or current.status != JobStatus.DISCOVERED:
                    continue
                try:
                    decision, approved = self._process_one(current)
                    consecutive_errors = 0
                except LLMError as exc:
                    consecutive_errors += 1
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log_event(
                        "qualification_llm_interrompida",
                        company=current.company,
                        title=current.title,
                        error=self.last_error,
                        consecutive=consecutive_errors,
                    )
                    if consecutive_errors >= 2:
                        self.last_error += (
                            " — qualificação ao vivo interrompida; vaga segue DISCOVERED"
                        )
                        return count
                    decision, approved = "ERROR", False
                processed += 1
                if approved:
                    count += 1
                remaining = self.repo.list_jobs(status=JobStatus.DISCOVERED)
                total_seen = max(total_seen, processed + len(remaining))
                self._emit_progress(processed, total_seen, current, decision, count)
        return count
