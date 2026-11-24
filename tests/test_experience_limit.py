"""Limite de experiências enviadas ao LLM e ao formulário."""
from __future__ import annotations

import json

from src.execution.profile.builder import profile_context
from src.llm.prompts import ANSWER_SYSTEM
from src.models.domain import CandidateProfile, Experience


def test_profile_context_exposes_only_three_latest_experiences() -> None:
    profile = CandidateProfile(
        experiences=[
            Experience(company=f"Empresa {indice}", title=f"Cargo {indice}")
            for indice in range(5)
        ]
    )

    context = json.loads(profile_context(profile))

    assert [item["company"] for item in context["experiences"]] == [
        "Empresa 0",
        "Empresa 1",
        "Empresa 2",
    ]


def test_answer_prompt_forbids_fourth_experience() -> None:
    assert "3 experiencias mais recentes" in ANSWER_SYSTEM
    assert "quarta experiencia" in ANSWER_SYSTEM
