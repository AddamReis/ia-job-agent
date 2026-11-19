"""Pré-filtro determinístico de stack, sem LLM e sem rede.

Usado no discovery para descartar títulos claramente focados em Java ou
FrontEnd antes de buscar a descrição, e na classificação para o veto de core.
A presença de .NET/C# no texto protege a vaga: Java/React/TS complementares
em vaga .NET ou full-stack .NET continuam elegíveis.
"""
from __future__ import annotations

import re

_DOTNET_RE = re.compile(r"\.net|dotnet|asp\.net|c\s*#|\bcsharp\b", re.I)
# \bjava\b não casa "JavaScript" (não há fronteira de palavra antes do 'S').
_JAVA_RE = re.compile(r"\bjava\b(?!script)", re.I)
_FRONTEND_RE = re.compile(
    r"\bfront[\s-]?end\b|\breact(?:\.?js)?\b|\bangular\b|\bvue(?:\.?js)?\b|"
    r"\bnext\.?js\b|\bui\s+(?:developer|engineer)\b",
    re.I,
)


def mentions_dotnet(text: str) -> bool:
    return bool(_DOTNET_RE.search(text or ""))


def mentions_java(text: str) -> bool:
    return bool(_JAVA_RE.search(text or ""))


def mentions_frontend(text: str) -> bool:
    return bool(_FRONTEND_RE.search(text or ""))


def is_off_stack_title(title: str) -> bool:
    """Título focado em Java/FrontEnd sem .NET — candidato a descarte imediato.

    É um pré-filtro conservador: atua só no título, que é o sinal mais forte de
    foco da vaga. O restante continua sendo avaliado pela classificação com o
    cross-check semântico completo.
    """
    if not (title or "").strip():
        return False
    if mentions_dotnet(title):
        return False
    return mentions_java(title) or mentions_frontend(title)
