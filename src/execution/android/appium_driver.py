"""Sessão Appium + UiAutomator2 sobre o dispositivo Android real."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from ...core.config import Settings
from ...core.errors import AppiumError, BlockedByPlatformError
from ...core.logging import log_event
from .adb import ADBClient

_KEYBOARD_PACKAGE_HINTS = (
    "inputmethod", "keyboard", "latin", "swiftkey", "honeyboard", "gboard",
)
_KEYBOARD_RESOURCE_HINTS = ("keyboard", "key_", "/key", "ime_")
_CHAR_KEYCODES: dict[str, tuple[int, int]] = {
    **{str(digit): (7 + digit, 0) for digit in range(10)},
    **{chr(code): (29 + index, 0) for index, code in enumerate(range(ord("a"), ord("z") + 1))},
    " ": (62, 0),
    ",": (55, 0),
    ".": (56, 0),
    "#": (18, 0),
    "-": (69, 0),
    "_": (69, 1),
    "@": (77, 1),
    "/": (76, 0),
}

_KEY_ALIASES = {
    "space": " ", "spacebar": " ", "space bar": " ",
    "shift": "<shift>", "caps lock": "<shift>", "capslock": "<shift>",
    "delete": "<backspace>", "backspace": "<backspace>", "del": "<backspace>",
    "enter": "<enter>", "done": "<enter>", "search": "<enter>",
    "go": "<enter>", "return": "<enter>",
    "?123": "<symbols>", "symbols": "<symbols>", "numeric": "<symbols>",
    "abc": "<letters>", "letters": "<letters>",
    "=\\<": "<symbols2>", "=\\": "<symbols2>", "~\\": "<symbols2>",
    "more symbols": "<symbols2>",
}


@dataclass(frozen=True)
class KeyboardKey:
    label: str
    by: str
    value: str


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in value.split("'")) + ")"


def _normalize_key_label(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    alias = _KEY_ALIASES.get(value.lower())
    if alias:
        return alias
    return value if len(value) == 1 else ""


def _resource_key_label(resource_id: str) -> str:
    if "key_" not in resource_id and not resource_id.endswith("key"):
        return ""
    return resource_id.rsplit("_", 1)[-1] if "_" in resource_id else ""


def _is_keyboard_node(node: ElementTree.Element) -> bool:
    attrs = node.attrib
    package = (attrs.get("package") or "").lower()
    resource_id = (attrs.get("resource-id") or "").lower()
    class_name = (attrs.get("class") or "").lower()
    if any(hint in package for hint in _KEYBOARD_PACKAGE_HINTS):
        return True
    if any(hint in resource_id for hint in _KEYBOARD_RESOURCE_HINTS):
        return True
    return "keyboard" in class_name or "inputmethod" in class_name


def parse_keyboard_keys(xml: str) -> dict[str, KeyboardKey]:
    """Mapeia rótulos do teclado visível para localizadores da árvore."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return {}

    keys: dict[str, KeyboardKey] = {}
    for node in root.iter():
        if not _is_keyboard_node(node):
            continue
        sources: list[tuple[str, str, str]] = []
        desc = (node.attrib.get("content-desc") or "").strip()
        text = (node.attrib.get("text") or "").strip()
        resource_id = (node.attrib.get("resource-id") or "").strip()
        if desc:
            sources.append((desc, "accessibility id", desc))
        if text:
            sources.append((text, "xpath", f"//*[@text={_xpath_literal(text)}]"))
        resource_label = _resource_key_label(resource_id)
        if resource_label:
            sources.append((resource_label, "id", resource_id))
        for raw, by, value in sources:
            label = _normalize_key_label(raw)
            if label and label not in keys:
                keys[label] = KeyboardKey(label=label, by=by, value=value)
    return keys


class AppiumDevice:
    """Abstração da interação com a interface nativa via árvore de acessibilidade."""

    def __init__(self, settings: Settings, adb: ADBClient) -> None:
        self.settings = settings
        self.adb = adb
        self.driver: Any = None

    def start(self) -> "AppiumDevice":
        if self.driver is not None:
            return self
        device = self.adb.require_device()
        self.adb.unlock_if_needed()
        if not self.adb.is_installed(self.settings.device.linkedin_package):
            raise AppiumError(
                f"LinkedIn não instalado ({self.settings.device.linkedin_package})"
            )
        try:
            from appium import webdriver
            from appium.options.android import UiAutomator2Options
        except ImportError as exc:  # pragma: no cover
            raise AppiumError("Appium Python Client não instalado") from exc

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.udid = device.serial
        options.app_package = self.settings.device.linkedin_package
        options.no_reset = True
        options.auto_grant_permissions = False
        options.dont_stop_app_on_reset = True
        options.new_command_timeout = 300
        options.adb_exec_timeout = 60_000

        try:
            self.driver = webdriver.Remote(
                self.settings.device.appium_url, options=options
            )
        except Exception as exc:
            raise AppiumError(
                "Não foi possível conectar ao Appium. Suba o servidor com:\n"
                "  npm install -g appium\n"
                "  appium driver install uiautomator2\n"
                "  appium --base-path /wd/hub --port 4723\n"
                f"Detalhe: {type(exc).__name__}: {exc}"
            ) from exc
        log_event("appium_session_started", serial=device.serial)
        return self

    def open_linkedin(self) -> None:
        self.start()
        self.adb.start_app(self.settings.device.linkedin_package)

    def page_source(self) -> str:
        self.start()
        return self.driver.page_source or ""

    def find(self, *, text: str | None = None, resource_id: str | None = None,
             content_desc: str | None = None) -> list[Any]:
        self.start()
        locator = "xpath"
        fragments: list[str] = []
        if text:
            fragments.append(f"@text='{text}'")
        if resource_id:
            fragments.append(f"@resource-id='{resource_id}'")
        if content_desc:
            fragments.append(f"@content-desc='{content_desc}'")
        if not fragments:
            return []
        return self.driver.find_elements(by=locator, value=f"//*[{' or '.join(fragments)}]")

    def has_text(self, text: str) -> bool:
        """Detecta texto visível sem clicar — usado para conferir Easy Apply."""
        elements = self.find(text=text) or self.find(content_desc=text)
        for element in elements:
            try:
                if element.is_displayed():
                    return True
            except Exception:
                continue
        escaped = text.replace("'", "\\'")
        return bool(
            self.driver.find_elements(
                by="xpath",
                value=f"//*[contains(@text,'{escaped}') or contains(@content-desc,'{escaped}')]",
            )
        )

    def click_text(self, text: str) -> bool:
        elements = self.find(text=text) or self.find(content_desc=text)
        for element in elements:
            try:
                if element.is_displayed():
                    element.click()
                    log_event("ui_click", target="text")
                    return True
            except Exception:
                continue
        # Cards do LinkedIn usam content-desc com sufixo, ex.:
        # "Lider Tecnico (.NET) (Verified job)". O match exato falha; cai
        # para substring e clica na row interativa ancestral (Jetpack Compose).
        if len(text) >= 12:
            escaped = text.replace("'", "\\'")
            xpaths = (
                f"//*[contains(@content-desc,'{escaped}') "
                f"or contains(@text,'{escaped}')]/ancestor::*[@clickable='true'][1]",
                f"//*[contains(@content-desc,'{escaped}') "
                f"or contains(@text,'{escaped}')]",
            )
            for xpath in xpaths:
                for element in self.driver.find_elements(by="xpath", value=xpath):
                    try:
                        if element.is_displayed():
                            element.click()
                            log_event("ui_click", target="text_like")
                            return True
                    except Exception:
                        continue
        return False

    def type_text(self, element: Any, value: str) -> None:
        element.clear()
        element.send_keys(value)
        log_event("ui_type", target="field")

    def _wait_for_elements(
        self, by: str, value: str, timeout_seconds: float = 4.0
    ) -> list[Any]:
        """Espera um elemento aparecer sem depender de screenshots."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            elements = self.driver.find_elements(by=by, value=value)
            if elements:
                return elements
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.25)

    def _tap_keyboard_key(self, label: str) -> bool:
        """Toca uma tecla do teclado visível sem registrar o caractere em log."""
        keys = parse_keyboard_keys(self.page_source())
        key = keys.get(label)
        if key is None:
            return False
        try:
            elements = self.driver.find_elements(by=key.by, value=key.value)
        except Exception:
            return False
        for element in elements:
            try:
                if element.is_displayed():
                    element.click()
                    return True
            except Exception:
                continue
        return False

    def _press_char_keyevent(self, char: str) -> bool:
        """Envia um evento de tecla por caractere, com shift para maiúsculas."""
        key = _CHAR_KEYCODES.get(char.lower() if char.isalpha() else char)
        if key is None:
            return False
        keycode, metastate = key
        if char.isalpha() and char.isupper():
            metastate = 1  # SHIFT_ON
        try:
            if metastate:
                self.driver.press_keycode(keycode, metastate=metastate)
            else:
                self.driver.press_keycode(keycode)
        except Exception:
            return False
        return True

    def type_on_visible_keyboard(self, value: str) -> None:
        """Digita como pessoa: tecla por tecla, com pausas pequenas e irregulares."""
        if not value:
            return
        self.start()
        fields = self._wait_for_elements(
            "class name", "android.widget.EditText", timeout_seconds=3.0
        )
        if not fields:
            raise AppiumError("Campo de busca do LinkedIn não encontrado")
        try:
            fields[0].click()
        except Exception:
            pass
        time.sleep(0.4)

        keys = parse_keyboard_keys(self.page_source())
        visible_keys = bool(keys)
        if visible_keys:
            try:
                existing = fields[0].text or ""
            except Exception:
                existing = ""
            for _ in existing:
                if not self._tap_keyboard_key("<backspace>"):
                    raise AppiumError("Teclado visível não permitiu limpar o campo")
        else:
            try:
                fields[0].clear()
            except Exception:
                pass

        symbols = set(".#&@-_/()[]{}!?%*+=:;\"'$~`|\\<>")
        for index, char in enumerate(value):
            tapped = False
            if visible_keys:
                if char == " ":
                    tapped = self._tap_keyboard_key(" ")
                elif char.isupper():
                    self._tap_keyboard_key("<shift>")
                    time.sleep(0.08)
                    tapped = self._tap_keyboard_key(char) or self._tap_keyboard_key(char.lower())
                elif char.isalnum():
                    tapped = self._tap_keyboard_key(char)
                    if not tapped:
                        self._tap_keyboard_key("<shift>")
                        time.sleep(0.08)
                        tapped = self._tap_keyboard_key(char)
                else:
                    tapped = self._tap_keyboard_key(char)
                    if not tapped and char in symbols:
                        if self._tap_keyboard_key("<symbols>"):
                            time.sleep(0.15)
                            tapped = self._tap_keyboard_key(char)
                            if not tapped and self._tap_keyboard_key("<symbols2>"):
                                time.sleep(0.15)
                                tapped = self._tap_keyboard_key(char)
                            if tapped:
                                self._tap_keyboard_key("<letters>")
            else:
                tapped = self._press_char_keyevent(char)
            if not tapped:
                raise AppiumError("Não foi possível digitar a consulta pelo teclado")
            time.sleep(0.08 + (index % 4) * 0.02)
        log_event(
            "keyboard_typed",
            keystrokes=len(value),
            mode="visible_keys" if visible_keys else "key_events",
        )

    def _ensure_search_bar(self) -> list[Any]:
        """Volta para a home do app; se não der, reinicia a tarefa do LinkedIn."""
        search_bar_id = "com.linkedin.android:id/search_bar"
        search_bars = self._wait_for_elements("id", search_bar_id, timeout_seconds=1.0)
        for _ in range(12):
            if search_bars:
                return search_bars
            try:
                self.driver.back()
            except Exception:
                break
            search_bars = self._wait_for_elements("id", search_bar_id, timeout_seconds=0.6)
        if search_bars:
            return search_bars

        package = self.settings.device.linkedin_package
        self.adb.shell(f"am force-stop {package}")
        self.adb.start_app(package)
        return self._wait_for_elements("id", search_bar_id, timeout_seconds=12.0)

    def _click_text_option(self, text: str, timeout_seconds: float = 4.0) -> bool:
        """Clica numa opção de filtro pelo texto, como um toque humano na row.

        No app do LinkedIn a opção é um TextView ``clickable=false`` dentro de uma
        row ``checkable/clickable=true`` (Jetpack Compose). O locator ``text`` não é
        suportado: usamos xpath e clicamos na row interativa mais próxima.
        """
        deadline = time.monotonic() + timeout_seconds
        xpaths = (
            f"//*[@text='{text}']/ancestor::*[@checkable='true'][1]",
            f"//*[@text='{text}']/ancestor::*[@clickable='true'][1]",
            f"//*[@text='{text}']",
        )
        while time.monotonic() < deadline:
            for xpath in xpaths:
                try:
                    nodes = self.driver.find_elements(by="xpath", value=xpath)
                except Exception:
                    continue
                for node in nodes:
                    try:
                        if not node.is_displayed():
                            continue
                        node.click()
                        log_event("filter_option_click", option=text)
                        return True
                    except Exception:
                        continue
            time.sleep(0.25)
        return False

    def _date_filter_is_applied(self) -> bool:
        """Confirma pelo chip final que o filtro de 24h foi aplicado."""
        source = self.driver.page_source or ""
        # Após confirmar, o chip deixa de ser genérico e passa a indicar o período.
        return (
            "Past 24 hours" in source
            or "Últimas 24 horas" in source
            or "Ultimas 24 horas" in source
        ) and "Filter by Date posted" not in source

    def _apply_date_filter(self) -> bool:
        """Best-effort: seleciona 'Past 24 hours' e confirma com 'Show results'."""
        chips = self._wait_for_elements(
            "accessibility id", "Filter by Date posted", timeout_seconds=2.0
        )
        if not chips:
            return False
        try:
            chips[0].click()
            time.sleep(0.8)  # pausa humana para o sheet abrir
            if not self._click_text_option("Past 24 hours"):
                self.driver.back()
                return False
            time.sleep(0.4)  # pausa entre seleção e confirmação
            if not self._click_text_option("Show results"):
                self.driver.back()
                return False
            time.sleep(1.6)  # espera os resultados recarregarem
            applied = self._date_filter_is_applied()
            log_event("date_filter_applied", applied=applied)
            return applied
        except Exception:
            try:
                self.driver.back()
            except Exception:
                pass
        return False

    def search(self, query: str) -> str:
        """Pesquisa no LinkedIn e abre a aba Jobs pela interface nativa."""
        self.start()
        search_bars = self._ensure_search_bar()
        if not search_bars:
            raise AppiumError("Barra de busca do LinkedIn não encontrada")

        try:
            search_bars[0].click()
        except Exception:
            search_bars = self._wait_for_elements(
                "id", "com.linkedin.android:id/search_bar", timeout_seconds=3.0
            )
            if not search_bars:
                raise AppiumError("Barra de busca do LinkedIn não encontrada")
            search_bars[0].click()

        # No app real o EditText só entra na árvore após a transição da busca.
        fields = self._wait_for_elements(
            "class name", "android.widget.EditText", timeout_seconds=6.0
        )
        if not fields:
            raise AppiumError("Campo de busca do LinkedIn não encontrado")
        if self.settings.device.use_visible_keyboard:
            self.type_on_visible_keyboard(query)
            if not self._tap_keyboard_key("<enter>"):
                self.driver.press_keycode(66)  # ENTER
        else:
            self.type_text(fields[0], query)
            self.driver.press_keycode(66)  # ENTER

        job_chips = self._wait_for_elements(
            "accessibility id", "Filter by Jobs", timeout_seconds=8.0
        )
        jobs_filter_clicked = False
        if job_chips:
            try:
                job_chips[0].click()
                jobs_filter_clicked = True
                time.sleep(2.0)
            except Exception:
                jobs_filter_clicked = False

        date_filter_applied = self._apply_date_filter()
        log_event(
            "linkedin_search",
            query_length=len(query),
            jobs_filter=jobs_filter_clicked,
            date_past_24h=date_filter_applied,
        )
        return self.page_source()

    def fill_visible_inputs(self, values: list[str]) -> int:
        """Preenche EditTexts visíveis na ordem da árvore, sem gravar os valores em log."""
        self.start()
        fields = self.driver.find_elements(by="class name", value="android.widget.EditText")
        filled = 0
        for element, value in zip(fields, values):
            if not value:
                continue
            try:
                if element.is_displayed():
                    element.clear()
                    element.send_keys(value)
                    filled += 1
            except Exception:
                continue
        log_event("form_filled", fields=filled)
        return filled

    def press_back(self) -> None:
        self.start()
        self.driver.back()

    def scroll_forward(self) -> None:
        self.start()
        size = self.driver.get_window_size()
        self.driver.swipe(
            size["width"] // 2,
            int(size["height"] * 0.8),
            size["width"] // 2,
            int(size["height"] * 0.25),
            600,
        )

    def detect_block(self) -> None:
        """Para quando há CAPTCHA, verificação ou restrição da plataforma."""
        xml = self.page_source().lower()
        markers = [
            "captcha", "verify you are human", "unusual activity",
            "account restricted", "confirm your identity", "enter the code",
            "verificação", "código de verificação", "atividade incomum",
        ]
        found = next((m for m in markers if m in xml), None)
        if found:
            log_event("platform_block_detected", marker=found)
            raise BlockedByPlatformError(f"Plataforma solicitou verificação: {found}")

    def screenshot_bytes(self) -> bytes | None:
        """Screenshot apenas em memória, nunca gravado em disco."""
        if self.settings.device.screenshot_policy == "disabled":
            return None
        self.start()
        return self.driver.get_screenshot_as_png()

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
