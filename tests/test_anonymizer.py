"""Tests for the anonymizer: determinism, identifier removal, forbid extra."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from content_machine.audience.normalize import normalize
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import (
    AnonymizedConnection,
    anonymize,
    pseudonym,
    strip_for_model,
)
from tests.conftest import SYNTHETIC_NAMES


def test_pseudonym_deterministic_same_salt() -> None:
    kwargs = dict(first_name="Ana", last_name="Exemplo", company="Acme", email=None)
    a = pseudonym("salt-1", **kwargs)
    b = pseudonym("salt-1", **kwargs)
    assert a == b
    assert a.startswith("id_")
    assert len(a) == len("id_") + 16


def test_pseudonym_differs_with_salt() -> None:
    kwargs = dict(first_name="Ana", last_name="Exemplo", company="Acme", email=None)
    assert pseudonym("salt-1", **kwargs) != pseudonym("salt-2", **kwargs)


def test_pseudonym_casefold_stable() -> None:
    a = pseudonym("s", first_name="ANA", last_name="exemplo", company="ACME", email=None)
    b = pseudonym("s", first_name="ana", last_name="Exemplo", company="acme", email=None)
    assert a == b


def test_ephemeral_salt_flag_and_instability() -> None:
    load = load_csv_synthetic()
    norm = normalize(load)
    first = anonymize(norm, salt=None)
    second = anonymize(norm, salt=None)
    assert first.ephemeral_salt is True
    # Ephemeral salt => IDs are not stable across runs.
    assert first.connections[0].id != second.connections[0].id


def test_fixed_salt_stable_across_runs() -> None:
    load = load_csv_synthetic()
    norm = normalize(load)
    first = anonymize(norm, salt="fixed")
    second = anonymize(norm, salt="fixed")
    assert first.ephemeral_salt is False
    assert [c.id for c in first.connections] == [c.id for c in second.connections]


def test_output_has_no_identifier_keys() -> None:
    fields = set(AnonymizedConnection.model_fields)
    for forbidden in ("first_name", "last_name", "email", "url", "company_raw"):
        assert forbidden not in fields


def test_output_contains_no_identifier_values() -> None:
    load = load_csv_synthetic()
    norm = normalize(load)
    anon = anonymize(norm, salt="fixed")
    blob = json.dumps([c.model_dump() for c in anon.connections])
    assert "@" not in blob
    assert "http://" not in blob
    assert "https://" not in blob
    for name in SYNTHETIC_NAMES:
        assert name not in blob


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        AnonymizedConnection(id="id_x", company="Acme", secret_name="Ana")  # type: ignore[call-arg]


def test_strip_for_model_only_company_position() -> None:
    load = load_csv_synthetic()
    norm = normalize(load)
    anon = anonymize(norm, salt="fixed")
    stripped = strip_for_model(anon.connections)
    for item in stripped:
        assert set(item.keys()) == {"company", "position"}
    # Rows with neither company nor position are dropped.
    assert all(item["company"] or item["position"] for item in stripped)


def load_csv_synthetic():  # type: ignore[no-untyped-def]
    from tests.conftest import SYNTHETIC_CSV

    return load_csv(SYNTHETIC_CSV)
