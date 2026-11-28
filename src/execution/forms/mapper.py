"""Resolução local de campos a partir do perfil — sem inventar dados."""
from __future__ import annotations

import calendar
import re

from ...core.config import Settings
from ...models.domain import CandidateProfile
from .field import FormField

_ENGLISH_YEARS = re.compile(
    r"\byears of (?:relevant )?experience\b|\byears experience\b", re.IGNORECASE
)
# Perguntas "at least 3 years?" são sim/não; o número do perfil não é valor de
# radio (todos os radios do grupo têm value="on"). A resposta é derivada do
# proprio perfil, sem inventar evidência humana.
_YEARS_THRESHOLD = re.compile(
    r"(?P<operator>at least|minimum|min\.?|more than|over|>=|>)\s*(?P<limit>\d{1,2})"
    r"|(?P<plus>\d{1,2})\s*(?:\+|\bor more\b)",
    re.IGNORECASE,
)
_THRESHOLD_QUESTION = re.compile(
    r"at least|minimum|min\.?|more than|over|>=|>|\d{1,2}\s*(?:\+|\bor more\b)",
    re.IGNORECASE,
)
_COUNTRY_ALIASES = {
    "brasil": "Brazil",
    "estados unidos": "United States",
    "eua": "United States",
}

_LOCAL_RULES: list[tuple[str, str, str]] = [
    (r"\bemail\b|e-mail", "personal.email", "profile.personal.email"),
    (r"\bfirst name\b|\bnome\b.*\bprimeiro\b", "personal.first_name", "profile.personal.first_name"),
    (r"\blast name\b|\bsobrenome\b", "personal.last_name", "profile.personal.last_name"),
    (r"\bphone\b|\btelefone\b|\bcelular\b", "personal.phone_display", "profile.personal.phone"),
    (r"\blinkedin\b", "personal.linkedin", "profile.personal.linkedin"),
    (r"\bgithub\b", "personal.github", "profile.personal.github"),
    (r"\bcity\b|\bcidade\b", "location.city", "profile.personal.location"),
    (r"\bcountry\b|\bpa[ií]s\b", "location.country", "profile.personal.location"),
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
    r"authorized to work|work authorization|right to work|visa sponsorship|"
    r"security clearance|"
    r"sal[aá]rio|salary|csalary|salarial|pretens[aã]o|contrato|legal|compromisso|"
    r"mfa|captcha|senha|password",
    re.IGNORECASE,
)

_SALARY_FIELD = re.compile(
    r"desired salary|sal[aá]rio deseado|salario deseado|csalary|"
    r"salary expectation|expected salary|pretens[aã]o salarial|"
    r"expectativa salarial|sal[aá]rio pretendido|salarial|remunera[cç][aã]o",
    re.IGNORECASE,
)

# Perguntas demográficas não precisam de dado do perfil quando o próprio portal
# oferece uma opção de não responder: escolher essa opção não divulga nada.
_DEMOGRAPHIC_FIELD = re.compile(
    r"race|racial|ethnic|veteran|disabil|defici[eê]ncia|lgbtqiapn|"
    r"sexual orientation|gender identity",
    re.IGNORECASE,
)
_OPT_OUT_OPTION = re.compile(
    r"don't wish to (?:answer|self.identify)|"
    r"do not wish to (?:answer|self.identify)|"
    r"prefer not to (?:say|answer|self.identify)|"
    r"prefiro n[aã]o responder|prefiro n[aã]o se identificar|"
    r"n[aã]o desejo responder|"
    r"choose not to self.identify|escolho n[aã]o se identificar",
    re.IGNORECASE,
)


def _get_path(profile: CandidateProfile, path: str) -> object:
    current: object = profile
    for part in path.split("."):
        if current is None:
            return None
        current = getattr(current, part, None)
    return current


def _location_part(profile: CandidateProfile, parte: str) -> str:
    """Cidade/estado/país vêm de ``personal.location``, único campo de endereço."""
    raw = str(_get_path(profile, "personal.location") or "")
    partes = [item.strip() for item in raw.split(",") if item.strip()]
    if not partes:
        return ""
    if parte == "city":
        return partes[0]
    if parte == "state":
        return partes[1] if len(partes) > 1 else ""
    if parte == "country":
        return partes[-1]
    return ""


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


def _years_threshold_answer(
    field: FormField, profile: CandidateProfile, context: str
) -> tuple[str, str] | None:
    """Converte anos do perfil em sim/não para perguntas com limite.

    "Do you have at least 3 years...?" é uma pergunta binária: enviar ``7``
    como valor de rádio é impreciso (o grupo costuma ter ``value="on"``). A
    comparação usa somente o número já persistido no perfil e o limite escrito
    na própria pergunta — nenhuma resposta é inventada.
    """
    if field.field_type not in {"radio", "checkbox"}:
        return None
    if not (
        _ENGLISH_YEARS.search(context)
        or re.search(r"experience years|anos de experi", context, re.IGNORECASE)
    ):
        return None
    if not _THRESHOLD_QUESTION.search(context):
        return None
    match = _YEARS_THRESHOLD.search(context)
    if not match:
        return None
    limite_raw = match.group("limit") or match.group("plus")
    if not limite_raw:
        return None
    atual = _get_path(profile, "years_experience")
    try:
        anos = int(atual)
        limite = int(limite_raw)
    except (TypeError, ValueError):
        return None
    operador = (match.group("operator") or "").lower()
    estrito = (
        operador.startswith("more than")
        or operador.startswith("over")
        or operador == ">"
    )
    aprovado = anos > limite if estrito else anos >= limite
    rotulo = "Yes" if aprovado else "No"
    for opcao in field.options:
        texto = str(opcao or "").strip()
        if aprovado and re.fullmatch(r"yes|sim|true", texto, re.IGNORECASE):
            return texto, "profile.years_experience:threshold"
        if not aprovado and re.fullmatch(r"no|n[aã]o|false", texto, re.IGNORECASE):
            return texto, "profile.years_experience:threshold"
    return rotulo, "profile.years_experience:threshold"


def human_evidence(profile: CandidateProfile, text: str) -> str:
    """Resposta cadastrada explicitamente pelo operador para tema sensível.

    Diferente de uma autorização genérica, cada regra traz o texto que o portal
    pergunta e o valor decidido pelo humano. Nunca é inferida pelo LLM.
    """
    contexto = " ".join(str(text or "").split()).lower()
    for regra in getattr(profile, "answer_rules", []) or []:
        if str(getattr(regra, "category", "")) != "human_evidence":
            continue
        try:
            if re.search(str(regra.match), contexto, re.I):
                return str(regra.value)
        except re.error:
            continue
    return ""


def _sensitive_evidence(
    field: FormField, profile: CandidateProfile
) -> tuple[str, str] | None:
    """Só aceita dado sensível se houver evidência explícita no perfil/portal.

    <<<origem>> é sempre verificável: perfil preenchido pelo operador ou opção
    do próprio formulário para não responder. Nunca há inferência por LLM.
    """
    signature = field.signature()
    rotulo = " ".join(str(field.nearby_text or "").split()).lower()
    pergunta = re.split(r"[?]", rotulo, maxsplit=1)[0]
    contexto = f"{signature} {rotulo}".lower()
    # Salário é sensível, mas só quando é a pergunta do campo. Uma pergunta
    # vizinha sobre anos de experiência não pode sequestrar a resposta.
    if _SALARY_FIELD.search(pergunta) or _SALARY_FIELD.search(signature):
        valor = profile.expectations.get("salary") or profile.expectations.get("salario")
        if valor not in (None, ""):
            return (str(valor), "profile.expectations.salary")
        return None

    if _GENDER_FIELD.search(contexto):
        caminho, origem = "personal.gender", "profile.personal.gender"
    elif _BIRTH_FULL_FIELD.search(contexto):
        caminho, origem = "personal.birth_date", "profile.personal.birth_date"
    elif _BIRTH_MONTH_FIELD.search(contexto):
        caminho, origem = "personal.birth_month", "profile.personal.birth_month"
    elif _BIRTH_DAY_FIELD.search(contexto):
        caminho, origem = "personal.birth_day", "profile.personal.birth_day"
    elif _BIRTH_YEAR_FIELD.search(contexto):
        caminho, origem = "personal.birth_year", "profile.personal.birth_year"
    else:
        caminho, origem = "", ""

    valor = _get_path(profile, caminho) if caminho else None
    if valor in (None, "") and caminho:
        parte = caminho.rsplit(".", 1)[-1].removeprefix("birth_")
        valor = _birth_part(profile, parte)
    if valor not in (None, ""):
        return (str(valor), origem)

    # Sem dado no perfil: a única resposta segura é a opção do próprio portal
    # de não responder. Ela não inventa nem divulga dado sensível.
    opcoes = list(field.options) or [field.label]
    for opcao in opcoes:
        if _OPT_OUT_OPTION.search(str(opcao)):
            return (str(opcao), "portal:opcao_nao_responder")
    return None


def resolve_local(
    field: FormField, profile: CandidateProfile, settings: Settings
) -> tuple[str, str, bool, str]:
    """Retorna (valor, origem, precisa_humano, categoria)."""
    signature = field.signature()
    rotulo_renderizado = " ".join(str(field.nearby_text or "").split()).lower()
    pergunta = re.split(r"[?]", rotulo_renderizado, maxsplit=1)[0]
    contexto = f"{signature} {rotulo_renderizado}".lower()
    contexto_anos = f"{signature} {pergunta}"
    evidencia_humana = human_evidence(profile, contexto)
    if evidencia_humana:
        return (evidencia_humana, "profile.human_evidence", False, "general")
    if (
        _SENSITIVE.search(signature)
        or _SENSITIVE.search(pergunta)
        or _GENDER_FIELD.search(pergunta)
        or _BIRTH_FULL_FIELD.search(pergunta)
        or _BIRTH_MONTH_FIELD.search(pergunta)
        or _BIRTH_DAY_FIELD.search(pergunta)
        or _BIRTH_YEAR_FIELD.search(pergunta)
        or _DEMOGRAPHIC_FIELD.search(signature)
        or _DEMOGRAPHIC_FIELD.search(pergunta)
    ):
        evidencia = _sensitive_evidence(field, profile)
        if evidencia is not None:
            valor, origem = evidencia
            return (valor, origem, False, "general")
        return ("", "politica:acao_sensivel", True, "sensitive")

    # A assinatura tem prioridade: rótulos like "Email*" vizinhos não podem
    # sequestrar um campo "Country". Só depois usamos o texto renderizado,
    # necessário quando o portal mostra placeholders ("Tony", "Answer").
    def _resolvido(path: str) -> str:
        value = (
            _location_part(profile, path.removeprefix("location."))
            if path.startswith("location.")
            else _get_path(profile, path)
        )
        if value in (None, ""):
            return ""
        if path == "location.country":
            valor = str(value).strip()
            return _COUNTRY_ALIASES.get(valor.lower(), valor)
        return str(value)

    for pattern, path, source in _LOCAL_RULES:
        if re.search(pattern, signature):
            if path == "years_experience":
                resposta = _years_threshold_answer(field, profile, contexto_anos)
                if resposta:
                    return (resposta[0], resposta[1], False, "general")
            value = _resolvido(path)
            if value:
                return (value, source, False, "general")

    # No texto renderizado só o início pertence ao campo; o restante pode ser
    # de irmãos. Casar no início evita que "Email*" vizinho resolva "Country".
    for pattern, path, source in _LOCAL_RULES:
        if re.match(rf"^\s*(?:{pattern})", rotulo_renderizado, re.IGNORECASE):
            if path == "years_experience":
                resposta = _years_threshold_answer(field, profile, contexto_anos)
                if resposta:
                    return (resposta[0], resposta[1], False, "general")
            value = _resolvido(path)
            if value:
                return (value, source, False, "general")

    if _ENGLISH_YEARS.search(pergunta):
        resposta = _years_threshold_answer(field, profile, contexto_anos)
        if resposta:
            return (resposta[0], resposta[1], False, "general")
        value = _get_path(profile, "years_experience")
        if value not in (None, ""):
            return (str(value), "profile.years_experience", False, "general")

    if re.search(r"\bcountry\b|\bpa[ií]s\b", rotulo_renderizado) and not re.search(
        r"country code|c\s*digo do pa\s*s|codigo do pais", signature
    ):
        cru = _location_part(profile, "country")
        desejado = _COUNTRY_ALIASES.get(cru.lower(), cru)
        if desejado:
            for option in field.options:
                if desejado.lower() in str(option).lower():
                    return (str(option), "profile.personal.country", False, "general")
            return (desejado, "profile.personal.country", False, "general")

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
        if re.search(rule.match, contexto, re.IGNORECASE):
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
