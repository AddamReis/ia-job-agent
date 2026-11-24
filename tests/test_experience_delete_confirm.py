"""Decisão aprendida/interpretada para exclusão de experiência extra."""
from __future__ import annotations

import re
from typing import Any

import pytest

from src.core.errors import HumanActionRequired
from src.execution.web.form_dom import remove_extra_experience_entry
from src.models.schemas import UiActionDecision


class FakeButton:
    def __init__(self, name: str, info: dict[str, Any] | None = None) -> None:
        self.name = name
        self.info = info or {}
        self.clicks = 0

    def is_visible(self) -> bool:
        return True

    def evaluate(self, _js: str) -> dict[str, Any]:
        return self.info

    def click(self) -> None:
        self.clicks += 1


class FakeLocator:
    def __init__(self, buttons: list[FakeButton]) -> None:
        self._buttons = buttons

    def count(self) -> int:
        return len(self._buttons)

    def nth(self, index: int) -> FakeButton:
        return self._buttons[index]

    @property
    def first(self) -> FakeButton:
        return self._buttons[0]


class FakePage:
    def __init__(self, buttons: list[FakeButton]) -> None:
        self._buttons = buttons
        self.waits: list[int] = []

    def get_by_role(self, role: str, name: re.Pattern[str]) -> FakeLocator:
        assert role == "button"
        return FakeLocator(
            [button for button in self._buttons if name.search(button.name)]
        )

    def evaluate(self, js: str) -> Any:
        if "aria-modal" in js or 'role="dialog"' in js:
            return {
                "url": "https://www.linkedin.com/jobs/view/1/",
                "text": "Tem certeza de que deseja excluir esta experiência?",
                "buttons": [
                    {"text": button.name, "aria": ""}
                    for button in self._buttons
                    if button.name
                ],
            }
        return {}

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def test_remove_extra_experience_confirms_dialog() -> None:
    delete = FakeButton(
        "Excluir experiência", {"title": "Suporte TI", "text": "Suporte TI FUNDAP"}
    )
    confirm = FakeButton("Remover")
    page = FakePage([delete, confirm])

    removido = remove_extra_experience_entry(page, ["Avanade", "Aliare", "Autoware"])

    assert removido is True
    assert delete.clicks == 1
    assert confirm.clicks == 1
    assert page.waits


def test_remove_extra_experience_uses_learned_decision() -> None:
    delete = FakeButton(
        "Excluir experiência", {"title": "Suporte TI", "text": "Suporte TI FUNDAP"}
    )
    confirm = FakeButton("Remover")
    cancel = FakeButton("Cancelar")
    page = FakePage([delete, confirm, cancel])
    observados: list[dict[str, Any]] = []

    def decider(intent: str, dialog: dict[str, Any], trigger: str) -> UiActionDecision:
        assert intent == "confirm_remove_extra_experience"
        assert trigger == "Excluir experiência"
        assert any(button["text"] == "Cancelar" for button in dialog["buttons"])
        return UiActionDecision(action="Remover", source="learned", reason="já funcionou")

    removido = remove_extra_experience_entry(
        page,
        ["Avanade", "Aliare", "Autoware"],
        decider=decider,
        observer=observados.append,
    )

    assert removido is True
    assert confirm.clicks == 1
    assert cancel.clicks == 0
    assert observados[0]["action"] == "Remover"
    assert observados[0]["source"] == "learned"


def test_remove_extra_experience_respects_needs_human() -> None:
    delete = FakeButton(
        "Excluir experiência", {"title": "Suporte TI", "text": "Suporte TI FUNDAP"}
    )
    confirm = FakeButton("Remover")
    page = FakePage([delete, confirm])

    def decider(_intent: str, _dialog: dict[str, Any], _trigger: str) -> UiActionDecision:
        return UiActionDecision(needs_human=True, reason="verificação solicitada")

    with pytest.raises(HumanActionRequired):
        remove_extra_experience_entry(
            page, ["Avanade", "Aliare", "Autoware"], decider=decider
        )

    assert delete.clicks == 1
    assert confirm.clicks == 0


def test_remove_extra_experience_keeps_known_title() -> None:
    delete = FakeButton(
        "Excluir experiência", {"title": "Avanade", "text": "Avanade Backend"}
    )
    confirm = FakeButton("Remover")
    page = FakePage([delete, confirm])

    removido = remove_extra_experience_entry(page, ["Avanade", "Aliare", "Autoware"])

    assert removido is False
    assert delete.clicks == 0
    assert confirm.clicks == 0
