"""Gate de prontidão do Chrome logado para as rotinas de Apply.

O preflight acontece ANTES de reivindicar eventos ou mudar status de vaga: se o
CDP não responder, o worker fica em standby (com heartbeat) e não cria nenhuma
aprovação, erro ou perfil novo.
"""
from __future__ import annotations

from typing import Callable

from ..core.config import Settings
from ..execution.web.linkedin_logged import wait_for_cdp


def ensure_browser_ready(settings: Settings) -> bool:
    """Standby até o perfil já logado estar disponível em ``browser.cdp_url``."""
    return wait_for_cdp(
        settings.browser.cdp_url,
        timeout_seconds=settings.application.cdp_wait_seconds,
        poll_seconds=settings.application.cdp_poll_seconds,
    )


def with_browser_gate(
    settings: Settings, callback: Callable[[], object]
) -> Callable[[], object]:
    """Envolve o ciclo do worker: sem CDP, não executa e não consome eventos."""

    def gated() -> object:
        if not ensure_browser_ready(settings):
            return {"cdp_ready": 0, "standby": True}
        return callback()

    return gated
