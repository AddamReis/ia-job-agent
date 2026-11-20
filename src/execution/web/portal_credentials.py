"""Credenciais de portais conhecidos no cofre do sistema operacional.

Usuário e senha ficam no Windows Credential Manager via ``keyring`` — nunca em
config.yaml, banco de dados ou logs. O domínio é a chave lógica do portal.
"""
from __future__ import annotations

from ...core.errors import AgentError


class PortalCredentialStore:
    SERVICE = "local-ai-job-agent"

    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AgentError("keyring não instalado. Rode: pip install keyring") from exc
        return keyring

    @staticmethod
    def _keys(domain: str) -> tuple[str, str]:
        base = f"portal:{domain.strip().lower()}"
        return f"{base}:user", f"{base}:pass"

    def set(self, domain: str, username: str, password: str) -> None:
        key_user, key_pass = self._keys(domain)
        keyring = self._keyring()
        keyring.set_password(self.service, key_user, username)
        keyring.set_password(self.service, key_pass, password)

    def get(self, domain: str) -> tuple[str, str] | None:
        key_user, key_pass = self._keys(domain)
        keyring = self._keyring()
        username = keyring.get_password(self.service, key_user)
        password = keyring.get_password(self.service, key_pass)
        if not username or not password:
            return None
        return username, password

    def has(self, domain: str) -> bool:
        return self.get(domain) is not None

    def delete(self, domain: str) -> None:
        key_user, key_pass = self._keys(domain)
        keyring = self._keyring()
        for key in (key_user, key_pass):
            try:
                keyring.delete_password(self.service, key)
            except Exception:
                pass
