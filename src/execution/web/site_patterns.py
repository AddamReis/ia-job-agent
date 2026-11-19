"""Memória local de padrões de formulários aprendidos pelo agente de browser.

O aprendizado é **local e auditável**: nada é enviado para fora, e apenas a
relação ``campo -> origem no perfil`` é persistida (nunca o valor pessoal).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def domain_of(url: str) -> str:
    """Domínio efetivo da página, já após redirecionamentos."""
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


class SitePatternStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "sites": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("sites"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "sites": {}}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def suggest(self, url: str, signature: str) -> dict[str, Any] | None:
        domain = domain_of(url)
        if not domain or not signature:
            return None
        sites = self.load()["sites"]
        return sites.get(domain, {}).get("fields", {}).get(signature) or None

    def record(
        self, url: str, signature: str, selector: str, source: str, *, success: bool = True
    ) -> None:
        """Registra/refuerça um padrão que levou a um envio bem-sucedido."""
        domain = domain_of(url)
        if not domain or not signature or not selector or not source:
            return
        data = self.load()
        sites = data.setdefault("sites", {})
        fields = sites.setdefault(domain, {}).setdefault("fields", {})
        current = fields.get(signature, {})
        if success:
            current["successes"] = int(current.get("successes", 0)) + 1
            current["selector"] = selector
            current["source"] = source
        else:
            current["failures"] = int(current.get("failures", 0)) + 1
        fields[signature] = current
        self._save(data)
