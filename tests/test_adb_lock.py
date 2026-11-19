"""Testes sem dispositivo para detecção de keyguard e digitação de desbloqueio."""
from src.execution.android.adb import ADBClient


def _adb(power: str, keyguard: str, focus: str) -> ADBClient:
    adb = ADBClient("adb", None)

    def fake_shell(command: str, timeout: int = 30) -> str:
        if command.startswith("dumpsys power"):
            return power
        if command.startswith("dumpsys window | grep isKeyguardShowing"):
            return keyguard
        return focus

    adb.shell = fake_shell  # type: ignore[method-assign]
    adb.current_focus = lambda: focus  # type: ignore[method-assign]
    return adb


def test_dozing_notification_shade_considerado_bloqueado():
    """MIUI mostra a tela de senha com foco no shade e power=Dozing."""
    adb = _adb(
        "mWakefulness=Dozing",
        "isKeyguardShowing=false",
        "mCurrentFocus=Window{... NotificationShade}",
    )
    assert adb.is_locked() is True


def test_iskeyguardshowing_true_bloqueia_mesmo_acordado():
    adb = _adb(
        "mWakefulness=Awake",
        "isKeyguardShowing=true",
        "mCurrentFocus=Window{... NotificationShade}",
    )
    assert adb.is_locked() is True


def test_launcher_acordado_nao_bloqueado():
    adb = _adb(
        "mWakefulness=Awake",
        "isKeyguardShowing=false",
        "mCurrentFocus=Window{... com.miui.home/.launcher.Launcher}",
    )
    assert adb.is_locked() is False


def test_keycodes_de_desbloqueio():
    assert ADBClient._unlock_keycode("a") == 29
    assert ADBClient._unlock_keycode(" ") == 62
    assert ADBClient._unlock_keycode("7") == 14
    assert ADBClient._unlock_keycode("ç") is None


def test_tipagem_por_keyevents_nao_expoe_valor():
    adb = ADBClient("adb", None)
    comandos: list[str] = []
    adb.shell = lambda command, timeout=30: comandos.append(command) or ""  # type: ignore[method-assign]
    adb._type_unlock_secret("a    ")
    assert comandos == [
        "input keyevent 29",
        "input keyevent 62",
        "input keyevent 62",
        "input keyevent 62",
        "input keyevent 62",
    ]
