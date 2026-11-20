"""Autenticação automatizada de portais, OAuth Google e candidaturas existentes."""
from __future__ import annotations

import sys

import pytest

from src.agents.browser_apply import ApplyObservation, BrowserApplyAgent
from src.core.config import PortalAuthConfig, Settings
from src.core.errors import BlockedByPlatformError, HumanActionRequired
from src.core.state import JobStatus
from src.execution.web.portal_credentials import PortalCredentialStore
from src.models.domain import Application, CandidateProfile, JobOpportunity

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
    return agent


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
