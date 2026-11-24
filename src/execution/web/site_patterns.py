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

    @staticmethod
    def _action_key(intent: str, trigger: str) -> str:
        return f"{str(intent or '').strip().lower()}::{str(trigger or '').strip().lower()}"

    def suggest_action(
        self, url: str, intent: str, trigger: str = ""
    ) -> dict[str, Any] | None:
        """Devolve uma ação já aprendida para este domínio/intenção/gatilho."""
        domain = domain_of(url)
        if not domain or not intent:
            return None
        actions = self.load().get("actions", {})
        entry = actions.get(domain, {}).get(self._action_key(intent, trigger))
        return entry or None

    def record_action(
        self,
        url: str,
        intent: str,
        trigger: str,
        action: str,
        dialog_signature: str = "",
        *,
        success: bool = True,
        source: str = "",
        reason: str = "",
    ) -> None:
        """Persiste uma decisão de UI que levou (ou não) a um envio concluído.

        Nunca guarda dados pessoais: apenas rótulos de botão, assinatura do
        diálogo e a origem da decisão (memória, LLM ou regra local).
        """
        domain = domain_of(url)
        if not domain or not intent or not action or action.lower() == "none":
            return
        data = self.load()
        actions = data.setdefault("actions", {})
        intent_map = actions.setdefault(domain, {})
        key = self._action_key(intent, trigger)
        current = intent_map.get(key, {})
        if success:
            current["successes"] = int(current.get("successes", 0)) + 1
            current["action"] = action
            current["dialog_signature"] = dialog_signature
            current["source"] = source
            current["reason"] = str(reason)[:240]
        else:
            current["failures"] = int(current.get("failures", 0)) + 1
        intent_map[key] = current
        self._save(data)
