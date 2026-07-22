"""Tests for normalization: company suffixes, seniority, dates, duplicates."""

from __future__ import annotations

import pytest

from content_machine.audience.normalize import (
    infer_seniority,
    normalize,
    normalize_company,
    parse_connected_year,
)
from content_machine.ingestion.csv_loader import LoadResult, RawConnection


def _load_result(rows: list[RawConnection]) -> LoadResult:
    return LoadResult(
        rows=rows,
        columns_present=["first_name", "last_name", "company", "position", "connected_on"],
        issues=[],
        encoding_used="utf-8",
        skipped_preamble_lines=0,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Umbrella Robotics Inc", "Umbrella Robotics"),
        ("Fictional Bank S.A.", "Fictional Bank"),
        ("DadosCorp Ltda", "DadosCorp"),
        ("Nimbus Cloud LLC", "Nimbus Cloud"),
        ("Foo Holdings, LLC", "Foo Holdings"),
        ("Acme Analytics", "Acme Analytics"),
        ("  Spaced   Out   Ltd ", "Spaced Out"),
    ],
)
def test_normalize_company(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


def test_normalize_company_none() -> None:
    assert normalize_company(None) is None
    assert normalize_company("   ") is None


@pytest.mark.parametrize(
    ("title", "bucket"),
    [
        # The 7 new buckets: founder_owner, c_level, vp_head_director,
        # manager_lead, individual_contributor, entry_student, unknown. Old
        # senior_ic + ic cases now both bucket as individual_contributor.
        ("Chief Executive Officer", "c_level"),
        ("VP of Marketing", "vp_head_director"),
        ("Director of Engineering", "vp_head_director"),
        ("Engineering Manager", "manager_lead"),
        ("Team Lead", "manager_lead"),
        ("Senior Software Engineer", "individual_contributor"),
        ("Software Engineer", "individual_contributor"),
        ("Founder", "founder_owner"),
        ("Co-Founder", "founder_owner"),
        ("Owner", "founder_owner"),
        ("Student", "entry_student"),
        ("Marketing Intern", "entry_student"),
        ("Financial Controller", "unknown"),
        (None, "unknown"),
    ],
)
def test_infer_seniority(title: str | None, bucket: str) -> None:
    assert infer_seniority(title) == bucket


@pytest.mark.parametrize(
    ("value", "year"),
    [
        ("18 Apr 2024", 2024),
        ("12 Jan 2023", 2023),
        ("2024-04-18", 2024),
        ("2019", 2019),
        ("not a date", None),
        (None, None),
    ],
)
def test_parse_connected_year(value: str | None, year: int | None) -> None:
    assert parse_connected_year(value) == year


def test_duplicate_detection() -> None:
    rows = [
        RawConnection(row_index=0, first_name="Ana", last_name="Exemplo", company="Acme"),
        RawConnection(row_index=1, first_name="Bruno", last_name="Ficticio", company="Acme"),
        RawConnection(row_index=2, first_name="ana", last_name="EXEMPLO", company="acme"),
    ]
    result = normalize(_load_result(rows))
    assert result.duplicate_pairs == [(0, 2)]
    assert result.connections[0].is_duplicate is False
    assert result.connections[2].is_duplicate is True


def test_seniority_is_marked_inferred() -> None:
    rows = [RawConnection(row_index=0, first_name="Ana", last_name="X", position="CEO")]
    result = normalize(_load_result(rows))
    assert result.connections[0].seniority_inferred is True


def test_company_raw_kept() -> None:
    rows = [RawConnection(row_index=0, first_name="Ana", last_name="X", company="Acme Inc")]
    result = normalize(_load_result(rows))
    conn = result.connections[0]
    assert conn.company == "Acme"
    assert conn.company_raw == "Acme Inc"
