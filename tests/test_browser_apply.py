"""Agente de candidatura no Chrome logado: URL real, aprendizado local e gate."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.browser_apply import (
    ApplyObservation,
    ApplyRoute,
    BrowserApplyAgent,
    _value_from_source,
)
from src.core.config import Settings
from src.core.errors import HumanActionRequired, ValidationError
from src.core.state import JobStatus
from src.execution.forms.answers import AnswerResolver
from src.execution.forms.field import FormField
from src.execution.web.form_dom import ensure_resume_uploaded, fill_field
from src.execution.web.site_patterns import SitePatternStore
from src.models.domain import ApprovalRequest, CandidateProfile, JobAnalysis, JobOpportunity
from src.models.schemas import AnswerDecision, UiActionDecision


class FakeResolver:
    def resolve(self, field):
        return AnswerDecision(needs_human=True, source="sem_evidencia")


class FakeGate:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.calls = 0

    def request(self, approval) -> bool:
        self.calls += 1
        return self.approved


class FakeRepo:
    def __init__(self, approval: ApprovalRequest | None = None) -> None:
        self.approval = approval
        self.resolutions: list[tuple[str, bool]] = []
        self.updated: list[JobStatus] = []

    def get_approval(self, approval_id: str):
        return self.approval

    def resolve_approval(self, approval_id: str, approved: bool):
        self.resolutions.append((approval_id, approved))
        if self.approval is not None:
            self.approval.status = "APPROVED" if approved else "REJECTED"
        return self.approval

    def normalize_company(self, name):
        return (name or "").lower().replace(" ", "").replace(".", "").replace("llc", "")

    def update_job(self, job):
        self.updated.append(job.status)


class FakeLocator:
    def __init__(self, href: str) -> None:
        self.href = href
        self.clicked = False

    def evaluate(self, _script: str) -> str:
        return self.href

    def click(self) -> None:
        self.clicked = True


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.context = SimpleNamespace(pages=[self])

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str, *_args):
        return ""


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed: list[FakePage] = []

    def open_tab(self, _url: str) -> FakePage:
        return self.page

    def close_tab(self, page: FakePage) -> None:
        self.closed.append(page)


def _route_agent() -> BrowserApplyAgent:
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo()
    agent.settings = Settings()
    return agent


def test_easy_apply_is_recorded_after_classification() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo()
    agent.settings = Settings()
    job = JobOpportunity(
        external_id="123456789", status=JobStatus.QUALIFIED, title="Engineer", company="Acme"
    )

    agent._record_easy_apply(job, ApplyObservation(True, "Easy Apply to this job"))

    assert job.easy_apply is True
    assert job.easy_apply_source == "browser_apply"
    assert job.channel == "easy_apply"


def test_external_apply_is_recorded_without_easy_apply() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo()
    agent.settings = Settings()
    job = JobOpportunity(
        external_id="123456789",
        status=JobStatus.QUALIFIED,
        title="Engineer",
        company="Acme",
        apply_marker="Apply on company website",
    )

    agent._record_easy_apply(job, ApplyObservation(False, "Apply on company website"))

    assert job.easy_apply is False
    assert job.easy_apply_source == "browser_apply"
    assert job.channel == "external_portal"


def test_route_easy_apply_uses_the_simplified_flow() -> None:
    agent = _route_agent()
    job = JobOpportunity(external_id="123456789", title="Engineer", company="Acme")

    rota = agent._decide_route(job, "Candidatura simplificada", "")

    assert rota is ApplyRoute.EASY_APPLY


def test_route_bairesdev_redirect_is_known_portal() -> None:
    agent = _route_agent()
    job = JobOpportunity(external_id="123456789", title="Engineer", company="BairesDev")
    href = (
        "https://www.linkedin.com/safety/go/?url="
        "https%3A%2F%2Fapplicants.bairesdev.com%2Fjob%2F453%2F298508%2Fapply%3Flang%3Den"
    )

    destino = BrowserApplyAgent._cta_destination(FakeLocator(href))

    assert destino == "https://applicants.bairesdev.com/job/453/298508/apply?lang=en"
    assert agent._decide_route(job, "Candidatar-se", destino) is ApplyRoute.KNOWN_PORTAL


def test_route_unknown_external_portal_requires_signup() -> None:
    agent = _route_agent()
    job = JobOpportunity(external_id="123456789", title="Engineer", company="Acme")

    rota = agent._decide_route(job, "Candidatar-se", "https://example.com/apply")

    assert rota is ApplyRoute.EXTERNAL_SIGNUP


def test_external_signup_opens_form_and_waits_for_login_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _route_agent()
    job = JobOpportunity(
        external_id="123456789", status=JobStatus.QUALIFIED, title="Engineer", company="Acme"
    )
    page = FakePage("https://www.linkedin.com/jobs/view/123456789")
    agent.browser = FakeBrowser(page)
    locator = FakeLocator("https://example.com/apply")
    snapshots = iter(
        [
            {"url": page.url, "title": "Engineer | LinkedIn", "text": "Candidatar-se"},
            {
                "url": "https://example.com/apply",
                "title": "Apply",
                "text": "Preencha seus dados",
                "fields": [],
            },
        ]
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form", lambda _page: next(snapshots)
    )
    monkeypatch.setattr(
        BrowserApplyAgent,
        "_apply_candidates",
        staticmethod(lambda _page: [(100, "Candidatar-se", "text", locator)]),
    )

    opened, snapshot, observation = agent._open(job)

    assert opened is page
    assert locator.clicked is True
    assert observation.easy_apply is False
    assert snapshot["url"] == "https://example.com/apply"
    assert agent.browser.closed == []


def test_easy_apply_is_not_recorded_for_rejected_job() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo()
    agent.settings = Settings()
    job = JobOpportunity(external_id="123456789", status=JobStatus.REJECTED_BY_FILTER)

    agent._record_easy_apply(job, ApplyObservation(True, "Easy Apply"))

    assert job.easy_apply is None
    assert job.easy_apply_source == ""
    assert agent.repo.updated == []


def test_prepare_requires_qualified_status() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo()
    job = JobOpportunity(external_id="123456789", status=JobStatus.REJECTED_BY_FILTER)

    with pytest.raises(ValidationError, match="QUALIFIED"):
        agent.prepare(job)


def test_job_url_uses_real_linkedin_id() -> None:
    job = JobOpportunity(external_id="1234567890", url="https://www.linkedin.com/jobs/view/0")
    assert BrowserApplyAgent.job_url(job) == "https://www.linkedin.com/jobs/view/1234567890"


def test_job_url_rejects_synthetic_identifier() -> None:
    with pytest.raises(ValidationError, match="ID real"):
        BrowserApplyAgent.job_url(JobOpportunity(external_id="abc", url=""))


def test_value_from_source_resolves_dict_keys() -> None:
    profile = CandidateProfile(documents={"resume": "C:/cv/addam.pdf"})
    assert _value_from_source(profile, "profile.documents.resume") == "C:/cv/addam.pdf"


def test_field_signature_drops_volatile_ids() -> None:
    assert FormField(label="Email*", identifier="_r_1g_").signature() == "email"
    assert FormField(label="Addam dos Reis cv.pdf", identifier="_r_1p_").signature() == "resume"


def test_site_pattern_store_keeps_only_selector_and_source(tmp_path) -> None:
    store = SitePatternStore(tmp_path / "site_patterns.json")
    url = "https://boards.greenhouse.io/acme/jobs/42"
    store.record(url, "email", "#candidate-email", "profile.personal.email")
    learned = store.suggest(url, "email")
    assert learned is not None
    assert learned["source"] == "profile.personal.email"
    assert learned["selector"] == "#candidate-email"
    assert learned["successes"] == 1
    assert "pessoal" not in store.path.read_text(encoding="utf-8").lower()


def test_site_pattern_store_learns_ui_actions(tmp_path) -> None:
    store = SitePatternStore(tmp_path / "site_patterns.json")
    url = "https://www.linkedin.com/jobs/view/4492884477/"
    store.record_action(
        url,
        "confirm_remove_extra_experience",
        "Excluir experiência",
        "Remover",
        "remover cancelar tem certeza que deseja excluir",
        success=True,
        source="learned",
        reason="confirmou a exclusão",
    )
    store.record_action(
        url,
        "confirm_remove_extra_experience",
        "Excluir experiência",
        "Remover",
        success=False,
    )
    learned = store.suggest_action(
        url, "confirm_remove_extra_experience", "Excluir experiência"
    )
    assert learned is not None
    assert learned["action"] == "Remover"
    assert learned["successes"] == 1
    assert learned["failures"] == 1
    assert "Suporte TI" not in store.path.read_text(encoding="utf-8")


class FakeLLMRouter:
    def __init__(self, decision: UiActionDecision) -> None:
        self.decision = decision
        self.calls = 0

    def structured(self, messages, schema):
        self.calls += 1
        assert schema is UiActionDecision
        return self.decision


def test_ui_action_interpretation_validates_visible_buttons(tmp_path) -> None:
    settings = Settings(root=tmp_path)
    router = FakeLLMRouter(
        UiActionDecision(
            action="Remover", source="dgx", reason="confirma exclusão da experiência extra"
        )
    )
    resolver = AnswerResolver(CandidateProfile(), router, settings)

    decision = resolver.interpret_action(
        "confirm_remove_extra_experience",
        "Tem certeza de que deseja excluir?",
        ["Remover", "Cancelar"],
    )

    assert router.calls == 1
    assert decision.action == "Remover"
    assert not decision.needs_human

    router = FakeLLMRouter(UiActionDecision(action="Enviar candidatura"))
    resolver = AnswerResolver(CandidateProfile(), router, settings)
    invalid = resolver.interpret_action(
        "confirm_remove_extra_experience", "Tem certeza?", ["Remover", "Cancelar"]
    )
    assert invalid.needs_human is True


def test_ensure_resume_uploaded_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(ValidationError, match="currículo informado não existe"):
        ensure_resume_uploaded(object(), tmp_path / "inexistente.pdf")


def test_ensure_resume_uploaded_skips_attached_resume(tmp_path) -> None:
    cv = tmp_path / "cv-internacional.pdf"
    cv.write_bytes(b"%PDF-1.4")

    class Page:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, expression, name=None):
            self.calls += 1
            return "radio.checked" in expression

        def get_by_role(self, *args, **kwargs):
            raise AssertionError("não deve clicar em upload quando já está anexado")

        def locator(self, *args, **kwargs):
            raise AssertionError("não deve procurar input[type=file] quando já está anexado")

    page = Page()
    assert ensure_resume_uploaded(page, cv) == "already_attached"
    assert page.calls == 1


def test_ensure_resume_uploaded_uses_visible_file_input(tmp_path) -> None:
    cv = tmp_path / "cv-internacional.pdf"
    cv.write_bytes(b"%PDF-1.4")
    nome = cv.name

    class FileInput:
        def __init__(self) -> None:
            self.value: str | None = None

        def count(self) -> int:
            return 1

        @property
        def last(self):
            return self

        def set_input_files(self, value: str) -> None:
            self.value = value

    class Page:
        def __init__(self) -> None:
            self.file_input = FileInput()
            self.uploaded = False
            self.selected = False

        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return self.selected
            if "const radio" in expression:
                self.selected = True
                return True
            if "input[type=radio]" in expression and "some" in expression:
                return self.uploaded
            if "files:" in expression:
                self.uploaded = self.file_input.value is not None
                return {
                    "blocked": False,
                    "files": [[nome]] if self.uploaded else [],
                    "radios": (
                        [{"label": nome, "checked": self.selected}] if self.uploaded else []
                    ),
                }
            return False

        def locator(self, selector):
            assert selector == 'input[type="file"]'
            return self.file_input

        def get_by_role(self, *args, **kwargs):
            raise AssertionError("input visível não deve exigir clique em botão")

        def get_by_text(self, *args, **kwargs):
            raise AssertionError("input visível não deve exigir clique em texto")

        def wait_for_timeout(self, _ms):
            pass

    page = Page()
    assert ensure_resume_uploaded(page, cv) == "uploaded"
    assert page.file_input.value == str(cv)


def test_fill_field_uses_select_option_for_selects() -> None:
    class Locator:
        def __init__(self) -> None:
            self.selected = ""

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def select_option(self, label=""):
            self.selected = label

        def fill(self, value):
            raise AssertionError("select não deve usar fill()")

    locator = Locator()

    class Page:
        def locator(self, selector):
            assert selector == '[data-agent-field="3"]'
            return locator

    assert fill_field(Page(), 3, "select", "Brasil (+55)") is True
    assert locator.selected == "Brasil (+55)"


def test_submit_is_blocked_without_persisted_approved() -> None:
    approval = ApprovalRequest(status="PENDING")
    agent = object.__new__(BrowserApplyAgent)
    agent.repo = FakeRepo(approval)
    job = JobOpportunity(external_id="123456789")

    with pytest.raises(HumanActionRequired, match="APPROVED"):
        agent.submit(job, approval, confirmed=True)


def test_approve_persists_human_decision() -> None:
    approval = ApprovalRequest()
    job = JobOpportunity(external_id="123456789", status=JobStatus.PENDING_APPROVAL)
    gate = FakeGate(True)
    repo = FakeRepo(approval)
    agent = object.__new__(BrowserApplyAgent)
    agent.gate = gate
    agent.repo = repo
    agent.bus = None

    assert agent.approve(job, approval) is True
    assert gate.calls == 1
    assert repo.resolutions == [(approval.id, True)]


def test_approve_rejection_blocks_submission() -> None:
    approval = ApprovalRequest()
    job = JobOpportunity(external_id="123456789", status=JobStatus.PENDING_APPROVAL)
    repo = FakeRepo(approval)
    agent = object.__new__(BrowserApplyAgent)
    agent.gate = FakeGate(False)
    agent.repo = repo
    agent.bus = None

    assert agent.approve(job, approval) is False
    assert repo.resolutions == [(approval.id, False)]
    assert job.status is JobStatus.REJECTED_BY_FILTER


def test_optional_field_without_evidence_is_skipped(tmp_path) -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.patterns = SitePatternStore(tmp_path / "patterns.json")
    agent.resolver = FakeResolver()
    snapshot = {
        "url": "https://boards.greenhouse.io/acme/jobs/42",
        "fields": [
            {
                "index": 0,
                "label": "LinkedIn URL",
                "type": "text",
                "required": False,
                "options": [],
                "autocomplete": "",
                "identifier": "linkedin",
                "placeholder": "",
                "nearby_text": "",
                "selector": "#linkedin",
            }
        ],
    }
    assert agent._decide(snapshot) == []


def test_recheck_remote_blocks_non_remote_analysis() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    job = JobOpportunity(external_id="123456789", title="Dev", location="São Paulo, SP")
    job.analysis = JobAnalysis(is_remote=False)

    with pytest.raises(ValidationError, match="100% remota"):
        agent._recheck_remote(job, {"text": "remote"})


def test_recheck_remote_blocks_partial_remote_in_description() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    job = JobOpportunity(
        external_id="123456789",
        title="Full-Stack Developer - 80% Remote",
        location="Töging am Inn",
        description="80% Remote",
    )

    with pytest.raises(ValidationError, match="remoto não confirmado"):
        agent._recheck_remote(job, {"text": "80% Remote"})
