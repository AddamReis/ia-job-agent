"""Testes de empresas, dependências e link público de cadastro."""
from __future__ import annotations

from pathlib import Path

from src.agents.browser_research import BrowserResearchAgent
from src.agents.dependencies import DependencyAgent
from src.core.config import Settings
from src.core.events import EventBus
from src.execution.web.linkedin_public import extract_external_links
from src.models.domain import Company, JobOpportunity
from src.storage.database import Repository

DETAIL_WITH_LINK = """
<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-20">
  <p>Candidate-se pelo link: <a href="https://jobs.bairesdev.com/apply/123">Apply here</a>.</p>
  <p><a href="https://www.bairesdev.com/privacy">Política de privacidade</a></p>
</div>
<span class="posted-time-ago__text">2 hours ago</span>
"""


class DetailBrowser:
    def close(self) -> None:
        pass

    def search_page(self, query: str, location: str = "", start: int = 0) -> str:
        return ""

    def job_page(self, url: str) -> str:
        return DETAIL_WITH_LINK


def _settings(tmp_path: Path) -> Settings:
    base = Settings.load()
    return base.model_copy(
        update={
            "storage": base.storage.model_copy(
                update={
                    "database": str(tmp_path / "agent.db"),
                    "control_file": str(tmp_path / "control.json"),
                }
            )
        }
    )


def test_extract_external_links_filters_institutional() -> None:
    links = extract_external_links(DETAIL_WITH_LINK)
    assert links == [{"url": "https://jobs.bairesdev.com/apply/123", "text": "Apply here"}]


def test_seed_known_companies(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "agent.db")
    assert repo.seed_known_companies() == 2
    assert repo.seed_known_companies() == 0  # idempotente
    baires = repo.get_company_by_name("BairesDev")
    strider = repo.get_company_by_name("Strider")
    assert baires is not None and baires.has_account
    assert baires.account_filled_at is None
    assert baires.technical_test == "known"
    assert strider is not None and strider.technical_test == "none"
    assert strider.last_form_filled_at is None


def test_upsert_job_links_company_id(tmp_path: Path) -> None:
    """O vínculo empresa ↔ vaga acontece na gravação, não só no APPLY."""
    repo = Repository(tmp_path / "agent.db", known_portals=["BairesDev"])
    job = JobOpportunity(company="BairesDev", title=".NET Engineer", location="Remote")

    assert repo.upsert_job(job) is True
    company = repo.get_company_by_name("BairesDev")
    assert company is not None
    assert job.company_id == company.id
    assert company.has_account  # portal conhecido
    saved = repo.list_jobs()[0]
    assert saved.company_id == company.id


def test_update_job_links_company_id_for_legacy_rows(tmp_path: Path) -> None:
    """Vagas antigas sem vínculo são ligadas ao regravar."""
    repo = Repository(tmp_path / "agent.db")
    job = JobOpportunity(company="Empresa Antiga", title="Dev", location="Remote")
    repo.upsert_job(job)
    job.company_id = ""
    repo.update_job(job)

    company = repo.get_company_by_name("Empresa Antiga")
    assert company is not None
    assert repo.list_jobs()[0].company_id == company.id


def test_upsert_company_does_not_erase_manual_dates(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "agent.db")
    original = Company(name="Nova Empresa", has_account=True)
    saved = repo.upsert_company(original)
    from datetime import datetime

    saved.last_form_filled_at = datetime(2026, 11, 17, 12, 0, 0)
    repo.upsert_company(saved)
    reread = repo.get_company_by_name("Nova Empresa")
    assert reread is not None
    assert reread.has_account
    assert reread.last_form_filled_at == datetime(2026, 11, 17, 12, 0, 0)


def test_dependency_classification_splits_human_and_agent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = Repository(tmp_path / "agent.db")
    agent = DependencyAgent(repo, settings, EventBus())
    job = JobOpportunity(
        company="BairesDev",
        title=".NET Engineer",
        description=(
            "Record a video with a 15 second self introduction. "
            "After that, complete a technical quiz with multiple choice questions."
        ),
    )
    repo.upsert_job(job)
    created = agent.classify(job)
    kinds = {dep.kind for dep in created}
    assert kinds == {"video_self_intro", "technical_test_quiz"}
    by_kind = {dep.kind: dep for dep in created}
    assert by_kind["video_self_intro"].owner == "human"
    assert by_kind["technical_test_quiz"].owner == "agent"
    assert job.company_id
    assert agent.classify(job) == []  # idempotente


def test_browser_agent_persists_external_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.search = settings.search.model_copy(
        update={"queries": [".NET Engineer"], "locations": ["Brazil"]}
    )
    repo = Repository(tmp_path / "agent.db")
    from tests.test_browser_research import SEARCH_HTML

    class SearchBrowser(DetailBrowser):
        def search_page(self, query: str, location: str = "", start: int = 0) -> str:
            return SEARCH_HTML

    agent = BrowserResearchAgent(SearchBrowser(), repo, settings)  # type: ignore[arg-type]
    assert agent.run() == 1
    job = repo.list_jobs()[0]
    assert job.external_apply_url == "https://jobs.bairesdev.com/apply/123"
    assert "Apply" in job.external_apply_source
