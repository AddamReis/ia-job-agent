"""Testes sem dispositivo para o teclado visível do Android."""
from src.execution.android.appium_driver import (
    _CHAR_KEYCODES,
    _xpath_literal,
    parse_keyboard_keys,
)

KEYBOARD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" package="com.linkedin.android">
    <node class="android.widget.EditText" package="com.linkedin.android" text="Senior"/>
    <node class="android.view.ViewGroup" package="com.google.android.inputmethod.latin" resource-id="com.google.android.inputmethod.latin:id/keyboard_view">
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" content-desc="Shift" resource-id="com.google.android.inputmethod.latin:id/key_shift"/>
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" text="A" resource-id="com.google.android.inputmethod.latin:id/key_a"/>
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" content-desc="space" resource-id="com.google.android.inputmethod.latin:id/key_space"/>
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" content-desc="?123" resource-id="com.google.android.inputmethod.latin:id/key_symbols"/>
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" text="#" resource-id="com.google.android.inputmethod.latin:id/key_number"/>
      <node class="android.widget.Button" package="com.google.android.inputmethod.latin" content-desc="enter" resource-id="com.google.android.inputmethod.latin:id/key_enter"/>
    </node>
  </node>
</hierarchy>
"""


def test_parse_keyboard_keys_ignores_app_fields():
    keys = parse_keyboard_keys(KEYBOARD_XML)
    assert keys["<shift>"].by == "accessibility id"
    assert keys["A"].value == "//*[@text='A']"
    assert keys[" "].value == "space"
    assert keys["<symbols>"].value == "?123"
    assert keys["#"].by == "xpath"
    assert keys["<enter>"].value == "enter"
    assert "Senior" not in keys


def test_keycodes_cover_search_queries():
    assert _CHAR_KEYCODES["s"] == (47, 0)
    assert _CHAR_KEYCODES[" "][0] == 62
    assert _CHAR_KEYCODES["."][0] == 56
    assert _CHAR_KEYCODES["#"][0] == 18
    assert "é" not in _CHAR_KEYCODES


def test_xpath_literal_escapes_quotes():
    assert _xpath_literal("Senior .NET") == "'Senior .NET'"
    assert _xpath_literal("it's") == '"it\'s"'
