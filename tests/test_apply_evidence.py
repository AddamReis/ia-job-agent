"""Formulários paginados, prints por step e anexo das evidências."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.agents.browser_apply as apply_module
import src.web.review_app as review
from src.agents.browser_apply import (
    BrowserApplyAgent,
    FillDecision,
    PreparedStep,
)
from src.core.config import Settings
from src.core.errors import BlockedByPlatformError, ValidationError
from src.core.state import JobStatus
from src.execution.web.form_dom import (
    capture_screenshot,
    click_next,
    is_experience_step,
    next_step_label,
)
from src.execution.web.site_patterns import SitePatternStore
from src.models.domain import (
    AnswerRecord,
    Application,
    ApplicationEvidence,
    ApprovalRequest,
    CandidateProfile,
    Experience,
    JobOpportunity,
)
from src.models.schemas import AnswerDecision
from src.storage.database import Repository


class FakeResolver:
    def resolve(self, field):
        valor = (
            "addam@example.com"
            if "email" in field.label.lower()
            else "https://www.linkedin.com/in/addam"
        )
        return AnswerDecision(
            value=valor,
            source="profile.personal.email",
            needs_human=False,
            reason="general",
        )


class FakePage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def _snapshot(url: str, label: str, index: int = 0) -> dict:
    return {
        "url": url,
        "title": "Apply",
        "text": "",
        "fields": [
            {
                "index": index,
                "label": label,
                "type": "text",
                "required": True,
                "options": [],
                "autocomplete": "",
                "identifier": "_r_9k_",
                "placeholder": "",
                "nearby_text": "",
                "selector": f"#{label.lower().replace(' ', '-')}",
            }
        ],
    }


def _walk_agent(tmp_path: Path) -> BrowserApplyAgent:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings(root=tmp_path)
    agent.patterns = SitePatternStore(tmp_path / "patterns.json")
    agent.resolver = FakeResolver()
    agent.profile = CandidateProfile()
    agent._pending_ui_actions = []
    return agent


def test_capture_screenshot_uses_chrome_webp_encoder(tmp_path: Path) -> None:
    import base64

    class Session:
        def send(self, comando: str, payload: dict) -> dict:
            assert comando == "Page.captureScreenshot"
            assert payload["format"] == "webp"
            assert payload["quality"] == 70
            return {"data": base64.b64encode(b"webp-comprimido").decode("ascii")}

        def detach(self) -> None:
            return None

    class Page:
        context = SimpleNamespace(new_cdp_session=lambda _page: Session())

        def screenshot(self, **_kwargs) -> bytes:
            raise AssertionError("WebP via CDP não deve usar page.screenshot")

    meta = capture_screenshot(
        Page(),
        tmp_path / "data/evidence/job-1",
        step=2,
        kind="step",
        label="Step 2 · 1 campo(s)",
        quality=70,
    )

    arquivo = Path(meta["path"])
    assert arquivo.is_file()
    assert arquivo.suffix == ".webp"
    assert arquivo.read_bytes() == b"webp-comprimido"
    assert meta["sha256"] == hashlib.sha256(b"webp-comprimido").hexdigest()
    assert meta["step"] == 2
    assert meta["kind"] == "step"


def test_capture_screenshot_falls_back_to_jpeg(tmp_path: Path) -> None:
    class Session:
        def send(self, *_args, **_kwargs) -> dict:
            raise RuntimeError("Chrome sem WebP")

        def detach(self) -> None:
            return None

    class Page:
        context = SimpleNamespace(new_cdp_session=lambda _page: Session())

        def screenshot(self, path: str, type: str, quality: int, full_page: bool) -> bytes:
            assert type == "jpeg"
            assert quality == 82
            assert full_page is False
            Path(path).write_bytes(b"jpeg-comprimido")
            return b"jpeg-comprimido"

    meta = capture_screenshot(
        Page(), tmp_path / "data/evidence/job-2", step=1, kind="confirmation"
    )

    assert Path(meta["path"]).suffix == ".jpg"
    assert Path(meta["path"]).read_bytes() == b"jpeg-comprimido"


def test_click_next_only_uses_advance_labels() -> None:
    class Locator:
        def __init__(self, total: int) -> None:
            self.total = total
            self.clicked = False

        @property
        def first(self):
            return self

        def count(self) -> int:
            return self.total

        def is_visible(self) -> bool:
            return True

        def click(self) -> None:
            self.clicked = True

    class Page:
        def __init__(self) -> None:
            self.locators: dict[str, Locator] = {}

        def get_by_role(self, _role: str, name):
            chave = getattr(name, "pattern", str(name))
            return self.locators.setdefault(chave, Locator(1 if "Next" in chave else 0))

        def locator(self, seletor: str):
            return self.locators.setdefault(seletor, Locator(0))

    page = Page()
    assert next_step_label(page) == "Next"
    assert click_next(page) is True
    assert next(
        locator.clicked for chave, locator in page.locators.items() if "Next" in chave
    ) is True
    assert click_next(page, "Submit") is False


def test_walk_steps_recomputes_all_steps_for_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _walk_agent(tmp_path)
    pagina = FakePage()
    snapshots = iter(
        [
            _snapshot("https://portal.test/step-1", "Email"),
            _snapshot("https://portal.test/step-2", "LinkedIn URL", index=3),
        ]
    )
    rotulos = iter(["Next", ""])
    avancos: list[str] = []
    monkeypatch.setattr(agent, "_handle_resume", lambda page, snap: (page, snap, "already_attached"))
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form", lambda page: next(snapshots)
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.next_step_label", lambda page: next(rotulos)
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.click_next",
        lambda page, label: avancos.append(label) or True,
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.submission_state", lambda page: "unknown"
    )

    steps = agent._walk_steps(
        pagina, JobOpportunity(external_id="123456789", title="Dev", company="Acme")
    )

    assert [step.number for step in steps] == [1, 2]
    assert [step.decisions[0].signature for step in steps] == ["email", "linkedin url"]
    assert [step.decisions[0].index for step in steps] == [0, 3]
    assert avancos == ["Next"]
    respostas = agent._answers_from_steps(steps)
    assert [resposta.step for resposta in respostas] == [1, 2]


def test_walk_steps_retries_resume_until_later_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Easy Apply só mostra o CV em steps posteriores; não pode reprovar no step 1."""
    agent = _walk_agent(tmp_path)
    snapshots = iter(
        [
            _snapshot("https://portal.test/step-1", "Email"),
            _snapshot("https://portal.test/step-2", "LinkedIn URL", index=3),
        ]
    )
    statuses = iter(["upload_button_not_found", "uploaded"])
    tentativas: list[str] = []

    def handle(page, snap):
        status = next(statuses)
        tentativas.append(status)
        return page, snap, status

    monkeypatch.setattr(agent, "_handle_resume", handle)
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form", lambda page: next(snapshots)
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.next_step_label", lambda page: "Next" if len(tentativas) == 1 else ""
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.click_next", lambda page, label: True
    )
    monkeypatch.setattr("src.agents.browser_apply.submission_state", lambda page: "unknown")

    steps = agent._walk_steps(
        FakePage(), JobOpportunity(external_id="123456789", title="Dev", company="Acme")
    )

    assert [step.number for step in steps] == [1, 2]
    assert tentativas == ["upload_button_not_found", "uploaded"]


def test_professional_profile_description_is_not_experience_step() -> None:
    snapshot = {
        "text": (
            "Professional Profile\n"
            "We use LinkedIn to review your professional work experience.\n"
            "Submit Your Application"
        )
    }
    assert not is_experience_step(snapshot)
    assert is_experience_step({"text": "Work experience\nAdd additional role"})


def test_walk_steps_skips_experience_step_and_removes_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work experience já vem do perfil: nunca preencher um quarto cartão."""
    agent = _walk_agent(tmp_path)
    agent.profile = CandidateProfile(
        experiences=[
            Experience(company=f"Empresa {indice}", title=f"Cargo {indice}")
            for indice in range(3)
        ]
    )
    experience_snapshot = _snapshot("https://portal.test/step-3", "Your title")
    experience_snapshot["text"] = "Work experience"
    snapshots = iter(
        [
            experience_snapshot,
            experience_snapshot,
            _snapshot("https://portal.test/step-4", "Email", index=5),
        ]
    )
    removals: list[list[str]] = []
    removal_results = iter([True, False])
    labels = iter(["Next", ""])

    monkeypatch.setattr(agent, "_handle_resume", lambda page, snap: (page, snap, "already_attached"))
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form", lambda page: next(snapshots)
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.next_step_label",
        lambda page: next(labels),
    )
    monkeypatch.setattr("src.agents.browser_apply.click_next", lambda page, label: True)
    monkeypatch.setattr("src.agents.browser_apply.submission_state", lambda page: "unknown")
    def fake_remove(_page, titles, **_kwargs):
        if next(removal_results):
            removals.append(titles)
            return True
        return False

    monkeypatch.setattr(
        "src.agents.browser_apply.remove_extra_experience_entry", fake_remove
    )

    steps = agent._walk_steps(
        FakePage(), JobOpportunity(external_id="123456789", title="Dev", company="Acme")
    )

    assert [step.number for step in steps] == [1, 2]
    assert steps[0].decisions == []
    assert steps[1].decisions[0].signature == "email"
    assert removals == [[exp.title for exp in agent.profile.experiences]]


def test_walk_steps_blocks_submit_without_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _walk_agent(tmp_path)
    monkeypatch.setattr(agent, "_handle_resume", lambda page, snap: (page, snap, "upload_button_not_found"))
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form",
        lambda page: _snapshot("https://portal.test/step-1", "Email"),
    )
    monkeypatch.setattr("src.agents.browser_apply.next_step_label", lambda page: "")
    monkeypatch.setattr("src.agents.browser_apply.submission_state", lambda page: "unknown")

    with pytest.raises(ValidationError, match="currículo não foi anexado"):
        agent._walk_steps(
            FakePage(), JobOpportunity(external_id="123456789", title="Dev", company="Acme")
        )


def test_walk_steps_stops_on_captcha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _walk_agent(tmp_path)
    monkeypatch.setattr(agent, "_handle_resume", lambda page, snap: (page, snap, "already_attached"))
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form", lambda page: _snapshot("https://x.test", "Email")
    )
    monkeypatch.setattr("src.agents.browser_apply.next_step_label", lambda page: "")
    monkeypatch.setattr("src.agents.browser_apply.submission_state", lambda page: "blocked")

    with pytest.raises(BlockedByPlatformError, match="CAPTCHA"):
        agent._walk_steps(FakePage(), JobOpportunity(external_id="123456789"))


class FakeApplyRepo:
    def __init__(self, approval: ApprovalRequest) -> None:
        self.approval = approval
        self.applications: list[Application] = []
        self.statuses: list[JobStatus] = []

    def get_approval(self, _approval_id: str) -> ApprovalRequest:
        return self.approval

    def update_job(self, job: JobOpportunity) -> None:
        self.statuses.append(job.status)

    def record_application(self, application: Application) -> None:
        self.applications.append(application)

    def get_company_by_name(self, _name: str):
        return None

    def ensure_company(self, _name: str):
        return SimpleNamespace(id="company-1")

    def mark_company_application(self, _company_id: str, *, created_account: bool) -> None:
        return None


def test_submit_attaches_step_and_confirmation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pagina = FakePage()
    snapshot = _snapshot("https://portal.test/apply", "Email")
    observacao = apply_module.ApplyObservation(False, "portal_externo")
    aprovacao = ApprovalRequest(
        status="APPROVED",
        answers=[
            AnswerRecord(field="email", source="profile.personal.email", step=1),
            AnswerRecord(field="linkedin url", source="profile.personal.linkedin", step=2),
        ],
    )
    repo = FakeApplyRepo(aprovacao)
    agent = _walk_agent(tmp_path)
    agent.repo = repo
    agent.profile = CandidateProfile(documents={"resume": "cv.pdf"})
    agent.settings.profile.cv_path = "/app/data/cv/Addam.pdf"
    agent.memory = None
    agent.bus = None
    agent.browser = SimpleNamespace(close_tab=lambda _page: None)
    agent._pending = {}

    etapas = [
        PreparedStep(
            number=1,
            url="https://portal.test/apply",
            domain="portal.test",
            decisions=[
                FillDecision(
                    index=0, signature="email", selector="#email", kind="text",
                    value="addam@example.com", source="profile.personal.email", required=True,
                )
            ],
        ),
        PreparedStep(
            number=2,
            url="https://portal.test/step-2",
            domain="portal.test",
            decisions=[
                FillDecision(
                    index=3, signature="linkedin url", selector="#li", kind="text",
                    value="https://www.linkedin.com/in/addam",
                    source="profile.personal.linkedin", required=True,
                )
            ],
        ),
    ]

    def fake_walk(_self, page, _job, *, on_step=None, allow_submit=False):
        for etapa in etapas:
            on_step(etapa, page, {})
        return etapas

    preenchidos: list[str] = []
    capturas: list[str] = []

    monkeypatch.setattr(BrowserApplyAgent, "_open", lambda self, job: (pagina, snapshot, observacao))
    monkeypatch.setattr(BrowserApplyAgent, "_record_easy_apply", lambda self, job, obs: None)
    monkeypatch.setattr(BrowserApplyAgent, "_detect_already_applied", lambda self, page, snap: False)
    monkeypatch.setattr(BrowserApplyAgent, "_recheck_remote", lambda self, job, snap: None)
    monkeypatch.setattr(BrowserApplyAgent, "_walk_steps", fake_walk)
    monkeypatch.setattr(
        BrowserApplyAgent, "_handle_post_submit_assessment", lambda self, page, domain: None
    )
    monkeypatch.setattr("src.agents.browser_apply.snapshot_form", lambda page: snapshot)
    monkeypatch.setattr("src.agents.browser_apply.submission_state", lambda page: "submitted")
    monkeypatch.setattr(
        "src.agents.browser_apply.fill_field",
        lambda page, index, kind, value: preenchidos.append(value) or True,
    )
    monkeypatch.setattr(
        "src.agents.browser_apply.capture_screenshot",
        lambda page, directory, *, step, kind, label, **_kwargs: capturas.append(kind)
        or {
            "step": step,
            "kind": kind,
            "label": label,
            "path": f"data/evidence/job-1/{kind}.webp",
            "sha256": "abc",
            "created_at": "2026-11-24T10:00:00",
        },
    )

    application = agent.submit(
        JobOpportunity(
            external_id="123456789",
            title="Dev",
            company="Acme",
            status=JobStatus.PENDING_APPROVAL,
        ),
        aprovacao,
        confirmed=True,
    )

    assert application is not None
    assert [item.kind for item in application.evidence] == [
        "step", "step", "confirmation"
    ]
    assert [item.step for item in application.evidence] == [1, 2, 2]
    assert [answer.step for answer in application.answers] == [1, 2]
    assert preenchidos == ["addam@example.com", "https://www.linkedin.com/in/addam"]
    assert capturas == ["step", "step", "confirmation"]
    assert application.resume_path == "/app/data/cv/Addam.pdf"
    assert repo.applications == [application]


def test_repository_roundtrip_keeps_evidence_and_review_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AI_JOB_DATABASE_URL", raising=False)
    repo = Repository(db_path=tmp_path / "agent.db")
    evidencia = ApplicationEvidence(
        step=1,
        kind="step",
        label="Step 1 · 1 campo(s)",
        path="data/evidence/job-1/step_01.webp",
        sha256="abc",
    )
    application = Application(
        opportunity_id="job-1", company="Acme", title="Dev", evidence=[evidencia]
    )
    repo.record_application(application)

    carregada = repo.get_application_for_job("job-1")
    assert carregada is not None
    assert carregada.evidence[0].path == evidencia.path

    decisao = repo.record_review_decision("job-1", "APPROVED")
    repo.attach_evidence_to_review_decision(decisao["id"], [evidencia.model_dump(mode="json")])
    gravada = repo.get_review_decision_for_job("job-1")
    assert gravada is not None
    assert gravada["checks"]["evidence"][0]["sha256"] == "abc"


def test_review_path_validation_blocks_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(review, "_settings", lambda: Settings(root=tmp_path))
    arquivo = tmp_path / "data/evidence/job-1/step_01.webp"
    arquivo.parent.mkdir(parents=True)
    arquivo.write_bytes(b"webp")

    assert review.resolve_evidence_path("job-1", "step_01.webp") == arquivo
    assert review.resolve_evidence_path("job-1", "../segredo.webp") is None
    assert review.resolve_evidence_path("../etc", "passwd.webp") is None
    assert review.resolve_evidence_path("job-1", "cv.pdf") is None


def test_job_detail_exposes_application_and_decision_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = JobOpportunity(
        id="job-1",
        external_id="123456789",
        company="Acme",
        title="Dev",
        description="100% remote .NET role",
        status=JobStatus.SUBMITTED,
        has_applied=True,
    )
    evidencia = ApplicationEvidence(
        step=1,
        kind="step",
        label="Step 1",
        path="data/evidence/job-1/step_01.webp",
        sha256="abc",
    )
    application = Application(opportunity_id="job-1", evidence=[evidencia])
    decision = {
        "checks": {
            "evidence": [
                {
                    "step": 2,
                    "kind": "confirmation",
                    "label": "Confirmação",
                    "path": "data/evidence/job-1/confirmation.webp",
                    "sha256": "def",
                }
            ]
        }
    }

    class Repo:
        def get_job(self, job_id: str):
            return job if job_id == job.id else None

        def get_approval_for_job(self, _job_id: str):
            return None

        def get_review_decision_for_job(self, _job_id: str):
            return decision

        def failed_agent_events(self, *_args, **_kwargs):
            return []

        def get_application_for_job(self, _job_id: str):
            return application

    monkeypatch.setattr(review, "_repo", lambda: Repo())
    monkeypatch.setattr(review, "_settings", lambda: Settings(root=tmp_path))

    detail = review.job_detail("job-1")

    assert detail is not None
    assert [item["kind"] for item in detail["evidence"]] == ["step", "confirmation"]


def test_pwa_renderiza_evidencias_no_detalhe() -> None:
    source = (review.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "Evidências do Apply" in source
    assert "/api/evidence/" in source
