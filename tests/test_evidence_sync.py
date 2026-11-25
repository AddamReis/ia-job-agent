"""Evidência humana explícita sincroniza com o perfil persistido no banco."""
from __future__ import annotations

import json

from src.agents.orchestrator import Orchestrator
from src.core.config import Settings
from src.core.events import EventBus
from src.models.domain import AnswerRule, CandidateProfile
from src.storage.database import Repository


def test_evidencia_humana_substitui_regra_antiga_do_banco(tmp_path) -> None:
    arquivo = tmp_path / "human_evidence.json"
    arquivo.write_text(
        json.dumps(
            [
                {
                    "match": "termos de uso",
                    "value": True,
                    "category": "human_evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings.load()
    settings.profile.human_evidence_file = str(arquivo)

    banco = tmp_path / "agent.db"
    settings.storage.database = str(banco)
    repo = Repository(banco)
    repo.save_profile(
        CandidateProfile(
            answer_rules=[
                AnswerRule(match="antiga", value="sim", category="human_evidence")
            ]
        )
    )

    orc = Orchestrator(settings, EventBus())

    regras = [
        regra for regra in orc.profile.answer_rules if regra.category == "human_evidence"
    ]
    assert [regra.match for regra in regras] == ["termos de uso"]
    assert regras[0].value is True
