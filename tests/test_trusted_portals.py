"""Portais confiáveis são flags na tabela de empresas, com seed idempotente."""
from __future__ import annotations

from src.shared.seed import TRUSTED_PORTALS
from src.storage.database import Repository


def _repo(tmp_path) -> Repository:
    return Repository(tmp_path / "agent.db")


def test_seed_marks_baires_and_strider(tmp_path) -> None:
    repo = _repo(tmp_path)

    assert repo.seed_known_companies() == 2
    portals = {company.name: company for company in repo.list_trusted_portals()}

    assert set(portals) == {"BairesDev", "Strider"}
    assert portals["BairesDev"].has_account is True
    assert portals["BairesDev"].is_trusted_portal is True
    assert ".NET" in portals["BairesDev"].trusted_portal_config["stack"]
    assert portals["Strider"].trusted_portal_config["currencies"] == ["USD"]


def test_seed_is_idempotent_and_preserves_operator_edits(tmp_path) -> None:
    repo = _repo(tmp_path)
    repo.seed_known_companies()
    repo.set_trusted_portal(
        "BairesDev", {**TRUSTED_PORTALS["BairesDev"], "first_run_hours": 99}
    )

    repo.seed_known_companies()

    company = repo.get_company_by_name("BairesDev")
    assert company is not None
    assert company.trusted_portal_config.get("first_run_hours") == 99


def test_reading_a_job_does_not_erase_trusted_flag(tmp_path) -> None:
    from datetime import datetime, timedelta

    from src.models.domain import JobOpportunity

    repo = _repo(tmp_path)
    repo.seed_known_companies()
    job = JobOpportunity(
        title=".NET Developer",
        company="BairesDev",
        external_id="123",
        posted_at=datetime.now() - timedelta(hours=1),
    )

    repo.upsert_job(job)

    company = repo.get_company_by_name("BairesDev")
    assert company is not None
    assert company.is_trusted_portal is True
    assert company.trusted_portal_config["remote_only"] is True
