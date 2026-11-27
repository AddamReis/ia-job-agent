"""Agente agêntico de candidatura no Chrome logado via CDP.

Fluxo: abre a vaga pelo ID real, segue o redirecionamento para o portal da
empresa, lê o formulário, usa o resolvedor local + DGX para decidir cada campo,
pede APROVAÇÃO HUMANA persistida e só então clica em Submit. Após o envio,
grava candidatura, ``has_applied`` e o cadastro da empresa.

O conhecimento de cada site é guardado em ``SitePatternStore`` (memória local),
para que a próxima candidatura reutilize o padrão aprendido.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from ..core.config import Settings
from ..core.errors import (
    BlockedByPlatformError,
    HumanActionRequired,
    JobUnavailable,
    ValidationError,
)
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.forms.answers import AnswerResolver
from ..execution.forms.field import FormField
from ..execution.web.challenge_agent import ChallengeAgent
from ..execution.web.form_dom import (
    capture_screenshot,
    click_next,
    click_submit,
    credential_barrier,
    ensure_resume_uploaded,
    fill_field,
    is_experience_step,
    next_step_label,
    peek_combobox_options,
    platform_block,
    remove_extra_experience_entry,
    required_field_state,
    resume_attached_on_page,
    snapshot_form,
    submission_state,
    visible_buttons,
)
from ..execution.web.linkedin_logged import LinkedInLoggedBrowser
from ..execution.web.portal_credentials import PortalCredentialStore
from ..execution.web.site_patterns import SitePatternStore, domain_of
from ..models.domain import (
    AnswerRecord,
    Application,
    ApplicationEvidence,
    ApprovalRequest,
    CandidateProfile,
    JobOpportunity,
)
from ..shared.remote import has_explicit_onsite_label
from ..storage.database import Repository
from ..storage.evidence_store import cleanup_expired
from .approval import ApprovalGate
from .qualification import classify_channel, is_remote_location
from .qualification_memory import QualificationMemory

_EASY_APPLY_LABELS = (
    "Easy Apply to this job", "Easy Apply",
    "Candidatura simplificada", "Candidatura rápida", "Candidatura rapida",
)
_EXTERNAL_APPLY_LABELS = (
    "Apply on company website", "Apply externally", "Solicitar no site da empresa",
    # O Chrome do operador está em pt-BR: o LinkedIn mostra "Candidatar-se"
    # (span, não botão) e redireciona para o portal da empresa.
    "Candidatar-se", "Candidatar", "Candidatura externa",
)
_APPLY_LABELS = _EASY_APPLY_LABELS + _EXTERNAL_APPLY_LABELS
# O LinkedIn Easy Apply traz "Inserir cidade ou localidade" como campo opcional
# de autocomplete. Escrever texto solto nele não seleciona um item real e o
# portal barra o avanço sem mostrar erro de validação. Como a localidade já vem
# do perfil nos campos obrigatórios (país/telefone), o opcional é ignorado.
_OPTIONAL_LOCATION_RE = re.compile(
    r"\b(?:cidade|city|localidade|location|localiza[cç][aã]o)\b", re.I
)

# Sinais de que o portal externo está pedindo login — nesse caso a candidatura
# depende de ação humana (credenciais/2FA), nunca de automação.
_LOGIN_MARKS = (
    "/login", "/signin", "/sign-in", "sign in", "iniciar sesión",
    "entrar", "ingresar", "iniciar sessão", "iniciar sessao",
)
# Sinais genéricos de candidatura já enviada (portais sem config específica).
_ALREADY_APPLIED_MARKERS = (
    "ver solicitud", "view application", "application submitted",
    "aplicación enviada", "aplicacion enviada", "candidatura enviada",
    "ya te has postulado", "you already applied", "ver candidatura",
)
# O LinkedIn registra a candidatura em um clique quando o perfil já tem CV.
# Essas frases são evidência do portal — não é um formulário a preencher.
_ONE_CLICK_MARKERS = (
    "a vaga foi movida para em andamento",
    "clicou em candidatar-se",
    "concluiu sua candidatura",
)
_MAX_FORM_STEPS = 8
_ALIGNERR_PREFERENCES = (
    "Coding", "Engineering", "Mathematics", "Voice Acting", "Science",
    "Writing", "Law", "Medicine", "Finance", "Education", "Language",
)
_CONSENT_INTENT = "accept_application_consent"

_LANDING_STATE_JS = r"""
() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const labelText = (el) => {
    let node = el;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const text = (node.innerText || '').trim();
      if (text && text.length <= 300) return text;
    }
    return (el.getAttribute('aria-label') || '').trim();
  };
  const nodes = [...document.querySelectorAll(
    'a,button,[role="button"],label,input[type=button],input[type=submit],input[type=image]'
  )].filter(visible);
  const buttons = nodes.map(el => ({
    text: (el.matches('input')
      ? (el.value || el.getAttribute('aria-label') || '')
      : (el.innerText || el.getAttribute('aria-label') || '')
    ).trim().slice(0, 120),
    aria: (el.getAttribute('aria-label') || '').trim().slice(0, 120),
    href: el.getAttribute('href') || '',
  })).filter(b => b.text).slice(0, 40);
  const checkboxes = [...document.querySelectorAll(
    'input[type=checkbox],[role=checkbox]'
  )].filter(visible).map(el => ({
    text: labelText(el).slice(0, 300),
    aria: (el.getAttribute('aria-label') || '').trim().slice(0, 120),
    checked: Boolean(el.checked || el.getAttribute('aria-checked') === 'true'),
  })).filter(c => c.text || c.aria).slice(0, 20);
  // Botoes em <input> e seletor de idioma nao sao campos de candidatura.
  const naoFormulario = (el) => {
    const tipo = (el.type || '').toLowerCase();
    if (['hidden', 'button', 'submit', 'reset', 'image'].includes(tipo)) return true;
    const valor = (el.value || '').trim();
    const papel = (el.getAttribute('role') || '').toLowerCase();
    if (tipo === 'search' || papel === 'searchbox') return true;
    if (papel === 'combobox' && (
      (el.getAttribute('inputmode') || '').toLowerCase() === 'none' ||
      /^(portugu[eê]s|english|español|espanhol|pt[- ]?br|en[- ]?us|es[- ]?es)$/i.test(valor)
    )) return true;
    const contexto = [
      el.name || '', el.id || '', el.placeholder || '',
      el.getAttribute('aria-label') || '',
      ...[...(el.labels || [])].map(l => l.innerText || ''),
    ].join(' ');
    return /^(language|idioma|locale)$/i.test(contexto.trim());
  };
  const controles = [...document.querySelectorAll(
    'input:not([type=hidden]),textarea,select'
  )].filter(visible).filter(el => !naoFormulario(el));
  const body = (document.body.innerText || '');
  return {
    url: location.href,
    text: body.slice(0, 1600),
    buttons,
    checkboxes,
    has_form: controles.length > 0,
    has_file: [...document.querySelectorAll('input[type=file]')]
      .filter(visible).length > 0,
    passwords: [...document.querySelectorAll('input[type=password]')]
      .filter(visible).length,
    blocked: /captcha|unusual activity|atividade incomum|(?:confirm|verify) you are human|verifica[cç][aã]o de (?:human|seguran[cç]a|identidade)|verificaci[oó]n de (?:humanidad|seguridad|identidad)|checkpoint/i
      .test(body.slice(0, 3000).replace(/protected by (?:re|h)?captcha/gi, ' ')),
  };
}
"""

# O anchor invisível do reCAPTCHA expõe texto de script (recaptcha.anchor.Main.init)
# no innerText do iframe. Somá-lo ao texto da tela gera falso "captcha".
_HIDDEN_CAPTCHA_FRAME_RE = re.compile(
    r"recaptcha/enterprise/anchor|frame=checkbox|checkbox-invisible", re.I
)


def _is_login_page(url: str, text: str) -> bool:
    """Detecta tela de autenticação no portal externo."""
    lowered_url = (url or "").lower()
    if any(mark in lowered_url for mark in ("/login", "/signin", "/sign-in")):
        return True
    lowered = (text or "").lower()
    credential_marks = (
        "senha", "password", "código de acesso", "codigo de acesso",
        "e-mail ou telefone", "forgot password", "olvidaste tu usuario",
    )
    return any(mark in lowered for mark in _LOGIN_MARKS) and any(
        mark in lowered for mark in credential_marks
    )


class ApplyRoute(str, Enum):
    """Rotas possíveis de candidatura — decididas pelo próprio agente.

    O agente NÃO cria um agente derivado para cada site: ele reconhece a rota
    e reutiliza o mesmo fluxo. ``KNOWN_PORTAL`` significa que já temos cadastro
    no site da empresa (config ``application.known_portals``): basta seguir o
    redirecionamento e clicar em Apply no portal, estando logados.
    """

    EASY_APPLY = "easy_apply"
    KNOWN_PORTAL = "known_portal"
    EXTERNAL_SIGNUP = "external_signup"


@dataclass
class FillDecision:
    index: int
    signature: str
    selector: str
    kind: str
    value: str
    source: str
    required: bool
    category: str = "general"


@dataclass
class PreparedStep:
    """Um passo do formulário, com decisões recalculadas na abertura atual."""

    number: int
    url: str
    domain: str
    decisions: list[FillDecision]
    next_label: str = ""


@dataclass
class PreparedForm:
    url: str
    domain: str
    decisions: list[FillDecision]
    steps: list[PreparedStep] | None = None
    submit_label: str = ""

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = []


@dataclass
class ApplyObservation:
    """Evidência colhida na MESMA abertura usada para candidatar.

    ``already_applied`` indica que o portal mostrou sinal de candidatura já
    enviada (ex.: "Ver solicitud" na BairesDev). Nesse caso o agente registra a
    candidatura existente e NÃO cria aprovação nem tenta upload/Submit.
    """

    easy_apply: bool | None
    label: str = ""
    already_applied: bool = False


def _only_search_fields(snapshot: dict[str, Any]) -> bool:
    """Indica que o snapshot só viu a busca global, não o formulário da vaga.

    O LinkedIn monta o modal de Easy Apply depois do clique. Nesse intervalo o
    DOM já expõe o campo "Pesquisar" da barra superior; tratá-lo como formulário
    faz o agente avançar sem detectar o currículo já anexado no step seguinte.
    """
    # A última etapa do Easy Apply é só revisão: não há campos, mas o modal
    # está aberto. Esperar 10s ali só atrasa o envio sem mudar o DOM.
    texto = str(snapshot.get("text") or "")
    if re.search(
        r"candidate-se à empresa|candidate-se a empresa|"
        r"\d+\s+de\s+\d+\s+p[aá]ginas|"
        r"revise sua candidatura|review your application",
        texto,
        re.I,
    ):
        return False
    campos = snapshot.get("fields") or []
    if not campos:
        return True
    for bruto in campos:
        texto = " ".join(
            str(bruto.get(chave) or "")
            for chave in ("label", "placeholder", "identifier", "autocomplete")
        ).lower()
        if not re.match(r"^\s*(?:pesquisar|search|buscar|pesquisa)\b", texto):
            return False
    return True


def _value_from_source(profile: CandidateProfile, source: str) -> str:
    """Resolve um caminho aprendido (``profile.personal.email`` etc.).

    Versões anteriores gravavam ``learned:profile....`` como origem; aceitar o
    prefixo evita que um padrão aprendido com sucesso vire barreira humana.
    """
    origem = str(source or "")
    while origem.startswith("learned:"):
        origem = origem.split(":", 1)[1]
    if not origem.startswith("profile."):
        return ""
    current: Any = profile
    for part in source.split(".")[1:]:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
        if current is None:
            return ""
    return str(current)


class BrowserApplyAgent:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        profile: CandidateProfile,
        resolver: AnswerResolver,
        gate: ApprovalGate,
        bus: EventBus | None = None,
        *,
        cdp_url: str = "http://127.0.0.1:9222",
        memory: QualificationMemory | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.profile = profile
        self.resolver = resolver
        self.gate = gate
        self.bus = bus
        self.memory = memory
        self.browser = LinkedInLoggedBrowser(cdp_url, timeout_ms=settings.browser.timeout_ms)
        self.portal_credentials = PortalCredentialStore()
        llm = getattr(resolver, "llm", None)
        cfg_desafio = getattr(settings, "challenges", None)
        self.challenges = (
            ChallengeAgent(
                llm,
                self.portal_credentials,
                max_attempts=int(getattr(cfg_desafio, "max_attempts", 2)),
                image_quality=int(getattr(cfg_desafio, "image_quality", 80)),
            )
            if llm is not None and getattr(cfg_desafio, "enabled", True)
            else None
        )
        self._active_job: JobOpportunity | None = None
        self.patterns = SitePatternStore(settings.resolve("data/site_patterns.json"))
        self._pending: dict[str, PreparedForm] = {}
        # Ações de UI observadas nesta execução só viram memória após o envio.
        self._pending_ui_actions: list[dict[str, Any]] = []
        # Durante o submit existe aprovação persistida; em ``prepare`` ainda não.
        # O agente usa isso apenas como contexto para aceites operacionais.
        self._consent_authorized = False

    @staticmethod
    def _evidence_dir(job: JobOpportunity) -> Path:
        """Diretório relativo (dentro de data/) das evidências da vaga."""
        seguro = re.sub(r"[^A-Za-z0-9_-]", "_", job.id or "sem-id")[:64]
        return Path("data/evidence") / seguro

    @staticmethod
    def _already_submitted(page: Any) -> bool:
        """Confirmação visível vale mais que selo de segurança na mesma tela."""
        try:
            return submission_state(page) == "submitted"
        except Exception:
            return False

    def _resolve_challenge(self, page: Any) -> bool:
        """Skill genérica: tenta CAPTCHA/senha/MFA antes de declarar bloqueio."""
        skill = getattr(self, "challenges", None)
        if skill is None:
            return False
        if self._already_submitted(page):
            log_event("challenge_skipped_already_submitted")
            return True
        ativo = getattr(self, "_active_job", None)
        evidencia = self._evidence_dir(ativo) if ativo is not None else None
        try:
            # O chamador já viu uma barreira: a visão decide, mas a skill exige
            # um alvo visível para não tratar formulário/loading como CAPTCHA.
            return skill.solve(page, evidence_dir=evidencia, force=True)
        except Exception as exc:
            log_event(
                "challenge_skill_failed", error=f"{type(exc).__name__}: {exc}"
            )
            return False

    def _ensure_not_blocked(self, page: Any) -> None:
        if submission_state(page) == "blocked":
            self._resolve_challenge(page)
            if submission_state(page) != "blocked":
                return
            raise BlockedByPlatformError(
                "verificação/CAPTCHA no formulário; a skill não conseguiu resolver"
            )

    def _ensure_no_credential_barrier(self, page: Any) -> None:
        """Usa a skill de desafios; segue sem segredo cadastrado se não houver evidência."""
        motivo = credential_barrier(page)
        if not motivo:
            return
        self._resolve_challenge(page)
        if not credential_barrier(page):
            return
        raise HumanActionRequired(
            "credencial",
            f"o portal pediu {motivo}; sem credencial/MFA no cofre ou na evidência",
        )

    def _walk_steps(
        self,
        page: Any,
        job: JobOpportunity,
        *,
        on_step: Callable[[PreparedStep, Any, dict[str, Any]], None] | None = None,
        allow_submit: bool = False,
    ) -> list[PreparedStep]:
        """ Percorre steps paginados recalculando campos na abertura atual.

        ``snapshot_form`` reatribui ``data-agent-field`` a cada chamada; por isso
        as decisões de cada step são sempre reconstruídas aqui. O retorno é a
        lista de steps com os índices válidos para esta página.
        """
        steps: list[PreparedStep] = []
        resume_ready = False
        for number in range(1, _MAX_FORM_STEPS + 1):
            self._ensure_not_blocked(page)
            self._ensure_no_credential_barrier(page)
            snapshot = snapshot_form(page)
            # SPAs (Alignerr/LinkedIn) montam os steps depois do JavaScript. Sem
            # isso o agente lê só a busca global e avança antes do modal abrir.
            if _only_search_fields(snapshot):
                for _tentativa in range(10):
                    page.wait_for_timeout(1000)
                    snapshot = snapshot_form(page)
                    if not _only_search_fields(snapshot):
                        break
            if not resume_ready:
                # Em candidaturas paginadas o campo de CV pode aparecer só no
                # step 2/3. Tentar em cada step evita reprovar o envio antes da
                # hora; a checagem final barra qualquer envio sem currículo.
                page, snapshot, status = self._handle_resume(page, snapshot)
                resume_ready = status in {"uploaded", "selected", "already_attached"}
                self._ensure_not_blocked(page)
                self._ensure_no_credential_barrier(page)
            max_experiences = max(
                1,
                int(
                    getattr(
                        self.settings.profile,
                        "max_experiences_in_forms",
                        3,
                    )
                    or 3
                ),
            )
            titulos_conhecidos = [
                exp.title for exp in self.profile.experiences[:max_experiences]
            ]
            if (
                is_experience_step(snapshot)
                and len(self.profile.experiences) >= max_experiences
            ):
                # O LinkedIn já traz as experiências do perfil. Não preencher,
                # duplicar ou criar um quarto cartão: apenas remover um cartão
                # extra vazio e seguir para a próxima etapa.
                removals = 0
                while (
                    remove_extra_experience_entry(
                        page,
                        titulos_conhecidos,
                        decider=self._decide_ui_action,
                        observer=self._queue_ui_action,
                        url=snapshot.get("url", ""),
                    )
                    and removals < 3
                ):
                    page.wait_for_timeout(500)
                    snapshot = snapshot_form(page)
                    removals += 1
                decisions = []
                log_event(
                    "browser_apply_experience_step_skipped",
                    known_experiences=len(titulos_conhecidos),
                    removed_extras=removals,
                )
            else:
                decisions = self._decide(snapshot, page)
            rotulo = next_step_label(page)
            url = str(snapshot.get("url", "") or "")
            step = PreparedStep(
                number=number,
                url=url,
                domain=domain_of(url),
                decisions=decisions,
                next_label=rotulo,
            )
            if on_step is not None:
                on_step(step, page, snapshot)
            steps.append(step)

            if not rotulo or not click_next(page, rotulo):
                if not resume_ready:
                    raise ValidationError("currículo não foi anexado ao formulário")
                return steps
            page.wait_for_timeout(1200)
            state = submission_state(page)
            if state == "blocked":
                raise BlockedByPlatformError(
                    "verificação/CAPTCHA ao avançar de step; o agente não interage"
                )
            if state == "submitted":
                if allow_submit:
                    if not resume_ready:
                        raise ValidationError("currículo não foi anexado ao formulário")
                    return steps
                raise ValidationError("formulário foi enviado antes da aprovação humana")
        if not resume_ready:
            raise ValidationError("currículo não foi anexado ao formulário")
        raise ValidationError(
            f"formulário excedeu {_MAX_FORM_STEPS} steps; requer inspeção humana"
        )

    @staticmethod
    def _answers_from_steps(steps: list[PreparedStep]) -> list[AnswerRecord]:
        answers: list[AnswerRecord] = []
        for step in steps:
            for decision in step.decisions:
                answers.append(
                    AnswerRecord(
                        field=decision.signature,
                        source=decision.source,
                        value_redacted="<preenchido>",
                        required=decision.required,
                        category=decision.category,
                        step=step.number,
                        step_label=f"Step {step.number}",
                    )
                )
        return answers

    @staticmethod
    def job_url(job: JobOpportunity) -> str:
        match = re.search(r"(\d{6,})", job.external_id or job.url or "")
        if not match:
            raise ValidationError("vaga sem ID real do LinkedIn; nunca usar URL sintética")
        return f"https://www.linkedin.com/jobs/view/{match.group(1)}"

    @staticmethod
    def _apply_candidates(page: Any) -> list[tuple[int, str, str, Any]]:
        """Locators visíveis dos CTAs de candidatura, com sua posição Y.

        Inclui rótulos em inglês e português. Elementos da barra lateral
        (recomendações de outras vagas) aparecem depois e são descartados pela
        ordenação por posição feita em ``_open``.
        """
        candidatos: list[tuple[int, str, str, Any]] = []
        vistos: set[str] = set()
        for label in _APPLY_LABELS:
            for origem, locator in (
                ("role", page.get_by_role("button", name=re.compile(re.escape(label), re.I))),
                ("text", page.get_by_text(label, exact=True)),
            ):
                for indice in range(min(locator.count(), 3)):
                    alvo = locator.nth(indice)
                    try:
                        if not alvo.is_visible():
                            continue
                        caixa = alvo.bounding_box() or {}
                        altura = float(caixa.get("height") or 0)
                        largura = float(caixa.get("width") or 0)
                        if altura <= 0 or largura <= 0:
                            continue
                        chave = f"{label}:{round(caixa['y'])}:{round(caixa['x'])}"
                        if chave in vistos:
                            continue
                        vistos.add(chave)
                        candidatos.append((int(caixa["y"]), label, origem, alvo))
                    except Exception:
                        continue
                if candidatos and any(c[1] == label for c in candidatos):
                    break
        return candidatos

    @staticmethod
    def _cta_destination(locator: Any) -> str:
        """URL final do CTA externo (o LinkedIn a embrulha em /safety/go/)."""
        try:
            href = str(
                locator.evaluate(
                    "e => (e.closest('a') || e).getAttribute('href') || ''"
                )
                or ""
            )
        except Exception:
            return ""
        if "/safety/go" in href or "url=" in href:
            alvo = parse_qs(urlparse(href).query).get("url", [""])[0]
            return unquote(alvo)
        return href

    def _decide_route(self, job: JobOpportunity, label: str, destino: str) -> ApplyRoute:
        """Decide o fluxo: simplificada, portal conhecido ou cadastro externo."""
        if label in _EASY_APPLY_LABELS:
            return ApplyRoute.EASY_APPLY
        dominio = domain_of(destino) or domain_of(job.external_apply_url or "")
        if dominio:
            for portal in self.settings.application.known_portals:
                normalizado = self.repo.normalize_company(portal).lower()
                if normalizado and normalizado in dominio:
                    return ApplyRoute.KNOWN_PORTAL
            return ApplyRoute.EXTERNAL_SIGNUP
        # CTA externo sem destino identificável: não inventar cadastro.
        return ApplyRoute.EXTERNAL_SIGNUP

    def _open(
        self, job: JobOpportunity
    ) -> tuple[Any, dict[str, Any], ApplyObservation]:
        """Abre a vaga, aciona Apply e devolve a evidência de Easy Apply.

        A sondagem acontece aqui — depois da classificação — para não abrir uma
        aba extra e para que vagas reprovadas nunca cheguem ao Chrome logado.
        """
        page = self.browser.open_tab(self.job_url(job))
        snapshot = snapshot_form(page)
        if domain_of(snapshot.get("url", "")) != "linkedin.com":
            observation = ApplyObservation(False, "portal_externo")
            if self._detect_already_applied(page, snapshot):
                observation.already_applied = True
                self._log_already_applied(job, snapshot)
                return page, snapshot, observation
            page, snapshot = self._ensure_external_entry(page, snapshot=snapshot)
            page = self._advance_external_landing(page)
            self._ensure_no_credential_barrier(page)
            snapshot = snapshot_form(page)
            if self._detect_already_applied(page, snapshot):
                observation.already_applied = True
                self._log_already_applied(job, snapshot)
            return page, snapshot, observation

        title = str(snapshot.get("title", "") or "").lower()
        text = str(snapshot.get("text", "") or "").lower()
        closed_marks = (
            "não aceita mais candidaturas",
            "nao aceita mais candidaturas",
            "no longer accepting applications",
        )
        if any(mark in text for mark in closed_marks):
            self.browser.close_tab(page)
            # Vaga encerrada não é erro de automação: vira EXPIRED e a falha
            # antiga sai da aba Erros, preservando o motivo no banco.
            job.status = JobStatus.EXPIRED
            self.repo.update_job(job)
            resolver = getattr(self.repo, "resolve_failed_agent_events", None)
            if callable(resolver):
                resolver(job.id)
            raise JobUnavailable("a vaga não aceita mais candidaturas no LinkedIn")
        has_easy_text = any(label.lower() in text for label in _EASY_APPLY_LABELS)
        has_external_text = any(label.lower() in text for label in _EXTERNAL_APPLY_LABELS)
        has_apply_label = has_easy_text or has_external_text
        if "vagas | linkedin" in title and not has_apply_label:
            self.browser.close_tab(page)
            raise ValidationError("página da vaga não carregou no Chrome logado; nada foi preparado")

        observation = ApplyObservation(None)
        clicked = False
        rota: ApplyRoute | None = None
        candidatos = self._apply_candidates(page)
        # O CTA da vaga fica no topo; a barra lateral repete "Candidatura
        # simplificada" em cards de outras vagas. Escolher o primeiro texto
        # encontrado clicava na vaga errada — por isso ordenamos por posição.
        candidatos.sort(key=lambda item: item[0])
        if candidatos:
            posicao, label, origem, locator = candidatos[0]
            destino = self._cta_destination(locator)
            rota = self._decide_route(job, label, destino)
            log_event(
                "browser_apply_route_decided",
                route=rota.value,
                label=label,
                destination=domain_of(destino),
                external_id=job.external_id,
            )
            # Portal novo não é mais barra automaticamente: ele pode ter apenas
            # um formulário de candidatura. A barreira real é login/CAPTCHA,
            # verificada por _ensure_no_login logo após o redirecionamento.
            try:
                before = {id(p) for p in page.context.pages}
                locator.click()
                clicked = True
                observation = ApplyObservation(
                    rota is ApplyRoute.EASY_APPLY, label
                )
                log_event(
                    "browser_apply_clicked",
                    label=label,
                    locator=origem,
                    y=posicao,
                    route=rota.value,
                    external_id=job.external_id,
                )
                page.wait_for_timeout(1800)
                new_pages = [p for p in page.context.pages if id(p) not in before]
                if new_pages:
                    target = new_pages[0]
                    for extra in new_pages[1:]:
                        self.browser.close_tab(extra)
                    self.browser.close_tab(page)
                    page = target
                    page.wait_for_timeout(1800)
            except Exception:
                pass
        if not clicked:
            # Fallback quando o botão está em iframe/aria-label e só o texto
            # renderizado é confiável. Nunca inventa Easy Apply sem evidência.
            if has_easy_text:
                observation = ApplyObservation(True, "texto_easy_apply")
            elif has_external_text:
                observation = ApplyObservation(False, "texto_apply_externo")
        try:
            if clicked:
                snapshot = snapshot_form(page)
                page, snapshot = self._ensure_external_entry(
                    page, known_portal=rota is ApplyRoute.KNOWN_PORTAL, snapshot=snapshot
                )
                if rota is ApplyRoute.KNOWN_PORTAL:
                    page = self._advance_external_portal(page)
                    snapshot = snapshot_form(page)
                elif domain_of(page.url) != "linkedin.com":
                    page = self._advance_external_landing(page)
                    self._ensure_no_credential_barrier(page)
                    snapshot = snapshot_form(page)
                if self._detect_already_applied(page, snapshot):
                    observation.already_applied = True
                    self._log_already_applied(job, snapshot)
        except Exception:
            self.browser.close_tab(page)
            raise
        return page, snapshot, observation

    @staticmethod
    def _ensure_no_login(page: Any, snapshot: dict[str, Any]) -> None:
        """Barreira: portal em tela de login exige ação humana, não automação."""
        if _is_login_page(
            str(snapshot.get("url", "") or page.url),
            str(snapshot.get("text", "") or ""),
        ):
            raise HumanActionRequired(
                "login",
                "o portal da empresa pediu login/verificação; faça login no "
                "Chrome debug antes de candidatar",
            )

    def _detect_already_applied(self, page: Any, snapshot: dict[str, Any]) -> bool:
        """Reconhece candidatura já enviada no portal, sem confundir com testes.

        A BairesDev troca o CTA "Apply" por "Ver solicitud" quando a candidatura
        existe. Isso é evidência do portal — não é um formulário a preencher.
        """
        dominio = domain_of(str(snapshot.get("url", "") or page.url))
        cfg = self.settings.application.portal_auth.get(dominio)
        marcadores = list(cfg.already_applied_markers) if cfg else list(_ALREADY_APPLIED_MARKERS)
        texto = str(snapshot.get("text", "") or "")
        try:
            texto += " " + str(page.evaluate("() => document.body.innerText") or "")
        except Exception:
            pass
        baixo = texto.lower()
        return any(marcador.lower() in baixo for marcador in marcadores)

    @staticmethod
    def _log_already_applied(job: JobOpportunity, snapshot: dict[str, Any]) -> None:
        log_event(
            "browser_apply_already_applied_detected",
            external_id=job.external_id,
            domain=domain_of(str(snapshot.get("url", "") or "")),
            url=str(snapshot.get("url", "") or "")[:160],
        )

    @staticmethod
    def _detect_one_click_applied(snapshot: dict[str, Any]) -> bool:
        """Reconhece o clique único do LinkedIn que já registrou a candidatura."""
        texto = str(snapshot.get("text", "") or "").lower()
        return any(marker in texto for marker in _ONE_CLICK_MARKERS)

    def _one_click_applied(self, page: Any, snapshot: dict[str, Any]) -> bool:
        if self._detect_one_click_applied(snapshot):
            return True
        try:
            body = str(page.evaluate("() => document.body.innerText") or "")
        except Exception:
            body = ""
        return self._detect_one_click_applied({"text": body})

    @staticmethod
    def _normalize_landing_signature(labels: list[str]) -> str:
        return re.sub(r"\W+", " ", " ".join(labels).lower()).strip()[:300]

    @staticmethod
    def _is_apply_cta_label(label: str) -> bool:
        """Reconhece CTA de candidatura, sem confundir com login/OAuth."""
        texto = re.sub(r"\s+", " ", str(label or "").strip())
        if re.search(
            r"linkedin|indeed|google|facebook|apple|oauth|sign in|iniciar sess|login",
            texto,
            re.I,
        ):
            return False
        return bool(
            re.match(
                r"^(?:apply|aplicar|candidatar-se|candidate-se|"
                r"candidatura externa|start application|apply for this job|"
                r"apply to this job|candidate-se a esta vaga|submit application)\b",
                texto,
                re.I,
            )
        )

    def _landing_state(self, page: Any) -> dict[str, Any]:
        """Lê o que está renderizado (main + iframes) ANTES de qualquer clique."""
        principal = page.evaluate(_LANDING_STATE_JS) or {}
        if not isinstance(principal, dict):
            principal = {"text": str(principal)}
        url = str(principal.get("url") or page.url or "")
        textos = [str(principal.get("text") or "")]
        botoes: list[dict[str, Any]] = []
        checkboxes: list[dict[str, Any]] = []
        for item in principal.get("checkboxes") or []:
            checkboxes.append({**item, "frame_index": 0, "frame_url": url})
        for item in principal.get("buttons") or []:
            href = str(item.get("href") or "")
            botoes.append(
                {
                    **item,
                    "href": urljoin(url, href) if href else "",
                    "frame_index": 0,
                    "frame_url": url,
                }
            )
        has_form = bool(principal.get("has_form"))
        has_file = bool(principal.get("has_file"))
        blocked = bool(principal.get("blocked"))
        passwords = int(principal.get("passwords") or 0)

        for indice, frame in enumerate(list(getattr(page, "frames", []))[1:], start=1):
            url_frame = str(frame.url or "")
            if _HIDDEN_CAPTCHA_FRAME_RE.search(url_frame):
                continue
            try:
                elemento = frame.frame_element()
            except Exception:
                elemento = None
            if elemento is not None:
                try:
                    caixa = elemento.bounding_box() or {}
                except Exception:
                    caixa = {}
                if caixa:
                    try:
                        if (
                            float(caixa.get("width") or 0) <= 0
                            or float(caixa.get("height") or 0) <= 0
                        ):
                            continue
                    except (TypeError, ValueError):
                        pass
            try:
                dados = frame.evaluate(_LANDING_STATE_JS) or {}
                if not isinstance(dados, dict):
                    dados = {"text": str(dados)}
            except Exception:
                continue
            textos.append(str(dados.get("text") or ""))
            for item in dados.get("checkboxes") or []:
                checkboxes.append(
                    {**item, "frame_index": indice, "frame_url": url_frame}
                )
            has_form = has_form or bool(dados.get("has_form"))
            has_file = has_file or bool(dados.get("has_file"))
            blocked = blocked or bool(dados.get("blocked"))
            passwords += int(dados.get("passwords") or 0)
            for item in dados.get("buttons") or []:
                href = str(item.get("href") or "")
                botoes.append(
                    {
                        **item,
                        "href": urljoin(url_frame, href) if href else "",
                        "frame_index": indice,
                        "frame_url": url_frame,
                    }
                )

        texto = "\n".join(textos)
        rotulos = [str(item.get("text") or "") for item in botoes]
        return {
            "url": url,
            "text": texto,
            "buttons": botoes,
            "checkboxes": checkboxes,
            "has_form": has_form,
            "has_file": has_file,
            "blocked": blocked or platform_block(texto),
            "passwords": passwords,
            "login": _is_login_page(url, texto) or passwords > 0,
            "terms_checkbox": bool(
                re.search(
                    r"terms of service|termos de servi[çc]o|privacy policy|"
                    r"pol[ií]tica de privacidade|release agreement",
                    texto,
                    re.I,
                )
            ),
            "oauth_google": any(
                re.search(
                    r"continue with google|sign in with google|entrar com (o )?google|"
                    r"ingresa con google",
                    rotulo,
                    re.I,
                )
                for rotulo in rotulos
            ),
            "oauth_linkedin": any(
                re.search(
                    r"continue with linkedin|sign in with linkedin|entrar com linkedin|"
                    r"ingresa con linkedin",
                    rotulo,
                    re.I,
                )
                for rotulo in rotulos
            ),
            "description": bool(
                re.search(
                    r"descri[cç][aã]o|description|responsabilidades|responsibilities|"
                    r"what you|o que voc[êe]",
                    texto,
                    re.I,
                )
            ),
        }

    def _observe_screen(self, page: Any) -> dict[str, Any]:
        estado = self._landing_state(page)
        log_event(
            "screen_observed",
            url=str(estado.get("url", ""))[:160],
            login=bool(estado.get("login")),
            terms_checkbox=bool(estado.get("terms_checkbox")),
            terms_unchecked=sum(
                1 for item in estado.get("checkboxes", []) if not item.get("checked")
            ),
            oauth_google=bool(estado.get("oauth_google")),
            oauth_linkedin=bool(estado.get("oauth_linkedin")),
            description=bool(estado.get("description")),
            has_form=bool(estado.get("has_form")),
            blocked=bool(estado.get("blocked")),
        )
        return estado

    def _portal_needs_login(self, page: Any, snapshot: dict[str, Any]) -> bool:
        """Distingue login real de URL legada já autenticada.

        Alguns portais (ex.: Alignerr) mantêm ``/signin`` na URL após o OAuth.
        O que decide é o que está renderizado: senha, OAuth ou termos ainda
        pendentes significam login; formulário/descrição sem esses sinais não.
        """
        url = str(snapshot.get("url", "") or page.url or "")
        texto = str(snapshot.get("text", "") or "")
        if not _is_login_page(url, texto):
            return False
        estado = self._landing_state(page)
        if estado.get("passwords") or estado.get("oauth_google") or estado.get("oauth_linkedin"):
            return True
        return not (estado.get("has_form") or estado.get("description"))

    def _authenticated_page(self, page: Any, dominio: str) -> bool:
        """Verifica se a aba voltou ao portal em estado utilizável."""
        if domain_of(str(page.url or "")) != dominio:
            return False
        estado = self._landing_state(page)
        if (
            estado.get("blocked")
            or estado.get("passwords")
            or estado.get("oauth_google")
            or estado.get("oauth_linkedin")
        ):
            return False
        if _is_login_page(str(page.url or ""), str(estado.get("text") or "")):
            return bool(estado.get("has_form") or estado.get("description"))
        return True

    @staticmethod
    def _frame_at(page: Any, indice: int) -> Any:
        """Obtém o frame observado; snapshot e clique usam o mesmo índice."""
        if not indice:
            return page
        frames = getattr(page, "frames", None) or []
        try:
            return frames[indice] or page
        except Exception:
            return page

    @staticmethod
    def _click_by_label(frame: Any, label: str) -> bool:
        padrao = re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
        fabricas = (
            lambda: frame.get_by_role("link", name=padrao),
            lambda: frame.get_by_role("button", name=padrao),
            lambda: frame.get_by_text(label, exact=True),
            lambda: frame.get_by_text(label, exact=False),
            lambda: frame.locator(f'a:has-text("{label}")'),
        )
        for fabrica in fabricas:
            try:
                locator = fabrica()
                for indice in range(min(int(locator.count() or 0), 8)):
                    alvo = locator.nth(indice)
                    if hasattr(alvo, "is_visible") and not alvo.is_visible():
                        continue
                    alvo.click()
                    return True
            except Exception:
                continue
        return False

    _JS_CLICK_LABEL = r"""
    (label) => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const alvo = norm(label);
      const visivel = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const el = [...document.querySelectorAll('a,button,[role="button"],label')]
        .filter(visivel)
        .find(item => norm(item.innerText || item.getAttribute('aria-label')) === alvo);
      if (!el) return false;
      el.click();
      return true;
    }
    """

    @staticmethod
    def _unwrap_href(href: str) -> str:
        if "/safety/go" in href or "url=" in href:
            alvo = parse_qs(urlparse(href).query).get("url", [""])[0]
            return unquote(alvo) or href
        return href

    @staticmethod
    def _click_landing_cta(
        page: Any, label: str, botoes: list[dict[str, Any]] | None = None
    ) -> bool:
        """Clica o CTA lido na tela, inclusive dentro de iframe."""
        alvos = [
            item
            for item in (botoes or [])
            if BrowserApplyAgent._normalize_landing_signature(
                [str(item.get("text") or "")]
            )
            == BrowserApplyAgent._normalize_landing_signature([label])
        ] or [{"frame_index": 0, "text": label, "href": ""}]
        for item in alvos:
            href = BrowserApplyAgent._unwrap_href(str(item.get("href") or ""))
            # Landing iCIMS (Avalara) abre o formulário dentro de iframe; seguir
            # o href real lido na tela leva ao mesmo formulário na aba principal.
            if href.startswith(("http://", "https://")) and hasattr(page, "goto"):
                try:
                    page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    return True
                except Exception:
                    pass
            frame = BrowserApplyAgent._frame_at(page, int(item.get("frame_index") or 0))
            if BrowserApplyAgent._click_by_label(frame, label):
                return True
            try:
                if frame.evaluate(BrowserApplyAgent._JS_CLICK_LABEL, label):
                    return True
            except Exception:
                pass
        return False

    # Dica semântica ampla: qualquer aceite na tela passa pelo julgamento do
    # agente. Não há rótulo por empresa nem lista fixa de termos aceitos.
    _CONSENT_HINT_RE = re.compile(
        r"agree|aceite|concordo|accept|terms|termos|privacy|privacidade|release agreement",
        re.I,
    )
    _JS_CHECK_TERMS = r"""
    (label) => {
      const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const alvo = norm(label);
      for (const el of document.querySelectorAll(
        'input[type=checkbox],[role=checkbox]'
      )) {
        let texto = '';
        let node = el;
        for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
          texto = (node.innerText || '').trim();
          if (texto && texto.length <= 300) break;
        }
        if (!norm(texto).includes(alvo) &&
            !norm(el.getAttribute('aria-label')).includes(alvo)) continue;
        if (el.checked || el.getAttribute('aria-checked') === 'true') return true;
        // MUI usa um <input> escondido dentro de um <span>: clicar no texto do
        // termo não alterna o valor. O click no input dispara o onChange do React.
        el.click();
        return true;
      }
      return false;
    }
    """

    @staticmethod
    def _consent_signature(texto: str) -> str:
        return re.sub(r"\W+", " ", str(texto or "").casefold()).strip()[:200]

    @staticmethod
    def _consent_text(item: dict[str, Any]) -> str:
        return str(item.get("text") or item.get("aria") or "").strip()

    @classmethod
    def _is_consent_checkbox(cls, item: dict[str, Any]) -> bool:
        return any(
            cls._CONSENT_HINT_RE.search(texto)
            for texto in (cls._consent_text(item), str(item.get("aria") or ""))
            if texto
        )

    def _approval_context(self) -> str:
        if getattr(self, "_consent_authorized", False):
            return (
                "A candidatura tem aprovação humana persistida na PWA. Isso autoriza "
                "o envio e aceites operacionais, nunca uma barreira sensível."
            )
        return (
            "Etapa de preparação: ainda não existe aprovação persistida para o envio. "
            "Avalie apenas o texto renderizado e as barreiras sensíveis."
        )

    def _consent_decision(
        self, url: str, texto: str, screen_context: str
    ) -> Any:
        """Pergunta ao agente (DGX + memória local) sobre um aceite na tela."""
        assinatura = self._consent_signature(texto)
        learned = self.patterns.suggest_action(url, _CONSENT_INTENT, assinatura)
        memoria = ""
        if learned:
            memoria = (
                f"ação={learned.get('action','')}; "
                f"êxitos={learned.get('successes', 0)}; "
                f"falhas={learned.get('failures', 0)}; "
                f"motivo={learned.get('reason','')}"
            )
        resolver = getattr(self, "resolver", None)
        if resolver is None:
            return SimpleNamespace(
                allow=False, needs_human=True, reason="agente sem resolvedor de aceites"
            )
        decision = resolver.interpret_consent(
            texto,
            screen_context,
            approval_context=self._approval_context(),
            memory_context=memoria,
        )
        if learned and learned.get("action") == "aceitar" and getattr(decision, "allow", False):
            decision.source = "learned"
            decision.reason = str(learned.get("reason") or decision.reason)
        if getattr(decision, "allow", False):
            self._queue_ui_action(
                {
                    "url": url,
                    "intent": _CONSENT_INTENT,
                    "trigger": assinatura,
                    "action": "aceitar",
                    "dialog_signature": assinatura,
                    "source": str(getattr(decision, "source", "") or "llm"),
                    "reason": str(getattr(decision, "reason", "") or ""),
                }
            )
        return decision

    def _accept_terms_if_needed(
        self, page: Any, estado: dict[str, Any], cfg: Any = None, *, tela: str = "landing"
    ) -> Any:
        """Deixa o agente julgar cada aceite operacional visível na tela.

        Não existe configuração por portal: o modelo lê o texto renderizado,
        usa aceites já aprendidos como conhecimento e para diante de qualquer
        termo legal/sensível sem evidência humana explícita.
        """
        dominio = domain_of(str(estado.get("url") or page.url or ""))
        pendentes = [
            item
            for item in (estado.get("checkboxes") or [])
            if not item.get("checked") and self._is_consent_checkbox(item)
        ]
        if not pendentes:
            return page

        for item in pendentes:
            texto = self._consent_text(item)
            if not texto:
                raise HumanActionRequired(
                    "termos", "aceite sem texto renderizado; não é possível interpretar"
                )
            decisao = self._consent_decision(
                str(estado.get("url") or page.url or ""),
                texto,
                f"Aceite encontrado na {tela}. Texto visível da tela: "
                f"{str(estado.get('text') or '')[:1200]}",
            )
            if not getattr(decisao, "allow", False):
                raise HumanActionRequired(
                    "termos",
                    str(getattr(decisao, "reason", "") or "aceite exige decisão humana"),
                )
            frame = self._frame_at(page, int(item.get("frame_index") or 0))
            clicou = False
            try:
                clicou = bool(frame.evaluate(self._JS_CHECK_TERMS, texto))
            except Exception:
                clicou = False
            if not clicou:
                try:
                    locator = frame.get_by_text(texto, exact=False)
                    for indice in range(min(int(locator.count() or 0), 4)):
                        alvo = locator.nth(indice)
                        if hasattr(alvo, "is_visible") and not alvo.is_visible():
                            continue
                        alvo.click()
                        clicou = True
                        break
                except Exception:
                    clicou = False
            if not clicou:
                raise HumanActionRequired(
                    "termos", "não foi possível marcar o aceite exibido na tela"
                )
            page.wait_for_timeout(400)

        novo = self._landing_state(page)
        ainda_pendente = [
            item
            for item in (novo.get("checkboxes") or [])
            if not item.get("checked") and self._is_consent_checkbox(item)
        ]
        if ainda_pendente:
            raise HumanActionRequired(
                "termos", "o aceite não foi confirmado na tela"
            )
        log_event(
            "portal_terms_accepted",
            domain=dominio,
            company=str(getattr(cfg, "company", "") or ""),
            source="agent",
        )
        return page

    def _ensure_external_entry(
        self,
        page: Any,
        known_portal: bool = False,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """Lê a tela antes de agir: termos, login OAuth ou barreira humana."""
        estado = self._observe_screen(page)
        # Um widget hCaptcha invisível no formulário de login não é barreira:
        # o bloqueio real é a ausência de sessão. Resolver um desafio que não
        # está renderizado só leva a um erro genérico e esconde a causa.
        def login_sem_senha() -> bool:
            return bool(estado.get("login")) and not int(estado.get("passwords") or 0)

        if estado.get("blocked") and not login_sem_senha() and not self._already_submitted(page):
            self._resolve_challenge(page)
            estado = self._observe_screen(page)
        if self._already_submitted(page):
            return page, snapshot or {}
        if estado.get("blocked") and not login_sem_senha():
            raise BlockedByPlatformError("verificação/CAPTCHA na entrada do portal")
        settings = getattr(self, "settings", None)
        cfg = None
        if settings is not None:
            dominio = domain_of(str(estado.get("url") or page.url))
            cfg = settings.application.portal_auth.get(dominio)
        # O aceite é avaliado pelo agente em QUALQUER portal — nunca por uma
        # chave de configuração por empresa.
        if known_portal or estado.get("login") or estado.get("terms_checkbox"):
            page = self._accept_terms_if_needed(page, estado, cfg, tela="landing")
        if cfg is not None and (known_portal or estado.get("login")):
            page = self._ensure_portal_login(page)
            snapshot = snapshot_form(page)
        elif estado.get("login") or estado.get("passwords"):
            snapshot = snapshot if snapshot is not None else snapshot_form(page)
            self._ensure_no_login(page, snapshot)
        else:
            snapshot = snapshot or {}
        return page, snapshot

    def _advance_external_landing(self, page: Any) -> Any:
        """Abre o formulário real em landing externa, com decisão aprendível.

        O agente lê o estado renderizado (incluindo iframes) antes de cada
        clique. Se a tela for login, ele usa apenas autenticação configurada
        (OAuth Google com termos aceitos); senão escolhe o CTA que abre o
        formulário real. Nunca toca login não configurado, senha ou CAPTCHA.
        """
        clicados: set[tuple[str, str]] = set()
        decidido: Any = None
        acao = ""
        rotulos: list[str] = []
        for _ in range(4):
            page, _ = self._ensure_external_entry(page)
            state = self._observe_screen(page)
            if state.get("blocked"):
                self._resolve_challenge(page)
                state = self._observe_screen(page)
            if state.get("blocked"):
                raise BlockedByPlatformError("verificação/CAPTCHA na landing externa")
            if state.get("has_form") or state.get("has_file"):
                break
            if state.get("login"):
                raise HumanActionRequired(
                    "login", "a landing externa pediu login e não tem automação configurada"
                )

            botoes = [
                item
                for item in (state.get("buttons") or [])
                if str(item.get("text") or "").strip()
            ]
            url_atual = self._normalize_landing_signature([str(state.get("url") or "")])
            sociais = re.compile(
                r"linkedin|indeed|facebook|apple|oauth|sign in|iniciar sess|"
                r"acceder|entrar|login",
                re.I,
            )
            # Banners de cookie, navegação e atalhos sociais não abrem formulário.
            irrelevantes = re.compile(
                r"cookie|privac|privacy|decline|accept|reject|skip to|main menu|"
                r"home|search for jobs|introduce yourself|english|portugu",
                re.I,
            )
            botoes = [
                item
                for item in botoes
                if not sociais.search(str(item.get("text", "")))
                and not irrelevantes.search(str(item.get("text", "")))
                and (
                    url_atual,
                    self._normalize_landing_signature([str(item.get("text", ""))]),
                )
                not in clicados
            ]
            candidatura = re.compile(
                r"apply|aplicar|candidat|inscri|submit application|start application",
                re.I,
            )
            explicitos = [
                item for item in botoes if candidatura.search(str(item.get("text", "")))
            ]
            if explicitos:
                botoes = explicitos
            rotulos = [str(item["text"]).strip() for item in botoes]
            if not rotulos:
                break

            dialog = {
                "url": str(state.get("url", "") or ""),
                "text": str(state.get("text", "") or ""),
                "buttons": botoes,
            }
            decidido = self._decide_ui_action(
                "open_external_apply", dialog, "landing externa sem formulário"
            )
            acao = str(getattr(decidido, "action", "") or "").strip()
            if getattr(decidido, "needs_human", False):
                # Se a própria tela mostra um único CTA explícito de candidatura,
                # ele é evidência suficiente: não depender do LLM para avançar.
                candidatos_seguros = [
                    item
                    for item in explicitos
                    if self._is_apply_cta_label(str(item.get("text", "")))
                ]
                # Portais como Quickin repetem o mesmo CTA no header e no corpo;
                # rótulo/destino idênticos são uma única decisão segura.
                unicos: dict[tuple[str, str], dict[str, Any]] = {}
                for item in candidatos_seguros:
                    chave = (
                        self._normalize_landing_signature([str(item.get("text", ""))]),
                        str(item.get("href") or ""),
                    )
                    unicos.setdefault(chave, item)
                if len(unicos) == 1:
                    acao = str(next(iter(unicos.values()))["text"]).strip()
                    decidido = SimpleNamespace(
                        action=acao,
                        source="regra:cta_explicito_na_tela",
                        reason="CTA de candidatura único e visível na landing",
                        needs_human=False,
                    )
                    log_event(
                        "browser_apply_landing_cta_direto",
                        action=acao[:80],
                        url=str(state.get("url", ""))[:160],
                    )
                else:
                    raise HumanActionRequired(
                        "landing externa",
                        str(
                            getattr(decidido, "reason", "")
                            or "nenhum CTA seguro identificado"
                        ),
                    )
            normalizados = {
                self._normalize_landing_signature([label]) for label in rotulos
            }
            if not acao or self._normalize_landing_signature([acao]) not in normalizados:
                raise ValidationError("CTA de candidatura não identificado na landing externa")
            clicados.add(
                (url_atual, self._normalize_landing_signature([acao]))
            )
            if not self._click_landing_cta(page, acao, botoes):
                raise ValidationError("falha ao abrir o formulário na landing externa")

            antes = {id(p) for p in page.context.pages}
            page.wait_for_timeout(2500)
            novas = [p for p in page.context.pages if id(p) not in antes]
            if novas:
                for extra in novas[1:]:
                    self.browser.close_tab(extra)
                self.browser.close_tab(page)
                page = novas[0]
                page.wait_for_timeout(1500)

        status = self._observe_screen(page)
        if status.get("blocked"):
            raise BlockedByPlatformError("verificação/CAPTCHA após abrir o formulário")
        snapshot = snapshot_form(page)
        self._ensure_no_login(page, snapshot)
        if decidido is not None and acao:
            source = str(getattr(decidido, "source", "") or "llm")[:40]
            reason = str(getattr(decidido, "reason", "") or "")[:240]
            log_event(
                "browser_apply_landing_advanced",
                action=acao[:80],
                source=source,
                url=str(page.url or "")[:160],
            )
            self._queue_ui_action(
                {
                    "url": str(status.get("url", "") or page.url),
                    "intent": "open_external_apply",
                    "trigger": "landing externa sem formulário",
                    "action": acao,
                    "dialog_signature": self._normalize_landing_signature(rotulos),
                    "source": source,
                    "reason": reason,
                }
            )
        return page

    # O Google pode pedir senha em qualquer idioma; "/challenge/pk" é o
    # desafio de senha, e "/challenge/mfa|2fa|sms|totp" os demais fatores.
    _GOOGLE_CHALLENGE_URL = re.compile(
        r"/challenge/(?:pk|mfa|2fa|sms|totp|reauth)", re.I
    )
    _GOOGLE_CHALLENGE_TEXT = (
        "digite sua senha", "informe sua senha", "enter your password",
        "type your password", "ingresa tu contraseña", "introduzca su contraseña",
        "escribe tu contraseña", "verify it's you", "verifica tu identidad",
    )
    # LinkedIn reaproveita a sessão do Chrome; se cair em login/checkpoint,
    # o agente para no mesmo gate humano (nunca digita senha nem resolve MFA).
    _LINKEDIN_CHALLENGE_URL = re.compile(
        r"linkedin\.com/(?:checkpoint|authwall|u/login|login)", re.I
    )
    _OAUTH_FALLBACK_CONTINUE_LABELS = (
        "Allow", "Authorize", "Accept", "Permitir", "Autorizar",
        "Continuar", "Continue",
    )
    # Último recurso quando o locator do Playwright não encontra o rótulo:
    # clicar no elemento renderizado, sem adivinhar seletor CSS.
    _JS_CLICK_BY_TEXT = r"""
    (rotulo) => {
      const alvo = (rotulo || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (!alvo) return false;
      for (const el of document.querySelectorAll('button,a,[role=button]')) {
        const texto = (el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase();
        if (!texto.includes(alvo) || el.disabled) continue;
        el.click();
        return true;
      }
      return false;
    }
    """

    @classmethod
    def _google_challenge_detected(cls, page: Any, texto: str) -> bool:
        url = str(page.url or "")
        baixo = str(texto or "").lower()
        return bool(cls._GOOGLE_CHALLENGE_URL.search(url)) or any(
            sinal in baixo for sinal in cls._GOOGLE_CHALLENGE_TEXT
        )

    @classmethod
    def _block_google_challenge(cls, page: Any, texto: str) -> None:
        """Para antes de qualquer senha, MFA ou verificação do Google."""
        if cls._google_challenge_detected(page, texto):
            raise HumanActionRequired(
                "login", "Google pediu senha/verificação; agente não digita credencial"
            )

    @staticmethod
    def _is_google_challenge_error(exc: Exception) -> bool:
        return "Google pediu senha" in str(exc)

    @staticmethod
    def _page_text(page: Any) -> str:
        try:
            return str(page.evaluate("() => document.body.innerText") or "")
        except Exception:
            return ""

    @classmethod
    def _block_linkedin_challenge(cls, page: Any, texto: str) -> None:
        """Barreira humana no fallback: senha, verificação ou checkpoint."""
        url = str(page.url or "")
        baixo = str(texto or "").lower()
        sinais = (
            "digite sua senha", "informe sua senha", "enter your password",
            "type your password", "ingresa tu contraseña", "escribe tu contraseña",
            "senha", "password", "contraseña", "contrasena", "verify it's you",
            "verifica tu identidad", "código de verificação", "verification code",
        )
        if cls._LINKEDIN_CHALLENGE_URL.search(url) or any(
            sinal in baixo for sinal in sinais
        ):
            raise HumanActionRequired(
                "login", "LinkedIn pediu senha/verificação; agente não digita credencial"
            )

    @classmethod
    def _block_oauth_captcha(cls, texto: str, provedor: str) -> None:
        texto = str(texto or "")
        if platform_block(texto) or re.search(r"\b(?:2fa|mfa)\b", texto, re.I):
            raise BlockedByPlatformError(
                f"verificação/CAPTCHA no OAuth {provedor}; agente não interage"
            )

    def _click_oauth_button(
        self, page: Any, labels: list[str], dominio: str, provedor: str
    ) -> str:
        """Clica no botão do provedor lido da tela, sem adivinhar seletor."""
        for rotulo in labels or []:
            alvo = page.get_by_text(rotulo, exact=False)
            if alvo.count() == 0:
                alvo = page.get_by_role("button", name=re.compile(re.escape(rotulo), re.I))
            if alvo.count():
                try:
                    alvo.first.click()
                    return rotulo
                except Exception:
                    pass
            try:
                if page.evaluate(self._JS_CLICK_BY_TEXT, rotulo):
                    return rotulo
            except Exception:
                pass
        raise HumanActionRequired(
            "login", f"botão de login {provedor} não encontrado em {dominio}"
        )

    @staticmethod
    def _wait_for_oauth_popup(
        page: Any, dominio: str, popup_domain: str, provedor: str
    ) -> Any:
        contexto = page.context
        for _ in range(8):
            popup = next(
                (
                    p for p in reversed(contexto.pages)
                    if popup_domain in str(p.url or "")
                ),
                None,
            )
            if popup is not None:
                return popup
            page.wait_for_timeout(1000)
        raise BlockedByPlatformError(
            f"OAuth {provedor} de {dominio} não abriu a seleção de conta"
        )

    def _close_oauth_pages(
        self, contexto: Any, popup_domain: str, preservar: tuple[Any, ...] = ()
    ) -> None:
        """Fecha só abas do provedor, nunca a aba do portal nem a janela."""
        browser = getattr(self, "browser", None)
        if browser is None:
            return
        for aba in list(contexto.pages):
            if popup_domain not in str(aba.url or ""):
                continue
            if any(aba is manter for manter in preservar):
                continue
            try:
                browser.close_tab(aba)
            except Exception:
                pass

    def _google_oauth_login(self, page: Any, cfg: Any, dominio: str) -> Any:
        """OAuth Google; se pedir senha, usa o fallback configurado uma vez."""
        try:
            return self._oauth_with_provider(
                page,
                cfg,
                dominio,
                provedor="Google",
                popup_domain="accounts.google.com",
                labels=list(cfg.oauth_labels or []),
                continue_labels=list(cfg.oauth_continue_labels or []),
                account_email=str(cfg.oauth_account_email or ""),
            )
        except HumanActionRequired as exc:
            fallbacks = list(getattr(cfg, "oauth_fallback_labels", []) or [])
            if not self._is_google_challenge_error(exc) or not fallbacks:
                raise
            log_event(
                "portal_google_password_fallback",
                domain=dominio,
                company=cfg.company,
                reason=str(exc)[:160],
            )
            self._close_oauth_pages(page.context, "accounts.google.com", preservar=(page,))
            return self._fallback_oauth_login(page, cfg, dominio)

    @staticmethod
    def _portal_page_for_fallback(
        page: Any, cfg: Any, dominio: str, labels: list[str]
    ) -> Any:
        """Escolhe a aba do portal que realmente exibe o botão alternativo.

        Depois do popup do Google, a aba usada no clique pode ter navigado para
        o callback. As outras abas do mesmo domínio continuam no /signin.
        """
        contexto = page.context
        util = lambda candidata: (
            "linkedin.com" not in str(candidata.url or "")
            and "accounts.google.com" not in str(candidata.url or "")
            and domain_of(str(candidata.url or "")) == dominio
        )
        candidatas = [page] + [
            p for p in contexto.pages if p is not page and util(p)
        ]
        for candidata in candidatas:
            for rotulo in labels:
                try:
                    if candidata.get_by_text(rotulo, exact=False).count():
                        return candidata
                except Exception:
                    continue
        for candidata in contexto.pages:
            if util(candidata) and hasattr(candidata, "goto"):
                try:
                    candidata.goto(
                        cfg.login_url, wait_until="domcontentloaded", timeout=30000
                    )
                    return candidata
                except Exception:
                    continue
        return page

    def _fallback_oauth_login(self, page: Any, cfg: Any, dominio: str) -> Any:
        """Tenta o provedor alternativo já logado no Chrome, sem digitar nada."""
        labels = list(getattr(cfg, "oauth_fallback_labels", []) or [])
        if not labels:
            raise HumanActionRequired(
                "login", f"fallback de login não configurado em {dominio}"
            )
        pagina_portal = self._portal_page_for_fallback(page, cfg, dominio, labels)
        log_event(
            "portal_oauth_fallback_page",
            domain=dominio,
            url=str(pagina_portal.url or "")[:160],
            atual=str(page.url or "")[:160],
        )
        page = pagina_portal
        # A aba do portal pode ter voltado ao /signin com os termos desmarcados
        # quando o popup do Google foi aberto. Reler e aceitar antes de clicar.
        page = self._accept_terms_if_needed(
            page, self._landing_state(page), cfg, tela="entrada alternativa"
        )
        continuar = list(self._OAUTH_FALLBACK_CONTINUE_LABELS) + list(
            cfg.oauth_continue_labels or []
        )
        vistos: list[str] = []
        for rotulo in continuar:
            if rotulo not in vistos:
                vistos.append(rotulo)
        log_event(
            "portal_oauth_fallback_started",
            domain=dominio,
            company=cfg.company,
            labels=[r[:40] for r in labels],
        )
        return self._oauth_with_provider(
            page,
            cfg,
            dominio,
            provedor="LinkedIn",
            popup_domain="linkedin.com",
            labels=labels,
            continue_labels=vistos,
            account_email="",
        )

    def _oauth_with_provider(
        self,
        page: Any,
        cfg: Any,
        dominio: str,
        *,
        provedor: str,
        popup_domain: str,
        labels: list[str],
        continue_labels: list[str],
        account_email: str,
    ) -> Any:
        """Fluxo OAuth compartilhado: ler a tela, clicar, consentir, confirmar."""
        contexto = page.context
        prefixo = "google" if provedor == "Google" else "linkedin"
        rotulo_clicado = self._click_oauth_button(page, labels, dominio, provedor)
        page.wait_for_timeout(2500)
        log_event(
            f"portal_{prefixo}_button",
            domain=dominio,
            company=cfg.company,
            rotulo=rotulo_clicado[:40],
        )
        popup = self._wait_for_oauth_popup(page, dominio, popup_domain, provedor)

        texto = self._page_text(popup)
        log_event(
            f"portal_{prefixo}_screen",
            domain=dominio,
            trecho=" ".join(texto[:240].split())[:240],
        )
        if provedor == "Google":
            self._block_google_challenge(popup, texto)
        else:
            self._block_linkedin_challenge(popup, texto)
        self._block_oauth_captcha(texto, provedor)

        if account_email and account_email.lower() in texto.lower():
            conta = popup.get_by_text(account_email, exact=False)
            if conta.count():
                conta.first.click()
                popup.wait_for_timeout(1500)
                texto = self._page_text(popup)
                if provedor == "Google":
                    self._block_google_challenge(popup, texto)
                else:
                    self._block_linkedin_challenge(popup, texto)
                self._block_oauth_captcha(texto, provedor)

        clicou_continue = self._click_oauth_button(
            popup, continue_labels, dominio, f"consentimento {provedor}"
        )
        popup.wait_for_timeout(4000)
        log_event(
            f"portal_{prefixo}_continue",
            domain=dominio,
            rotulo=clicou_continue[:40],
            urls=[str(p.url or "")[:160] for p in contexto.pages][:8],
        )

        destino = None
        for _ in range(8):
            for candidata in contexto.pages:
                if popup_domain in str(candidata.url or ""):
                    texto_aba = self._page_text(candidata)
                    if provedor == "Google":
                        self._block_google_challenge(candidata, texto_aba)
                    else:
                        self._block_linkedin_challenge(candidata, texto_aba)
                    self._block_oauth_captcha(texto_aba, provedor)
            for candidata in contexto.pages:
                if self._authenticated_page(candidata, dominio):
                    destino = candidata
                    break
            if destino is not None:
                break
            page.wait_for_timeout(1000)

        self._close_oauth_pages(
            contexto, popup_domain, preservar=tuple(p for p in (page, destino) if p is not None)
        )
        if destino is None:
            log_event(
                f"portal_{prefixo}_oauth_inconclusivo",
                domain=dominio,
                urls=[str(p.url or "")[:160] for p in contexto.pages][:8],
            )
            raise HumanActionRequired(
                "login",
                f"OAuth {provedor} de {dominio} não retornou ao portal autenticado",
            )
        log_event(
            f"portal_{prefixo}_oauth",
            domain=dominio,
            company=cfg.company,
            account=account_email,
        )
        return destino

    def _ensure_portal_login(self, page: Any) -> Any:
        """Faz login automático em portal conhecido (OAuth Google ou keyring).

        Nunca resolve CAPTCHA/MFA/verificação: se a plataforma pedir isso, a
        barreira abaixo interrompe e sinaliza ação humana.
        """
        snapshot = snapshot_form(page)
        url = str(snapshot.get("url", "") or page.url)
        if not self._portal_needs_login(page, snapshot):
            return page
        dominio = domain_of(url)
        cfg = self.settings.application.portal_auth.get(dominio)
        if cfg is None:
            raise HumanActionRequired(
                "login", f"portal {dominio or 'desconhecido'} exige login e não tem config de automação"
            )
        if cfg.auth_mode == "google":
            return self._google_oauth_login(page, cfg, dominio)
        credenciais = self.portal_credentials.get(dominio)
        if credenciais is None:
            raise HumanActionRequired(
                "login",
                f"credenciais de {dominio} não cadastradas; "
                f"rode: python -m src.cli portal-auth --set --domain {dominio}",
            )
        usuario, senha = credenciais
        try:
            campo_usuario = page.locator(cfg.username_selector).first
            campo_senha = page.locator(cfg.password_selector).first
            campo_usuario.fill(usuario)
            campo_senha.fill(senha)
            clicou = False
            for label in cfg.submit_labels:
                locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                if locator.count() == 0:
                    locator = page.get_by_text(label, exact=True)
                if locator.count():
                    locator.first.click()
                    clicou = True
                    break
            if not clicou:
                raise ValidationError("botão de login não encontrado no portal")
            page.wait_for_timeout(3000)
        except HumanActionRequired:
            raise
        except Exception as exc:
            raise BlockedByPlatformError(
                f"falha ao automatizar login em {dominio}: {type(exc).__name__}"
            ) from exc

        snapshot = snapshot_form(page)
        url = str(snapshot.get("url", "") or page.url)
        texto = str(snapshot.get("text", "") or "").lower()
        if self._portal_needs_login(page, snapshot):
            if (
                platform_block(texto)
                or "just a moment" in str(texto).lower()
                or re.search(r"\b(?:2fa|mfa)\b", str(texto), re.I)
            ):
                self._resolve_challenge(page)
                snapshot = snapshot_form(page)
                texto = str(snapshot.get("text", "") or "").lower()
                if not self._portal_needs_login(page, snapshot):
                    log_event("portal_login_challenge_resolved", domain=dominio)
                    return page
                raise BlockedByPlatformError(
                    f"verificação/CAPTCHA no login de {dominio}; a skill não conseguiu resolver"
                )
            raise HumanActionRequired(
                "login", f"login automático em {dominio} não foi confirmado; verifique as credenciais"
            )
        log_event("portal_login_automated", domain=dominio, company=cfg.company)
        return page

    def _handle_post_submit_assessment(self, page: Any, domain: str) -> None:
        """Distingue confirmação de envio de testes internos da plataforma.

        No LinkedIn, o diálogo pós-envio traz dados da candidatura (e-mail, nome).
        Em portais próprios pode trazer testes internos da empresa — isso NÃO é
        parte da candidatura em si e, quando já concluídos, só é informativo.
        """
        if domain == "linkedin.com":
            return
        cfg = self.settings.application.portal_auth.get(domain)
        if cfg is None:
            return
        texto = str(page.evaluate("() => document.body.innerText") or "").lower()
        if not any(marker in texto for marker in cfg.assessment_markers):
            return
        log_event(
            "portal_assessment_detected",
            domain=domain,
            company=cfg.company,
            status=cfg.assessment_status,
        )
        if cfg.assessment_status == "completed":
            log_event(
                "portal_assessment_already_completed",
                domain=domain,
                company=cfg.company,
            )
            return
        log_event("portal_assessment_pending", domain=domain, company=cfg.company)

    def _advance_external_portal(self, page: Any) -> Any:
        """Portais conhecidos abrem o form só após um segundo clique.

        Na BairesDev, o LinkedIn redireciona para a página da vaga, que exige
        clicar em "Apply"/"Aplicar" para revelar o formulário. Se esse clique
        cair no login, a barreira abaixo interrompe e pede ação humana.
        """
        page = self._ensure_portal_login(page)
        snapshot = snapshot_form(page)
        self._ensure_no_login(page, snapshot)
        if snapshot.get("fields"):
            return page
        # O CTA real da BairesDev é o botão do card da vaga. Usar o seletor
        # semântico evita confundir com links de navegação do cabeçalho.
        especifico = page.locator('[data-cy="jobApplyApply"]')
        opcoes: list[tuple[str, Any]] = []
        if especifico.count():
            opcoes.append(("data-cy:jobApplyApply", especifico.first))
        for label in ("Aplicar", "Apply", "Apply now!", "Candidatar-se", "Enviar candidatura"):
            locator = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if locator.count() == 0:
                locator = page.get_by_text(label, exact=True)
            if locator.count():
                opcoes.append((label, locator.first))

        for label, alvo in opcoes:
            try:
                if not alvo.is_visible():
                    continue
            except Exception:
                continue
            antes = {id(p) for p in page.context.pages}
            alvo.click()
            page.wait_for_timeout(3000)
            novas = [p for p in page.context.pages if id(p) not in antes]
            if novas:
                for extra in novas[1:]:
                    self.browser.close_tab(extra)
                self.browser.close_tab(page)
                page = novas[0]
                page.wait_for_timeout(2000)
            log_event("browser_apply_portal_advanced", label=label, url=page.url[:160])
            page = self._ensure_portal_login(page)
            snapshot = snapshot_form(page)
            self._ensure_no_login(page, snapshot)
            return page
        return page

    def _record_easy_apply(self, job: JobOpportunity, observation: ApplyObservation) -> None:
        """Persiste o achado somente para vagas já classificadas como válidas."""
        if observation.easy_apply is None:
            return
        elegiveis = {
            JobStatus.QUALIFIED,
            JobStatus.PENDING_APPROVAL,
            JobStatus.APPLYING,
        }
        if job.status not in elegiveis:
            log_event(
                "browser_apply_easy_apply_ignored",
                external_id=job.external_id,
                status=job.status.value,
            )
            return
        novo_canal = (
            "easy_apply" if observation.easy_apply else classify_channel(job, self.settings)
        )
        if (
            job.easy_apply == observation.easy_apply
            and job.easy_apply_source == "browser_apply"
            and job.channel == novo_canal
        ):
            return
        job.easy_apply = observation.easy_apply
        job.easy_apply_source = "browser_apply"
        job.channel = novo_canal
        self.repo.update_job(job)
        log_event(
            "easy_apply_detected_after_classification",
            external_id=job.external_id,
            easy_apply=observation.easy_apply,
            label=observation.label,
            channel=novo_canal,
        )

    def _recheck_remote(self, job: JobOpportunity, snapshot: dict[str, Any]) -> None:
        """Barreira final: 100% remoto continua obrigatório no momento do envio."""
        if not self.settings.search.remote_only:
            return
        analysis_remote = job.analysis.is_remote if job.analysis else None
        if analysis_remote is False:
            raise ValidationError("a vaga deixou de ser 100% remota; envio bloqueado")
        job_text = str(snapshot.get("job_text") or job.description or "")
        page_text = str(snapshot.get("text", "") or "")
        # Card/título/descrição da própria vaga são a fonte primária. O texto
        # renderizado (modal do LinkedIn, "Vagas semelhantes", rodapé) contém
        # "the office"/"blend of both" e não pode vetoar uma vaga cujo card já
        # declara 100% remoto — era exatamente o caso de vagas sem descrição.
        remote_ok = is_remote_location(job.location, job.title, job.description or "")
        if not remote_ok:
            remote_ok = is_remote_location(job.location, job.title, job_text or page_text)
        if not remote_ok:
            raise ValidationError("remoto não confirmado na rechecagem pré-envio")
        # O card da vaga pode dizer "Híbrido" mesmo quando a descrição afirma
        # "100% remoto". O rótulo explícito do cabeçalho vence a descrição;
        # apenas rótulos fortes (Híbrido/Presencial/On-site) entram aqui —
        # "the office"/"blend of both" de cards laterais ficam de fora.
        cabecalho = " ".join(
            [
                str(job.title or ""),
                str(job.location or ""),
                str(snapshot.get("title") or ""),
                page_text[:600],
            ]
        )
        if has_explicit_onsite_label(cabecalho):
            raise ValidationError("card da vaga marca híbrido/presencial; envio bloqueado")

    def _decide_ui_action(
        self, intent: str, dialog: dict[str, Any], trigger: str
    ) -> Any:
        """Usa memória local + LLM da DGX para decidir o próximo clique."""
        url = str(dialog.get("url", "") or "")
        learned = self.patterns.suggest_action(url, intent, trigger)
        buttons = [
            str(item.get("text") or item.get("aria") or "")
            for item in dialog.get("buttons", []) or []
        ]
        buttons = [label for label in buttons if label]
        memory_context = ""
        if learned:
            memory_context = (
                f"ação={learned.get('action','')}; "
                f"êxitos={learned.get('successes', 0)}; "
                f"falhas={learned.get('failures', 0)}; "
                f"motivo={learned.get('reason','')}"
            )
        decision = self.resolver.interpret_action(
            intent,
            str(dialog.get("text", "") or ""),
            buttons,
            memory_context,
        )
        if learned and learned.get("action") == getattr(decision, "action", ""):
            decision.source = "learned"
        log_event(
            "browser_apply_ui_action_decided",
            intent=intent,
            source=str(getattr(decision, "source", "") or "")[:40],
            needs_human=bool(getattr(decision, "needs_human", False)),
        )
        return decision

    def _queue_ui_action(self, info: dict[str, Any]) -> None:
        pending = getattr(self, "_pending_ui_actions", None)
        if pending is None:
            pending = []
            self._pending_ui_actions = pending
        pending.append(dict(info))

    def _flush_ui_actions(self, *, success: bool) -> None:
        if not self._pending_ui_actions:
            return
        actions, self._pending_ui_actions = self._pending_ui_actions, []
        for info in actions:
            self.patterns.record_action(
                str(info.get("url", "")),
                str(info.get("intent", "")),
                str(info.get("trigger", "")),
                str(info.get("action", "")),
                str(info.get("dialog_signature", "")),
                success=success,
                source=str(info.get("source", "")),
                reason=str(info.get("reason", "")),
            )
        log_event(
            "browser_apply_ui_actions_learned",
            actions=len(actions),
            success=success,
        )

    def _cv_path(self) -> str:
        """Caminho real do CV no runtime (override da DGX tem precedência)."""
        return str(
            self.settings.profile.cv_path or self.profile.documents.get("resume", "")
        )

    def _click_primary_apply_cta(self, page: Any) -> Any | None:
        """Abre o formulário pelo CTA principal quando o CV ainda não apareceu.

        Portais como o joblet.ai têm um formulário de login/newsletter na página
        da vaga, então ``snapshot_form`` vê campos e o fluxo antigo desistia do
        upload sem clicar no "Apply Now" real. Este clique é apenas de abertura:
        ignora vagas semelhantes, login/OAuth, salvar vaga e botões finais, e
        nunca submete a candidatura. Devolve a página ativa (pode ser uma nova
        aba) ou ``None`` quando não há CTA seguro.
        """
        clicados = getattr(self, "_primary_cta_clicked", None)
        if clicados is None:
            clicados = set()
            self._primary_cta_clicked = clicados
        chave_url = str(getattr(page, "url", "") or "")
        try:
            antes = {id(item) for item in page.context.pages}
        except Exception:
            antes = set()
        try:
            rotulo = page.evaluate(
                r"""() => {
                  const permitido = /^(?:apply|aplicar|candidatar-se|candidate-se|start application|apply for this job|apply to this job|apply now)\b/i;
                  const proibido = /sign in|login|oauth|google|linkedin|facebook|apple|submit|enviar|finalizar|review|save job|salvar vaga/i;
                  const candidatos = [...document.querySelectorAll('button,[role="button"],a')]
                    .filter((el) => {
                      const texto = ((el.getAttribute('aria-label') || el.innerText || '') + '').trim().replace(/\s+/g, ' ');
                      if (!permitido.test(texto) || proibido.test(texto)) return false;
                      if (el.closest('#similar-jobs, .jd-similar-jobs-list, nav, footer, [class*="similar"]')) return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    });
                  if (!candidatos.length) return '';
                  const alvo = candidatos.find((el) =>
                    el.matches('.jd-hero-cta-apply, [aria-label="Apply Now"]')
                  ) || candidatos[0];
                  alvo.click();
                  return ((alvo.getAttribute('aria-label') || alvo.innerText || '') + '').trim();
                }"""
            )
        except Exception:
            return None
        if not rotulo:
            return None
        rotulo = str(rotulo)[:80]
        if (chave_url, rotulo) in clicados:
            return None
        clicados.add((chave_url, rotulo))
        log_event("browser_apply_primary_cta_for_resume", label=rotulo, url=chave_url[:160])
        page.wait_for_timeout(2000)
        try:
            novas = [item for item in page.context.pages if id(item) not in antes]
        except Exception:
            novas = []
        if novas:
            navegador = getattr(self, "browser", None)
            for extra in novas[1:]:
                if navegador is not None:
                    navegador.close_tab(extra)
            if navegador is not None:
                navegador.close_tab(page)
            return novas[0]
        return page

    def _handle_resume(
        self, page: Any, snapshot: dict[str, Any]
    ) -> tuple[Any, dict[str, Any], str]:
        """Anexa o CV e devolve ``(página, snapshot, status)``.

        ``upload_button_not_found`` é provisório: em landing externa o botão
        "Apply" ainda precisa ser clicado, e em formulários paginados o controle
        aparece em steps posteriores. Nunca faz Submit.
        """
        self._ensure_no_credential_barrier(page)
        cv_path = self._cv_path()
        status = ensure_resume_uploaded(page, cv_path)
        pendente = {
            "upload_button_not_found", "file_input_not_found", "resume_input_not_found",
        }
        if status in pendente:
            # A tela ainda é a landing da vaga (careerpuck/iCIMS/Greenhouse):
            # abrir o formulário real usando apenas CTA visível na própria tela.
            for _ in range(2):
                # Portais com form de login/newsletter na página (joblet.ai)
                # expõem "fields" antes do formulário de candidatura: clicar o
                # CTA principal da vaga é necessário mesmo nesse caso.
                aberta = self._click_primary_apply_cta(page)
                if aberta is not None:
                    page = aberta
                    status = ensure_resume_uploaded(page, cv_path)
                    if status not in pendente:
                        break
                    continue
                atual = snapshot_form(page)
                if atual.get("fields"):
                    break
                try:
                    page = self._advance_external_landing(page)
                except (HumanActionRequired, BlockedByPlatformError):
                    raise
                except Exception:
                    break
                status = ensure_resume_uploaded(page, cv_path)
                if status not in pendente:
                    break
        if status in {"upload_button_not_found", "file_input_not_found", "unverified"}:
            # O upload pode ter ocorrido na aba anterior do mesmo fluxo
            # (Alignerr: /upload-resume -> /profile). Nesse caso o step atual
            # não tem mais input de arquivo, mas o CV está anexado ao cadastro.
            for aba in list(getattr(getattr(page, "context", None), "pages", []) or []):
                if aba is page:
                    continue
                try:
                    if resume_attached_on_page(aba, cv_path):
                        status = "already_attached"
                        log_event(
                            "browser_apply_resume_already_attached_in_other_tab",
                            filename=Path(cv_path).name if cv_path else "",
                        )
                        break
                except Exception:
                    continue
        log_event(
            "browser_apply_resume",
            status=status,
            filename=Path(cv_path).name if cv_path else "",
        )
        if status == "blocked":
            self._resolve_challenge(page)
            if ensure_resume_uploaded(page, cv_path) == "blocked":
                raise BlockedByPlatformError("verificação/CAPTCHA ao anexar currículo")
        if status == "missing":
            raise ValidationError("currículo informado não existe")
        invalid = {
            "select_failed", "file_input_not_found", "resume_input_not_found", "unverified",
        }
        if status in invalid:
            raise ValidationError(f"upload do currículo não confirmado: {status}")
        if status in {"uploaded", "selected"}:
            # O upload insere um novo radio: os índices do snapshot mudam.
            page.wait_for_timeout(1200)
            return page, snapshot_form(page), status
        return page, snapshot, status

    def _decide(
        self, snapshot: dict[str, Any], page: Any | None = None
    ) -> list[FillDecision]:
        url = snapshot.get("url", "")
        decisions: list[FillDecision] = []
        for raw in snapshot.get("fields", []):
            opcoes = list(raw.get("options", []))
            if page is not None and raw.get("type") == "combobox" and not opcoes:
                # React Select cria a listbox só ao abrir. Buscar as opções aqui
                # permite usar a opção do portal para não responder em campos
                # demográficos sem inventar dado do perfil.
                opcoes = peek_combobox_options(page, int(raw["index"]))
                raw["options"] = opcoes
            field = FormField(
                label=raw.get("label", ""),
                field_type=raw.get("type", "text"),
                required=bool(raw.get("required")),
                options=opcoes,
                autocomplete=raw.get("autocomplete", ""),
                identifier=raw.get("identifier", ""),
                placeholder=raw.get("placeholder", ""),
                nearby_text=raw.get("nearby_text", ""),
            )
            # Na Alignerr a última etapa é uma lista fixa de preferências; o
            # operador escolheu explicitamente Coding/Engineering/Language.
            # Marcar só essas evita que o agente deixe "Science" marcado por
            # padrão e envie preferências diferentes das autorizadas.
            if (
                domain_of(url) == "app.alignerr.com"
                and "/application/preferences" in url
                and field.field_type == "checkbox"
                and field.label.strip().casefold()
                in {item.casefold() for item in _ALIGNERR_PREFERENCES}
            ):
                desejadas = {
                    item.casefold() for item in self.profile.project_preferences
                }
                decisions.append(
                    FillDecision(
                        index=int(raw["index"]),
                        signature=field.signature(),
                        selector=str(raw.get("selector", "")),
                        kind=str(raw.get("type", "text")),
                        value="true" if field.label.strip().casefold() in desejadas else "false",
                        source="profile.project_preferences",
                        required=field.required,
                        category="general",
                    )
                )
                continue
            if field.field_type == "checkbox":
                texto = " ".join(
                    parte.strip()
                    for parte in (field.label, field.nearby_text)
                    if str(parte or "").strip()
                )
                if self._CONSENT_HINT_RE.search(texto):
                    decisao = self._consent_decision(
                        str(url or ""),
                        texto,
                        f"Campo de aceite no formulário. Pergunta renderizada: {texto[:1200]}",
                    )
                    if getattr(decisao, "allow", False):
                        decisions.append(
                            FillDecision(
                                index=int(raw["index"]),
                                signature=field.signature(),
                                selector=str(raw.get("selector", "")),
                                kind=str(raw.get("type", "text")),
                                value="true",
                                source=str(getattr(decisao, "source", "") or "llm"),
                                required=field.required,
                                category="consent",
                            )
                        )
                        continue
                    raise HumanActionRequired(
                        "termos",
                        str(getattr(decisao, "reason", "") or "aceite exige decisão humana"),
                    )
            signature = field.signature()
            if not field.required and _OPTIONAL_LOCATION_RE.search(
                " ".join([str(field.label or ""), str(field.nearby_text or "")])
            ):
                log_event("browser_apply_optional_field_skipped", signature=signature)
                continue
            learned = self.patterns.suggest(url, signature)
            value = ""
            source = ""
            needs_human = False
            category = "general"

            valor_aprendido = ""
            if (
                learned
                and learned.get("source")
                and "location" not in str(learned["source"])
            ):
                # "location" aprendido é a string inteira ("São Paulo, SP,
                # Brasil") e não serve para um <select> de código do país;
                # o mapper extrai a parte correta com evidência do perfil.
                valor_aprendido = _value_from_source(self.profile, str(learned["source"]))
            if valor_aprendido:
                value = valor_aprendido
                source = f"learned:{learned['source']}"
                needs_human = False
            else:
                # Origem aprendida ausente/inválida não é evidência: consultar o
                # mapper com o perfil atual em vez de bloquear o campo.
                decision = self.resolver.resolve(field)
                value = decision.value
                source = decision.source
                needs_human = decision.needs_human
                category = decision.reason if hasattr(decision, "reason") else "general"
                category = "sensitive" if needs_human and "sens" in str(category).lower() else category

            sensivel = (
                category == "sensitive" or source == "politica:acao_sensivel"
            )
            if needs_human or not value:
                # Campo sensível sem evidência bloqueia mesmo quando o DOM não
                # marca ``required``: gênero/data de nascimento não podem ser
                # inventados nem ignorados para liberar o Submit.
                if not field.required and not sensivel:
                    log_event("browser_apply_optional_field_skipped", signature=signature)
                    continue
                rotulo = str(field.label or signature).strip()
                # Rótulos como "NoYes"/"Select..." não dizem ao operador o que
                # o portal perguntou; o contexto renderizado é mais actionable.
                if len(rotulo) < 24 and str(field.nearby_text or "").strip():
                    rotulo = " ".join(str(field.nearby_text).split())[:160]
                raise HumanActionRequired(
                    "campo de formulário", f"{rotulo} não tem evidência no perfil"
                )
            decisions.append(
                FillDecision(
                    index=int(raw["index"]),
                    signature=signature,
                    selector=str(raw.get("selector", "")),
                    kind=str(raw.get("type", "text")),
                    value=value,
                    source=source,
                    required=field.required,
                    category=category,
                )
            )
        return decisions

    def _record_existing_application(
        self, job: JobOpportunity, snapshot: dict[str, Any], observation: ApplyObservation
    ) -> Application:
        """Registra candidatura já existente no portal, com evidência observada.

        Não passa pelo ApprovalGate: nada foi enviado por esta execução. O
        ``APPROVED`` continua obrigatório apenas para envios novos.
        """
        url = str(snapshot.get("url", "") or "")
        dominio = domain_of(url)
        cfg = self.settings.application.portal_auth.get(dominio)
        marcadores = list(cfg.already_applied_markers) if cfg else list(_ALREADY_APPLIED_MARKERS)
        texto = str(snapshot.get("text", "") or "")
        evidencias = [m for m in marcadores if m.lower() in texto.lower()]
        evidencia = ", ".join(evidencias[:3]) or "sinal de candidatura existente no portal"
        application = Application(
            trace_id=job.trace_id,
            opportunity_id=job.id,
            company=job.company,
            title=job.title,
            status="SUBMITTED",
            channel=job.channel,
            resume_path=self._cv_path(),
            portal_url=url,
            notes=f"candidatura já existente no portal: {evidencia}",
            success_feedback=evidencia,
        )
        self.repo.record_application(application)
        job.status = JobStatus.SUBMITTED
        job.has_applied = True
        job.applied_at = application.applied_at or datetime.now()
        self.repo.update_job(job)
        log_event(
            "browser_apply_already_applied",
            external_id=job.external_id,
            company=job.company,
            title=job.title,
            domain=dominio,
            evidence=evidencia,
            label=observation.label,
        )
        if self.bus:
            self.bus.emit("application.already_applied", company=job.company, title=job.title)
        return application

    def prepare(self, job: JobOpportunity) -> ApprovalRequest | None:
        """Lê o formulário e cria/reusa a aprovação. Não clica em Submit.

        Devolve ``None`` quando o portal mostra que a candidatura já existe:
        nesse caso a candidatura é registrada e a vaga marcada SUBMITTED, sem
        aprovação, upload ou Submit.
        """
        if job.status not in {JobStatus.QUALIFIED, JobStatus.PENDING_APPROVAL}:
            raise ValidationError(
                "vaga precisa estar classificada como QUALIFIED antes de abrir o Chrome"
            )
        if self.repo.has_application_for(job.id):
            raise ValidationError("candidatura duplicada para esta vaga")
        existing = self.repo.get_approval_for_job(job.id)
        if existing is not None and existing.status == "APPROVED":
            raise ValidationError("aprovação já registrada; use o modo de envio")
        self._consent_authorized = False
        self._active_job = job
        page, snapshot, observation = self._open(job)
        try:
            self._record_easy_apply(job, observation)
            snapshot = snapshot_form(page)
            snapshot["job_text"] = job.description or ""
            if observation.already_applied or self._detect_already_applied(page, snapshot):
                self._record_existing_application(job, snapshot, observation)
                return None
            if self._one_click_applied(page, snapshot):
                self._record_existing_application(
                    job,
                    snapshot,
                    ApplyObservation(False, "clique_unico_linkedin", True),
                )
                return None
            steps = self._walk_steps(page, job)
            if not any(step.decisions for step in steps):
                raise HumanActionRequired(
                    "formulário", "nenhum campo preenchível encontrado; requer inspeção humana"
                )
            prepared = PreparedForm(
                url=steps[0].url,
                domain=steps[0].domain,
                decisions=[decision for step in steps for decision in step.decisions],
                steps=steps,
            )
        finally:
            self._consent_authorized = False
            self.browser.close_tab(page)

        answers = self._answers_from_steps(steps)
        if existing is not None and existing.status == "PENDING":
            approval = existing
            approval.answers = answers
            self.repo.update_approval(approval)
        else:
            approval = ApprovalRequest(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                score=job.score,
                summary=job.analysis.summary if job.analysis else "",
                answers=answers,
            )
            self.repo.create_approval(approval)
        self._pending[job.id] = prepared
        job.status = JobStatus.PENDING_APPROVAL
        self.repo.update_job(job)
        log_event(
            "browser_apply_prepared",
            company=job.company,
            fields=len(answers),
            steps=len(steps),
            domain=prepared.domain,
            approval_id=approval.id,
        )
        if self.bus:
            self.bus.emit("application.prepared", company=job.company, title=job.title)
        return approval

    def confirm(self, approval: ApprovalRequest) -> bool:
        """Pergunta ao humano. Não persiste a decisão (uso legado)."""
        return self.gate.request(approval)

    def approve(self, job: JobOpportunity, approval: ApprovalRequest) -> bool:
        """Aprovação humana obrigatória, persistida ANTES de qualquer envio."""
        approved = self.gate.request(approval)
        self.repo.resolve_approval(approval.id, approved)
        if not approved:
            job.status = JobStatus.REJECTED_BY_FILTER
            self.repo.update_job(job)
            log_event("browser_apply_rejected", approval_id=approval.id, company=job.company)
            return False
        log_event("browser_apply_approved", approval_id=approval.id, company=job.company)
        return True

    def submit(
        self, job: JobOpportunity, approval: ApprovalRequest, *, confirmed: bool = False
    ) -> Application | None:
        """Preenche e envia. Nunca é chamado sem APPROVED persistido."""
        stored = self.repo.get_approval(approval.id)
        if stored is None:
            raise HumanActionRequired("aprovação", "aprovação não encontrada; rode prepare")
        if stored.status != "APPROVED":
            if confirmed:
                raise HumanActionRequired(
                    "aprovação", "envio confirmado exige status APPROVED persistido"
                )
            if not self.gate.request(stored):
                job.status = JobStatus.REJECTED_BY_FILTER
                self.repo.update_job(job)
                self.repo.resolve_approval(stored.id, False)
                return None
            self.repo.resolve_approval(stored.id, True)

        job.status = JobStatus.APPLYING
        self.repo.update_job(job)

        self._active_job = job
        self._consent_authorized = True
        try:
            page, snapshot, observation = self._open(job)
        except Exception:
            self._consent_authorized = False
            raise
        try:
            self._record_easy_apply(job, observation)
            snapshot = snapshot_form(page)
            snapshot["job_text"] = job.description or ""
            if observation.already_applied or self._detect_already_applied(page, snapshot):
                self._record_existing_application(job, snapshot, observation)
                return None
            if self._one_click_applied(page, snapshot):
                self._record_existing_application(
                    job,
                    snapshot,
                    ApplyObservation(False, "clique_unico_linkedin", True),
                )
                return None
            self._recheck_remote(job, snapshot)

            # Só campos que estavam na aprovação podem ser preenchidos. A
            # assinatura é a chave estável; o índice é recalculado por step.
            permitidos_por_step: dict[int, set[str]] = {}
            for answer in stored.answers:
                permitidos_por_step.setdefault(answer.step or 1, set()).add(answer.field)
            tem_aprovacao = bool(permitidos_por_step)

            evidencias: list[ApplicationEvidence] = []
            preenchidos = 0

            def preencher_e_capturar(
                step: PreparedStep, pagina: Any, _snapshot: dict[str, Any]
            ) -> None:
                nonlocal preenchidos
                permitidas = permitidos_por_step.get(step.number)
                selecionadas = [
                    decision
                    for decision in step.decisions
                    if not tem_aprovacao
                    or permitidas is None
                    or decision.signature in permitidas
                ]
                step.decisions = selecionadas
                for decision in selecionadas:
                    # O React Select re-renderiza e troca os índices depois de
                    # cada escolha. Reatribuir o rótulo e casar pela assinatura
                    # evita escrever no campo errado (ou em índice que sumiu).
                    indice = decision.index
                    try:
                        atual = snapshot_form(pagina)
                        for bruto in atual.get("fields", []):
                            campo = FormField(
                                label=bruto.get("label", ""),
                                field_type=bruto.get("type", "text"),
                                required=bool(bruto.get("required")),
                                options=list(bruto.get("options", [])),
                                autocomplete=bruto.get("autocomplete", ""),
                                identifier=bruto.get("identifier", ""),
                                placeholder=bruto.get("placeholder", ""),
                                nearby_text=bruto.get("nearby_text", ""),
                            )
                            if campo.signature() == decision.signature:
                                indice = int(bruto["index"])
                                break
                    except Exception:
                        pass
                    if fill_field(pagina, indice, decision.kind, decision.value):
                        preenchidos += 1
                    else:
                        # Nunca registra o valor: ele pode ser sensível. A assinatura
                        # identifica o campo sem expor a resposta do operador.
                        log_event(
                            "browser_apply_field_fill_failed",
                            signature=decision.signature,
                            index=decision.index,
                            kind=decision.kind,
                            source=decision.source,
                        )
                self._ensure_not_blocked(pagina)
                metadados = capture_screenshot(
                    pagina,
                    self._evidence_dir(job),
                    step=step.number,
                    kind="step",
                    label=f"Step {step.number} · {len(selecionadas)} campo(s)",
                    image_format=self.settings.application.evidence_format,
                    quality=self.settings.application.evidence_quality,
                )
                evidencias.append(ApplicationEvidence.model_validate(metadados))

            steps = self._walk_steps(page, job, on_step=preencher_e_capturar, allow_submit=True)
            prepared = PreparedForm(
                url=steps[0].url,
                domain=steps[0].domain or domain_of(str(snapshot.get("url", "") or "")),
                decisions=[decision for step in steps for decision in step.decisions],
                steps=steps,
            )
            log_event(
                "browser_apply_filled",
                fields=preenchidos,
                steps=len(steps),
                domain=prepared.domain,
            )

            state = submission_state(page)
            if state == "blocked":
                self._resolve_challenge(page)
                state = submission_state(page)
            if state == "blocked":
                raise BlockedByPlatformError("verificação/CAPTCHA durante a candidatura")
            self._ensure_no_credential_barrier(page)
            if state != "submitted":
                log_event(
                    "browser_apply_submit_readiness",
                    fields=required_field_state(page),
                )
                if not click_submit(page):
                    log_event(
                        "browser_apply_submit_not_found",
                        buttons=visible_buttons(page),
                        step=len(steps),
                    )
                    raise ValidationError("botão de envio não encontrado no formulário")
                page.wait_for_timeout(2500)
                state = submission_state(page)
                if state == "blocked":
                    self._resolve_challenge(page)
                    state = submission_state(page)
                if state == "blocked":
                    raise BlockedByPlatformError("verificação/CAPTCHA durante a candidatura")
                if state != "submitted":
                    # Alguns portais exigem Review antes de Submit.
                    click_submit(page, "Review application")
                    page.wait_for_timeout(1200)
                    click_submit(page)
                    page.wait_for_timeout(2500)
                    state = submission_state(page)
                    if state == "blocked":
                        self._resolve_challenge(page)
                        state = submission_state(page)
                    if state == "blocked":
                        raise BlockedByPlatformError(
                            "verificação/CAPTCHA durante a candidatura"
                        )
            if state != "submitted":
                # Diagnóstico sem PII: só linhas com marcadores de envio e rótulos
                # de botões. Assim sabemos o texto real da confirmação do portal
                # sem registrar e-mail, telefone ou endereço no log.
                try:
                    corpo = str(page.locator("body").inner_text() or "")
                except Exception:
                    corpo = ""
                marcadores = re.compile(
                    r"application|submitted|sent|received|thank|success|complete|"
                    r"candidatura|inscri[cç][aã]o|solicitud|postula|enviado|confirm",
                    re.I,
                )
                trecho = " | ".join(
                    linha.strip()
                    for linha in corpo.splitlines()
                    if marcadores.search(linha)
                )[:700]
                log_event(
                    "browser_apply_submit_unconfirmed",
                    url=str(getattr(page, "url", "") or ""),
                    buttons=visible_buttons(page),
                    markers=trecho,
                    invalid=required_field_state(page),
                )
                job.status = JobStatus.FAILED
                self.repo.update_job(job)
                raise ValidationError("envio não confirmado pelo portal")

            metadados = capture_screenshot(
                page,
                self._evidence_dir(job),
                step=steps[-1].number,
                kind="confirmation",
                label="Confirmação de envio",
                image_format=self.settings.application.evidence_format,
                quality=self.settings.application.evidence_quality,
            )
            evidencias.append(ApplicationEvidence.model_validate(metadados))
            domain = prepared.domain
            self._handle_post_submit_assessment(page, domain)

            application = Application(
                trace_id=job.trace_id,
                opportunity_id=job.id,
                company=job.company,
                title=job.title,
                status="SUBMITTED",
                channel=job.channel,
                resume_path=self._cv_path(),
                portal_url=prepared.url,
                answers=self._answers_from_steps(steps),
                evidence=evidencias,
            )
            self.repo.record_application(application)
            # Limpeza no envio: só evidências ANTIGAS da vaga saem; os prints
            # recém-gravados ficam dentro da janela de 30 dias.
            cleanup_expired(
                self.settings.resolve("data/evidence"),
                retention_days=self.settings.application.evidence_retention_days,
                job_id=job.id,
            )
            job.status = JobStatus.SUBMITTED
            job.has_applied = True
            job.applied_at = application.applied_at or datetime.now()
            self.repo.update_job(job)

            company_id = job.company_id
            if not company_id:
                company = self.repo.ensure_company(job.company)
                company_id = company.id if company else ""
            existing = self.repo.get_company_by_name(job.company)
            self.repo.mark_company_application(
                company_id, created_account=not bool(existing and existing.has_account)
            )

            for decision in prepared.decisions:
                self.patterns.record(
                    prepared.url,
                    decision.signature,
                    decision.selector,
                    decision.source,
                    success=True,
                )
            self._flush_ui_actions(success=True)
            self._pending.pop(job.id, None)
            if self.memory is not None:
                self.memory.record_outcome(job, "SUBMITTED")
            log_event("browser_apply_submitted", company=job.company, title=job.title)
            if self.bus:
                self.bus.emit("application.submitted", company=job.company, title=job.title)
            return application
        except Exception as exc:
            self._flush_ui_actions(success=False)
            if self.memory is not None:
                self.memory.record_outcome(job, "FAILED", str(exc))
            raise
        finally:
            self._consent_authorized = False
            self.browser.close_tab(page)
