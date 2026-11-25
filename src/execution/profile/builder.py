"""Construção do CandidateProfile a partir do CV real e do perfil legado."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from ...core.config import Settings
from ...core.logging import log_event
from ...models.domain import (
    AnswerRule,
    CandidateProfile,
    Certification,
    Education,
    Experience,
    Language,
    PersonalInfo,
)

_SENSITIVE_CATEGORIES = {
    "legal", "work_authorization", "visa_sponsorship", "authentication",
    "captcha_mfa", "profile_changes", "salary_expectation",
}

# Formulários usam no máximo as 3 experiências mais recentes. Nunca criar uma
# quarta entry com dados repetidos ou inventados.
MAX_EXPERIENCES_IN_FORMS = 3


def extract_cv_text(path: Path) -> str:
    """Extrai texto do PDF sem gravar screenshots ou arquivos intermediários."""
    try:
        import pymupdf  # type: ignore

        with pymupdf.open(path) as doc:
            return "\n".join(page.get_text() for page in doc)
    except ImportError:  # pragma: no cover
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _parse_language(raw: str) -> list[Language]:
    result: list[Language] = []
    for part in re.split(r"[;,]", raw or ""):
        part = part.strip()
        if not part:
            continue
        name, _, level = part.partition("(")
        result.append(
            Language(language=name.strip(), level=level.replace(")", "").strip())
        )
    return result


def profile_identity_evidence(settings: Settings) -> dict[str, str]:
    """Dados de identidade explicitamente cadastrados no perfil legado.

    O banco é a fonte corrente do perfil, mas campos sensíveis e o sobrenome
    legal precisam refletir o que o operador cadastrou no YAML. Esta função
    devolve apenas chaves conhecidas, nunca inferidas do currículo.
    """
    raw = _load_yaml(settings.resolve(settings.profile.legacy_profile)).get(
        "personal", {}
    )
    keys = (
        "full_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "phone_display",
        "gender",
        "birth_date",
        "birth_month",
        "birth_day",
        "birth_year",
    )
    return {key: str(raw.get(key, "") or "") for key in keys}


def profile_project_preferences(settings: Settings) -> list[str]:
    """Preferências de projeto explicitamente cadastradas no perfil legado."""
    raw = _load_yaml(settings.resolve(settings.profile.legacy_profile))
    values = raw.get("preferences", raw.get("personal", {}).get("preferences", []))
    if isinstance(values, str):
        values = [item.strip() for item in re.split(r"[;,]", values)]
    return [str(item).strip() for item in (values or []) if str(item).strip()]


def profile_human_evidence(settings: Settings) -> list[AnswerRule]:
    """Regras sensíveis decididas uma a uma pelo operador, fora do git.

    O perfil corrente vem do banco; esta função também é chamada pelo
    Orchestrator para sincronizar o arquivo local com o perfil persistido.
    """
    arquivo = settings.resolve(settings.profile.human_evidence_file)
    try:
        brutas = (
            json.loads(arquivo.read_text(encoding="utf-8")) if arquivo.exists() else []
        )
    except (OSError, ValueError) as exc:
        log_event("human_evidence_file_error", error=type(exc).__name__)
        return []
    regras: list[AnswerRule] = []
    for rule in brutas if isinstance(brutas, list) else []:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match", "")).strip()
        if not match:
            continue
        regras.append(
            AnswerRule(
                match=match,
                value=rule.get("value", ""),
                category="human_evidence",
                requires_human=False,
            )
        )
    if regras:
        log_event("human_evidence_rules_loaded", total=len(regras))
    return regras


def build_profile(settings: Settings) -> CandidateProfile:
    legacy = _load_yaml(settings.resolve(settings.profile.legacy_profile))
    knowledge = _load_yaml(settings.resolve(settings.profile.legacy_knowledge))

    personal_raw = legacy.get("personal", {})
    links_raw = legacy.get("links", {})
    address_raw = legacy.get("address", {})
    experience_raw = legacy.get("experience", {})
    edu_raw = legacy.get("education", {})
    expectations_raw = legacy.get("expectations", {})
    docs_raw = legacy.get("documents", {})
    pessoal_raw = knowledge.get("pessoal", {})
    exp_total = knowledge.get("experiencia_total", {})

    personal = PersonalInfo(
        full_name=personal_raw.get("full_name") or pessoal_raw.get("nome", ""),
        first_name=personal_raw.get("first_name", ""),
        last_name=personal_raw.get("last_name", ""),
        email=personal_raw.get("email") or pessoal_raw.get("email", ""),
        phone=personal_raw.get("phone", ""),
        phone_display=pessoal_raw.get("telefone")
        or personal_raw.get("phone_display", ""),
        gender=personal_raw.get("gender", ""),
        birth_date=personal_raw.get("birth_date", ""),
        birth_month=personal_raw.get("birth_month", ""),
        birth_day=str(personal_raw.get("birth_day", "") or ""),
        birth_year=str(personal_raw.get("birth_year", "") or ""),
        linkedin=links_raw.get("linkedin") or pessoal_raw.get("linkedin", ""),
        github=links_raw.get("github") or pessoal_raw.get("github", ""),
        location=pessoal_raw.get("localizacao")
        or ", ".join(
            filter(
                None,
                [
                    address_raw.get("city", ""),
                    address_raw.get("state", ""),
                    address_raw.get("country", ""),
                ],
            )
        ),
    )

    max_experiences = max(
        1,
        int(
            getattr(
                settings.profile,
                "max_experiences_in_forms",
                MAX_EXPERIENCES_IN_FORMS,
            )
            or MAX_EXPERIENCES_IN_FORMS
        ),
    )
    experiences: list[Experience] = []
    for item in knowledge.get("experiencias", [])[:max_experiences]:
        experiences.append(
            Experience(
                company=item.get("empresa", ""),
                title=item.get("cargo", ""),
                period=item.get("periodo", ""),
                location=item.get("local", ""),
                sector=item.get("setor", ""),
                summary=item.get("resumo", ""),
                achievements=list(item.get("conquistas", [])),
                technologies=list(item.get("tecnologias", [])),
            )
        )

    education: list[Education] = []
    for entry in knowledge.get("educacao", []):
        text = str(entry)
        institution = ""
        year = ""
        match = re.search(r"\(([^)]*?)(20\d{2})\)", text)
        if match:
            institution = match.group(1).strip(" -")
            year = match.group(2)
        education.append(
            Education(
                course=edu_raw.get("course", ""),
                level=text,
                institution=institution,
                year=year or str(edu_raw.get("year", "")),
            )
        )

    certifications = [
        Certification(name=str(item))
        for item in knowledge.get("certificacoes", [])
    ]

    languages = _parse_language(pessoal_raw.get("idiomas", ""))

    rules: list[AnswerRule] = []
    for rule in legacy.get("answers", []):
        match = str(rule.get("match", ""))
        value = rule.get("value", "")
        category = "general"
        if re.search(r"termos|privacidade|dados|autoriz|concordo|aceito", match, re.I):
            # Nunca aceitar termos em nome do usuário.
            continue
        if re.search(r"sal[aá]rio|pretens", match, re.I):
            category = "salary_expectation"
        if re.search(r"visto|sponsor|autoriza[cç][aã]o de trabalho", match, re.I):
            category = "visa_sponsorship"
        rules.append(
            AnswerRule(match=match, value=value, category=category)
        )

    # Evidência explícita do operador: fora do git e fora do filtro de termos.
    rules.extend(profile_human_evidence(settings))

    cv_path = settings.resolve(settings.profile.cv_path)
    cv_text = extract_cv_text(cv_path) if cv_path.exists() else ""
    lowered = cv_text.lower()

    unverified = [
        claim
        for claim, needle in [
            ("Azure Service Bus", "service bus"),
            ("Entity Framework", "entity framework"),
            ("TypeScript", "typescript"),
            ("Technical Lead (título formal)", "technical lead"),
        ]
        if needle not in lowered
    ]

    profile = CandidateProfile(
        personal=personal,
        years_experience=int(exp_total.get("anos") or experience_raw.get("years", 0) or 0),
        years_csharp=int(exp_total.get("anos_csharp") or experience_raw.get("years_csharp", 0) or 0),
        current_title=experience_raw.get("current_title")
        or (experiences[0].title if experiences else ""),
        current_company=experience_raw.get("current_company")
        or (experiences[0].company if experiences else ""),
        summary=experiences[0].summary if experiences else "",
        experiences=experiences,
        skills={k: list(v or []) for k, v in (knowledge.get("habilidades") or {}).items()},
        education=education,
        certifications=certifications,
        languages=languages,
        project_preferences=profile_project_preferences(settings),
        expectations={
            k: str(v) for k, v in expectations_raw.items() if v not in (None, "")
        },
        documents={
            k: str(v) for k, v in docs_raw.items() if v not in (None, "")
        },
        unverified_claims=unverified,
        answer_rules=rules,
        source_files=[str(cv_path), str(settings.resolve(settings.profile.legacy_profile))],
    )
    log_event(
        "profile_built",
        experiences=len(profile.experiences),
        certifications=len(profile.certifications),
        unverified=len(unverified),
    )
    return profile


def profile_context(
    profile: CandidateProfile,
    max_experiences: int = MAX_EXPERIENCES_IN_FORMS,
) -> str:
    """Contexto compacto e com procedência para o LLM responder perguntas.

    Só as experiências mais recentes entram no prompt de preenchimento: o
    agente não deve criar nem preencher uma quarta experiência inexistente.
    """
    data = profile.model_dump(mode="json")
    data.pop("unverified_claims", None)
    if max_experiences > 0 and isinstance(data.get("experiences"), list):
        data["experiences"] = data["experiences"][:max_experiences]
    return json.dumps(data, ensure_ascii=False, indent=2)[:16000]
