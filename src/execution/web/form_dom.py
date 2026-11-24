"""Leitura e escrita de formulários no Chrome logado via CDP.

Os campos recebem um atributo temporário ``data-agent-field`` para que a
escrita seja estável dentro da mesma página. Nenhum dado é enviado fora da
máquina; o preenchimento usa apenas o perfil local.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ...core.errors import HumanActionRequired, ValidationError
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
  // Controles de player (video.js etc.) também usam checkbox/radio; não são
  // campos da candidatura e poluem a assinatura do formulário.
  const playerControl = (el) => Boolean(el.closest(
    '.video-js,.vjs-control-bar,[class*="vjs-"],[data-testid*="video"]'
  ));
  const nodes = [...document.querySelectorAll(
    'input, textarea, select, [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"]'
  )].filter(el => !playerControl(el));
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
      value: String(el.value || '').slice(0, 300),
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
    "Submit application", "Submit", "Send application", "Enviar candidatura",
    "Enviar aplicação", "Enviar aplicacao", "Enviar inscrição", "Enviar inscricao",
    "Enviar agora", "Enviar", "Aplicar", "Concluir", "Finalizar",
    "Review application", "Revisar candidatura",
)
_CONFIRM_MARKS = (
    "application submitted", "application sent", "candidatura enviada",
    "candidatura enviada com sucesso", "inscrição enviada",
    "inscricao enviada", "seu pedido foi enviado",
    "your application was submitted", "thank you for applying",
    "we have received your application", "your application has been received",
)
_BLOCK_MARKS = ("captcha", "verificação", "verificacao", "unusual activity", "atividade incomum")

_NEXT_LABELS = (
    "Next", "Next step", "Continue", "Continuar", "Próximo", "Proximo",
    "Avançar", "Avancar", "Seguinte", "Avaliar", "Revisar", "Review",
)
# Rótulos de avanço nunca podem confundir com Submit/Review final.
_SUBMIT_WORDS = (
    "submit", "enviar candidatura", "enviar", "aplicar", "review application",
    "revisar candidatura", "finalizar",
)

_UPLOAD_LABELS = (
    "Carregar currículo", "Carregar curriculo", "Anexar currículo",
    "Anexar curriculo", "Selecionar arquivo", "Escolher arquivo",
    "Upload resume", "Upload CV", "Upload curriculo", "Attach resume",
    "Attach CV", "Choose file", "Browse", "Fazer upload", "Subir currículo",
    "Subir curriculo", "Adjuntar currículum", "Cargar CV",
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

# Portais externos mostram o nome do arquivo em texto/link, sem radio. A checagem
# abaixo reconhece esse estado real; ela NÃO preenche nem inventa o upload.
_RESUME_ATTACHED_JS = r"""
(name) => {
  const alvo = String(name || '').toLowerCase();
  // Alignerr/MUI mostram o nome do arquivo em texto solto, sem aria/title.
  if ((document.body.innerText || '').toLowerCase().includes(alvo)) return true;
  return [...document.querySelectorAll('input, a, [aria-label], [title]')].some(el => {
    const value = String(el.value || '') + ' ' +
      String(el.getAttribute('aria-label') || '') + ' ' +
      String(el.getAttribute('title') || '') + ' ' +
      String(el.innerText || '');
    return value.toLowerCase().includes(alvo);
  });
}
"""

# Portais como Alignerr primeiro mostram "File • Uploading" e só depois
# "File • Uploaded". Não liberar o Continue enquanto ainda estiver enviando.
_RESUME_UPLOADING_JS = r"""
(name) => {
  const alvo = String(name || '').toLowerCase();
  const texto = (document.body.innerText || '').toLowerCase();
  if (!alvo || !texto.includes(alvo)) return false;
  return /uploading|upload in progress|enviando|carregando|processando|processing|cargando/i
    .test(texto);
}
"""


_VISIBLE_BUTTONS_JS = r"""
() => [...document.querySelectorAll('button')]
  .filter(b => {
    const rect = b.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  })
  .map(b => ({
    text: (b.innerText || '').trim().slice(0, 120),
    aria: b.getAttribute('aria-label') || '',
    disabled: Boolean(b.disabled),
  }))
  .filter(b => b.text || b.aria)
  .slice(0, 40)
"""


def snapshot_form(page: Any) -> dict[str, Any]:
    return page.evaluate(_SNAPSHOT_JS) or {}


def visible_buttons(page: Any) -> list[dict[str, Any]]:
    """Botões visíveis do step atual, para diagnóstico sem expor dados."""
    return page.evaluate(_VISIBLE_BUTTONS_JS) or []


_EXPERIENCE_STEP_MARKS = (
    "work experience",
    "work history",
    "experiência profissional",
    "experiencia profissional",
)
_EXPERIENCE_REMOVE_LABELS = (
    "Excluir experiência",
    "Excluir experiência profissional",
    "Remove experience",
    "Delete experience",
    "Eliminar experiencia",
)
# O LinkedIn abre um dialogo de confirmacao depois de "Excluir experiencia".
# Esses rotulos sao apenas o fallback local: a decisao normal e aprendida ou
# interpretada pelo agente a partir do estado real do dialogo.
_EXPERIENCE_CONFIRM_LABELS = (
    "Remover", "Delete", "Remove", "Confirmar", "Confirm", "Excluir",
)
_DIALOG_STATE_JS = r"""
() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const dialogs = [...document.querySelectorAll(
    '[role="dialog"],[role="alertdialog"],[aria-modal="true"]'
  )].filter(visible);
  const root = dialogs[dialogs.length - 1] || document.body;
  const buttons = [...root.querySelectorAll('button')]
    .filter(visible)
    .map((b) => ({
      text: (b.innerText || '').trim().slice(0, 120),
      aria: (b.getAttribute('aria-label') || '').trim().slice(0, 120),
    }))
    .filter((b) => b.text || b.aria)
    .slice(0, 40);
  return {
    url: location.href,
    text: (root.innerText || '').trim().slice(0, 1600),
    buttons,
  };
}
"""
_EXPERIENCE_CARD_JS = r"""
(btn) => {
  const root = btn.closest('li, article, section, [role="group"]')
    || btn.parentElement?.parentElement
    || btn.parentElement;
  const inputs = root ? [...root.querySelectorAll('input, textarea')] : [];
  const field = inputs.find(el => /your title|job title|\btitle\b|cargo|t[ií]tulo/i.test(
    ((el.getAttribute('aria-label') || '') + ' ' +
     (el.getAttribute('placeholder') || '') + ' ' +
     (el.getAttribute('name') || ''))
  ));
  return {
    title: field ? String(field.value || '').trim() : '',
    text: root ? (root.innerText || '').slice(0, 300) : '',
  };
}
"""


def is_experience_step(snapshot: dict[str, Any]) -> bool:
    """True quando a etapa do formulário pede histórico profissional."""
    text = str(snapshot.get("text", "") or "").lower()
    return any(mark in text for mark in _EXPERIENCE_STEP_MARKS)


def _normalize_title(value: object) -> str:
    return re.sub(r"\W+", " ", str(value or "").lower()).strip()


def _decision_field(decision: Any, name: str, default: Any = None) -> Any:
    if decision is None:
        return default
    if isinstance(decision, dict):
        return decision.get(name, default)
    return getattr(decision, name, default)


def _dialog_labels(dialog: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for button in dialog.get("buttons", []) or []:
        for key in ("text", "aria"):
            value = str(button.get(key, "") or "").strip()
            if value and value not in labels:
                labels.append(value)
    return labels


def _click_visible_button(page: Any, label: str) -> bool:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    locators = (
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.locator(f'button:has-text("{label}")'),
    )
    for factory in locators:
        try:
            locator = factory()
            for indice in range(locator.count()):
                target = locator.nth(indice)
                if hasattr(target, "is_visible") and not target.is_visible():
                    continue
                target.click()
                return True
        except Exception:
            continue
    return False


def _confirm_experience_removal(
    page: Any,
    *,
    decider: Any = None,
    observer: Any = None,
    url: str = "",
) -> bool:
    """Interpreta o dialogo aberto por 'Excluir experiencia' e confirma.

    A decisão vem da memória local/LLM via ``decider``. O fallback de rótulos
    só é usado quando não há decider (ferramentas e testes antigos).
    """
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        wait(300)

    evaluate = getattr(page, "evaluate", None)
    dialog = evaluate(_DIALOG_STATE_JS) if callable(evaluate) else None
    if not isinstance(dialog, dict):
        dialog = {"url": url, "text": "", "buttons": []}
    dialog.setdefault("url", url)
    labels = _dialog_labels(dialog)
    if not labels:
        return False

    intent = "confirm_remove_extra_experience"
    trigger = "Excluir experiência"
    dialog_signature = _normalize_title(" ".join(labels) + " " + str(dialog.get("text", "")))[:300]

    decision = decider(intent, dialog, trigger) if callable(decider) else None
    if decision is None:
        for label in _EXPERIENCE_CONFIRM_LABELS:
            if label in labels and label.lower() != trigger.lower():
                decision = {
                    "action": label,
                    "source": "local_fallback",
                    "reason": "rótulo de confirmação conhecido",
                    "needs_human": False,
                }
                break

    if decision is None or _decision_field(decision, "needs_human", False):
        reason = str(_decision_field(decision, "reason", ""))[:120]
        log_event("browser_apply_ui_action_needs_human", intent=intent, reason=reason)
        raise HumanActionRequired("confirmacao_de_exclusao", reason)

    action = str(_decision_field(decision, "action", "") or "").strip()
    if action.lower() == "none" or action not in labels:
        log_event("browser_apply_ui_action_rejected", intent=intent, action=action[:80])
        raise ValidationError("botão de confirmação da exclusão não identificado")
    if not _click_visible_button(page, action):
        raise ValidationError("falha ao confirmar a exclusão da experiência extra")

    source = str(_decision_field(decision, "source", "llm") or "llm")[:40]
    reason = str(_decision_field(decision, "reason", "") or "")[:240]
    log_event(
        "browser_apply_extra_experience_confirmed",
        label=action,
        source=source,
        reason=reason,
    )
    if callable(observer):
        observer(
            {
                "url": str(dialog.get("url", "") or url),
                "intent": intent,
                "trigger": trigger,
                "action": action,
                "dialog_signature": dialog_signature,
                "source": source,
                "reason": reason,
            }
        )
    return True


def remove_extra_experience_entry(
    page: Any,
    profile_titles: list[str],
    *,
    decider: Any = None,
    observer: Any = None,
    url: str = "",
) -> bool:
    """Remove um cartão de experiência vazio/extra sem tocar nos reais.

    Só clica em "Excluir experiência" quando o cartão não contém nenhum título
    conhecido do perfil. Isso evita apagar uma das 3 experiências verdadeiras
    e impede que um quarto cartão seja criado/preenchido.
    """
    conhecidos = {
        normalized
        for title in profile_titles
        if (normalized := _normalize_title(title))
    }
    for label in _EXPERIENCE_REMOVE_LABELS:
        locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
        for indice in range(locator.count()):
            button = locator.nth(indice)
            try:
                if not button.is_visible():
                    continue
                info = button.evaluate(_EXPERIENCE_CARD_JS) or {}
                titulo = _normalize_title(info.get("title", ""))
                texto = _normalize_title(info.get("text", ""))
                if titulo in conhecidos or any(
                    conhecido and conhecido in texto for conhecido in conhecidos
                ):
                    continue
                button.click()
                confirmed = _confirm_experience_removal(
                    page, decider=decider, observer=observer, url=url
                )
                log_event(
                    "browser_apply_extra_experience_removed",
                    title=str(info.get("title", ""))[:80],
                    confirmed=confirmed,
                )
                return confirmed
            except (HumanActionRequired, ValidationError):
                raise
            except Exception:
                continue
    return False


def _resume_selected(page: Any, name: str) -> bool:
    return bool(page.evaluate(_RESUME_SELECTED_JS, name))


def resume_attached_on_page(page: Any, path: str | Path) -> bool:
    """Indica se este page já mostra o CV anexado, sem fazer upload."""
    if not path:
        return False
    name = Path(path).name
    if page.evaluate(_RESUME_SELECTED_JS, name):
        return True
    if page.evaluate(_RESUME_ATTACHED_JS, name) and not page.evaluate(
        _RESUME_UPLOADING_JS, name
    ):
        return True
    state = page.evaluate(_RESUME_STATE_JS) or {}
    return any(name in item for group in state.get("files", []) for item in group)


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

    # Portais paginados (Alignerr) exibem "Arquivo • Uploaded" em texto solto e
    # removem o input de arquivo ao avançar. Reconhecer esse estado evita exigir
    # um segundo upload em uma etapa que já não tem campo de arquivo.
    if page.evaluate(_RESUME_ATTACHED_JS, name) and not page.evaluate(
        _RESUME_UPLOADING_JS, name
    ):
        log_event("browser_apply_resume_already_attached", filename=name)
        return "already_attached"

    if page.evaluate(_RESUME_CHECK_JS, name):
        if page.evaluate(_RESUME_SELECT_JS, name) and _resume_selected(page, name):
            log_event("browser_apply_resume_selected", filename=name)
            return "selected"
        return "select_failed"

    clicked = ""
    # Alguns portais (Greenhouse, formulários paginados) já expõem o input.
    # Nesses casos não há botão para clicar: usar o input direto evita o falso
    # "upload_button_not_found" quando o campo está visível.
    file_input = page.locator('input[type="file"]')
    if file_input.count() == 0:
        clicked = ""
        for label in _UPLOAD_LABELS:
            try:
                locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if locator.count() == 0:
                    locator = page.get_by_text(label, exact=False)
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
        if file_input.count() == 0:
            page.wait_for_timeout(1000)
        if file_input.count() == 0:
            return "file_input_not_found"
    file_input.last.set_input_files(str(cv))

    # Uploads reais (Alignerr) podem levar 15–20s antes de habilitar o Continue.
    deadline = time.time() + 60
    while time.time() < deadline:
        state = page.evaluate(_RESUME_STATE_JS) or {}
        if state.get("blocked"):
            return "blocked"
        uploaded = any(name in item for group in state.get("files", []) for item in group)
        attached = bool(page.evaluate(_RESUME_ATTACHED_JS, name))
        if uploaded or attached:
            if page.evaluate(_RESUME_UPLOADING_JS, name):
                page.wait_for_timeout(500)
                continue
            radios = state.get("radios", [])
            match = next((r for r in radios if name in r.get("label", "")), None)
            if match is not None and not match.get("checked"):
                page.evaluate(_RESUME_SELECT_JS, name)
            # Radio é uma forma de confirmar no LinkedIn; em portais externos o
            # próprio arquivo/input já é a evidência. Não exigir radio evita o
            # falso "currículo não foi anexado" quando não existe essa opção.
            if _resume_selected(page, name) or uploaded or attached:
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


def next_step_label(page: Any) -> str:
    """Rótulo de avanço de step, sem confundir com o Submit final."""
    for text in _NEXT_LABELS:
        lowered = text.lower()
        if any(mark in lowered for mark in _SUBMIT_WORDS):
            continue
        pattern = re.compile(rf"\b{re.escape(text)}\b", re.I)
        for make_locator in (
            lambda p=pattern: page.get_by_role("button", name=p),
            lambda p=pattern: page.locator(f'button:has-text("{text}")'),
        ):
            try:
                locator = make_locator()
                if locator.count() == 0:
                    continue
                target = locator.first
                if hasattr(target, "is_visible") and not target.is_visible():
                    continue
                return text
            except Exception:
                continue
    return ""


def click_next(page: Any, label: str = "") -> bool:
    """Avança para o próximo step preenchido. Nunca clica em Submit."""
    text = label or next_step_label(page)
    if not text or any(mark in text.lower() for mark in _SUBMIT_WORDS):
        return False
    pattern = re.compile(rf"\b{re.escape(text)}\b", re.I)
    for make_locator in (
        lambda: page.get_by_role("button", name=pattern),
        lambda: page.locator(f'button:has-text("{text}")'),
    ):
        try:
            locator = make_locator()
            if locator.count() == 0:
                continue
            locator.first.click()
            log_event("browser_apply_step_advanced", label=text)
            return True
        except Exception:
            continue
    return False


def _capture_webp(page: Any, quality: int) -> bytes:
    """Usa o próprio encoder WebP do Chrome via CDP (sem dependência nova).

    Playwright só expõe PNG/JPEG em ``page.screenshot``; o CDP do Chrome aceita
    ``format=webp``. Se a sessão não existir ou o Chrome não oferecer WebP,
    devolvemos vazio para o chamador fazer fallback para JPEG.
    """
    context = getattr(page, "context", None)
    factory = getattr(context, "new_cdp_session", None)
    if not callable(factory):
        return b""
    sessao = None
    try:
        sessao = factory(page)
        resposta = sessao.send(
            "Page.captureScreenshot",
            {
                "format": "webp",
                "quality": quality,
                "captureBeyondViewport": False,
                "fromSurface": True,
            },
        ) or {}
        dados = str(resposta.get("data") or "")
        return base64.b64decode(dados) if dados else b""
    except Exception:
        return b""
    finally:
        if sessao is not None:
            try:
                sessao.detach()
            except Exception:
                pass


def capture_screenshot(
    page: Any,
    directory: str | Path,
    *,
    step: int,
    kind: str,
    label: str = "",
    image_format: str = "webp",
    quality: int = 82,
) -> dict[str, Any]:
    """Salva um print WebP/JPEG local e devolve metadados com hash.

    Nunca grava PNG novo: WebP é pedido ao Chrome via CDP e, na ausência dele,
    o fallback é JPEG com qualidade controlada. O arquivo fica em disco; o banco
    guarda só o caminho relativo e o hash (sem BLOB, sem gzip extra).
    """
    if kind not in {"step", "confirmation"}:
        raise ValidationError(f"tipo de evidência desconhecido: {kind}")
    qualidade = max(1, min(100, int(quality)))
    payload = _capture_webp(page, qualidade) if str(image_format).lower() == "webp" else b""
    extensao = "webp" if payload else "jpg"

    destino = Path(directory)
    destino.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:40]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    filename = f"step{int(step):02d}_{kind}_{stamp}"
    if safe_label:
        filename += f"_{safe_label}"
    path = destino / f"{filename}.{extensao}"

    if not payload:
        # Playwright não aceita type="webp": o fallback é sempre JPEG.
        payload = page.screenshot(
            path=str(path), type="jpeg", quality=qualidade, full_page=False
        )
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        payload = path.read_bytes()
    path.write_bytes(bytes(payload))
    return {
        "step": int(step),
        "kind": kind,
        "label": label[:200],
        "path": path.as_posix(),
        "sha256": hashlib.sha256(bytes(payload)).hexdigest(),
        "created_at": datetime.now().isoformat(),
    }


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
