"""Modelos de domínio — fonte de verdade tipada do agente."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..core.state import JobStatus
from .schemas import JobFitAssessment

ApplicationChannel = Literal[
    "easy_apply", "known_portal", "external_portal", "unknown"
]


class PersonalInfo(BaseModel):
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    phone_display: str = ""
    gender: str = ""
    birth_date: str = ""
    birth_month: str = ""
    birth_day: str = ""
    birth_year: str = ""
    linkedin: str = ""
    github: str = ""
    location: str = ""


class Experience(BaseModel):
    company: str
    title: str
    period: str = ""
    location: str = ""
    sector: str = ""
    summary: str = ""
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Education(BaseModel):
    course: str = ""
    level: str = ""
    institution: str = ""
    year: str = ""


class Certification(BaseModel):
    name: str
    issuer: str = ""
    year: str = ""


class Language(BaseModel):
    language: str
    level: str


class Fact(BaseModel):
    """Valor com procedência. Sem procedência, a resposta não é usada."""

    value: str
    source: str
    verified: bool = True


class AnswerRule(BaseModel):
    match: str
    value: str | bool
    category: str = "general"
    requires_human: bool = False


class CandidateProfile(BaseModel):
    personal: PersonalInfo = Field(default_factory=PersonalInfo)
    years_experience: int = 0
    years_csharp: int = 0
    current_title: str = ""
    current_company: str = ""
    summary: str = ""
    experiences: list[Experience] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    project_preferences: list[str] = Field(default_factory=list)
    expectations: dict[str, str] = Field(default_factory=dict)
    documents: dict[str, str] = Field(default_factory=dict)
    unverified_claims: list[str] = Field(default_factory=list)
    answer_rules: list[AnswerRule] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)

    def all_technologies(self) -> set[str]:
        values: set[str] = set()
        for techs in self.skills.values():
            values.update(t.strip().lower() for t in techs if t.strip())
        for exp in self.experiences:
            values.update(t.strip().lower() for t in exp.technologies if t.strip())
        return values


class JobAnalysis(BaseModel):
    """Saída estruturada do LLM — apenas fatos extraídos da descrição."""

    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    seniority: str = "unknown"
    years_required: int | None = None
    salary_present: bool = False
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    salary_period: str | None = None
    is_remote: bool | None = None
    location_eligible: bool | None = None
    work_authorization_required: bool | None = None
    visa_sponsorship_mentioned: bool = False
    visa_sponsorship_required: bool | None = None
    language_requirements: list[str] = Field(default_factory=list)
    language_details: list[dict] = Field(default_factory=list)
    residence_restriction: str = ""
    eligible_regions: list[str] = Field(default_factory=list)
    worldwide_remote: bool | None = None
    employment_types: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    # Liderança explícita (tech lead/manager) — diferente de mentoria/coordenação.
    explicit_leadership_role: bool = False
    leadership_evidence: str = ""
    # Experiência de domínio: exigência bloqueia; diferencial apenas informa.
    required_domain_experience: list[str] = Field(default_factory=list)
    preferred_domain_experience: list[str] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0


class QualificationResult(BaseModel):
    score: int = 0
    breakdown: dict[str, int] = Field(default_factory=dict)
    decision: Literal["APPLY", "SKIP"] = "SKIP"
    reasons: list[str] = Field(default_factory=list)
    hard_reject_reason: str | None = None


class ClassificationValidation(BaseModel):
    """Revisão humana de uma classificação, com o antes/depois auditável."""

    validated_at: datetime = Field(default_factory=datetime.now)
    correct: bool
    reason: str = ""
    before_status: str = ""
    after_status: str = ""
    before_decision: str = ""
    after_decision: str = ""
    before_score: int | None = None
    after_score: int | None = None
    reclassified: bool = False


class JobOpportunity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # Correlaciona o card descoberto à execução que o encontrou (rastreabilidade).
    trace_id: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    # ``País - informação original da vaga`` (ex.: "Brasil - Manaus AM").
    # Gravado na descoberta e retrofill na qualificação para que a análise
    # saiba se a vaga é brasileira ou internacional.
    location_normalized: str = ""
    url: str = ""
    external_id: str = ""
    source: str = "linkedin"
    posted_at: datetime | None = None
    posted_precision: Literal["hour", "day", "week", "month", "unknown"] = "unknown"
    posted_raw: str = ""
    salary_raw: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    employment_type: str = ""
    description: str = ""
    status: JobStatus = JobStatus.DISCOVERED
    channel: ApplicationChannel = "unknown"
    # Marcador bruto do cartão ("Easy Apply", "Apply on company website", ...).
    apply_marker: str = ""
    # Tri-estado: None = ainda NÃO analisado no celular; True = botão de
    # candidatura simplificada visto na tela; False = visto e NÃO é Easy Apply.
    # A API pública NÃO informa Easy Apply — só o app logado no celular.
    easy_apply: bool | None = None
    # Origem da evidência: "device_screen", "browser_apply" ou "" (não analisado).
    easy_apply_source: str = ""
    # Relacionamento com a entidade empresa e link público de cadastro extraído
    # da descrição (quando a vaga redireciona para o site da contratante).
    company_id: str = ""
    external_apply_url: str = ""
    external_apply_source: str = ""
    # Barreiras de elegibilidade detectadas na análise (visto, idioma, residência).
    has_restriction: bool = False
    restriction_details: str = ""
    restriction_kinds: list[str] = Field(default_factory=list)
    # Flag de candidatura já realizada (qualquer canal: simplificado ou portal).
    has_applied: bool = False
    applied_at: datetime | None = None
    # Indicador salarial normalizado: texto original + base (hora/ano/mês).
    salary_disclosed: bool = False
    salary_basis: str = ""  # hourly | yearly | monthly | unknown
    salary_text: str = ""
    # Origem da busca (para agrupar o resumo da execução por termo/localidade).
    search_query: str = ""
    search_location: str = ""
    # Motivo legível quando a candidatura falhou ( CAPTCHA, portal, etc. ).
    apply_failure_reason: str = ""
    # Região inferida da localização (americas_north, australia, new_zealand, europe, brazil, other).
    region: str = "other"
    # Prioridade por nível explícito: 0=sem nível (prioritário), 1=mid,
    # 2=senior (elegível, mas não prioritário), 3=junior. Nunca é filtro de exclusão.
    level_priority: int = 0
    # Indicador de moeda detectada na vaga/descrição — informativo, NÃO filtro de aprovação.
    currency_signal: str = ""
    multi_currency: bool = False
    # Contexto aprendido de classificações/decisões anteriores, injetado no DGX
    # como informação auditável (nunca substitui regra dura nem peso do score).
    memory_context: str = ""
    # Última revisão humana da classificação. Quando corrige, o antes/depois
    # fica gravado na própria vaga para auditoria e para a PWA.
    validation: ClassificationValidation | None = None
    score: int | None = None
    score_breakdown: dict[str, int] = Field(default_factory=dict)
    analysis: JobAnalysis | None = None
    # Analise semantica curriculo x vaga (plataformas nao descontam, conceitos
    # equivalentes atendem, linguagem principal define o core).
    fit_assessment: JobFitAssessment | None = None
    qualification: QualificationResult | None = None
    dedupe_hash: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    # Auditoria do histórico: último reprocessamento sem entrada na fila de Apply.
    audit_classified_at: datetime | None = None


class Company(BaseModel):
    """Empresa relacionada às vagas, com histórico de cadastro e testes."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = ""
    name_normalized: str = ""
    has_account: bool = False
    # Datas em branco significam cadastro antigo, sem data conhecida.
    account_filled_at: datetime | None = None
    last_form_filled_at: datetime | None = None
    technical_test: Literal["none", "known", "unknown"] = "unknown"
    technical_test_notes: str = ""
    # Conhecimento de elegibilidade alimentado pela página pública da empresa.
    hiring_regions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    visa_policy: str = ""
    eligibility_notes: str = ""
    # Supressao TEMPORARIA por periodo: empresa com restricao geografica
    # comprovada (ex.: so aceita NJ/NY/Charlotte). Nao e blacklist: apos
    # suppressed_until a empresa volta a ser avaliada normalmente.
    suppressed_until: datetime | None = None
    suppression_reason: str = ""
    suppression_checked_at: datetime | None = None
    suppression_history: list[str] = Field(default_factory=list)
    # Portal de confiança: cadastro/proposta já aprovados. O fast lane usa
    # ``trusted_portal_config`` com filtros simples em vez da classificação cheia.
    is_trusted_portal: bool = False
    trusted_portal_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ApplicationDependency(BaseModel):
    """Etapa pendente de uma candidatura, com dono explícito (humano ou agente)."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    opportunity_id: str = ""
    application_id: str = ""
    company: str = ""
    title: str = ""
    kind: Literal[
        "video_self_intro",
        "camera_test",
        "technical_test_quiz",
        "technical_test_code",
        "work_authorization",
        "other",
    ] = "other"
    owner: Literal["human", "agent"] = "human"
    status: Literal["pending", "in_progress", "done", "blocked"] = "pending"
    description: str = ""
    url: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    notified_at: datetime | None = None
    # Prazo informado pela plataforma (quando houver).
    due_at: datetime | None = None
    # Último lembrete enviado — controla o intervalo entre reenvios.
    last_notified_at: datetime | None = None


class AnswerRecord(BaseModel):
    field: str
    source: str
    value_redacted: str = "<preenchido>"
    required: bool = False
    category: str = "general"
    # Formulários paginados podem repetir a mesma assinatura em steps distintos.
    step: int = 1
    step_label: str = ""


class ApplicationEvidence(BaseModel):
    """Print local de um step preenchido ou da confirmação final do envio.

    O arquivo fica em ``data/evidence/<job_id>/``; o banco guarda apenas o
    caminho relativo e o hash, para não inchar o Postgres com BLOB.
    """

    step: int
    kind: Literal["step", "confirmation"] = "step"
    label: str = ""
    path: str
    sha256: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Application(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    opportunity_id: str = ""
    applied_at: datetime = Field(default_factory=datetime.now)
    company: str = ""
    title: str = ""
    status: str = "SUBMITTED"
    channel: ApplicationChannel = "unknown"
    resume_path: str = ""
    portal_url: str = ""
    answers: list[AnswerRecord] = Field(default_factory=list)
    evidence: list[ApplicationEvidence] = Field(default_factory=list)
    result: str = ""
    notes: str = ""
    success_feedback: str = ""


class ExecutionHistory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # ID da execução usado como trace ID em logs, banco e e-mails.
    trace_id: str = ""
    # Fase do serviço independente: discovery | apply | all.
    phase: str = "all"
    started_at: datetime
    finished_at: datetime | None = None
    status: str = "RUNNING"
    jobs_discovered: int = 0
    jobs_analyzed: int = 0
    jobs_qualified: int = 0
    applications_submitted: int = 0
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


class DailyLimit(BaseModel):
    date: str
    submitted: int = 0
    limit: int = 10

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.submitted)


class QualificationFeedback(BaseModel):
    """Sinal aprendido pela classificação, sempre rastreável e auditável."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    opportunity_id: str = ""
    source: Literal[
        "human_approval", "application_outcome", "reclassification", "human_validation"
    ]
    verdict: Literal["positive", "negative", "attention"]
    topic: str
    detail: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    opportunity_id: str = ""
    company: str = ""
    title: str = ""
    score: int | None = None
    summary: str = ""
    answers: list[AnswerRecord] = Field(default_factory=list)
    sensitive_actions: list[str] = Field(default_factory=list)
    status: Literal["PENDING", "APPROVED", "REJECTED"] = "PENDING"
    created_at: datetime = Field(default_factory=datetime.now)
    resolved_at: datetime | None = None
