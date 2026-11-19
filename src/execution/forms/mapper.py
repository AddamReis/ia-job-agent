"""Resolução local de campos a partir do perfil — sem inventar dados."""
from __future__ import annotations

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

_SENSITIVE = re.compile(
    r"termos|privacidade|pol[ií]tica|autoriza[cç][aã]o|trabalho|visto|sponsorship|"
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


def resolve_local(
    field: FormField, profile: CandidateProfile, settings: Settings
) -> tuple[str, str, bool, str]:
    """Retorna (valor, origem, precisa_humano, categoria)."""
    signature = field.signature()
    if _SENSITIVE.search(signature):
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
