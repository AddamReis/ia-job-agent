"""Detecção do dispositivo, verificação do ADB e desbloqueio de tela."""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from ...core.errors import ADBError, HumanActionRequired
from ...core.logging import log_event
from .credential_store import CredentialStore


@dataclass(slots=True)
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    @property
    def authorized(self) -> bool:
        return self.state == "device"


class ADBClient:
    def __init__(self, adb_path: str, credential_store: CredentialStore | None = None) -> None:
        self.adb_path = adb_path
        self.credentials = credential_store
        self.udid: str | None = None

    def run(self, *args: str, timeout: int = 30) -> str:
        command = [self.adb_path, *args]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
            )
        except FileNotFoundError as exc:
            raise ADBError(f"ADB não encontrado em: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ADBError(f"ADB timeout: {' '.join(args)}") from exc
        if result.returncode != 0 and "error:" in result.stderr.lower():
            raise ADBError(result.stderr.strip()[:500])
        return (result.stdout or "") + (result.stderr or "")

    def _base(self) -> list[str]:
        return ["-s", self.udid] if self.udid else []

    def version(self) -> str:
        return self.run("version", timeout=10).strip().splitlines()[0]

    def devices(self) -> list[DeviceInfo]:
        output = self.run("devices", "-l", timeout=20)
        devices: list[DeviceInfo] = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            model = product = ""
            for piece in parts[2:]:
                if piece.startswith("model:"):
                    model = piece.split(":", 1)[1]
                elif piece.startswith("product:"):
                    product = piece.split(":", 1)[1]
            devices.append(DeviceInfo(serial, state, model, product))
        return devices

    def connected_device(self) -> DeviceInfo | None:
        """Retorna o primeiro dispositivo autorizado, sem lançar exceção.

        Usado para consultar a fila Easy Apply mesmo com o celular desconectado.
        """
        try:
            return next((device for device in self.devices() if device.authorized), None)
        except ADBError:
            return None

    def has_device(self) -> bool:
        return self.connected_device() is not None

    def require_device(self) -> DeviceInfo:
        devices = self.devices()
        if not devices:
            raise ADBError(
                "Nenhum dispositivo detectado. Conecte o celular via USB e autorize o ADB."
            )
        unauthorized = [d for d in devices if not d.authorized]
        if unauthorized and not any(d.authorized for d in devices):
            raise ADBError(
                f"Dispositivo {unauthorized[0].serial} não autorizado. "
                "Aceite a solicitação 'Permitir depuração USB?' no celular."
            )
        device = next(d for d in devices if d.authorized)
        self.udid = device.serial
        log_event("device_detected", serial=device.serial, model=device.model)
        return device

    def shell(self, command: str, timeout: int = 30) -> str:
        return self.run(*self._base(), "shell", command, timeout=timeout)

    def is_installed(self, package: str) -> bool:
        output = self.shell(f"pm list packages {package}")
        return f"package:{package}" in output

    def start_app(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1", timeout=20)
        log_event("app_opened", package=package)

    def current_focus(self) -> str:
        return self.shell("dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'")

    def open_url(
        self, url: str, *, label: str = "", package: str = "", force_restart: bool = True
    ) -> None:
        """Abre uma URL no app via deep link (somente dispositivo logado).

        O ID real da vaga entra na URL pública do LinkedIn; nenhum token ou
        credencial é passado. ``label`` é o que aparece nos logs (sem dados
        sensíveis).

        O LinkedIn reaproveita a atividade atual e ignora o novo deep link,
        mantendo a vaga anterior na tela. Por isso o app é reiniciado antes do
        intent e o pacote é fixado com ``-p``.
        """
        if not url:
            raise ADBError("URL vazia para deep link")
        safe = url.replace('"', "%22").replace(" ", "%20")
        target = package or "com.linkedin.android"
        if force_restart and target:
            self.shell(f"am force-stop {target}", timeout=20)
        command = f'am start -a android.intent.action.VIEW -d "{safe}"'
        if target:
            command += f" -p {target}"
        self.shell(command, timeout=30)
        log_event(
            "deep_link_opened",
            label=label,
            scheme=safe.split(":", 1)[0],
            forced=force_restart,
        )

    def push_file(self, local_path: str, remote_name: str) -> str:
        """Copia o CV para o dispositivo quando o app exige seleção de arquivo."""
        from pathlib import Path

        local = Path(local_path)
        if not local.exists():
            raise ADBError(f"Arquivo não encontrado: {local}")
        remote = f"/sdcard/Download/{remote_name or local.name}"
        self.run(*self._base(), "push", str(local), remote, timeout=120)
        log_event("cv_pushed_to_device", file=local.name)
        return remote

    def is_locked(self) -> bool:
        """Detecta keyguard mesmo quando o foco é o NotificationShade.

        Em MIUI, a tela de senha aparece com foco em ``NotificationShade`` e
        ``mWakefulness=Dozing``; checar apenas a palavra "keyguard" no foco
        produzia falso negativo. ``isKeyguardShowing`` é o sinal confiável.
        """
        power = self.shell("dumpsys power | grep mWakefulness").lower()
        if "asleep" in power or "dozing" in power:
            return True
        keyguard = self.shell("dumpsys window | grep isKeyguardShowing").lower()
        if "iskeyguardshowing=true" in keyguard:
            return True
        return "keyguard" in self.current_focus().lower()

    @staticmethod
    def _encode_input_text(text: str) -> str:
        """`input text` usa %s para espaço; nunca registrar o valor original."""
        return text.replace(" ", "%s")

    @staticmethod
    def _unlock_keycode(char: str) -> int | None:
        """Keycode Android por caractere, sem registrar o valor digitado."""
        if char == " ":
            return 62
        lower = char.lower()
        if lower.isdigit():
            return 7 + int(lower)
        if "a" <= lower <= "z":
            return 29 + (ord(lower) - ord("a"))
        return None

    def _type_unlock_secret(self, secret: str) -> None:
        """Digita via keyevents (funciona com espaços no keyguard do MIUI)."""
        for char in secret:
            code = self._unlock_keycode(char)
            if code is None:
                raise HumanActionRequired(
                    "desbloqueio de tela", "senha contém caractere sem keycode conhecido"
                )
            self.shell(f"input keyevent {code}")
            time.sleep(0.08)

    def unlock_if_needed(self) -> bool:
        """Desbloqueia usando a senha guardada no cofre do SO. Nunca faz log da senha."""
        if not self.is_locked():
            return False
        if self.credentials is None or not self.credentials.has_unlock_secret():
            raise HumanActionRequired(
                "desbloqueio de tela",
                "dispositivo bloqueado e nenhuma senha cadastrada. Rode: agent device --set-unlock",
            )
        secret = self.credentials.get_unlock_secret() or ""
        try:
            self.shell("input keyevent 26")  # wake
            time.sleep(0.5)
            self.shell("input swipe 300 1300 300 400 250")  # abrir campo de senha
            time.sleep(0.5)
            self._type_unlock_secret(secret)
            self.shell("input keyevent 66")  # ENTER
            time.sleep(1.5)
        except ADBError as exc:
            raise HumanActionRequired("desbloqueio de tela", str(exc)) from exc
        if self.is_locked():
            raise HumanActionRequired(
                "desbloqueio de tela", "a senha salva não desbloqueou o dispositivo"
            )
        log_event("device_unlocked")
        return True
