"""Overrides de configuração necessários para o deploy no DGX Spark."""
from __future__ import annotations

from pathlib import Path

from src.core.config import Settings


def test_browser_source_can_be_overridden_for_dgx(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "browser:\n  source: chrome\n", encoding="utf-8"
    )
    monkeypatch.setenv("AI_JOB_BROWSER_SOURCE", "")

    settings = Settings.load(tmp_path)

    assert settings.browser.source == ""


def test_browser_source_defaults_to_config_when_env_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        "browser:\n  source: chrome\n", encoding="utf-8"
    )
    monkeypatch.delenv("AI_JOB_BROWSER_SOURCE", raising=False)

    settings = Settings.load(tmp_path)

    assert settings.browser.source == "chrome"
