"""Armazenamento seguro da senha de desbloqueio do dispositivo.

A senha nunca é gravada em config.yaml, banco de dados ou logs.
Usa o Windows Credential Manager via `keyring`.
"""
from __future__ import annotations

from ...core.errors import AgentError


class CredentialStore:
    def __init__(self, service: str, username: str) -> None:
        self.service = service
        self.username = username

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AgentError(
                "keyring não instalado. Rode: pip install keyring"
            ) from exc
        return keyring

    def set_unlock_secret(self, secret: str) -> None:
        """Salva (ou substitui) a senha de desbloqueio no cofre do SO."""
        self._keyring().set_password(self.service, self.username, secret)

    def get_unlock_secret(self) -> str | None:
        """Retorna a senha ou None se não estiver cadastrada."""
        return self._keyring().get_password(self.service, self.username)

    def has_unlock_secret(self) -> bool:
        return self.get_unlock_secret() is not None

    def delete_unlock_secret(self) -> None:
        try:
            self._keyring().delete_password(self.service, self.username)
        except Exception:
            pass
