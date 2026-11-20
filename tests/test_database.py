"""Testes da cota diária persistente e da origem da vaga."""
from src.models.domain import JobOpportunity
from src.storage.database import Repository


def test_first_search_query_survives_rediscovery(tmp_path):
    """Termo/localidade gravados são os da PRIMEIRA query que achou a vaga."""
    repo = Repository(tmp_path / "agent.db")
    first = JobOpportunity(
        title=".NET Engineer",
        company="Acme",
        location="Remote - Brazil",
        url="https://www.linkedin.com/jobs/view/111222333",
        external_id="111222333",
        search_query=".NET Engineer",
        search_location="Brazil",
    )
    assert repo.upsert_job(first) is True

    # A mesma vaga reaparece em outro termo da mesma execução.
    again = first.model_copy(
        update={
            "id": "second-card-uuid",
            "search_query": "Backend Developer",
            "search_location": "Latin America",
        }
    )
    assert repo.upsert_job(again) is False

    stored = repo.get_job(first.id)
    assert stored is not None
    assert stored.search_query == ".NET Engineer"
    assert stored.search_location == "Brazil"


def test_empty_original_term_is_filled_on_rediscovery(tmp_path):
    """Registros antigos sem termo ganham a origem no primeiro reencontro."""
    repo = Repository(tmp_path / "agent.db")
    old = JobOpportunity(
        title=".NET Engineer",
        company="Acme",
        location="Remote",
        url="https://www.linkedin.com/jobs/view/444555666",
        external_id="444555666",
    )
    assert repo.upsert_job(old) is True

    again = old.model_copy(
        update={"search_query": ".NET Engineer", "search_location": "Brazil"}
    )
    assert repo.upsert_job(again) is False

    stored = repo.get_job(old.id)
    assert stored is not None
    assert stored.search_query == ".NET Engineer"
    assert stored.search_location == "Brazil"


def test_daily_slot_reserve_release_and_limit(tmp_path):
    repo = Repository(tmp_path / "agent.db")

    assert repo.reserve_daily_slot(2)
    assert repo.reserve_daily_slot(2)
    assert not repo.reserve_daily_slot(2)

    assert repo.get_daily_limit(limit=2).submitted == 2
    assert repo.release_daily_slot()
    assert repo.get_daily_limit(limit=2).submitted == 1
    assert repo.reserve_daily_slot(2)
