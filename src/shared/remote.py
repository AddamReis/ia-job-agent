"""Validação determinística de 100% remoto.

Uma única regra serve para descoberta pública, classificação, PWA e envio:
precisa haver sinal explícito de remoto e não pode haver sinal de híbrido,
presencial ou remoto parcial. ``hybrid cloud`` é tecnologia, não modelo de
trabalho, e por isso é limpo antes da checagem.
"""
from __future__ import annotations

import re

_REMOTE_RE = re.compile(
    r"\b(remote|remoto|work from home|wfh|anywhere|distributed|telecommut|teletrabalho)\b",
    re.I,
)
_ONSITE_RE = re.compile(
    r"\b(hybrid|h[íi]brido|on-?site|onsite|presencial|in[- ]office|office[- ]based|"
    r"the office|blend of both)\b",
    re.I,
)
_TECH_HYBRID_RE = re.compile(
    r"\bhybrid\s+(?:cloud|app|mobile|application|framework|integration|database)\b",
    re.I,
)
# Rótulo explícito do card da vaga ("Híbrido", "Presencial"). Não inclui
# "the office"/"blend of both" para não vetar por cards laterais de outras
# vagas; esses termos continuam bloqueados quando vêm da descrição.
_ONSITE_LABEL_RE = re.compile(
    r"\b(?:hybrid|h[íi]brido|on-?site|onsite|presencial)\b",
    re.I,
)
_REMOTE_PERCENT_RE = re.compile(
    r"(\d{1,3})\s*%\s*(?:remote|remoto)|(?:remote|remoto)\s*(\d{1,3})\s*%",
    re.I,
)


def has_partial_remote(*texts: str) -> bool:
    """Detecta "80% remote" e outras variantes que não são 100% remoto."""
    text = " ".join(value or "" for value in texts)
    for match in _REMOTE_PERCENT_RE.finditer(text):
        percent = int(match.group(1) or match.group(2) or 0)
        if percent < 100:
            return True
    return False


def has_explicit_onsite_label(*texts: str) -> bool:
    """Detecta o rótulo de modelo de trabalho no cabeçalho da vaga.

    ``hybrid cloud`` é tecnologia e é limpo antes da checagem, mas
    ``Híbrido``/``Presencial`` no card da vaga é veto determinístico mesmo
    quando a descrição diz "100% remoto".
    """
    text = _TECH_HYBRID_RE.sub(" ", " ".join(value or "" for value in texts))
    return bool(_ONSITE_LABEL_RE.search(text))


def is_remote_text(*texts: str) -> bool:
    """True somente com remoto integral e sem veto de presencial/híbrido."""
    text = " ".join(value or "" for value in texts)
    cleaned = _TECH_HYBRID_RE.sub(" ", text)
    if _ONSITE_RE.search(cleaned) or has_partial_remote(cleaned):
        return False
    return bool(_REMOTE_RE.search(cleaned))
