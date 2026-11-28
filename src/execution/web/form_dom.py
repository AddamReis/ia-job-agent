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
import unicodedata
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
  // Greenhouse/Breezy misturam sentinels decorativos aos controles reais:
  // inputs aria-hidden/tabindex=-1 usados pelo React Select para validação,
  // e honeypots hp_*. Eles não recebem dados e não podem virar "campo sem rótulo".
  const decorative = (el) => {
    const name = el.getAttribute('name') || el.id || '';
    return el.getAttribute('aria-hidden') === 'true'
      || el.getAttribute('tabindex') === '-1'
      || el.classList.contains('requiredInput')
      || el.classList.contains('iti__search-input')
      || /__search-input$/.test(el.id || '')
      || /^hp[_-]/i.test(name)
      || Boolean(el.closest('[aria-hidden="true"]'));
  };
  const nodes = [...document.querySelectorAll(
    'input, textarea, select, [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"]'
  )].filter(el => !playerControl(el) && !decorative(el));
  const fields = [];
  let index = 0;
  // Selects MUI expõem um [role=combobox] e um input interno para o mesmo campo.
  // Ignorar o interno evita índice duplicado e preenchimento no controle errado.
  const interno = (el) => el.getAttribute('aria-hidden') === 'true' &&
    el.parentElement && el.parentElement.querySelector(':scope > [role=combobox]');
  for (const el of nodes.filter(el => !interno(el))) {
    // Botões Radix são <button type="button" role="combobox">; o role define
    // a ação correta (abrir lista), não o type nativo do botão.
    const role = (el.getAttribute('role') || '').toLowerCase();
    const type = el.tagName.toLowerCase() === 'select'
      ? 'select'
      : (/^(textbox|combobox|checkbox|radio)$/.test(role)
          ? role
          : (el.getAttribute('type') || role || 'text').toLowerCase());
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
    label = label || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')
      || el.getAttribute('placeholder') || '';
    if (!String(label).trim()) {
      // MUI/Select (Alignerr): o controle é um input readonly dentro do container
      // que traz o texto do rótulo ("Select Gender", "Month", "Day", "Year").
      // Sem isso o agente acha que o formulário não tem campos e tenta submeter.
      const caixa = el.closest('div');
      if (caixa) label = (caixa.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200);
    }
    const meaningful = String(label).trim() && !generic.test(String(label));
    if (!scoped && !meaningful) continue;
    const parent = el.parentElement || el;
    // Radios/checkboxes costumam ter o <label> como pai imediato. Subir alguns
    // níveis recupera a pergunta do grupo ("Gender", "Desired Salary") sem
    // alterar a assinatura do campo.
    const arredores = [];
    let node = parent;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const texto = (node.innerText || '').replace(/\s+/g, ' ').trim();
      if (texto && texto.length <= 300 && !arredores.includes(texto)) {
        arredores.push(texto);
      }
    }
    // Não juntar irmãos do formulário: isso fazia o campo "Country" conter
    // "Email*" e casar com a regra de e-mail. Preferir o texto mais próximo
    // que pareça rótulo/pergunta (termina em * ou ?), senão o mais curto.
    const marcados = arredores
      .filter(t => /[?*]/.test(t) && (t.match(/[*?]/g) || []).length <= 2 && t.length <= 180)
      .sort((a, b) => a.length - b.length);
    const pergunta = marcados[0]
      || arredores.find(t => t.length <= 80)
      || arredores[0] || '';
    const options = el.tagName === 'SELECT'
      ? [...el.options].map(o => (o.text || '').trim()).filter(Boolean)
      : (() => {
          // React Select mantém a listbox associada no DOM mesmo fechada.
          // Sem isso o resolvedor não vê opções como "I don't wish to
          // self-identify" e bloqueia o envio sem necessidade.
          const ids = [];
          const controls = el.getAttribute('aria-controls');
          if (controls) ids.push(controls);
          if (el.id) ids.push('react-select-' + el.id + '-listbox');
          for (const listId of ids) {
            const caixa = document.getElementById(listId);
            if (caixa) {
              return [...caixa.querySelectorAll('[role="option"]')]
                .map(o => (o.innerText || '').replace(/\\s+/g, ' ').trim())
                .filter(Boolean);
            }
          }
          return [];
        })();
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
      nearby_text: pergunta.slice(0, 200),
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
    "Complete application", "Complete", "Finish application",
    "Submit application", "Submit", "Send application", "Enviar candidatura",
    "Enviar aplicação", "Enviar aplicacao", "Enviar inscrição", "Enviar inscricao",
    "Enviar agora", "Enviar", "Aplicar", "Concluir", "Finalizar",
    "Review application", "Revisar candidatura",
)
_CONFIRM_MARKS = (
    "withdraw", "application submitted", "application sent", "candidatura enviada",
    "candidatura enviada com sucesso", "inscrição enviada",
    "inscricao enviada", "seu pedido foi enviado",
    "your application was submitted", "your application has been submitted",
    "application has been submitted", "application was submitted",
    "submission successful", "successfully submitted", "application complete",
    "thank you for applying", "thank you for your application", "thanks for applying",
    "we have received your application", "your application has been received",
    "application received", "candidatura recebida", "inscrição recebida",
    "solicitud enviada", "solicitud recibida", "postulación enviada",
)
_CONFIRM_URL = re.compile(r"/(?:confirmation|submitted|thank-you|thanks|success|complete)(?:/|\?|$)", re.I)
# "Verificação realizada" é selo de empregador verificado no LinkedIn, não
# bloqueio. Só parar diante de desafio humano explícito.
_BLOCK_RE = re.compile(
    r"captcha|unusual activity|atividade incomum|"
    r"(?:confirm|verify) you are human|"
    r"verifica[cç][aã]o de (?:human|seguran[cç]a|identidade)|"
    r"verificaci[oó]n de (?:humanidad|seguridad|identidad)|"
    r"verificar que (?:voc[eê]|es) (?:é|e) humano|checkpoint",
    re.IGNORECASE,
)
# Selo de branding ("protected by reCAPTCHA"/"Protected by hCaptcha") não é
# desafio: Greenhouse, Workday e iCIMS exibem esse rodapé em formulários normais.
_BLOCK_BADGE_RE = re.compile(
    r"protected by (?:re|h)?captcha|"
    r"recaptcha\.anchor\.[A-Za-z.]+\([^)]*\)",
    re.IGNORECASE,
)


def platform_block(text: str) -> bool:
    """True somente diante de desafio humano explícito, não de selo de Grupo."""
    return bool(_BLOCK_RE.search(_BLOCK_BADGE_RE.sub(" ", str(text or ""))))

# Barreiras que o agente nunca transpõe: criar conta, digitar senha, resolver
# CAPTCHA/MFA. Diferenciá-las de erro técnico evita retries inúteis.
_CREDENTIAL_BARRIER_JS = r"""
() => {
  const texto = (document.body.innerText || '').slice(0, 4000)
    .replace(/protected by reCAPTCHA/gi, ' ');
  const visivel = el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const senhas = [...document.querySelectorAll('input[type=password]')]
    .filter(visivel).length;
  if (/captcha|unusual activity|atividade incomum|confirm you are human|\bmfa\b|\b2fa\b/i
      .test(texto)) return 'verificação/CAPTCHA/MFA';
  if (senhas > 0 || /password\s*\*?|senha|verify new password|confirm password/i
      .test(texto)) return 'senha';
  if (/create account|criar conta|sign in|iniciar sess[aã]o|entrar com/i.test(texto)) {
    return 'criação de conta/login';
  }
  return '';
}
"""

_NEXT_LABELS = (
    "Next", "Next step", "Continue", "Continuar", "Próximo", "Proximo",
    "Avançar", "Avancar", "Seguinte", "Avaliar", "Revisar", "Review",
)
# Rótulos de avanço nunca podem confundir com Submit/Review final.
_SUBMIT_WORDS = (
    "submit", "complete", "finish", "enviar candidatura", "enviar", "aplicar", "review application",
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
  if (!alvo) return false;
  // "processing" aparece em textos de privacidade/demografia sem ter relação
  // com o arquivo. Só avalia linhas/atributos que citam o nome do CV.
  const linhas = (document.body.innerText || '')
    .split(/\r?\n|\u2028|\u2029/)
    .map(s => s.trim().toLowerCase())
    .filter(s => s.includes(alvo));
  const atributos = [...document.querySelectorAll('[aria-label],[title],[alt]')]
    .map(el => ((el.getAttribute('aria-label') || '') + ' ' +
                (el.getAttribute('title') || '') + ' ' +
                (el.getAttribute('alt') || '')).trim().toLowerCase())
    .filter(s => s.includes(alvo));
  const contextos = linhas.concat(atributos);
  if (!contextos.length) return false;
  return /uploading|upload in progress|enviando|carregando|processando|processing|cargando/i
    .test(contextos.join(' '));
}
"""

# Greenhouse/iCIMS têm mais de um input de arquivo (CV e carta). Escolher o
# último anexava o CV no campo errado; este JS lê o contexto real de cada input.
_RESUME_INPUT_JS = r"""
() => [...document.querySelectorAll('input[type=file]')].map((input, index) => {
  let contexto = [input.id, input.name, input.getAttribute('aria-label') || '']
    .filter(Boolean).join(' ');
  let node = input;
  for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
    contexto += ' ' + (node.innerText || '').replace(/\s+/g, ' ').trim();
  }
  return {index, context: contexto.slice(0, 600)};
})
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


def credential_barrier(page: Any) -> str:
    """Motivo da barreira humana na tela, ou ``''`` quando não há barreira."""
    try:
        return str(page.evaluate(_CREDENTIAL_BARRIER_JS) or "").strip()
    except Exception:
        return ""


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
# Uma aba reaproveitada pode continuar na mesma URL da vaga com o diálogo de
# feedback/report do LinkedIn aberto. As opções ("É irritante...", "Já vi este
# mesmo anúncio...") não são campos de candidatura: nenhuma dela pode ser
# clicada. Este reconhecedor só lê o diálogo visível.
_FEEDBACK_DIALOG_RE = re.compile(
    r"irritante ou n[ãa]o (?:[ée]|e) interessante|"
    r"j[áa] vi este mesmo an[úu]ncio|"
    r"(?:i(?:'|\u2019)?ve|have) seen this (?:same )?(?:job |job posting |posting |ad )?(?:many times|multiple times|before)|"
    r"report (?:this )?(?:job|post|ad)|"
    r"denunciar (?:esta |este )?(?:vaga|an[úu]ncio)",
    re.IGNORECASE,
)


def feedback_dialog_text(page: Any) -> str:
    """Texto do diálogo de feedback/report, sem interagir com ele."""
    try:
        state = page.evaluate(_DIALOG_STATE_JS) or {}
    except Exception:
        return ""
    if not isinstance(state, dict):
        return ""
    texto = str(state.get("text") or "")
    return texto if _FEEDBACK_DIALOG_RE.search(texto) else ""


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
    """True quando a etapa do formulário pede histórico profissional.

    A marca precisa ser um título isolado. Frases descritivas como "We use
    LinkedIn to review your professional work experience" não são uma etapa
    de work experience: tratá-las como tal faria o agente pular todo o formulário.
    """
    text = str(snapshot.get("text", "") or "").lower()
    for mark in _EXPERIENCE_STEP_MARKS:
        if re.search(
            rf"(?m)^\s*{re.escape(mark)}\s*(?:\*|:|\d+)?\s*$", text
        ):
            return True
    return False


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
    entradas = page.evaluate(_RESUME_INPUT_JS) or []
    if not entradas and page.locator('input[type="file"]').count():
        # Compatibilidade com páginas que não expõem evaluate rico (fakes e
        # portais antigos): um único input visível é inequívoco.
        entradas = [{"index": 0, "context": "file input"}]
    # Alguns portais (Greenhouse, formulários paginados) já expõem o input.
    # Nesses casos não há botão para clicar: usar o input direto evita o falso
    # "upload_button_not_found" quando o campo está visível.
    if not entradas:
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
        page.wait_for_timeout(1000)
        entradas = page.evaluate(_RESUME_INPUT_JS) or []
        if not entradas and page.locator('input[type="file"]').count():
            entradas = [{"index": 0, "context": "file input"}]
        if not entradas:
            return "file_input_not_found"

    resume_re = re.compile(r"resume|curr[ií]culo|curriculum|\bcv\b", re.I)
    relevantes = [item for item in entradas if resume_re.search(str(item.get("context", "")))]
    if relevantes:
        indice = int(relevantes[0]["index"])
    elif len(entradas) == 1:
        indice = int(entradas[0]["index"])
    else:
        # Múltiplos arquivos sem contexto de CV (ex.: carta e documentos):
        # escolher qualquer um lançaria o currículo no campo errado.
        return "resume_input_not_found"
    file_input = page.locator('input[type="file"]')
    nth = getattr(file_input, "nth", None)
    alvo = nth(indice) if callable(nth) else file_input.last
    alvo.set_input_files(str(cv))

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


def _combobox_state(target: Any) -> dict[str, str]:
    """Estado do controle sem usar o value do input.

    No React Select do Greenhouse o ``input`` fica vazio depois da escolha: o
    valor visível mora em ``.select__single-value`` (no Country, só ``+55``).
    """
    info = target.evaluate(
        """(el) => {
          const caixa = el.closest('.select__control')
            || (el.parentElement && el.parentElement.parentElement
                && el.parentElement.parentElement.parentElement)
            || el;
          const visivel = caixa.querySelector('.select__single-value');
          return {
            value: String(el.value || ''),
            display: visivel ? (visivel.innerText || '').trim() : '',
            controls: el.getAttribute('aria-controls') || '',
            id: el.id || '',
            expanded: el.getAttribute('aria-expanded') || '',
          };
        }""",
        timeout=3000,
    )
    return info if isinstance(info, dict) else {}


def peek_combobox_options(page: Any, index: int) -> list[str]:
    """Abre brevemente um React Select e lê as opções da listbox associada.

    A listbox só entra no DOM quando o combobox é aberto. Sem esta leitura o
    resolvedor vê ``options=[]`` e não consegue usar a opção de não responder.
    """
    try:
        alvo = page.locator(f'[data-agent-field="{index}"]').first
        alvo.click(timeout=2000, force=True)
        page.wait_for_timeout(150)
        info = _combobox_state(alvo)
        seletores: list[str] = []
        if info.get("controls"):
            seletores.append(f'[id="{info["controls"]}"]')
        if info.get("id"):
            seletores.append(f'[id="react-select-{info["id"]}-listbox"]')
        for seletor in seletores:
            lista = page.locator(seletor)
            if not (lista.count() and lista.first.locator('[role="option"]').count()):
                continue
            opcoes = lista.first.locator('[role="option"]')
            texto: list[str] = []
            for posicao in range(min(opcoes.count(), 60)):
                try:
                    item = opcoes.nth(posicao).inner_text(timeout=1000).strip()
                except Exception:
                    continue
                if item:
                    texto.append(item)
            return texto
    except Exception:
        pass
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return []


def _choose_combobox(page: Any, target: Any, value: str) -> bool:
    """Seleciona opção em combobox MUI/Radix/React Select (``role=combobox``).

    A correspondência é exata ou por prefixo. Prefixo é necessário em portais
    que devolvem ``Cidade, UF, País`` ou ``Brazil +55``; ainda assim ``Male``
    nunca casa com ``Female`` porque o prefixo exige vírgula ou espaço.
    Cada combobox usa SOMENTE a listbox que ele controla: a página do
    Greenhouse mantém várias listboxes no DOM, inclusive a do telefone.
    """
    escolhido = str(value or "").strip()
    if not escolhido:
        return False

    def _norm(texto: str) -> str:
        sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
        return re.sub(r"\s+", " ", sem_acento.encode("ascii", "ignore").decode()).strip().casefold()

    alvo = _norm(escolhido)

    def _ranking(texto: str) -> int | None:
        normalizado = _norm(texto)
        if normalizado == alvo:
            return 0
        if normalizado.startswith(alvo + ",") or normalizado.startswith(alvo + " "):
            return 1 + len(normalizado)
        return None

    def _lista() -> Any:
        try:
            info = _combobox_state(target)
        except Exception:
            info = {}
        seletores: list[str] = []
        if info.get("controls"):
            seletores.append(f'[id="{info["controls"]}"]')
        if info.get("id"):
            seletores.append(f'[id="react-select-{info["id"]}-listbox"]')
        for seletor in seletores:
            candidata = page.locator(seletor)
            try:
                if candidata.count() and candidata.first.locator('[role="option"]').count():
                    return candidata.first
            except Exception:
                continue
        visiveis = page.locator('[role="listbox"]:visible')
        try:
            for indice in range(visiveis.count()):
                if visiveis.nth(indice).locator('[role="option"]').count():
                    return visiveis.nth(indice)
        except Exception:
            pass
        return page.locator('[role="listbox"]').first

    def _abrir() -> bool:
        try:
            page.keyboard.press("Escape")
            try:
                target.click(timeout=3000)
            except Exception:
                target.evaluate("(el) => el.click()", timeout=3000)
            page.wait_for_timeout(250)
            lista = _lista()
            if not lista.locator('[role="option"]').count():
                try:
                    target.click(timeout=3000)
                except Exception:
                    target.evaluate("(el) => el.click()", timeout=3000)
                page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    def _escolher() -> bool:
        opcoes = _lista().locator('[role="option"]')
        # Listas Radix carregam as opções de forma assíncrona após a digitação.
        for _tentativa in range(20):
            try:
                if opcoes.count() > 0:
                    break
            except Exception:
                return False
            page.wait_for_timeout(150)
        candidatos: list[tuple[int, int, Any]] = []
        for indice in range(opcoes.count()):
            try:
                texto = opcoes.nth(indice).inner_text(timeout=2000).strip()
            except Exception:
                continue
            rank = _ranking(texto)
            if rank is not None:
                candidatos.append((rank, indice, opcoes.nth(indice)))
        if not candidatos:
            return False
        candidatos.sort(key=lambda item: (item[0], item[1]))
        opcao = candidatos[0][2]
        try:
            opcao.click(timeout=3000)
        except Exception:
            return False
        page.wait_for_timeout(200)
        try:
            atual = _combobox_state(target)
        except Exception:
            atual = {}
        if _ranking(atual.get("value", "")) is not None:
            return True
        if atual.get("display", "").strip():
            return True
        # O React Select pode esconder o rótulo; a própria opção guarda o
        # estado de seleção antes de a listbox fechar.
        try:
            if opcao.get_attribute("aria-selected", timeout=2000) == "true":
                return True
        except Exception:
            pass
        return False

    if not _abrir():
        return False
    if _escolher():
        return True
    # Combobox que filtra opções ao digitar: limpar antes para não duplicar.
    if not _abrir():
        return False
    try:
        target.fill("", timeout=3000)
    except Exception:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:
            return False
    try:
        page.keyboard.type(escolhido)
    except Exception:
        return False
    if _escolher():
        return True
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
    except Exception:
        pass
    try:
        return _ranking(_combobox_state(target).get("value", "")) is not None
    except Exception:
        return False


def fill_field(page: Any, index: int, kind: str, value: str) -> bool:
    locator = page.locator(f'[data-agent-field="{index}"]')
    if locator.count() == 0:
        return False
    target = locator.first
    if kind == "file":
        target.set_input_files(value)
    elif kind in {"checkbox", "radio"}:
        bruto = str(value or "").strip()
        minusculo = bruto.lower()
        # Em rádio, "Yes"/"Sim" é uma opção do grupo (ao lado de "No"), não um
        # booleano para marcar o input atual. Booleanos literais continuam
        # seguindo o caminho antigo por compatibilidade com decisões de aceite.
        radio_booleano = kind == "radio" and minusculo in {
            "true", "false", "1", "0", "checked", "unchecked"
        }
        truthy = minusculo in {"true", "sim", "yes", "1", "checked"}
        if kind == "radio" and not radio_booleano:
            # Radio de currículo recebe o CAMINHO como valor; outros radios
            # recebem o valor decidido pelo operador (ex.: PJ/CLT/Yes/No). Em
            # todos os casos a escolha segura é procurar em TODO o grupo — o
            # rótulo visível pode ser "Yes" enquanto o value é "on".
            desejado = Path(bruto).name if bruto else ""
            resultado = target.evaluate(
                r"""(el, wanted) => {
                  const group = el.name
                    ? [...document.querySelectorAll('input[type=radio]')]
                        .filter(r => r.name === el.name)
                    : [el];
                  const norm = s => String(s || '').toLowerCase().replace(/\W+/g, ' ').trim();
                  const want = norm(wanted);
                  // O aria-label do grupo é a pergunta inteira; o rótulo da
                  // opção ("Yes"/"No") mora em um texto vizinho. Preferir o
                  // texto curto visível evita casar "Yes" com a pergunta.
                  const textoCurto = (r) => {
                    const candidatos = [];
                    const rotulo = r.getAttribute('aria-label') || r.getAttribute('title') || '';
                    if (r.id) {
                      const associado = document.querySelector(
                        'label[for=' + JSON.stringify(r.id) + ']'
                      );
                      if (associado) candidatos.push(associado.innerText || '');
                    }
                    const proprio = r.closest ? r.closest('label') : null;
                    if (proprio) candidatos.push(proprio.innerText || '');
                    let node = r.parentElement;
                    for (let i = 0; node && i < 4; i++, node = node.parentElement) {
                      candidatos.push(node.innerText || '');
                    }
                    const uteis = candidatos
                      .map(t => String(t || '').replace(/\s+/g, ' ').trim())
                      .filter(t => t && t.length <= 200 && !/[?]/.test(t));
                    const binario = uteis.find(t =>
                      /^(yes|no|sim|não|nao|true|false)$/i.test(t)
                    );
                    return binario || uteis.sort((a, b) => a.length - b.length)[0] || rotulo;
                  };
                  const opts = group.map(r => ({
                    el: r,
                    value: String(r.value || ''),
                    text: norm(textoCurto(r)),
                  }));
                  const describe = () => opts
                    .map(o => (o.value + ':' + o.text).slice(0, 90)).join(' | ');
                  if (group.length === 1) {
                    if (!group[0].checked) group[0].click();
                    return {matched: true, clicked: !!group[0].checked, options: describe()};
                  }
                  const found = opts.find(o =>
                    (want && norm(o.value) === want)
                    || (want && o.text === want)
                    || (want && o.text.includes(want))
                    || (want && want.includes(o.text))
                  );
                  if (!found) {
                    return {matched: false, options: describe()};
                  }
                  if (!found.el.checked) found.el.click();
                  return {matched: true, clicked: !!found.el.checked, options: describe()};
                }""",
                desejado,
            ) or {}
            if not resultado.get("matched"):
                log_event(
                    "browser_apply_radio_no_match",
                    desired=desejado,
                    options=str(resultado.get("options", ""))[:160],
                )
                return False
            return bool(resultado.get("clicked"))
        # Inputs customizados do LinkedIn são visualmente ocultos; check()
        # falha por "element is not visible". O clique via DOM é equivalente.
        target.evaluate(
            "(el, want) => { if (el.checked !== want) el.click(); return el.checked; }",
            truthy,
        )
    elif kind == "select":
        alvo = str(value or "").strip()
        opcoes: list[dict[str, str]] = []
        try:
            opcoes = list(
                target.evaluate(
                    "(el) => [...el.options].map(o => ({"
                    "value: String(o.value || ''), label: String(o.textContent || '').trim()}))"
                )
                or []
            )
        except Exception:
            opcoes = []
        if opcoes:
            alvo_norm = re.sub(r"\W+", " ", alvo.lower()).strip()
            sinonimos = {"brazil": "brasil", "brasil": "brazil"}

            def normalizar(texto: str) -> str:
                resultado = re.sub(r"\W+", " ", str(texto or "").lower()).strip()
                return " ".join(sinonimos.get(parte, parte) for parte in resultado.split())

            alvo_norm = normalizar(alvo)
            escolhida = ""
            for opcao in opcoes:
                for campo in ("value", "label"):
                    bruto = str(opcao.get(campo) or "").strip()
                    if bruto and bruto.lower() == alvo.lower():
                        escolhida = str(opcao.get("value") or "")
                        break
                if escolhida:
                    break
            if not escolhida:
                for opcao in opcoes:
                    if normalizar(opcao.get("label")) == alvo_norm:
                        escolhida = str(opcao.get("value") or "")
                        break
            if not escolhida and alvo_norm:
                for opcao in opcoes:
                    rotulo = normalizar(opcao.get("label"))
                    if rotulo and (alvo_norm in rotulo or rotulo in alvo_norm):
                        escolhida = str(opcao.get("value") or "")
                        break
            if not escolhida and "+55" in alvo:
                for opcao in opcoes:
                    if "+55" in str(opcao.get("label") or ""):
                        escolhida = str(opcao.get("value") or "")
                        break
            if not escolhida:
                log_event(
                    "browser_apply_select_no_match",
                    index=index,
                    desired_hash=hashlib.sha256(alvo.encode("utf-8")).hexdigest()[:8],
                    options=str([opcao.get("label") for opcao in opcoes])[:300],
                )
                return False
            target.select_option(value=escolhida)
            return True
        try:
            target.select_option(label=alvo)
        except Exception:
            target.select_option(alvo)
    elif kind == "combobox":
        # O React Select pode re-renderizar no meio da escolha e deixar a
        # listbox vazia por um instante. Tentar de novo com o locator atual
        # evita marcar campo obrigatório como preenchido quando não foi.
        for tentativa in range(3):
            if _choose_combobox(page, target, value):
                return True
            if tentativa == 2:
                break
            try:
                page.wait_for_timeout(250)
                atualizado = page.locator(f'[data-agent-field="{index}"]')
                if atualizado.count():
                    target = atualizado.first
            except Exception:
                return False
        return False
    else:
        try:
            tipo = str(target.get_attribute("type") or "").lower()
        except Exception:
            tipo = ""
        candidato = str(value or "").strip()
        if tipo == "number":
            # Nunca transformar "A combinar" (ou qualquer texto) em número:
            # isso inventaria uma resposta humana num campo obrigatório.
            limpo = (
                candidato.replace("R$", "")
                .replace("$", "")
                .replace(" ", "")
                .replace(",", ".")
                .strip()
            )
            if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", limpo):
                log_event(
                    "browser_apply_number_value_rejected",
                    index=index,
                    reason="valor nao numerico",
                )
                raise HumanActionRequired(
                    "campo numérico",
                    "o portal exige valor numérico; a evidência do perfil não é numérica",
                )
            target.fill(limpo)
            return True
        target.fill(candidato)
    return True


_READINESS_JS = r"""
() => [...document.querySelectorAll('input, select, textarea, [role="combobox"]')]
  .filter(el => el.required || el.getAttribute('aria-required') === 'true')
  .map(el => {
    const caixa = el.closest('.select__control')
      || (el.parentElement && el.parentElement.parentElement
          && el.parentElement.parentElement.parentElement)
      || el;
    const visivel = caixa.querySelector('.select__single-value');
    const erro = document.getElementById(el.getAttribute('aria-errormessage') || '');
    const tipo = (el.type || '').toLowerCase();
    const preenchido = tipo === 'checkbox' || tipo === 'radio'
      ? Boolean(el.checked)
      : Boolean(
          String(el.value || '').trim()
          || (visivel && (visivel.innerText || '').trim())
          || (el.files && el.files.length)
        );
    return {
      id: el.id || el.getAttribute('aria-label') || el.getAttribute('name') || '',
      filled: preenchido,
      error: erro ? (erro.innerText || '').trim().slice(0, 120) : '',
    };
  })
"""


def required_field_state(page: Any) -> list[dict[str, Any]]:
    """Estado booleano dos campos obrigatórios, sem expor valores preenchidos."""
    try:
        return page.evaluate(_READINESS_JS) or []
    except Exception:
        return []


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
    # O Easy Apply do LinkedIn mantém botões de steps anteriores no DOM. Consultar
    # primeiro os botões realmente visíveis evita escolher um "Avançar" fantasma
    # quando a etapa atual já mostra apenas "Avaliar".
    try:
        visiveis = visible_buttons(page)
    except Exception:
        visiveis = []
    for text in _NEXT_LABELS:
        lowered = text.lower()
        if any(mark in lowered for mark in _SUBMIT_WORDS):
            continue
        padrao = re.compile(rf"\b{re.escape(text)}\b", re.I)
        if any(
            not botao.get("disabled")
            and padrao.search(
                " ".join(
                    str(botao.get(chave) or "")
                    for chave in ("text", "aria")
                )
            )
            for botao in visiveis
        ):
            return text
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
            alvo = locator.first
            try:
                alvo.click()
            except Exception:
                # Banner de cookies/overlays pode interceptar o clique real.
                # O botão já foi confirmado como visível; o clique via DOM é
                # equivalente e nunca é usado para Submit.
                alvo.evaluate("el => el.click()")
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
    # A confirmação do portal tem precedência: páginas como o LinkedIn mantêm
    # o selo "Verificação realizada"/widget de segurança mesmo depois de
    # "Candidatura enviada". Um envio confirmado nunca é reclassificado erro.
    if any(mark in text for mark in _CONFIRM_MARKS):
        return "submitted"
    if platform_block(text):
        return "blocked"
    # Portais que confirmam só por URL: exigimos também texto de candidatura,
    # para não confundir "/complete" de cadastro com envio de formulário.
    url = str(getattr(page, "url", "") or "")
    if _CONFIRM_URL.search(url) and re.search(
        r"application|candidatura|inscri[cç][aã]o|solicitud|postula", text, re.I
    ):
        return "submitted"
    return "unknown"
