"""Seeds idempotentes — nunca sobrescrevem dados já ajustados pelo operador."""
from __future__ import annotations

from typing import Any

# Portais onde o cadastro e a proposta salarial já estão aprovados. A validação
# é simplificada (fast lane); o classificador completo não é necessário.
TRUSTED_PORTALS: dict[str, dict[str, Any]] = {
    "BairesDev": {
        "stack": [".NET", "C#"],
        "roles": ["backend", "full stack", "software developer", "software engineer"],
        "remote_only": True,
        "currencies": ["USD"],
        "language": "english",
        "approval_mode": "per_submission",
        "account_ready": True,
    },
    "Strider": {
        "stack": [".NET", "C#"],
        "roles": ["backend", "full stack", "software developer", "software engineer"],
        "remote_only": True,
        "currencies": ["USD"],
        "language": "english",
        "approval_mode": "per_submission",
        "account_ready": True,
    },
}

# Top 10 europeus que mais contratam tecnologia/estrangeiros + Irlanda (Dublin),
# que o operador pediu explicitamente mesmo fora do top 10.
EUROPE_LOCATIONS: list[str] = [
    "Germany",
    "United Kingdom",
    "Netherlands",
    "France",
    "Switzerland",
    "Spain",
    "Portugal",
    "Sweden",
    "Denmark",
    "Finland",
    "Ireland",
]

REGION_LOCATIONS: list[str] = ["LATAM", "Latin America", *EUROPE_LOCATIONS]

__all__ = ["TRUSTED_PORTALS", "EUROPE_LOCATIONS", "REGION_LOCATIONS"]
