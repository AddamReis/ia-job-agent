"""Schemas auxiliares para extração estruturada pelo LLM."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LanguageRequirement(BaseModel):
    """Idioma exigido pela vaga, com nível e exigência de falante nativo."""

    language: str = ""
    level: Literal["basic", "intermediate", "fluent", "native", "unknown"] = "unknown"
    native_required: bool = False


class ExtractedJobFacts(BaseModel):
    """Contrato rígido de extração. Campos desconhecidos devem ser null/vazio."""

    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    seniority: Literal["junior", "mid", "senior", "lead", "principal", "unknown"] = "unknown"
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
    # Novo campo: a vaga exige patrocínio de visto (diferente de apenas mencionar).
    visa_sponsorship_required: bool | None = None
    language_requirements: list[str] = Field(default_factory=list)
    language_details: list[LanguageRequirement] = Field(default_factory=list)
    # Restrição explícita de residência, ex.: "must reside in the EU".
    residence_restriction: str = ""
    # Países/regiões explicitamente aceitos, ex.: ["Europe", "Worldwide"].
    eligible_regions: list[str] = Field(default_factory=list)
    # "open to candidates worldwide" / "any location" => True.
    worldwide_remote: bool | None = None
    employment_types: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    # true somente se a vaga coloca o candidato COMO tech lead/manager de squad/equipe.
    # Mentoria, orientar outros ou "technical guidance" NAO devem marcar true.
    explicit_leadership_role: bool = False
    leadership_evidence: str = ""
    # Experiência em domínio/indústria (ex.: healthcare, fintech) separada entre
    # exigência (bloqueia se ausente) e diferencial (nao bloqueia).
    required_domain_experience: list[str] = Field(default_factory=list)
    preferred_domain_experience: list[str] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.0


class RequirementVerdict(BaseModel):
    """Veredito semântico de UM requisito da vaga contra o perfil."""

    requirement: str = ""
    verdict: Literal[
        "matched",                 # tecnologia/competência explicitamente no perfil
        "equivalent_concept",      # mesmo conceito/arquitetura (API ≈ API REST, .NET ⊃ C#)
        "platform_or_tool",        # Jenkins/Docker/CI/CD: plataforma, não conta contra
        "nice_to_have_missing",    # diferencial ausente: sem desconto
        "core_missing",            # essencial ausente: desconta
    ] = "core_missing"
    note: str = ""


class JobFitAssessment(BaseModel):
    """Julgamento do analista de currículo: vaga × perfil completo.

    Substitui o matching determinístico por um cross-check com discernimento:
    plataformas não penalizam, conceitos equivalentes atendem e a linguagem
    principal da vaga define o core.
    """

    core_language: str = ""
    # aligned = C#/.NET é o core; complementary = outra linguagem apenas
    # complementar (inclusive Java/React/TS em vaga full-stack .NET);
    # misaligned = Java/FrontEnd/Python como core, ou stack oposta.
    language_fit: Literal["aligned", "complementary", "misaligned"] = "aligned"
    language_reason: str = ""
    # Fração de aderência técnica JÁ ajustada pelas regras semânticas (0..1).
    technical_fit: float = 0.0
    requirement_verdicts: list[RequirementVerdict] = Field(default_factory=list)
    core_stack: list[str] = Field(default_factory=list)
    reasoning: str = ""
    confidence: float = 0.0


class CompanyEligibilityFacts(BaseModel):
    """Fatos de elegibilidade extraídos da página pública da empresa."""

    hiring_regions: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    visa_policy: str = ""
    notes: str = ""
    confidence: float = 0.0


class AnswerDecision(BaseModel):
    """Resposta proposta para um campo de formulário."""

    value: str = ""
    source: str = ""
    needs_human: bool = False
    reason: str = ""


class UiActionDecision(BaseModel):
    """Próxima ação segura em um diálogo/formulário, escolhida pelo agente."""

    action: str = ""
    source: str = ""
    needs_human: bool = False
    reason: str = ""
