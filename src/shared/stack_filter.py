"""Pré-filtro determinístico de stack, sem LLM e sem rede.

Usado no discovery para descartar títulos claramente focados em Java,
FrontEnd ou Inteligência Artificial antes de buscar a descrição, e na
classificação para o veto de core. A presença de .NET/C# antes do sinal
concorrente protege a vaga: Java/React/AI complementares em vaga .NET ou
full-stack .NET continuam elegíveis.

A regra atua somente no TÍTULO, que é o sinal mais forte da função principal.
Uma menção a AI/FrontEnd apenas na descrição é tratada pela classificação,
onde complemento não gera veto.
"""
from __future__ import annotations

import re

_DOTNET_RE = re.compile(r"\.net|dotnet|asp\.net|c\s*#|\bcsharp\b", re.I)
# \bjava\b não casa "JavaScript" (não há fronteira de palavra antes do 'S').
_JAVA_RE = re.compile(r"\bjava\b(?!script)", re.I)
_FRONTEND_RE = re.compile(
    r"\bfront[\s-]?end\b|\breact(?:\.?js)?\b|\bangular\b|\bvue(?:\.?js)?\b|"
    r"\bnext\.?js\b|\bnext\s*\+\s*(?:python|node|typescript|javascript|js|\.net|c#)|"
    r"\bui\s+(?:developer|engineer)\b",
    re.I,
)
_AI_RE = re.compile(
    r"\bartificial\s+intelligence\b|\bgen(?:erative)?\s*ai\b|\bgenai\b|"
    r"\bai\b|\bia\b|\bmachine\s+learning\b|\bml\b|\bllms?\b|"
    r"\bdeep\s+learning\b|\bdata\s+scientist\b|\bmlops\b",
    re.I,
)


def mentions_dotnet(text: str) -> bool:
    return bool(_DOTNET_RE.search(text or ""))


def mentions_java(text: str) -> bool:
    return bool(_JAVA_RE.search(text or ""))


def mentions_frontend(text: str) -> bool:
    return bool(_FRONTEND_RE.search(text or ""))


def mentions_ai(text: str) -> bool:
    return bool(_AI_RE.search(text or ""))


def is_ai_focused(text: str) -> bool:
    """AI é a função principal do texto, não um complemento citado depois de .NET.

    'AI Full-stack Engineer (Next + Python)' é focada em AI. Já
    'Senior .NET Engineer with AI integration' mantém .NET como core porque o
    sinal de .NET aparece primeiro.
    """
    ai = _AI_RE.search(text or "")
    if not ai:
        return False
    dotnet = _DOTNET_RE.search(text or "")
    return not (dotnet is not None and dotnet.start() < ai.start())


def is_off_stack_title(title: str) -> bool:
    """Título focado em Java/FrontEnd/AI sem .NET como core — descarte imediato.

    É um pré-filtro conservador: atua só no título, que é o sinal mais forte de
    foco da vaga. O restante continua sendo avaliado pela classificação com o
    cross-check semântico completo.
    """
    if not (title or "").strip():
        return False
    if is_ai_focused(title):
        return True
    if mentions_dotnet(title):
        return False
    return mentions_java(title) or mentions_frontend(title)
