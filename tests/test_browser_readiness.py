"""Preflight/standby do Chrome logado antes de qualquer preparação ou envio."""
from __future__ import annotations

import pytest

import src.agents.browser_readiness as readiness
import src.execution.web.linkedin_logged as logged
from src.core.config import Settings


def test_wait_for_cdp_stays_in_standby_until_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tentativas: list[str] = []
    esperas: list[float] = []

    def versao(url: str, timeout: float = 3.0) -> str:
        tentativas.append(url)
        if len(tentativas) < 3:
            raise OSError("CDP indisponível")
        return "Chrome/140.0"

    monkeypatch.setattr(logged, "cdp_browser_version", versao)

    relogio = [0.0]

    def avancar(segundos: float) -> None:
        esperas.append(segundos)
        relogio[0] += segundos

    pronto = logged.wait_for_cdp(
        "http://127.0.0.1:9222",
        timeout_seconds=30,
        poll_seconds=1,
        sleep=avancar,
        clock=lambda: relogio[0],
    )

    assert pronto is True
    assert tentativas == ["http://127.0.0.1:9222"] * 3
    assert esperas == [1.0, 1.0]


def test_wait_for_cdp_times_out_without_touching_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def indisponivel(*_args, **_kwargs) -> str:
        raise OSError("CDP indisponível")

    monkeypatch.setattr(logged, "cdp_browser_version", indisponivel)

    pronto = logged.wait_for_cdp(
        "http://127.0.0.1:9222",
        timeout_seconds=0,
        poll_seconds=1,
        sleep=lambda _segundos: None,
        clock=lambda: 0.0,
    )

    assert pronto is False


def test_browser_wrapper_uses_configured_cdp_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamados: dict[str, object] = {}

    def falso_wait(url: str, **kwargs) -> bool:
        chamados.update(url=url, **kwargs)
        return True

    monkeypatch.setattr(logged, "wait_for_cdp", falso_wait)
    navegador = logged.LinkedInLoggedBrowser(
        "http://127.0.0.1:9222", timeout_ms=60000
    )

    assert navegador.wait_until_ready(timeout_seconds=90, poll_seconds=2) is True
    assert chamados == {
        "url": "http://127.0.0.1:9222",
        "timeout_seconds": 90,
        "poll_seconds": 2,
    }
    # Preflight é somente HTTP: nenhuma conexão Playwright foi aberta.
    assert navegador.is_connected() is False


def test_worker_gate_skips_callback_when_cdp_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "ensure_browser_ready", lambda _settings: False)
    executados: list[int] = []

    def callback() -> dict[str, int]:
        executados.append(1)
        return {"prepared": 1}

    resultado = readiness.with_browser_gate(Settings(), callback)()

    assert resultado == {"cdp_ready": 0, "standby": True}
    assert executados == []


def test_worker_gate_runs_callback_when_cdp_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "ensure_browser_ready", lambda _settings: True)
    resultado = readiness.with_browser_gate(Settings(), lambda: {"prepared": 1})()
    assert resultado == {"prepared": 1}
