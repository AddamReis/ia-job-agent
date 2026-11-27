"""Agente de candidatura no Chrome logado: URL real, aprendizado local e gate."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.agents.browser_apply import (
    ApplyObservation,
    ApplyRoute,
    BrowserApplyAgent,
    _value_from_source,
)
from src.core.config import Settings
from src.core.errors import HumanActionRequired, JobUnavailable, ValidationError
from src.core.state import JobStatus
from src.execution.forms.answers import AnswerResolver
from src.execution.forms.field import FormField
from src.execution.forms.mapper import resolve_local
from src.execution.web.form_dom import (
    _SNAPSHOT_JS,
    _choose_combobox,
    credential_barrier,
    ensure_resume_uploaded,
    fill_field,
    peek_combobox_options,
    platform_block,
    required_field_state,
    resume_attached_on_page,
    submission_state,
)
from src.execution.web.site_patterns import SitePatternStore
from src.models.domain import ApprovalRequest, CandidateProfile, JobAnalysis, JobOpportunity
from src.models.schemas import AnswerDecision, ConsentDecision, UiActionDecision


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


def test_ensure_resume_uploaded_recognizes_filename_in_page_text(tmp_path) -> None:
    cv = tmp_path / "cv-alignerr.pdf"
    cv.write_bytes(b"%PDF-1.4")

    class Page:
        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return True
            if "uploading|upload in progress" in expression:
                return False
            raise AssertionError("não deve procurar input quando o texto já confirma o CV")

        def locator(self, *args, **kwargs):
            raise AssertionError("não deve reenviar um CV já confirmado na tela")

    assert ensure_resume_uploaded(Page(), cv) == "already_attached"


def test_upload_em_andamento_nao_usa_texto_alheio_ao_arquivo(tmp_path) -> None:
    cv = tmp_path / "cv-alignerr.pdf"
    cv.write_bytes(b"%PDF-1.4")

    class Page:
        texto = (
            "Resume/CV*\n"
            "cv-alignerr.pdf\n"
            "By checking this box, I consent to processing my responses.\n"
        )

        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return True
            if "uploading|upload in progress" in expression:
                alvo = (name or "").lower()
                return any(
                    alvo in linha.lower()
                    and re.search(
                        r"uploading|upload in progress|enviando|carregando|processing",
                        linha,
                        re.I,
                    )
                    for linha in self.texto.splitlines()
                )
            raise AssertionError("expressão de upload inesperada")

        def locator(self, *args, **kwargs):
            raise AssertionError("CV já anexado não deve ser reenviado")

    assert ensure_resume_uploaded(Page(), cv) == "already_attached"


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


def test_ensure_resume_uploaded_accepts_file_without_radio(tmp_path) -> None:
    cv = tmp_path / "cv-internacional.pdf"
    cv.write_bytes(b"%PDF-1.4")
    nome = cv.name

    class FileInput:
        def count(self) -> int:
            return 1

        @property
        def last(self):
            return self

        def set_input_files(self, value: str) -> None:
            assert value == str(cv)

    class Page:
        def __init__(self) -> None:
            self.file_input = FileInput()

        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input[type=radio]" in expression and "some" in expression:
                return False
            if "files:" in expression:
                return {"blocked": False, "files": [[nome]], "radios": []}
            if "input, a, [aria-label], [title]" in expression:
                return False
            return False

        def locator(self, selector):
            assert selector == 'input[type="file"]'
            return self.file_input

        def wait_for_timeout(self, _ms):
            pass

    assert ensure_resume_uploaded(Page(), cv) == "uploaded"


def test_gender_and_birth_date_are_sensitive_without_evidence() -> None:
    perfil = CandidateProfile()
    for rotulo in (
        "Gender", "Date of Birth", "Gênero", "Data de Nascimento",
        "Month", "Day", "Year",
    ):
        campo = FormField(label=rotulo, required=True)
        valor, _origem, precisa_humano, categoria = resolve_local(
            campo, perfil, Settings()
        )
        assert valor == ""
        assert precisa_humano is True
        assert categoria == "sensitive"


def test_gender_birth_date_and_legal_last_name_use_recorded_evidence() -> None:
    perfil = CandidateProfile()
    perfil.personal.gender = "Male"
    perfil.personal.birth_date = "1996-08-29"
    perfil.personal.birth_month = "August"
    perfil.personal.birth_day = "29"
    perfil.personal.birth_year = "1996"
    perfil.personal.last_name = "Oliveira"

    esperado = {
        "Gender": "Male",
        "Date of Birth": "1996-08-29",
        "Month": "August",
        "Day": "29",
        "Year": "1996",
        "Last Name": "Oliveira",
    }
    for rotulo, valor_esperado in esperado.items():
        campo = FormField(label=rotulo, required=True)
        valor, origem, precisa_humano, categoria = resolve_local(
            campo, perfil, Settings()
        )
        assert valor == valor_esperado
        assert origem.startswith("profile.personal.")
        assert precisa_humano is False
        assert categoria == "general"


def test_birth_parts_are_derived_from_iso_date() -> None:
    perfil = CandidateProfile()
    perfil.personal.birth_date = "1996-08-29"
    for rotulo, valor_esperado in (
        ("Month", "August"), ("Day", "29"), ("Year", "1996")
    ):
        campo = FormField(label=rotulo, required=True)
        valor, _origem, precisa_humano, categoria = resolve_local(
            campo, perfil, Settings()
        )
        assert valor == valor_esperado
        assert precisa_humano is False
        assert categoria == "general"


def test_resume_attached_on_page_reads_filename_from_body_text(tmp_path) -> None:
    cv = tmp_path / "cv-alignerr.pdf"
    cv.write_bytes(b"%PDF-1.4")

    class Page:
        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return True
            if "uploading|upload in progress" in expression:
                return False
            if "input[type=radio]" in expression:
                return False
            return {"blocked": False, "files": [], "radios": []}

    assert resume_attached_on_page(Page(), cv) is True


def test_handle_resume_accepts_upload_done_in_previous_tab(tmp_path) -> None:
    cv = tmp_path / "cv-alignerr.pdf"
    cv.write_bytes(b"%PDF-1.4")

    class Zero:
        def count(self) -> int:
            return 0

    class CurrentPage:
        def __init__(self, context) -> None:
            self.context = context

        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return False
            if "uploading|upload in progress" in expression:
                return False
            if "input[type=radio]" in expression and "some" in expression:
                return False
            if "files:" in expression:
                return {"blocked": False, "files": [], "radios": []}
            return False

        def locator(self, _selector):
            return Zero()

        def get_by_role(self, *_args, **_kwargs):
            return Zero()

        def get_by_text(self, *_args, **_kwargs):
            return Zero()

    class UploadedTab:
        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return True
            if "uploading|upload in progress" in expression:
                return False
            return {"blocked": False, "files": [], "radios": []}

    context = SimpleNamespace(pages=[])
    page = CurrentPage(context)
    context.pages = [page, UploadedTab()]
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    agent.profile = CandidateProfile(documents={"resume": str(cv)})

    _page, snapshot, status = agent._handle_resume(
        page, {"url": "https://x/profile", "text": ""}
    )

    assert status == "already_attached"
    assert snapshot == {"url": "https://x/profile", "text": ""}


def test_ensure_resume_uploaded_waits_for_upload_to_finish(tmp_path) -> None:
    cv = tmp_path / "cv-alignerr.pdf"
    cv.write_bytes(b"%PDF-1.4")
    nome = cv.name

    class FileInput:
        def count(self) -> int:
            return 1

        @property
        def last(self):
            return self

        def set_input_files(self, value: str) -> None:
            assert value == str(cv)

    class Page:
        def __init__(self) -> None:
            self.estado = "uploading"
            self.esperas = 0

        def evaluate(self, expression, name=None):
            if "checked &&" in expression:
                return False
            if "input[type=radio]" in expression and "some" in expression:
                return False
            if "files:" in expression:
                return {"blocked": False, "files": [[nome]], "radios": []}
            if "uploading|upload in progress" in expression:
                return self.estado == "uploading"
            if "input, a, [aria-label], [title]" in expression:
                return False
            return False

        def locator(self, selector):
            assert selector == 'input[type="file"]'
            return FileInput()

        def wait_for_timeout(self, _ms):
            self.esperas += 1
            if self.esperas >= 2:
                self.estado = "uploaded"

    page = Page()
    assert ensure_resume_uploaded(page, cv) == "uploaded"
    assert page.estado == "uploaded"


def test_advance_external_landing_decides_safe_cta(tmp_path) -> None:
    class Locator:
        def __init__(self, visible: bool = True) -> None:
            self.visible = visible
            self.clicked = False

        def count(self) -> int:
            return 1 if self.visible else 0

        def nth(self, _index):
            return self

        def is_visible(self) -> bool:
            return self.visible

        def click(self) -> None:
            self.clicked = True

    class Page:
        def __init__(self) -> None:
            self.url = "https://vetsez.breezy.hr/p/vaga/apply"
            self.context = self
            self.pages = [self]
            self.link = Locator()
            self.clicked = False

        def evaluate(self, expression, name=None):
            if "has_form" in expression:
                return {
                    "url": self.url,
                    "text": "Aplicar a la posición",
                    "buttons": [
                        {"text": "Aplicar a la posición", "aria": "", "href": "/apply"}
                    ],
                    "has_form": self.clicked,
                    "has_file": self.clicked,
                    "blocked": False,
                }
            return {}

        def get_by_role(self, role, name=None):
            if role == "link":
                return self.link
            return Locator(False)

        def get_by_text(self, *_args, **_kwargs):
            return Locator(False)

        def locator(self, _selector):
            return Locator(False)

        def wait_for_timeout(self, _ms):
            pass

    agent = object.__new__(BrowserApplyAgent)
    agent.patterns = object()
    agent.resolver = object()
    agent.browser = object()
    agent._pending_ui_actions = []
    agent._decide_ui_action = lambda *_args: SimpleNamespace(
        action="Aplicar a la posición",
        needs_human=False,
        source="llm",
        reason="abre o formulário real",
    )

    page = Page()
    result = agent._advance_external_landing(page)
    assert result is page
    assert page.link.clicked is True
    assert len(agent._pending_ui_actions) == 1
    assert agent._pending_ui_actions[0]["intent"] == "open_external_apply"
    assert agent._pending_ui_actions[0]["action"] == "Aplicar a la posición"


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


def test_fill_field_select_aceita_rotulo_equivalente_do_linkedin() -> None:
    class Locator:
        def __init__(self) -> None:
            self.selected = ""

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def evaluate(self, expression, *args):
            return [
                {"value": "US", "label": "United States (+1)"},
                {"value": "BR", "label": "Brazil (+55)"},
            ]

        def select_option(self, value="", label="", **kwargs):
            self.selected = value or label

        def fill(self, value):
            raise AssertionError("select não deve usar fill()")

    locator = Locator()

    class Page:
        def locator(self, selector):
            return locator

    assert fill_field(Page(), 1, "select", "Brasil (+55)") is True
    assert locator.selected == "BR"


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


def test_optional_sensitive_field_without_evidence_blocks(tmp_path) -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.patterns = SitePatternStore(tmp_path / "patterns.json")

    class SensitiveResolver:
        def resolve(self, field):
            return AnswerDecision(
                needs_human=True,
                source="politica:acao_sensivel",
                reason="ação sensível exige aprovação humana",
            )

    agent.resolver = SensitiveResolver()
    snapshot = {
        "url": "https://app.alignerr.com/application/profile",
        "fields": [
            {
                "index": 0,
                "label": "Month",
                "type": "combobox",
                "required": False,
                "options": [],
                "autocomplete": "",
                "identifier": "",
                "placeholder": "",
                "nearby_text": "",
                "selector": "[role=combobox]",
            }
        ],
    }
    with pytest.raises(HumanActionRequired, match="Month"):
        agent._decide(snapshot)


def test_alignerr_preferences_use_only_operator_choices(tmp_path) -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.patterns = SitePatternStore(tmp_path / "patterns.json")
    agent.resolver = FakeResolver()
    agent.profile = CandidateProfile(
        project_preferences=["Coding", "Engineering", "Language"]
    )
    snapshot = {
        "url": "https://app.alignerr.com/application/preferences",
        "fields": [
            {
                "index": index,
                "label": label,
                "type": "checkbox",
                "required": False,
                "options": [],
                "autocomplete": "",
                "identifier": "",
                "placeholder": "",
                "nearby_text": "",
                "selector": f"#{index}",
            }
            for index, label in enumerate(("Coding", "Engineering", "Science", "Language"))
        ],
    }
    decisoes = {d.signature: d.value for d in agent._decide(snapshot)}
    assert decisoes["coding"] == "true"
    assert decisoes["engineering"] == "true"
    assert decisoes["science"] == "false"
    assert decisoes["language"] == "true"


def test_form_consent_is_decided_by_agent_not_by_company_rule(tmp_path) -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.patterns = SitePatternStore(tmp_path / "patterns.json")

    class ConsentResolver:
        def interpret_consent(self, text, context, **_kwargs):
            assert "Candidate Terms" in text
            return ConsentDecision(allow=True, source="dgx", reason="aceite operacional")

    agent.resolver = ConsentResolver()
    snapshot = {
        "url": "https://recruitcrm.io/apply/17951232341070072484QXg",
        "fields": [
            {
                "index": 0,
                "label": "I agree to Candidate Terms",
                "type": "checkbox",
                "required": False,
                "options": [],
                "autocomplete": "",
                "identifier": "candidateTermsBlock",
                "placeholder": "",
                "nearby_text": "I agree to Candidate Terms",
                "selector": "#candidateTermsBlock",
            }
        ],
    }

    decisoes = agent._decide(snapshot)
    assert len(decisoes) == 1
    assert decisoes[0].value == "true"
    assert decisoes[0].source == "dgx"
    assert decisoes[0].category == "consent"


def test_submission_state_recognizes_recruitcrm_confirmation() -> None:
    class Page:
        def locator(self, _selector):
            return type("Body", (), {"inner_text": lambda self: "Thank you for applying!"})()

    assert submission_state(Page()) == "submitted"


def test_submission_state_recognizes_alignerr_withdraw_marker() -> None:
    class Page:
        def locator(self, _selector):
            return type(
                "Body",
                (),
                {
                    "inner_text": lambda self: "Software Engineer - Project Creation | Withdraw"
                },
            )()

    assert submission_state(Page()) == "submitted"


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


def test_landing_state_reads_checkbox_and_iframe_buttons() -> None:
    class Frame:
        url = "https://careers.example.com/iframe"

        def evaluate(self, expression, *args):
            if "has_form" in expression:
                return {
                    "url": self.url,
                    "text": "Candidate-se a esta vaga",
                    "buttons": [{"text": "Candidate-se a esta vaga", "href": "/apply"}],
                    "checkboxes": [],
                    "has_form": False,
                    "has_file": False,
                    "blocked": False,
                    "passwords": 0,
                }
            return {}

    class Page:
        url = "https://careers.example.com/job/1"

        def __init__(self) -> None:
            self.frame = Frame()
            self.frames = [self, self.frame]

        def evaluate(self, expression, *args):
            if "has_form" in expression:
                return {
                    "url": self.url,
                    "text": "I agree to the Terms of Service and Privacy Policy",
                    "buttons": [{"text": "Continue with Google", "href": ""}],
                    "checkboxes": [
                        {
                            "text": "I agree to the Terms of Service and Privacy Policy",
                            "checked": False,
                        }
                    ],
                    "has_form": False,
                    "has_file": False,
                    "blocked": False,
                    "passwords": 0,
                }
            return {}

    estado = object.__new__(BrowserApplyAgent)._landing_state(Page())

    assert estado["terms_checkbox"] is True
    assert estado["oauth_google"] is True
    assert estado["checkboxes"][0]["frame_index"] == 0
    assert estado["buttons"][-1]["frame_index"] == 1
    assert estado["buttons"][-1]["href"] == "https://careers.example.com/apply"


def test_advance_external_landing_follows_same_cta_on_second_screen() -> None:
    class Clicavel:
        def __init__(self, page) -> None:
            self.page = page

        def count(self) -> int:
            return 1

        def nth(self, _index):
            return self

        def is_visible(self) -> bool:
            return True

        def click(self) -> None:
            self.page.etapa += 1

    class Page:
        def __init__(self) -> None:
            self.etapa = 0
            self.context = self
            self.pages = [self]

        @property
        def url(self) -> str:
            return [
                "https://recruitcrm.io/apply/1",
                "https://recruitcrm.io/apply/1?step=2",
                "https://recruitcrm.io/apply/1?step=3",
            ][min(self.etapa, 2)]

        def evaluate(self, expression, *args):
            if "has_form" in expression:
                return {
                    "url": self.url,
                    "text": "Apply To This Job",
                    "buttons": [{"text": "Apply To This Job", "href": ""}],
                    "checkboxes": [],
                    "has_form": self.etapa >= 2,
                    "has_file": self.etapa >= 2,
                    "blocked": False,
                }
            return {}

        def get_by_role(self, *_args, **_kwargs):
            return Clicavel(self)

        def get_by_text(self, *_args, **_kwargs):
            return Clicavel(self)

        def locator(self, *_args):
            return Clicavel(self)

        def wait_for_timeout(self, _ms) -> None:
            return None

    agent = object.__new__(BrowserApplyAgent)
    agent._pending_ui_actions = []
    agent._decide_ui_action = lambda *_args: SimpleNamespace(
        action="Apply To This Job",
        needs_human=False,
        source="learned",
        reason="CTA abre a próxima etapa",
    )

    page = Page()
    result = agent._advance_external_landing(page)

    assert result is page
    assert page.etapa == 2
    assert len(agent._pending_ui_actions) == 1
    assert agent._pending_ui_actions[0]["source"] == "learned"


def test_recheck_remote_blocks_hybrid_card_label() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    description = "Trabaja 100 % remoto en un proyecto internacional."
    job = JobOpportunity(
        external_id="123456789",
        title="Ingeniero de software",
        location="Colombia - Bogota, D.C.",
        description=description,
    )

    with pytest.raises(ValidationError, match="h[íi]brido"):
        agent._recheck_remote(
            job,
            {
                "job_text": description,
                "title": "Ingeniero de software at Cegeka",
                "text": "Híbrido\n" + description,
            },
        )


def test_recheck_remote_uses_description_not_sidebar_text() -> None:
    agent = object.__new__(BrowserApplyAgent)
    agent.settings = Settings()
    description = "Trabaja 100 % remoto en un proyecto internacional."
    job = JobOpportunity(
        external_id="123456789",
        title="Ingeniero de software",
        location="Colombia - Bogota, D.C.",
        description=description,
    )

    agent._recheck_remote(
        job,
        {
            "job_text": description,
            "text": "the office, your home, or a blend of both",
        },
    )


def test_salary_evidence_resolves_desired_salary_and_csalary() -> None:
    perfil = CandidateProfile(expectations={"salary": "A combinar"})
    settings = Settings()
    for rotulo in ("Desired Salary*", "Salario deseado", "csalary"):
        valor, origem, precisa_humano, _categoria = resolve_local(
            FormField(label=rotulo, required=True), perfil, settings
        )
        assert valor == "A combinar"
        assert origem == "profile.expectations.salary"
        assert precisa_humano is False


def test_demographic_opt_out_uses_portal_option_without_profile_data() -> None:
    campo = FormField(
        label="I don't wish to answer",
        field_type="radio",
        nearby_text="How would you describe your gender identity?",
    )
    valor, origem, precisa_humano, _categoria = resolve_local(
        campo, CandidateProfile(), Settings()
    )
    assert valor == "I don't wish to answer"
    assert origem == "portal:opcao_nao_responder"
    assert precisa_humano is False


def test_racial_background_uses_portal_self_identify_opt_out() -> None:
    opcao = "I don't wish to self-identify"
    campo = FormField(
        label="How would you describe your racial/ethnic background?",
        field_type="combobox",
        nearby_text="Select...",
        options=[
            "White or European",
            "Hispanic, Latinx or of Spanish Origin",
            opcao,
        ],
    )
    valor, origem, precisa_humano, _categoria = resolve_local(
        campo, CandidateProfile(), Settings()
    )
    assert valor == opcao
    assert origem == "portal:opcao_nao_responder"
    assert precisa_humano is False


def test_credential_barrier_raises_human_action_required() -> None:
    class Pagina:
        def evaluate(self, expression, _name=None):
            if "input[type=password]" in expression:
                return "senha"
            return False

    pagina = Pagina()
    assert credential_barrier(pagina) == "senha"
    agente = object.__new__(BrowserApplyAgent)
    with pytest.raises(HumanActionRequired, match="credencial"):
        agente._ensure_no_credential_barrier(pagina)


def test_resume_upload_uses_context_to_pick_cv_input(tmp_path) -> None:
    cv = tmp_path / "curriculo.pdf"
    cv.write_bytes(b"%PDF-1.4")
    nome = cv.name
    escolhidos: list[int] = []

    class Entrada:
        def __init__(self, indice: int) -> None:
            self.indice = indice
            self.arquivo = ""

        def set_input_files(self, valor: str) -> None:
            self.arquivo = valor
            escolhidos.append(self.indice)

    entradas = [Entrada(0), Entrada(1)]

    class Pagina:
        def evaluate(self, expression, name=None):
            if "input[type=file]" in expression and "context" in expression:
                return [
                    {"index": 0, "context": "Resume/CV* Attach"},
                    {"index": 1, "context": "Cover Letter Attach"},
                ]
            if "checked &&" in expression:
                return False
            if "input[type=radio]" in expression and "some" in expression:
                return False
            if "files:" in expression:
                return {"blocked": False, "files": [[nome]], "radios": []}
            if "uploading|upload in progress" in expression:
                return False
            if "input, a, [aria-label], [title]" in expression:
                return False
            return False

        def locator(self, _selector):
            return SimpleNamespace(nth=lambda indice: entradas[indice])

    assert ensure_resume_uploaded(Pagina(), cv) == "uploaded"
    assert escolhidos == [0]
    assert entradas[0].arquivo == str(cv)
    assert entradas[1].arquivo == ""


def test_submission_state_recognizes_application_has_been_submitted() -> None:
    class Pagina:
        url = "https://recruitcrm.io/apply/123"

        @property
        def locator(self):
            return lambda _selector: SimpleNamespace(
                inner_text=lambda: "Your application has been submitted"
            )

    assert submission_state(Pagina()) == "submitted"


def test_verified_employer_badge_is_not_captcha_block() -> None:
    class Pagina:
        url = "https://www.linkedin.com/jobs/view/123"

        @property
        def locator(self):
            return lambda _selector: SimpleNamespace(
                inner_text=lambda: "Verificação realizada | Candidatar-se | protected by reCAPTCHA"
            )

    assert submission_state(Pagina()) != "blocked"


def test_confirmacao_de_envio_tem_precedencia_sobre_selo_de_verificacao() -> None:
    class Pagina:
        url = "https://www.linkedin.com/jobs/view/4494618968/"

        @property
        def locator(self):
            return lambda _selector: SimpleNamespace(
                inner_text=lambda: (
                    "Status da candidatura\nCandidatura enviada agora\n"
                    "Verificação de segurança\nprotected by reCAPTCHA"
                )
            )

    assert submission_state(Pagina()) == "submitted"


def test_recaptcha_branding_is_not_a_platform_challenge() -> None:
    assert not platform_block("Apply | protected by reCAPTCHA")
    assert not platform_block(
        'recaptcha.anchor.Main.init("[\\x22ainput\\x22,[\\x22bgdata\\x22,null]")'
    )
    assert platform_block("Verificação de humano: confirme que você é humano")


def test_snapshot_js_discards_decorative_and_honeypot_controls() -> None:
    """Greenhouse/Breezy escondem sentinels que não são campos do formulário."""
    assert 'aria-hidden' in _SNAPSHOT_JS
    assert 'tabindex' in _SNAPSHOT_JS
    assert 'requiredInput' in _SNAPSHOT_JS
    assert 'iti__search-input' in _SNAPSHOT_JS
    assert "'react-select-' + el.id + '-listbox'" in _SNAPSHOT_JS
    assert 'aria-controls' in _SNAPSHOT_JS
    assert 'hp[_-]' in _SNAPSHOT_JS
    assert '!playerControl(el) && !decorative(el)' in _SNAPSHOT_JS
    assert 'const marcados = arredores' in _SNAPSHOT_JS
    assert '(t.match(/[*?]/g)' in _SNAPSHOT_JS


def test_peek_combobox_options_reads_associated_listbox() -> None:
    opcoes = ["White or European", "I don't wish to self-identify"]

    class Lista:
        def locator(self, _selector):
            return self

        def count(self) -> int:
            return len(opcoes)

        def nth(self, indice: int):
            return type(
                "Opcao", (), {"inner_text": lambda self, timeout=None: opcoes[indice]}
            )()

    class Alvo:
        def click(self, **_kwargs) -> None:
            return None

        def evaluate(self, _expression: str, timeout: int | None = None):
            return {
                "value": "",
                "display": "",
                "controls": "",
                "id": "31772",
                "expanded": "true",
            }

    class Page:
        def __init__(self) -> None:
            self.presses: list[str] = []

        @property
        def keyboard(self):
            return self

        def press(self, tecla: str) -> None:
            self.presses.append(tecla)

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def locator(self, seletor: str):
            if "data-agent-field" in seletor:
                return type("Unico", (), {"first": property(lambda self: Alvo())})()
            assert "react-select-31772-listbox" in seletor
            return type(
                "Caixa",
                (),
                {
                    "count": lambda self: 1,
                    "first": property(lambda self: Lista()),
                },
            )()

    pagina = Page()
    assert peek_combobox_options(pagina, 11) == opcoes
    assert "Escape" in pagina.presses


def test_choose_combobox_uses_its_own_listbox_and_visible_selection() -> None:
    """React Select do Greenhouse mantém várias listboxes e input vazio."""

    class Option:
        def __init__(self, texto: str, *, escolhida: bool = False) -> None:
            self.texto = texto
            self.escolhida = escolhida

        def inner_text(self, timeout: int | None = None) -> str:
            return self.texto

        def click(self, timeout: int | None = None) -> None:
            self.escolhida = True
            state["display"] = "+55"

        def get_attribute(self, nome: str, timeout: int | None = None) -> str | None:
            assert nome == "aria-selected"
            return "true" if self.escolhida else "false"

    class Lista:
        def __init__(self, opcoes: list[Option]) -> None:
            self.opcoes = opcoes

        def locator(self, selector: str):
            assert selector == '[role="option"]'
            return self

        def count(self) -> int:
            return len(self.opcoes)

        def nth(self, indice: int) -> Option:
            return self.opcoes[indice]

    state = {"display": ""}
    phone = Lista([Option("Brazil +55")])
    country = Lista([Option("Argentina +54"), Option("Brazil +55")])

    class Target:
        def evaluate(self, expression: str, timeout: int | None = None) -> dict[str, str]:
            assert "select__single-value" in expression
            return {
                "value": "",
                "display": state["display"],
                "controls": "react-select-country-listbox",
                "id": "country",
                "expanded": "true",
            }

        def click(self, timeout: int | None = None) -> None:
            return None

        def fill(self, valor: str, timeout: int | None = None) -> None:
            return None

    class Page:
        def __init__(self) -> None:
            self.seletores: list[str] = []

        @property
        def keyboard(self):
            return self

        def press(self, _tecla: str) -> None:
            return None

        def type(self, _texto: str) -> None:
            raise AssertionError("não deve digitar quando a opção é encontrada")

        def wait_for_timeout(self, _ms: int) -> None:
            return None

        def locator(self, seletor: str):
            self.seletores.append(seletor)
            if "react-select-country-listbox" in seletor:
                return type("Unica", (), {"count": lambda self: 1, "first": property(lambda self: country)})()
            if "iti-0__country-listbox" in seletor:
                return type("Unica", (), {"count": lambda self: 1, "first": property(lambda self: phone)})()
            return type("Vazia", (), {"count": lambda self: 0, "first": property(lambda self: phone)})()

    pagina = Page()
    assert _choose_combobox(pagina, Target(), "Brazil") is True
    assert any("react-select-country-listbox" in s for s in pagina.seletores)
    assert country.opcoes[1].escolhida is True
    assert phone.opcoes[0].escolhida is False


def test_required_field_state_reports_boolean_without_value() -> None:
    class Page:
        def evaluate(self, _expression: str):
            return [
                {"id": "first_name", "filled": True, "error": ""},
                {"id": "country", "filled": False, "error": "Select a country"},
            ]

    assert required_field_state(Page()) == [
        {"id": "first_name", "filled": True, "error": ""},
        {"id": "country", "filled": False, "error": "Select a country"},
    ]


def test_fill_field_retries_transient_combobox_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tentativas: list[str] = []

    def escolhe(_page, _target, value):
        tentativas.append(value)
        return len(tentativas) == 2

    monkeypatch.setattr("src.execution.web.form_dom._choose_combobox", escolhe)

    class Pagina:
        def locator(self, _seletor):
            return type(
                "Unica",
                (),
                {
                    "count": lambda self: 1,
                    "first": property(lambda self: object()),
                },
            )()

        def wait_for_timeout(self, _ms: int) -> None:
            return None

    assert fill_field(Pagina(), 5, "combobox", "São Paulo") is True
    assert tentativas == ["São Paulo", "São Paulo"]


def test_closed_linkedin_job_is_expired_without_agent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agente = _route_agent()
    job = JobOpportunity(
        external_id="4492885469",
        status=JobStatus.APPLYING,
        title="Engineer",
        company="Joblet-AI",
    )
    pagina = FakePage("https://www.linkedin.com/jobs/view/4492885469")
    agente.browser = FakeBrowser(pagina)
    monkeypatch.setattr(
        "src.agents.browser_apply.snapshot_form",
        lambda _page: {
            "url": pagina.url,
            "title": "Engineer | LinkedIn",
            "text": "Não aceita mais candidaturas",
        },
    )

    with pytest.raises(JobUnavailable, match="não aceita mais"):
        agente._open(job)
    assert job.status is JobStatus.EXPIRED
