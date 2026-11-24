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
from urllib.parse import parse_qs, unquote, urlparse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..core.config import Settings
from ..core.errors import BlockedByPlatformError, HumanActionRequired, ValidationError
from ..core.events import EventBus
from ..core.logging import log_event
from ..core.state import JobStatus
from ..execution.forms.answers import AnswerResolver
from ..execution.forms.field import FormField
from ..execution.web.linkedin_public import is_remote_public
from ..execution.web.form_dom import (
    capture_screenshot,
    click_next,
    click_submit,
    ensure_resume_uploaded,
    fill_field,
    is_experience_step,
    next_step_label,
    remove_extra_experience_entry,
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
_MAX_FORM_STEPS = 8


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


def _value_from_source(profile: CandidateProfile, source: str) -> str:
    """Resolve um caminho aprendido (``profile.personal.email`` etc.)."""
    if not source.startswith("profile."):
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
        self.patterns = SitePatternStore(settings.resolve("data/site_patterns.json"))
        self._pending: dict[str, PreparedForm] = {}
        # Ações de UI observadas nesta execução só viram memória após o envio.
        self._pending_ui_actions: list[dict[str, Any]] = []

    @staticmethod
    def _evidence_dir(job: JobOpportunity) -> Path:
        """Diretório relativo (dentro de data/) das evidências da vaga."""
        seguro = re.sub(r"[^A-Za-z0-9_-]", "_", job.id or "sem-id")[:64]
        return Path("data/evidence") / seguro

    @staticmethod
    def _ensure_not_blocked(page: Any) -> None:
        if submission_state(page) == "blocked":
            raise BlockedByPlatformError(
                "verificação/CAPTCHA no formulário; o agente não interage"
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
            snapshot = snapshot_form(page)
            if not resume_ready:
                # Em candidaturas paginadas o campo de CV pode aparecer só no
                # step 2/3. Tentar em cada step evita reprovar o envio antes da
                # hora; a checagem final barra qualquer envio sem currículo.
                snapshot, status = self._handle_resume(page, snapshot)
                resume_ready = status in {"uploaded", "selected", "already_attached"}
                self._ensure_not_blocked(page)
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
                decisions = self._decide(snapshot)
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

        title = str(snapshot.get("title", "") or "").lower()
        text = str(snapshot.get("text", "") or "").lower()
        closed_marks = (
            "não aceita mais candidaturas",
            "nao aceita mais candidaturas",
            "no longer accepting applications",
        )
        if any(mark in text for mark in closed_marks):
            self.browser.close_tab(page)
            raise ValidationError("a vaga não aceita mais candidaturas no LinkedIn")
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
            if clicked and rota is ApplyRoute.KNOWN_PORTAL:
                page = self._ensure_portal_login(page)
                page = self._advance_external_portal(page)
            if clicked:
                snapshot = snapshot_form(page)
                self._ensure_no_login(page, snapshot)
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

    def _google_oauth_login(self, page: Any, cfg: Any, dominio: str) -> Any:
        """Completa OAuth Google reaproveitando a sessão do Chrome CDP.

        Nunca digita senha nem resolve verificação: se o Google pedir senha,
        CAPTCHA ou outra checagem, o agente para com ação humana.
        """
        contexto = page.context
        clicou = False
        for rotulo in cfg.oauth_labels:
            alvo = page.get_by_text(rotulo, exact=False)
            if alvo.count() == 0:
                alvo = page.get_by_role("button", name=re.compile(re.escape(rotulo), re.I))
            if alvo.count():
                alvo.first.click()
                clicou = True
                page.wait_for_timeout(2500)
                break
        if not clicou:
            raise HumanActionRequired(
                "login", f"botão de login Google não encontrado em {dominio}"
            )

        google = None
        for _ in range(8):
            google = next(
                (p for p in contexto.pages if "accounts.google.com" in p.url), None
            )
            if google is not None:
                break
            page.wait_for_timeout(1000)
        if google is None:
            raise BlockedByPlatformError(
                f"OAuth Google de {dominio} não abriu a seleção de conta"
            )

        try:
            texto = str(google.evaluate("() => document.body.innerText") or "")
        except Exception:
            texto = ""
        baixo = texto.lower()
        if any(
            sinal in baixo
            for sinal in (
                "captcha", "unusual traffic", "verificação", "verificacao",
                "confirm you are human", "2fa",
            )
        ):
            raise BlockedByPlatformError(
                "verificação/CAPTCHA no OAuth Google; agente não interage"
            )

        email = cfg.oauth_account_email
        if email and email.lower() in baixo:
            conta = google.get_by_text(email, exact=False)
            if conta.count():
                conta.first.click()
                google.wait_for_timeout(1500)
                try:
                    texto = str(google.evaluate("() => document.body.innerText") or "")
                except Exception:
                    texto = ""
                if any(sinal in texto.lower() for sinal in ("digite sua senha", "enter your password")):
                    raise HumanActionRequired(
                        "login", "Google pediu senha; agente não digita credencial"
                    )

        for rotulo in cfg.oauth_continue_labels:
            botao = google.get_by_text(rotulo, exact=False)
            if botao.count() == 0:
                botao = google.get_by_role("button", name=re.compile(re.escape(rotulo), re.I))
            if botao.count():
                try:
                    botao.first.click()
                except Exception:
                    continue
                google.wait_for_timeout(4000)
                break

        destino = None
        for _ in range(8):
            for candidata in contexto.pages:
                url = str(candidata.url or "")
                if domain_of(url) == dominio and not _is_login_page(url, ""):
                    destino = candidata
                    break
            if destino is not None:
                break
            page.wait_for_timeout(1000)

        for aba in list(contexto.pages):
            if "accounts.google.com" in str(aba.url or "") and aba is not destino:
                try:
                    self.browser.close_tab(aba)
                except Exception:
                    pass
        if destino is None:
            raise HumanActionRequired(
                "login", f"OAuth Google de {dominio} não retornou ao portal autenticado"
            )
        log_event("portal_google_oauth", domain=dominio, company=cfg.company, account=email)
        return destino

    def _ensure_portal_login(self, page: Any) -> Any:
        """Faz login automático em portal conhecido (OAuth Google ou keyring).

        Nunca resolve CAPTCHA/MFA/verificação: se a plataforma pedir isso, a
        barreira abaixo interrompe e sinaliza ação humana.
        """
        snapshot = snapshot_form(page)
        url = str(snapshot.get("url", "") or page.url)
        if not _is_login_page(url, str(snapshot.get("text", "") or "")):
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
        if _is_login_page(url, texto):
            sinais_verificacao = (
                "captcha", "just a moment", "verificação", "verificacao",
                "verificacion", "confirm you are human", "mfa", "2fa",
            )
            if any(sinal in texto for sinal in sinais_verificacao):
                raise BlockedByPlatformError(
                    f"verificação/CAPTCHA no login de {dominio}; agente não interage"
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
        page_text = str(snapshot.get("text", "") or "")
        remote_ok = (
            analysis_remote is True
            or is_remote_location(job.location, job.title)
            or is_remote_public(page_text)
        )
        if not remote_ok:
            raise ValidationError("remoto não confirmado na rechecagem pré-envio")

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
        self._pending_ui_actions.append(dict(info))

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

    def _handle_resume(
        self, page: Any, snapshot: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Tenta anexar o CV no step atual e devolve ``(snapshot, status)``.

        ``upload_button_not_found`` é provisório: em formulários paginados o
        controle de upload aparece em steps posteriores. A checagem final em
        ``_walk_steps`` impede o envio caso nenhum step tenha anexado o CV.
        """
        cv_path = self._cv_path()
        status = ensure_resume_uploaded(page, cv_path)
        log_event(
            "browser_apply_resume",
            status=status,
            filename=Path(cv_path).name if cv_path else "",
        )
        if status == "blocked":
            raise BlockedByPlatformError("verificação/CAPTCHA ao anexar currículo")
        if status == "missing":
            raise ValidationError("currículo informado não existe")
        invalid = {"select_failed", "file_input_not_found", "unverified"}
        if status in invalid:
            raise ValidationError(f"upload do currículo não confirmado: {status}")
        if status in {"uploaded", "selected"}:
            # O upload insere um novo radio: os índices do snapshot mudam.
            page.wait_for_timeout(1200)
            return snapshot_form(page), status
        return snapshot, status

    def _decide(self, snapshot: dict[str, Any]) -> list[FillDecision]:
        url = snapshot.get("url", "")
        decisions: list[FillDecision] = []
        for raw in snapshot.get("fields", []):
            field = FormField(
                label=raw.get("label", ""),
                field_type=raw.get("type", "text"),
                required=bool(raw.get("required")),
                options=list(raw.get("options", [])),
                autocomplete=raw.get("autocomplete", ""),
                identifier=raw.get("identifier", ""),
                placeholder=raw.get("placeholder", ""),
                nearby_text=raw.get("nearby_text", ""),
            )
            signature = field.signature()
            learned = self.patterns.suggest(url, signature)
            value = ""
            source = ""
            needs_human = False
            category = "general"

            if learned and learned.get("source"):
                value = _value_from_source(self.profile, str(learned["source"]))
                source = f"learned:{learned['source']}"
                needs_human = not value
            else:
                decision = self.resolver.resolve(field)
                value = decision.value
                source = decision.source
                needs_human = decision.needs_human
                category = decision.reason if hasattr(decision, "reason") else "general"
                category = "sensitive" if needs_human and "sens" in str(category).lower() else category

            if needs_human or not value:
                if not field.required:
                    log_event("browser_apply_optional_field_skipped", signature=signature)
                    continue
                raise HumanActionRequired(
                    "campo de formulário", f"{field.label or signature} não tem evidência no perfil"
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
        page, snapshot, observation = self._open(job)
        try:
            self._record_easy_apply(job, observation)
            snapshot = snapshot_form(page)
            if observation.already_applied or self._detect_already_applied(page, snapshot):
                self._record_existing_application(job, snapshot, observation)
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

        page, snapshot, observation = self._open(job)
        try:
            self._record_easy_apply(job, observation)
            snapshot = snapshot_form(page)
            if observation.already_applied or self._detect_already_applied(page, snapshot):
                self._record_existing_application(job, snapshot, observation)
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
                    if fill_field(pagina, decision.index, decision.kind, decision.value):
                        preenchidos += 1
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
                raise BlockedByPlatformError("verificação/CAPTCHA durante a candidatura")
            if state != "submitted":
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
                    raise BlockedByPlatformError("verificação/CAPTCHA durante a candidatura")
                if state != "submitted":
                    # Alguns portais exigem Review antes de Submit.
                    click_submit(page, "Review application")
                    page.wait_for_timeout(1200)
                    click_submit(page)
                    page.wait_for_timeout(2500)
                    state = submission_state(page)
                    if state == "blocked":
                        raise BlockedByPlatformError(
                            "verificação/CAPTCHA durante a candidatura"
                        )
            if state != "submitted":
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
            self.browser.close_tab(page)
