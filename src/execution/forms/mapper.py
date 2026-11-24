"""Resolução local de campos a partir do perfil — sem inventar dados."""
from __future__ import annotations

import calendar
import re

from ...core.config import Settings
from ...models.domain import CandidateProfile
from .field import FormField

_LOCAL_RULES: list[tuple[str, str, str]] = [
    (r"\bemail\b|e-mail", "personal.email", "profile.personal.email"),
    (r"\bfirst name\b|\bnome\b.*\bprimeiro\b", "personal.first_name", "profile.personal.first_name"),
    (r"\blast name\b|\bsobrenome\b", "personal.last_name", "profile.personal.last_name"),
    (r"\bphone\b|\btelefone\b|\bcelular\b", "personal.phone_display", "profile.personal.phone"),
    (r"\blinkedin\b", "personal.linkedin", "profile.personal.linkedin"),
    (r"\bgithub\b", "personal.github", "profile.personal.github"),
    (r"\bcity\b|\bcidade\b", "address.city", "profile.personal.location"),
    (r"\bcountry\b|\bpa[ií]s\b", "address.country", "profile.personal.location"),
    (r"\bcurrent company\b|\bempresa atual\b", "current_company", "profile.current_company"),
    (r"\bcurrent title\b|\bcargo atual\b", "current_title", "profile.current_title"),
    (r"\bexperience years\b|\banos de experi", "years_experience", "profile.years_experience"),
]

_GENDER_FIELD = re.compile(r"gender|genero|g nero|sexo", re.IGNORECASE)
_BIRTH_FULL_FIELD = re.compile(
    r"date of birth|birth date|birthday|data de nascimento|nascimento", re.IGNORECASE
)
_BIRTH_MONTH_FIELD = re.compile(r"^\s*month\s*$", re.IGNORECASE)
_BIRTH_DAY_FIELD = re.compile(r"^\s*day\s*$", re.IGNORECASE)
_BIRTH_YEAR_FIELD = re.compile(r"^\s*year\s*$", re.IGNORECASE)

_SENSITIVE = re.compile(
    # Gênero e data de nascimento são dados pessoais sensíveis: o agente não os
    # infere do currículo nem pede ao LLM que os invente.
    r"gender|genero|g nero|sexo|date of birth|birth date|birthday|data de nascimento|"
    r"nascimento|^\s*month\s*$|^\s*day\s*$|^\s*year\s*$|"
    r"termos|privacidade|politica|autorizacao|trabalho|visto|sponsorship|"
    r"sal[aá]rio|pretens[aã]o|contrato|legal|compromisso|mfa|captcha|senha|password",
    re.IGNORECASE,
)


def _get_path(profile: CandidateProfile, path: str) -> object:
    current: object = profile
    for part in path.split("."):
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


def _birth_part(profile: CandidateProfile, parte: str) -> str:
    """Deriva mês/dia/ano de ``personal.birth_date`` quando não há campo explícito."""
    raw = str(_get_path(profile, "personal.birth_date") or "")
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if not match:
        return ""
    ano, mes, dia = match.groups()
    if parte == "month":
        try:
            return calendar.month_name[int(mes)]
        except (ValueError, IndexError):
            return ""
    if parte == "day":
        return str(int(dia))
    if parte == "year":
        return ano
    return ""


def _sensitive_evidence(
    signature: str, profile: CandidateProfile
) -> tuple[str, str] | None:
    """Só aceita dado sensível se ele estiver cadastrado explicitamente no perfil."""
    if _GENDER_FIELD.search(signature):
        caminho, origem = "personal.gender", "profile.personal.gender"
    elif _BIRTH_FULL_FIELD.search(signature):
        caminho, origem = "personal.birth_date", "profile.personal.birth_date"
    elif _BIRTH_MONTH_FIELD.search(signature):
        caminho, origem = "personal.birth_month", "profile.personal.birth_month"
    elif _BIRTH_DAY_FIELD.search(signature):
        caminho, origem = "personal.birth_day", "profile.personal.birth_day"
    elif _BIRTH_YEAR_FIELD.search(signature):
        caminho, origem = "personal.birth_year", "profile.personal.birth_year"
    else:
        return None

    valor = _get_path(profile, caminho)
    if valor in (None, ""):
        parte = caminho.rsplit(".", 1)[-1].removeprefix("birth_")
        valor = _birth_part(profile, parte)
    if valor in (None, ""):
        return None
    return (str(valor), origem)


def resolve_local(
    field: FormField, profile: CandidateProfile, settings: Settings
) -> tuple[str, str, bool, str]:
    """Retorna (valor, origem, precisa_humano, categoria)."""
    signature = field.signature()
    if _SENSITIVE.search(signature):
        evidencia = _sensitive_evidence(signature, profile)
        if evidencia is not None:
            valor, origem = evidencia
            return (valor, origem, False, "general")
        return ("", "politica:acao_sensivel", True, "sensitive")

    for pattern, path, source in _LOCAL_RULES:
        if re.search(pattern, signature):
            value = _get_path(profile, path)
            if value not in (None, ""):
                return (str(value), source, False, "general")

    if re.search(r"c\s*digo do pa\s*s|country code|codigo do pais", signature):
        location = str(_get_path(profile, "personal.location") or "")
        country = location.split(",")[-1].strip().lower()
        for option in field.options:
            if country and country in option.lower():
                return (option, "profile.personal.location", False, "general")

    if re.search(r"technolog|tecnolog|skills|compet", signature):
        techs = sorted(profile.all_technologies())
        if techs:
            return (", ".join(techs), "profile.skills", False, "general")

    # Opções de currículo salvo aparecem como radios cujo rótulo é só o nome do
    # arquivo; nem sempre contêm "cv"/"resume" (ex.: "... Software Engineer.pdf").
    is_resume_option = re.search(r"resume|curriculo|curriculum", signature) or (
        field.field_type == "radio" and re.search(r"\b(pdf|docx?|doc)\b", signature)
    )
    if is_resume_option:
        return (profile.documents.get("resume", ""), "profile.documents.resume", False, "resume")

    for rule in profile.answer_rules:
        if re.search(rule.match, signature, re.IGNORECASE):
            category = rule.category
            if (
                rule.requires_human
                or category in _SENSITIVE_CATEGORIES
                or category in settings.approval.always_human
            ):
                return ("", f"rule:{category}", True, category)
            return (str(rule.value), "profile.answer_rules", False, category)

    return ("", "sem_evidencia", True, "unknown")


_SENSITIVE_CATEGORIES = {
    "legal", "work_authorization", "visa_sponsorship", "authentication",
    "captcha_mfa", "profile_changes", "salary_expectation", "sensitive",
}
