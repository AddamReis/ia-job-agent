"""Leitura e escrita de formulários no Chrome logado via CDP.

Os campos recebem um atributo temporário ``data-agent-field`` para que a
escrita seja estável dentro da mesma página. Nenhum dado é enviado fora da
máquina; o preenchimento usa apenas o perfil local.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ...core.errors import ValidationError
from ...core.logging import log_event

_SNAPSHOT_JS = r"""
() => {
  const cssFor = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const attrs = ['name', 'aria-label', 'placeholder'];
    for (const a of attrs) {
      const v = el.getAttribute(a);
      if (v) return '[' + a + '=' + JSON.stringify(v) + ']';
    }
    const path = [];
    let node = el;
    while (node && node.tagName && path.length < 6) {
      let step = node.tagName.toLowerCase();
      if (node.id) { step += '#' + CSS.escape(node.id); path.unshift(step); break; }
      const parent = node.parentElement;
      if (parent) {
        const siblings = [...parent.children].filter(n => n.tagName === node.tagName);
        if (siblings.length > 1) step += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      path.unshift(step);
      node = parent;
    }
    return path.join(' > ');
  };

  const scopes = [...document.querySelectorAll(
    'dialog[open], [role="dialog"], [data-test-id="dialog"], form'
  )];
  const inScope = (el) => scopes.some(s => s.contains(el));
  const generic = /selecionar idioma|select language|alerta para vagas|job alert|buscar|search/i;
  const nodes = [...document.querySelectorAll(
    'input, textarea, select, [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"]'
  )];
  const fields = [];
  let index = 0;
  for (const el of nodes) {
    const type = el.tagName.toLowerCase() === 'select'
      ? 'select'
      : (el.getAttribute('type') || el.getAttribute('role') || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) continue;
    if (el.disabled) continue;
    const scoped = inScope(el);
    const rect = el.getBoundingClientRect();
    if (!scoped && (!rect.width || !rect.height)) continue;
    let label = '';
    if (el.id) {
      const labelEl = document.querySelector('label[for=' + JSON.stringify(el.id) + ']');
      label = labelEl ? labelEl.innerText.trim() : '';
    }
    label = label || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || '';
    const meaningful = String(label).trim() && !generic.test(String(label));
    if (!scoped && !meaningful) continue;
    const parent = el.parentElement || el;
    const options = el.tagName === 'SELECT'
      ? [...el.options].map(o => (o.text || '').trim()).filter(Boolean)
      : [];
    el.setAttribute('data-agent-field', String(index));
    fields.push({
      index: index,
      tag: el.tagName.toLowerCase(),
      type: type,
      label: String(label).slice(0, 200),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 200),
      name: (el.getAttribute('name') || '').slice(0, 200),
      identifier: (el.id || el.getAttribute('name') || '').slice(0, 200),
      autocomplete: (el.getAttribute('autocomplete') || '').slice(0, 100),
      required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
      options: options.slice(0, 60),
      nearby_text: (parent.innerText || '').slice(0, 200),
      selector: cssFor(el),
    });
    index += 1;
  }
  return {
    url: location.href,
    title: document.title,
    fields: fields,
    text: (document.body.innerText || '').slice(0, 8000),
  };
}
"""

_SUBMIT_LABELS = (
    "Submit application", "Submit", "Enviar candidatura", "Enviar", "Aplicar",
    "Review application", "Revisar candidatura",
)
_CONFIRM_MARKS = (
    "application submitted", "candidatura enviada", "inscrição enviada",
    "inscricao enviada", "seu pedido foi enviado", "your application was submitted",
)
_BLOCK_MARKS = ("captcha", "verificação", "verificacao", "unusual activity", "atividade incomum")

_UPLOAD_LABELS = (
    "Carregar currículo", "Carregar curriculo", "Anexar currículo",
    "Anexar curriculo", "Selecionar arquivo", "Escolher arquivo",
    "Upload resume", "Attach resume", "Choose file",
)

_RESUME_STATE_JS = r"""
() => ({
  blocked: /captcha|verifica[cç][aã]o|unusual activity|atividade incomum/i
    .test((document.body.innerText || '').slice(0, 3000)),
  files: [...document.querySelectorAll('input[type=file]')]
    .map(input => [...(input.files || [])].map(file => file.name)),
  radios: [...document.querySelectorAll('input[type=radio]')].map(radio => ({
    label: radio.getAttribute('aria-label') || radio.getAttribute('title') || '',
    checked: Boolean(radio.checked),
  })),
})
"""

_RESUME_CHECK_JS = r"""
(name) => [...document.querySelectorAll('input[type=radio]')].some(radio =>
  (radio.getAttribute('aria-label') || radio.getAttribute('title') || '').includes(name)
)
"""

_RESUME_SELECTED_JS = r"""
(name) => [...document.querySelectorAll('input[type=radio]')].some(radio =>
  radio.checked &&
  ((radio.getAttribute('aria-label') || radio.getAttribute('title') || '').includes(name) ||
   (radio.parentElement && (radio.parentElement.innerText || '').includes(name)))
)
"""

_RESUME_SELECT_JS = r"""
(name) => {
  const radio = [...document.querySelectorAll('input[type=radio]')].find(r =>
    (r.getAttribute('aria-label') || r.getAttribute('title') || '').includes(name)
  );
  if (!radio) return false;
  radio.click();
  return true;
}
"""


def snapshot_form(page: Any) -> dict[str, Any]:
    return page.evaluate(_SNAPSHOT_JS) or {}


def _resume_selected(page: Any, name: str) -> bool:
    return bool(page.evaluate(_RESUME_SELECTED_JS, name))


def ensure_resume_uploaded(page: Any, path: str | Path) -> str:
    """Garante que o CV desejado esteja anexado ao formulário.

    Retorna ``already_attached``, ``selected``, ``uploaded`` ou um código de
    falha. Nunca faz Submit. Se o CV já estiver anexado, não uploada de novo.
    """
    if not path:
        return "missing"
    cv = Path(path)
    if not cv.is_file():
        raise ValidationError(f"currículo informado não existe: {cv}")
    name = cv.name

    if _resume_selected(page, name):
        log_event("browser_apply_resume_already_attached", filename=name)
        return "already_attached"

    if page.evaluate(_RESUME_CHECK_JS, name):
        if page.evaluate(_RESUME_SELECT_JS, name) and _resume_selected(page, name):
            log_event("browser_apply_resume_selected", filename=name)
            return "selected"
        return "select_failed"

    clicked = ""
    for label in _UPLOAD_LABELS:
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if locator.count() == 0:
                continue
            locator.first.click()
            clicked = label
            break
        except Exception:
            continue
    if not clicked:
        return "upload_button_not_found"

    # O LinkedIn cria um input[type=file] oculto quando o botão é clicado.
    file_input = page.locator('input[type="file"]')
    if file_input.count() == 0:
        page.wait_for_timeout(1000)
    if file_input.count() == 0:
        return "file_input_not_found"
    file_input.last.set_input_files(str(cv))

    deadline = time.time() + 20
    while time.time() < deadline:
        state = page.evaluate(_RESUME_STATE_JS) or {}
        if state.get("blocked"):
            return "blocked"
        uploaded = any(name in item for group in state.get("files", []) for item in group)
        if uploaded:
            radios = state.get("radios", [])
            match = next((r for r in radios if name in r.get("label", "")), None)
            if match is not None and not match.get("checked"):
                page.evaluate(_RESUME_SELECT_JS, name)
            if _resume_selected(page, name):
                log_event("browser_apply_resume_uploaded", filename=name, button=clicked)
                return "uploaded"
        page.wait_for_timeout(500)
    return "unverified"


def fill_field(page: Any, index: int, kind: str, value: str) -> bool:
    locator = page.locator(f'[data-agent-field="{index}"]')
    if locator.count() == 0:
        return False
    target = locator.first
    if kind == "file":
        target.set_input_files(value)
    elif kind in {"checkbox", "radio"}:
        truthy = value.strip().lower() in {"true", "sim", "yes", "1", "checked"}
        if kind == "radio" and not truthy:
            # Radio de currículo recebe o CAMINHO como valor. Nunca marque o
            # primeiro da grupo: escolha a opção cujo rótulo contém o arquivo.
            info = target.evaluate(
                """(el) => {
                  const label = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                  const parentText = el.parentElement ? (el.parentElement.innerText || '') : '';
                  const group = el.name
                    ? [...document.querySelectorAll('input[type=radio]')]
                        .filter(r => r.name === el.name)
                    : [el];
                  return {text: label + ' ' + parentText, count: group.length};
                }"""
            ) or {}
            desired = Path(value.strip()).name if value.strip() else ""
            matches = desired and desired.lower() in str(info.get("text", "")).lower()
            if not matches and int(info.get("count", 0)) != 1:
                log_event(
                    "browser_apply_radio_no_match",
                    desired=desired,
                    options=str(info.get("text", ""))[:120],
                )
                return False
            target.evaluate("(el) => { if (!el.checked) el.click(); return el.checked; }")
            return True
        # Inputs customizados do LinkedIn são visualmente ocultos; check()
        # falha por "element is not visible". O clique via DOM é equivalente.
        target.evaluate(
            "(el, want) => { if (el.checked !== want) el.click(); return el.checked; }",
            truthy,
        )
    elif kind == "select":
        try:
            target.select_option(label=value)
        except Exception:
            target.select_option(value)
    else:
        target.fill(value)
    return True


def click_submit(page: Any, label: str = "") -> bool:
    labels = [label] if label else list(_SUBMIT_LABELS)
    for text in labels:
        if not text:
            continue
        try:
            locator = page.get_by_role("button", name=re.compile(re.escape(text), re.I))
            if locator.count() > 0:
                locator.first.click()
                log_event("browser_apply_submit_clicked", label=text)
                return True
        except Exception:
            continue
        try:
            locator = page.locator(f'button:has-text("{text}")')
            if locator.count() > 0:
                locator.first.click()
                log_event("browser_apply_submit_clicked", label=text)
                return True
        except Exception:
            continue
    return False


def submission_state(page: Any) -> str:
    try:
        text = (page.locator("body").inner_text() or "").lower()
    except Exception:
        return "unknown"
    if any(mark in text for mark in _BLOCK_MARKS):
        return "blocked"
    if any(mark in text for mark in _CONFIRM_MARKS):
        return "submitted"
    return "unknown"
