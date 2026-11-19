"""Agente de candidatura no Chrome logado: URL real, aprendizado local e gate."""
from __future__ import annotations

import pytest

from src.agents.browser_apply import ApplyObservation, BrowserApplyAgent, _value_from_source
from src.core.config import Settings
from src.core.errors import HumanActionRequired, ValidationError
from src.core.state import JobStatus
from src.execution.forms.field import FormField
from src.execution.web.form_dom import ensure_resume_uploaded, fill_field
from src.execution.web.site_patterns import SitePatternStore
from src.models.domain import ApprovalRequest, CandidateProfile, JobAnalysis, JobOpportunity
from src.models.schemas import AnswerDecision


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

    def update_job(self, job):
        self.updated.append(job.status)


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
