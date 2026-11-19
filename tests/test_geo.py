"""Normalização de localização: país - informação original da vaga."""
from __future__ import annotations

import pytest

from src.core.geo import detect_country, normalize_location


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("Manaus, AM", "Brazil", "Brasil - Manaus, AM"),
        ("Salvador, BA", "Brazil", "Brasil - Salvador, BA"),
        ("Bethesda, MD", "United States", "Estados Unidos - Bethesda, MD"),
        ("Cambridge, MA", "United States", "Estados Unidos - Cambridge, MA"),
        ("Southlake, TX", "United States", "Estados Unidos - Southlake, TX"),
        ("São Luís, MA", "Brazil", "Brasil - São Luís, MA"),
        ("Lisbon, Portugal", "Portugal", "Portugal - Lisbon"),
        ("São Paulo, Brazil (Remote)", "Brazil", "Brasil - São Paulo (Remote)"),
        ("Remote", "", "Remote"),
        ("LATAM", "Latin America", "América Latina"),
        ("Rome", "Italy", "Itália - Rome"),
        ("Sweden", "Sweden", "Suécia"),
        ("", "", ""),
    ],
)
def test_normalize_location(raw: str, country: str, expected: str) -> None:
    assert detect_country(raw) == country
    assert normalize_location(raw) == expected


def test_normalize_location_is_idempotent() -> None:
    """Rodar duas vezes não duplica o separador (importante no retrofill)."""
    once = normalize_location("United States - Bethesda MD")
    assert once == "Estados Unidos - Bethesda MD"
    assert normalize_location(once) == once
    assert normalize_location("Brasil - Manaus, AM") == "Brasil - Manaus, AM"


def test_brazilian_state_does_not_hijack_latam() -> None:
    # "AM" é UF, mas "LATAM" precisa continuar sendo região.
    assert detect_country("LATAM - Remote") == "Latin America"
