"""Confirmacao do dialogo de exclusao de experiencia extra."""
from __future__ import annotations

import re
from typing import Any

from src.execution.web.form_dom import remove_extra_experience_entry


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
