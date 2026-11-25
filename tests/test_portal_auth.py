"""Autenticação automatizada de portais, OAuth Google e candidaturas existentes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.agents.browser_apply import ApplyObservation, BrowserApplyAgent
from src.core.config import PortalAuthConfig, Settings
from src.core.errors import BlockedByPlatformError, HumanActionRequired
from src.core.state import JobStatus
from src.execution.forms.answers import AnswerResolver
from src.execution.web.portal_credentials import PortalCredentialStore
from src.models.domain import AnswerRule, Application, CandidateProfile, JobOpportunity
from src.models.schemas import ConsentDecision

PASSWORD_DOMAIN = "portal.example.com"
PORTAL_DOMAIN = "applicants.bairesdev.com"


class FakeKeyring:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


class FakeField:
    def __init__(self) -> None:
        self.value = ""

    def fill(self, value: str) -> None:
        self.value = value


class FakeClickable:
    def __init__(self, callback=None) -> None:
        self.clicked = False
        self.callback = callback

    def click(self) -> None:
        self.clicked = True
        if self.callback is not None:
            self.callback()


class FakeLocator:
    def __init__(self, target: FakeField | FakeClickable, count: int = 1) -> None:
        self.first = target
        self.count_value = count

    def count(self) -> int:
        return self.count_value

    def nth(self, _index: int) -> FakeLocator:
        return self

    def click(self) -> None:
        self.first.click()


class FakeZeroLocator:
    def count(self) -> int:
        return 0


class FakeLoginPage:
    def __init__(self, domain: str = PASSWORD_DOMAIN) -> None:
        self.user = FakeField()
        self.password = FakeField()
        self.button = FakeClickable()
        self.url = f"https://{domain}/login?careerId=453"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.user if "username" in selector else self.password)

    def get_by_role(self, *_args, **_kwargs) -> FakeLocator:
        return FakeLocator(self.button)

    def get_by_text(self, *_args, **_kwargs) -> FakeLocator:
        return FakeLocator(self.button)

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str) -> str:
        return "Usuario Código de acceso Ingresar"


class FakeCredentials:
    def __init__(self, value: tuple[str, str] | None) -> None:
        self.value = value

    def get(self, _domain: str) -> tuple[str, str] | None:
        return self.value


class FakeResultPage:
    def __init__(self, text: str) -> None:
        self.text = text

    def evaluate(self, _script: str) -> str:
        return self.text


class FakeContext:
    def __init__(self, pages: list) -> None:
        self.pages = pages


class FakeGooglePage:
    """Seletor de conta do Google com o fluxo observado no CDP."""

    def __init__(self, portal_page, text: str = "Escolha uma conta addamreis@gmail.com") -> None:
        self.url = "https://accounts.google.com/v3/signin/accountchooser?client_id=123"
        self.text = text
        self.portal_page = portal_page
        self.account = FakeClickable()
        self.continue_button = FakeClickable(self._finish)

    def _finish(self) -> None:
        self.portal_page.url = "https://applicants.bairesdev.com/home?jobId=298508&lang=es"
        self.url = "about:blank"

    def evaluate(self, _script: str) -> str:
        return self.text

    def get_by_text(self, rotulo: str, exact: bool = False):
        if "addamreis@gmail.com" in rotulo:
            return FakeLocator(self.account)
        if rotulo in ("Continuar", "Prosseguir para Applicant Site"):
            return FakeLocator(self.continue_button)
        return FakeZeroLocator()

    def get_by_role(self, *_args, **_kwargs):
        return FakeZeroLocator()

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class FakeRepo:
    def __init__(self) -> None:
        self.applications: list[Application] = []
        self.updated: list[JobOpportunity] = []

    def record_application(self, application: Application) -> None:
        self.applications.append(application)

    def update_job(self, job: JobOpportunity) -> None:
        self.updated.append(job)


class FakeConsentRouter:
    """DGX falso: aceita apenas o que passa pela barreira do agente."""

    def structured(self, _messages, schema):
        assert schema is ConsentDecision
        return ConsentDecision(allow=True, source="dgx", reason="aceite operacional")


class FakePatternStore:
    def suggest_action(self, *_args, **_kwargs):
        return None

    def record_action(self, *_args, **_kwargs):
        return None


def _agent(
    monkeypatch: pytest.MonkeyPatch,
    creds: tuple[str, str] | None = None,
    *,
    domain: str = PASSWORD_DOMAIN,
) -> BrowserApplyAgent:
    agent = object.__new__(BrowserApplyAgent)
    settings = Settings()
    if domain == PASSWORD_DOMAIN:
        settings.application.portal_auth[domain] = PortalAuthConfig(
            company="Portal de teste",
            login_url=f"https://{domain}/login",
            auth_mode="password",
        )
    agent.settings = settings
    agent.portal_credentials = FakeCredentials(creds)
    agent.bus = None
    agent.repo = FakeRepo()
    agent.profile = CandidateProfile()
    agent.resolver = AnswerResolver(CandidateProfile(), FakeConsentRouter(), settings)
    agent.patterns = FakePatternStore()
    return agent


def test_consent_with_explicit_human_evidence_is_allowed() -> None:
    settings = Settings()
    perfil = CandidateProfile(
        answer_rules=[
            AnswerRule(
                match=r"terms of use|termos de uso",
                value=True,
                category="human_evidence",
            )
        ]
    )
    resolver = AnswerResolver(perfil, FakeConsentRouter(), settings)

    decisao = resolver.interpret_consent(
        "I accept the terms of use", "Aceite encontrado na landing"
    )

    assert decisao.allow is True
    assert decisao.needs_human is False
    assert decisao.source == "profile.human_evidence"


def test_credentials_roundtrip_in_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
    store = PortalCredentialStore()

    assert store.has(PORTAL_DOMAIN) is False
    store.set(PORTAL_DOMAIN, "usuario", "senha")
    assert store.get(PORTAL_DOMAIN) == ("usuario", "senha")
    assert store.has(PORTAL_DOMAIN) is True
    store.delete(PORTAL_DOMAIN)
    assert store.get(PORTAL_DOMAIN) is None


def test_bairesdev_config_uses_google_oauth_and_completed_assessment() -> None:
    cfg = Settings().application.portal_auth[PORTAL_DOMAIN]

    assert cfg.company == "BairesDev"
    assert cfg.login_url == "https://applicants.bairesdev.com/login"
    assert cfg.auth_mode == "google"
    assert cfg.oauth_account_email == "addamreis@gmail.com"
    assert "Ver solicitud" in cfg.already_applied_markers
    assert cfg.assessment_status == "completed"


def test_password_login_fills_credentials_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, ("usuario", "senha"))
    page = FakeLoginPage()
    snapshots = [
        {"url": page.url, "text": "Usuario Código de acceso Ingresar"},
        {"url": f"https://{PASSWORD_DOMAIN}/job/1/apply", "text": "Descripción"},
    ]
    monkeypatch.setattr("src.agents.browser_apply.snapshot_form", lambda _page: snapshots.pop(0))

    agent._ensure_portal_login(page)

    assert page.user.value == "usuario"
    assert page.password.value == "senha"
    assert page.button.clicked is True
    assert ("portal_login_automated", {"domain": PASSWORD_DOMAIN, "company": "Portal de teste"}) in eventos


def test_password_login_stops_on_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(monkeypatch, ("usuario", "senha"))
    page = FakeLoginPage()
    snapshots = [
        {"url": page.url, "text": "Usuario Código de acceso Ingresar"},
        {"url": page.url, "text": "Just a moment... CAPTCHA verificação"},
        {"url": page.url, "text": "Just a moment... CAPTCHA verificação"},
    ]
    monkeypatch.setattr("src.agents.browser_apply.snapshot_form", lambda _page: snapshots.pop(0))

    with pytest.raises(BlockedByPlatformError, match="verificação"):
        agent._ensure_portal_login(page)


def test_google_oauth_selects_account_and_returns_to_portal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, domain=PORTAL_DOMAIN)
    agent.browser = type("FakeBrowser", (), {"close_tab": lambda self, _p: None})()
    login = FakeLoginPage(PORTAL_DOMAIN)
    login.oauth = FakeClickable()
    login.get_by_text = lambda rotulo, exact=False: FakeLocator(login.oauth)
    google = FakeGooglePage(login)
    login.context = FakeContext([login, google])
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form",
        lambda _page: {"url": login.url, "text": "Ingresa con Google"},
    )

    resultado = agent._ensure_portal_login(login)

    assert login.oauth.clicked is True
    assert google.account.clicked is True
    assert google.continue_button.clicked is True
    assert resultado is login
    assert ("portal_google_oauth", {
        "domain": PORTAL_DOMAIN,
        "company": "BairesDev",
        "account": "addamreis@gmail.com",
    }) in eventos


def test_google_oauth_stops_on_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(monkeypatch, domain=PORTAL_DOMAIN)
    agent.browser = type("FakeBrowser", (), {"close_tab": lambda self, _p: None})()
    login = FakeLoginPage(PORTAL_DOMAIN)
    login.oauth = FakeClickable()
    login.get_by_text = lambda rotulo, exact=False: FakeLocator(login.oauth)
    google = FakeGooglePage(login, text="CAPTCHA - confirme que você é humano")
    login.context = FakeContext([login, google])
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form",
        lambda _page: {"url": login.url, "text": "Ingresa con Google"},
    )

    with pytest.raises(BlockedByPlatformError, match="verificação"):
        agent._ensure_portal_login(login)


def test_already_applied_marker_is_detected_on_portal() -> None:
    agent = _agent(None, domain=PORTAL_DOMAIN)
    page = FakeResultPage("Ver solicitud Referir Feedback")
    snapshot = {
        "url": "https://applicants.bairesdev.com/job/453/298508/apply",
        "text": "Ver solicitud Referir Feedback",
    }

    assert agent._detect_already_applied(page, snapshot) is True
    sem_sinal = FakeResultPage("Descripción")
    assert agent._detect_already_applied(
        sem_sinal, {"url": snapshot["url"], "text": "Descripción"}
    ) is False


ALIGNERR_DOMAIN = "app.alignerr.com"
ALIGNERR_TERMS = (
    "I agree to the Terms of Service, Privacy Policy and Release Agreement"
)
ALIGNERR_CONSENT = "I agree to Candidate Terms"


def test_alignerr_config_has_no_fixed_terms_rule() -> None:
    cfg = Settings().application.portal_auth[ALIGNERR_DOMAIN]

    assert cfg.company == "Alignerr"
    assert cfg.auth_mode == "google"
    assert cfg.oauth_account_email == "addamreis@gmail.com"
    # Aceites são conhecimento do agente, não regra por empresa.
    assert getattr(cfg, "terms_checkbox_required", None) is None
    assert getattr(cfg, "terms_checkbox_labels", None) is None
    assert "Continue with Google" in cfg.oauth_labels
    assert "Continue with LinkedIn" in cfg.oauth_fallback_labels


def test_agent_accepts_operational_consent_before_google_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, domain=ALIGNERR_DOMAIN)
    chamadas: list[tuple[str, str, bool]] = []

    def falso_oauth(page, cfg, dominio):
        chamadas.append((dominio, cfg.company, page.terms_checked))
        page.url = "https://app.alignerr.com/home?job=fd47"
        page.autenticado = True
        return page

    monkeypatch.setattr(
        BrowserApplyAgent,
        "_google_oauth_login",
        lambda self, page, cfg, dominio: falso_oauth(page, cfg, dominio),
    )

    class Page:
        def __init__(self) -> None:
            self.url = "https://app.alignerr.com/signin?job=fd47"
            self.context = FakeContext([self])
            self.terms_checked = False
            self.autenticado = False
            self.term_checkbox = FakeClickable(self._aceitar)
            self.oauth_button = FakeClickable()

        def _aceitar(self) -> None:
            self.terms_checked = True

        def evaluate(self, script, *_args):
            if "has_form" in script:
                return {
                    "url": self.url,
                    "text": ALIGNERR_CONSENT if not self.terms_checked else "Welcome",
                    "buttons": [{"text": "Continue with Google", "href": ""}],
                    "checkboxes": [
                        {"text": ALIGNERR_CONSENT, "checked": self.terms_checked}
                    ],
                    "has_form": self.autenticado,
                    "has_file": False,
                    "blocked": False,
                    "passwords": 0,
                }
            return {}

        def get_by_text(self, rotulo, exact=False):
            if "terms" in rotulo.lower():
                return FakeLocator(self.term_checkbox)
            if "Google" in rotulo:
                return FakeLocator(self.oauth_button)
            return FakeZeroLocator()

        def get_by_role(self, *_args, **_kwargs):
            return FakeZeroLocator()

        def wait_for_timeout(self, _ms) -> None:
            return None

    page = Page()
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form",
        lambda p: {
            "url": p.url,
            "text": ALIGNERR_CONSENT if not p.terms_checked else "Welcome",
        },
    )

    resultado, _ = agent._ensure_external_entry(page)

    assert resultado is page
    assert page.terms_checked is True
    assert chamadas == [(ALIGNERR_DOMAIN, "Alignerr", True)]
    assert any(event == "portal_terms_accepted" for event, _ in eventos)


def test_agent_stops_on_legal_terms_without_human_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch, domain=ALIGNERR_DOMAIN)

    class Page:
        url = "https://app.alignerr.com/signin?job=fd47"

        def evaluate(self, script, *_args):
            if "has_form" in script:
                return {
                    "url": self.url,
                    "text": ALIGNERR_TERMS,
                    "buttons": [],
                    "checkboxes": [{"text": ALIGNERR_TERMS, "checked": False}],
                    "has_form": False,
                    "has_file": False,
                    "blocked": False,
                    "passwords": 0,
                }
            return {}

        def wait_for_timeout(self, _ms) -> None:
            return None

    with pytest.raises(HumanActionRequired, match="termos"):
        agent._ensure_external_entry(Page())


def test_google_oauth_stops_when_password_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch, domain=PORTAL_DOMAIN)
    agent.browser = type("FakeBrowser", (), {"close_tab": lambda self, _p: None})()
    login = FakeLoginPage(PORTAL_DOMAIN)
    login.oauth = FakeClickable()
    login.get_by_text = lambda rotulo, exact=False: FakeLocator(login.oauth)
    google = FakeGooglePage(
        login,
        text="Escolha uma conta addamreis@gmail.com. Digite sua senha para continuar.",
    )
    login.context = FakeContext([login, google])
    cfg = agent.settings.application.portal_auth[PORTAL_DOMAIN]

    with pytest.raises(HumanActionRequired, match="senha"):
        agent._google_oauth_login(login, cfg, PORTAL_DOMAIN)


def test_google_spanish_password_challenge_stops_agent() -> None:
    class ChallengePage:
        url = "https://accounts.google.com/v3/signin/challenge/pk?TL=123"

    class TextPage:
        url = "https://accounts.google.com/v3/signin/accountchooser"

    with pytest.raises(HumanActionRequired, match="senha"):
        BrowserApplyAgent._block_google_challenge(
            ChallengePage(), "Selecciona una cuenta"
        )
    with pytest.raises(HumanActionRequired, match="senha"):
        BrowserApplyAgent._block_google_challenge(
            TextPage(), "Ingresa tu contraseña para continuar"
        )


class FakeFallbackPortal:
    """Portal Alignerr: botão Google abre desafio; LinkedIn retorna autenticado."""

    def __init__(self) -> None:
        self.url = "https://app.alignerr.com/signin?job=fd47"
        self.google = FakeClickable()
        self.linkedin = FakeClickable(self._autenticar)
        self.context: FakeContext | None = None

    def _autenticar(self) -> None:
        self.url = "https://app.alignerr.com/home?job=fd47"

    def get_by_text(self, rotulo, exact=False):
        if "Google" in rotulo:
            return FakeLocator(self.google)
        if "LinkedIn" in rotulo:
            return FakeLocator(self.linkedin)
        return FakeZeroLocator()

    def get_by_role(self, *_args, **_kwargs):
        return FakeZeroLocator()

    def wait_for_timeout(self, _ms) -> None:
        return None


class FakeLinkedInOauthPage:
    def __init__(self, portal: FakeFallbackPortal) -> None:
        self.url = "https://www.linkedin.com/oauth/v2/authorization?client_id=1"
        self.portal = portal
        self.allow = FakeClickable(self._autorizar)

    def _autorizar(self) -> None:
        self.portal._autenticar()
        self.url = "about:blank"

    def evaluate(self, _script: str) -> str:
        return "Allow"

    def get_by_text(self, rotulo, exact=False):
        if rotulo in BrowserApplyAgent._OAUTH_FALLBACK_CONTINUE_LABELS:
            return FakeLocator(self.allow)
        return FakeZeroLocator()

    def get_by_role(self, *_args, **_kwargs):
        return FakeZeroLocator()

    def wait_for_timeout(self, _ms) -> None:
        return None


def test_google_password_challenge_uses_linkedin_fallback_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, domain=ALIGNERR_DOMAIN)
    fechadas: list[object] = []
    agent.browser = type(
        "FakeBrowser", (), {"close_tab": lambda self, page: fechadas.append(page)}
    )()

    portal = FakeFallbackPortal()
    google = FakeGooglePage(
        portal, text="Selecciona una cuenta. Ingresa tu contraseña para continuar."
    )
    linkedin = FakeLinkedInOauthPage(portal)
    portal.context = FakeContext([portal, google, linkedin])

    def landing(page):
        if "/home" in str(page.url or ""):
            return {
                "url": page.url,
                "text": "Welcome",
                "checkboxes": [],
                "passwords": 0,
                "oauth_google": False,
                "oauth_linkedin": False,
                "has_form": True,
                "blocked": False,
            }
        return {
            "url": page.url,
            "text": ALIGNERR_TERMS + " Continue with Google Continue with LinkedIn",
            "checkboxes": [
                {"text": ALIGNERR_TERMS, "checked": True, "frame_index": 0}
            ],
            "passwords": 0,
            "oauth_google": True,
            "oauth_linkedin": True,
            "has_form": False,
            "blocked": False,
        }

    monkeypatch.setattr(agent, "_landing_state", landing)
    cfg = agent.settings.application.portal_auth[ALIGNERR_DOMAIN]

    resultado = agent._google_oauth_login(portal, cfg, ALIGNERR_DOMAIN)

    assert resultado is portal
    assert google in fechadas
    assert linkedin.allow.clicked is True
    nomes = [evento for evento, _ in eventos]
    assert "portal_google_password_fallback" in nomes
    assert "portal_oauth_fallback_started" in nomes
    assert "portal_linkedin_oauth" in nomes


def test_linkedin_challenge_stops_fallback_without_typing() -> None:
    class LoginPage:
        url = "https://www.linkedin.com/u/login/identifier?state=abc"

    class CheckpointPage:
        url = "https://www.linkedin.com/oauth/v2/authorization"

    with pytest.raises(HumanActionRequired, match="LinkedIn"):
        BrowserApplyAgent._block_linkedin_challenge(LoginPage(), "Sign in")
    with pytest.raises(HumanActionRequired, match="LinkedIn"):
        BrowserApplyAgent._block_linkedin_challenge(
            CheckpointPage(), "Introduce tu contraseña"
        )


def test_existing_application_is_recorded_without_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, domain=PORTAL_DOMAIN)
    job = JobOpportunity(
        external_id="4492155861",
        status=JobStatus.QUALIFIED,
        title="Senior .NET Developer",
        company="BairesDev",
    )
    snapshot = {
        "url": "https://applicants.bairesdev.com/job/453/298508/apply",
        "text": "Ver solicitud Referir Feedback",
    }

    application = agent._record_existing_application(
        job, snapshot, ApplyObservation(False, "Candidatar-se", already_applied=True)
    )

    assert application.status == "SUBMITTED"
    assert "Ver solicitud" in application.success_feedback
    assert job.status is JobStatus.SUBMITTED
    assert job.has_applied is True
    assert agent.repo.applications == [application]
    assert ("browser_apply_already_applied", {
        "external_id": "4492155861",
        "company": "BairesDev",
        "title": "Senior .NET Developer",
        "domain": PORTAL_DOMAIN,
        "evidence": "Ver solicitud",
        "label": "Candidatar-se",
    }) in eventos


def test_internal_assessment_is_not_confused_with_application_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eventos: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "src.agents.browser_apply.log_event",
        lambda event, **context: eventos.append((event, context)),
    )
    agent = _agent(monkeypatch, domain=PORTAL_DOMAIN)
    page = FakeResultPage("Candidatura enviada. Test: evaluación interna de la plataforma")

    agent._handle_post_submit_assessment(page, PORTAL_DOMAIN)

    nomes = [evento for evento, _ in eventos]
    assert "portal_assessment_detected" in nomes
    assert "portal_assessment_already_completed" in nomes
    assert "portal_assessment_pending" not in nomes


def test_machine_pin_stays_only_in_local_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.execution.web.machine_pin import MachinePinStore

    guardado: dict[tuple[str, str], str] = {}

    class FakeKeyringModule:
        @staticmethod
        def set_password(service: str, username: str, value: str) -> None:
            guardado[(service, username)] = value

        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            return guardado.get((service, username))

        @staticmethod
        def delete_password(service: str, username: str) -> None:
            guardado.pop((service, username), None)

    monkeypatch.setattr(
        MachinePinStore, "_keyring", staticmethod(lambda: FakeKeyringModule)
    )
    store = MachinePinStore()

    assert store.has() is False
    store.set(" 78451200 ")
    assert store.has() is True
    assert store.get() == "78451200"
    assert guardado == {(MachinePinStore.SERVICE, MachinePinStore.USERNAME): "78451200"}
    assert "78451200" not in Path("config/config.yaml").read_text(encoding="utf-8")

    store.delete()
    assert store.has() is False

