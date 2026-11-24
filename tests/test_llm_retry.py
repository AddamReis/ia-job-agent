"""Retry transitório no DGX, sem fallback para outro provedor."""
from __future__ import annotations

import pytest

import src.llm.factory as factory
from src.core.config import ProviderConfig, Settings
from src.core.errors import LLMError
from src.llm.factory import LLMRouter


class FakeBackend:
    name = "dgx"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def chat(self, **kwargs) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("Request timed out")
        return "ok"


def _router(monkeypatch: pytest.MonkeyPatch, failures: int) -> tuple[LLMRouter, FakeBackend]:
    backend = FakeBackend(failures)
    monkeypatch.setattr(factory, "build_backend", lambda name, settings: backend)
    monkeypatch.setattr(factory.time, "sleep", lambda seconds: None)
    settings = Settings().model_copy(
        update={
            "llm": Settings().llm.model_copy(
                update={
                    "primary": "dgx",
                    "max_retries": 2,
                    "providers": {
                        "dgx": ProviderConfig(base_url="http://10.10.10.2:8011/v1", model="qwen")
                    },
                }
            )
        }
    )
    return LLMRouter(settings), backend


def test_timeout_is_retried_on_same_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    router, backend = _router(monkeypatch, failures=2)

    assert router.chat([{"role": "user", "content": "oi"}]) == "ok"
    assert backend.calls == 3


def test_persistent_timeout_raises_after_configured_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, backend = _router(monkeypatch, failures=99)

    with pytest.raises(LLMError, match="Todos os backends falharam"):
        router.chat([{"role": "user", "content": "oi"}])
    assert backend.calls == 3


def test_named_router_uses_only_the_requested_dgx_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamados: list[str] = []

    def build(name: str, settings: Settings) -> FakeBackend:
        chamados.append(name)
        backend = FakeBackend(0)
        backend.name = name
        return backend

    monkeypatch.setattr(factory, "build_backend", build)
    base = Settings()
    settings = base.model_copy(
        update={
            "llm": base.llm.model_copy(
                update={
                    "primary": "dgx",
                    "runner": "dgx_runner",
                    "providers": {
                        "dgx": ProviderConfig(base_url="http://10.10.10.2:8011/v1", model="27b"),
                        "dgx_runner": ProviderConfig(
                            base_url="http://10.10.10.2:8012/v1", model="moe"
                        ),
                    },
                }
            )
        }
    )

    router = LLMRouter(settings, name=settings.llm.runner)

    assert chamados == ["dgx_runner"]
    assert router.primary_name == "dgx_runner"
