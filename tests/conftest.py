"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_CSV = REPO_ROOT / "examples" / "synthetic-connections.csv"

# Distinctive synthetic surnames that must never appear in anonymized
# outputs/reports. We use the invented last names (not first names like "Ana",
# which is an innocent substring of the legitimate company "Acme Analytics").
SYNTHETIC_NAMES = [
    "Exemplo",
    "Ficticio",
    "Sintetica",
    "Inventado",
    "Emulado",
    "Impostado",
    "Conjectural",
]


@pytest.fixture
def synthetic_csv() -> Path:
    return SYNTHETIC_CSV
