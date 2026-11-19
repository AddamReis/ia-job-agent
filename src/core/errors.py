"""Exceções de domínio do agente."""


class AgentError(Exception):
    """Erro base."""


class ConfigError(AgentError):
    """Configuração inválida ou ausente."""


class DeviceError(AgentError):
    """Falha de comunicação com o dispositivo."""


class ADBError(DeviceError):
    """Falha no ADB."""


class AppiumError(DeviceError):
    """Falha na sessão Appium/UiAutomator2."""


class BlockedByPlatformError(AgentError):
    """A plataforma apresentou bloqueio, CAPTCHA, MFA ou verificação."""


class HumanActionRequired(AgentError):
    """Ação sensível que exige intervenção humana."""

    def __init__(self, reason: str, details: str | None = None) -> None:
        self.reason = reason
        self.details = details
        super().__init__(f"{reason}: {details or 'sem detalhes'}")


class LLMError(AgentError):
    """Falha no backend de linguagem."""


class LimitReached(AgentError):
    """Limite diário ou de execução atingido."""


class DuplicateApplication(AgentError):
    """Candidatura já registrada."""


class ValidationError(AgentError):
    """Validação pós-envio falhou."""


class PausedError(AgentError):
    """Execução pausada pelo usuário."""


class StoppedError(AgentError):
    """Execução interrompida pelo usuário."""
