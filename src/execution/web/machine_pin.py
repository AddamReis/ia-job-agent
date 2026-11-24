"""PIN de desbloqueio da máquina Windows no cofre do sistema operacional.

O valor nunca vai para ``config.yaml``, banco de dados, evidências ou logs, e
nunca é enviado à DGX: o worker remoto usa apenas o Chrome logado via CDP.
Este módulo existe no código local para que o operador possa cadastrar/consultar
o segredo sem expor o número.
"""
from __future__ import annotations

from ...core.errors import AgentError


class MachinePinStore:
    SERVICE = "local-ai-job-agent-machine"
    USERNAME = "windows-pin"

    def __init__(self, service: str = SERVICE, username: str = USERNAME) -> None:
        self.service = service
        self.username = username

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AgentError("keyring não instalado. Rode: pip install keyring") from exc
        return keyring

    def set(self, pin: str) -> None:
        valor = str(pin or "").strip()
        if not valor:
            raise AgentError("PIN da máquina não pode ser vazio")
        self._keyring().set_password(self.service, self.username, valor)

    def get(self) -> str | None:
        return self._keyring().get_password(self.service, self.username)

    def has(self) -> bool:
        return self.get() is not None

    def delete(self) -> None:
        try:
            self._keyring().delete_password(self.service, self.username)
        except Exception:
            pass
