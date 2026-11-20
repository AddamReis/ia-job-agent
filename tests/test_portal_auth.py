"""Autenticação automatizada de portais e testes internos pós-envio."""
from __future__ import annotations

import sys

import pytest

from src.agents.browser_apply import BrowserApplyAgent
from src.core.config import Settings
from src.core.errors import BlockedByPlatformError
from src.execution.web.portal_credentials import PortalCredentialStore


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


class FakeField:
    def __init__(self) -> None:
        self.value = ""

    def fill(self, value: str) -> None:
        self.value = value


class FakeButton:
    def __init__(self) -> None:
        self.clicked = False

    def click(self) -> None:
        self.clicked = True


class FakeLocator:
    def __init__(self, target: FakeField | FakeButton) -> None:
        self.first = target
        self.count_value = 1

    def count(self) -> int:
        return self.count_value


class FakeLoginPage:
    def __init__(self) -> None:
        self.user = FakeField()
        self.password = FakeField()
        self.button = FakeButton()
        self.url = "https://applicants.bairesdev.com/login?careerId=453"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.user if "username" in selector else self.password)

    def get_by_role(self, *_args, **_kwargs) -> FakeLocator:
        return FakeLocator(self.button)

    def get_by_text(self, *_args, **_kwargs) -> FakeLocator:
        return FakeLocator(self.button)

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str) -> str:
        return "Ingresa con Google Usuario Código de acceso Ingresar"


class FakeCredentials:
    def __init__(self, value: tuple[str, str] | None) -> None:
        self.value = value

    def get(self, _domain: str) -> tuple[str, str] | None:
        return self.value


class FakeResultPage:
    def __init__(self, text: str) -> None:
        self.text = text

    def evaluate(self, _script: str) -> str:
        return self.text


def _agent(monkeypatch: pytest.MonkeyPatch, creds: tuple[str, str] | None) -> BrowserApplyAgent:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    agent.portal_credentials = FakeCredentials(creds)
    return agent


def test_credentials_roundtrip_in_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
    store = PortalCredentialStore()
    dominio = "applicants.bairesdev.com"

    assert store.has(dominio) is False
    store.set(dominio, "usuario", "senha")
    assert store.get(dominio) == ("usuario", "senha")
    assert store.has(dominio) is True
    store.delete(dominio)
    assert store.get(dominio) is None


def test_bairesdev_config_declares_login_and_completed_assessment() -> None:
    cfg = Settings().application.portal_auth["applicants.bairesdev.com"]

    assert cfg.company == "BairesDev"
    assert cfg.login_url == "https://applicants.bairesdev.com/login"
    assert cfg.assessment_status == "completed"


def test_automated_login_fills_credentials_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, ("usuario", "senha"))
    page = FakeLoginPage()
    snapshots = [
        {"url": page.url, "text": "Ingresa con Google Usuario Código de acceso Ingresar"},
        {"url": "https://applicants.bairesdev.com/job/453/298508/apply", "text": "Descripción"},
    ]
    monkeypatch.setattr("src.agents.browser_apply.snapshot_form", lambda _page: snapshots.pop(0))

    agent._ensure_portal_login(page)

    assert page.user.value == "usuario"
    assert page.password.value == "senha"
    assert page.button.clicked is True
    assert ("portal_login_automated", {"domain": "applicants.bairesdev.com", "company": "BairesDev"}) in eventos


def test_automated_login_stops_on_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(monkeypatch, ("usuario", "senha"))
    page = FakeLoginPage()
    snapshots = [
        {"url": page.url, "text": "Usuario Código de acceso Ingresar"},
        {"url": page.url, "text": "Just a moment... CAPTCHA verificação"},
    ]
    monkeypatch.setattr("src.agents.browser_apply.snapshot_form", lambda _page: snapshots.pop(0))

    with pytest.raises(BlockedByPlatformError, match="verificação"):
        agent._ensure_portal_login(page)


def test_internal_assessment_is_not_confused_with_application_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, None)
    page = FakeResultPage("Candidatura enviada. Test: evaluación interna de la plataforma")

    agent._handle_post_submit_assessment(page, "applicants.bairesdev.com")

    nomes = [evento for evento, _ in eventos]
    assert "portal_assessment_detected" in nomes
    assert "portal_assessment_already_completed" in nomes
    assert "portal_assessment_pending" not in nomes
