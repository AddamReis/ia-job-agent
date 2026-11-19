"""Leitura da árvore de acessibilidade e extração de vagas sem screenshots."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from xml.etree import ElementTree

from ...execution.forms.field import FormField
from ...models.domain import JobOpportunity

_JOB_ID_HINTS = ("job-card", "jobs-search", "search-result", "result-card", "jobposting")
_POSTED_HINTS = ("ago", "hour", "day", "week", "month", "há ", "dia", "semana")
_SALARY_HINTS = ("$", "€", "£", "usd", "eur", "gbp", "r$")


@dataclass
class UIElement:
    text: str = ""
    content_desc: str = ""
    resource_id: str = ""
    class_name: str = ""
    clickable: bool = False
    enabled: bool = True
    children: list["UIElement"] = field(default_factory=list)

    def all_text(self) -> list[str]:
        values = [self.text, self.content_desc]
        for child in self.children:
            values.extend(child.all_text())
        return [v.strip() for v in values if v and v.strip()]


def parse_tree(xml: str) -> list[UIElement]:
    """Converte o XML do uiautomator2 em árvore de elementos."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    def build(node: ElementTree.Element) -> UIElement:
        return UIElement(
            text=node.attrib.get("text", ""),
            content_desc=node.attrib.get("content-desc", ""),
            resource_id=node.attrib.get("resource-id", ""),
            class_name=node.attrib.get("class", ""),
            clickable=node.attrib.get("clickable") == "true",
            enabled=node.attrib.get("enabled") != "false",
            children=[build(child) for child in list(node)],
        )

    return [build(node) for node in list(root)]


def flatten(elements: list[UIElement]) -> list[UIElement]:
    result: list[UIElement] = []
    for element in elements:
        result.append(element)
        result.extend(flatten(element.children))
    return result


def parse_posted(
    raw: str, now: datetime | None = None
) -> tuple[datetime | None, str]:
    """Interpreta recência sem presumir precisão inexistente.

    ``now`` permite congelar a âncora da execução (discovery-agent); sem ele,
    mantém o comportamento legado usado pelos testes e pela leitura avulsa.
    """
    value = raw.strip().lower()
    if not value:
        return None, "unknown"
    now = now or datetime.now()
    if "just now" in value or "agora" in value:
        return now, "hour"

    match = re.search(
        r"(\d+)\s*(min|minute|minuto|month|mo|m[eê]s|mes|hour|hora|h|day|dia|d|week|semana|w|m)",
        value,
    )
    if not match:
        return None, "unknown"
    amount = int(match.group(1))
    unit = match.group(2)
    if unit in {"min", "minute", "minuto", "m"}:
        return now - timedelta(minutes=amount), "hour"
    if unit in {"h", "hour", "hora"}:
        return now - timedelta(hours=amount), "hour"
    if unit in {"d", "day", "dia"}:
        return now - timedelta(days=amount), "day"
    if unit in {"w", "week", "semana"}:
        return now - timedelta(weeks=amount), "week"
    return now - timedelta(days=30 * amount), "month"


def looks_like_job_card(element: UIElement) -> bool:
    identifier = f"{element.resource_id} {element.content_desc}".lower()
    if any(hint in identifier for hint in _JOB_ID_HINTS):
        return True
    texts = element.all_text()
    has_posted = any(any(h in t.lower() for h in _POSTED_HINTS) for t in texts)
    has_salary = any(any(h in t.lower() for h in _SALARY_HINTS) for t in texts)
    return element.clickable and has_posted and len(texts) >= 2 or (element.clickable and has_salary and len(texts) >= 3)


# Marcadores de canal presentes no cartão — capturados antes de filtrar ruído.
_APPLY_MARKERS = (
    "easy apply", "candidatura rápida", "candidatura rapida",
    "apply on company", "company website", "site da empresa", "apply externally",
)


def _apply_marker(value: str) -> str:
    lowered = (value or "").strip().lower()
    for marker in _APPLY_MARKERS:
        if marker in lowered:
            return marker
    return ""


_NOISE_JOB_CARD_TEXTS = (
    "easy apply", "be an early applicant", "promoted", "verified job",
    "company alumni", "school alumni", "connections work here",
    "connection works here", "alumni work here", "applicants", "applicant",
    "show all", "how promoted jobs", "see more", "show more", "ver mais",
)
_LOCATION_RE = re.compile(
    r"\b(remote|hybrid|on-?site|brazil|brasil|united states|usa|canada|"
    r"united kingdom|uk|germany|netherlands|ireland|portugal|spain|"
    r"latin america|s[aã]o paulo|rio de janeiro|lisbon|berlin|london|"
    r"dublin|amsterdam|europe|worldwide)\b",
    re.IGNORECASE,
)


def _clean_dismiss_title(content_desc: str) -> str:
    title = re.sub(r"^Dismiss\s+", "", content_desc.strip())
    title = re.sub(r"\s+job$", "", title, flags=re.IGNORECASE)
    return re.sub(r"\s*\(Verified job\)$", "", title, flags=re.IGNORECASE).strip()


def _node_values(node: ElementTree.Element) -> list[str]:
    values: list[str] = []
    for child in node.iter():
        for key in ("text", "content-desc"):
            value = (child.attrib.get(key) or "").strip()
            if value:
                values.append(value)
    return values


def _find_card_container(
    dismiss_node: ElementTree.Element,
    parent_map: dict[ElementTree.Element, ElementTree.Element],
    dismiss_desc: str,
) -> ElementTree.Element | None:
    """Sobe até o ancestral de um único cartão, sem incluir a lista inteira."""
    current = parent_map.get(dismiss_node)
    minimal_candidate: ElementTree.Element | None = None
    for _ in range(12):
        if current is None:
            break
        values = _node_values(current)
        dismissals = [
            value for value in values
            if value.startswith("Dismiss ") and value.endswith(" job")
        ]
        if len(values) >= 3 and dismissals == [dismiss_desc]:
            has_posted = any(
                value.lower().startswith("posted ")
                or any(hint in value.lower() for hint in _POSTED_HINTS)
                for value in values
            )
            if has_posted:
                return current
            if minimal_candidate is None:
                minimal_candidate = current
        current = parent_map.get(current)
    return minimal_candidate


def _is_card_noise(value: str, title: str) -> bool:
    lowered = value.strip().lower()
    title_normalized = title.strip().lower()
    if not lowered or lowered in {"·", "|", "-"}:
        return True
    if lowered.startswith("dismiss "):
        return True
    if lowered == title_normalized or lowered == f"{title_normalized} (verified job)":
        return True
    return any(fragment in lowered for fragment in _NOISE_JOB_CARD_TEXTS)


def _extract_linkedin_job_cards(xml: str, max_jobs: int) -> list[JobOpportunity]:
    """Fallback para o formato real do app: título em content-desc de dismiss."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    cards: list[JobOpportunity] = []
    seen: set[str] = set()

    for node in root.iter():
        desc = (node.attrib.get("content-desc") or "").strip()
        if not (desc.startswith("Dismiss ") and desc.endswith(" job")):
            continue
        title = _clean_dismiss_title(desc)
        if not title:
            continue
        container = _find_card_container(node, parent_map, desc)
        if container is None:
            continue

        company = ""
        location = ""
        posted_raw = ""
        salary_raw = ""
        apply_marker = ""
        for value in _node_values(container):
            marker = _apply_marker(value)
            if marker and not apply_marker:
                apply_marker = marker
            if _is_card_noise(value, title):
                continue
            lowered = value.lower()
            if lowered.startswith("posted ") or any(h in lowered for h in _POSTED_HINTS):
                if not posted_raw:
                    posted_raw = re.sub(r"^Posted\s+", "", value, flags=re.IGNORECASE).strip()
                continue
            if any(h in lowered for h in _SALARY_HINTS):
                if not salary_raw:
                    salary_raw = value
                continue
            if not location and _LOCATION_RE.search(lowered):
                location = value
                continue
            if not company and len(value) <= 80:
                company = value

        if not company and not location:
            continue
        posted_at, precision = parse_posted(posted_raw)
        external_id = hashlib.sha256(
            f"{title}|{company}|{location}".encode("utf-8")
        ).hexdigest()[:16]
        if external_id in seen:
            continue
        seen.add(external_id)
        cards.append(
            JobOpportunity(
                title=title[:200],
                company=company[:200],
                location=location[:200],
                external_id=external_id,
                posted_raw=posted_raw,
                posted_at=posted_at,
                posted_precision=precision,  # type: ignore[arg-type]
                salary_raw=salary_raw,
                apply_marker=apply_marker,
            )
        )
        if len(cards) >= max_jobs:
            break
    return cards


def _extract_structural_job_cards(xml: str, max_jobs: int) -> list[JobOpportunity]:
    """Extrai vagas de árvores com resource-id de card ou nós estruturados."""
    cards: list[JobOpportunity] = []
    seen: set[str] = set()
    for element in parse_tree(xml):
        if not looks_like_job_card(element):
            continue
        texts = element.all_text()
        if len(texts) < 2:
            continue
        title = texts[0][:200]
        company = texts[1][:200] if len(texts) > 1 else ""
        location = ""
        posted_raw = ""
        salary_raw = ""
        apply_marker = ""
        for text in texts:
            marker = _apply_marker(text)
            if marker and not apply_marker:
                apply_marker = marker
            lowered = text.lower()
            if any(h in lowered for h in _POSTED_HINTS) and not posted_raw:
                posted_raw = text
            if any(h in lowered for h in _SALARY_HINTS) and not salary_raw:
                salary_raw = text
            if not location and (
                "remote" in lowered or "brasil" in lowered or "brazil" in lowered
                or "united states" in lowered or "ireland" in lowered
                or "germany" in lowered or "portugal" in lowered
            ):
                location = text
        posted_at, precision = parse_posted(posted_raw)
        external_id = hashlib.sha256(
            f"{title}|{company}|{location}".encode("utf-8")
        ).hexdigest()[:16]
        if external_id in seen:
            continue
        seen.add(external_id)
        cards.append(
            JobOpportunity(
                title=title,
                company=company,
                location=location,
                external_id=external_id,
                posted_raw=posted_raw,
                posted_at=posted_at,
                posted_precision=precision,  # type: ignore[arg-type]
                salary_raw=salary_raw,
                apply_marker=apply_marker,
            )
        )
        if len(cards) >= max_jobs:
            break
    return cards


def extract_job_cards(xml: str, max_jobs: int = 25) -> list[JobOpportunity]:
    """Extrai vagas da árvore de acessibilidade. Nenhuma imagem é gravada."""
    cards = _extract_structural_job_cards(xml, max_jobs)
    if cards:
        return cards
    return _extract_linkedin_job_cards(xml, max_jobs)


def extract_form_fields(xml: str) -> list[FormField]:
    """Descobre campos de formulário na árvore de acessibilidade."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    fields: list[FormField] = []
    target_classes = ("EditText", "CheckBox", "RadioButton", "Spinner")
    for node in root.iter():
        class_name = node.attrib.get("class", "")
        if not any(target in class_name for target in target_classes):
            continue
        label = (
            node.attrib.get("text", "")
            or node.attrib.get("content-desc", "")
            or node.attrib.get("hint", "")
        )
        field_type = "text"
        if "CheckBox" in class_name:
            field_type = "checkbox"
        elif "RadioButton" in class_name:
            field_type = "radio"
        elif "Spinner" in class_name:
            field_type = "select"
        fields.append(
            FormField(
                label=label,
                field_type=field_type,
                required=node.attrib.get("required") == "true",
                autocomplete=node.attrib.get("autocomplete", ""),
                identifier=node.attrib.get("resource-id", ""),
                placeholder=node.attrib.get("hint", ""),
            )
        )
    return fields


_DESC_HINTS = (
    "jobdescription", "job_description", "jobdetails", "job_details",
    "description", "showmore", "see more", "ver mais", "ver descricao",
)


def extract_job_description(xml: str) -> str:
    """Lê a descrição da vaga da árvore de acessibilidade, sem screenshots."""
    roots = parse_tree(xml)
    if not roots:
        return ""

    candidates: list[str] = []
    for element in flatten(roots):
        identifier = f"{element.resource_id} {element.content_desc}".lower()
        if any(hint in identifier for hint in _DESC_HINTS):
            candidate = " ".join(element.all_text())
            if len(candidate) >= 40:
                candidates.append(candidate)

    if not candidates:
        for element in flatten(roots):
            candidate = element.text.strip()
            if len(candidate) >= 120:
                candidates.append(candidate)

    unique = list(dict.fromkeys(candidates))
    return "\n".join(unique)[:14000]
