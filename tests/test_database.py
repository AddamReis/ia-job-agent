"""Testes da cota diária persistente."""
from src.storage.database import Repository


def test_daily_slot_reserve_release_and_limit(tmp_path):
    repo = Repository(tmp_path / "agent.db")

    assert repo.reserve_daily_slot(2)
    assert repo.reserve_daily_slot(2)
    assert not repo.reserve_daily_slot(2)

    assert repo.get_daily_limit(limit=2).submitted == 2
    assert repo.release_daily_slot()
    assert repo.get_daily_limit(limit=2).submitted == 1
    assert repo.reserve_daily_slot(2)
