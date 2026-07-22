"""Loader tests for realistic export variations:

- explicit column-order independence;
- localized (Portuguese/Spanish) header aliases and pt date parsing;
- clear failure for unsupported formats (XLSX/ZIP, JSON, alien headers).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_machine.audience.normalize import normalize
from content_machine.ingestion.csv_loader import CsvLoadError, load_csv
from tests.conftest import REPO_ROOT

_VARIANTS = REPO_ROOT / "examples" / "synthetic-connections-variants"


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_column_order_independence(tmp_path: Path) -> None:
    # Same data, columns in a scrambled order -> identical field mapping.
    a = _write(
        tmp_path / "a.csv",
        b"First Name,Last Name,Company,Position\nAna,Exemplo,Acme,CEO\n",
    )
    b = _write(
        tmp_path / "b.csv",
        b"Position,Company,Last Name,First Name\nCEO,Acme,Exemplo,Ana\n",
    )
    ra = load_csv(a)
    rb = load_csv(b)
    assert ra.rows[0].first_name == rb.rows[0].first_name == "Ana"
    assert ra.rows[0].last_name == rb.rows[0].last_name == "Exemplo"
    assert ra.rows[0].company == rb.rows[0].company == "Acme"
    assert ra.rows[0].position == rb.rows[0].position == "CEO"
    assert set(ra.columns_present) == set(rb.columns_present)


def test_scrambled_fixture_with_missing_optional_columns() -> None:
    result = load_csv(_VARIANTS / "scrambled-order-missing-cols.csv")
    assert set(result.columns_present) == {
        "first_name",
        "last_name",
        "company",
        "position",
    }
    # Optional columns are absent, not empty.
    assert result.rows[0].email is None
    assert result.rows[0].url is None
    assert result.rows[0].connected_on is None
    assert result.rows[0].first_name == "Ana"


def test_portuguese_localized_headers_and_dates() -> None:
    result = load_csv(_VARIANTS / "pt-localized.csv")
    assert set(result.columns_present) == {
        "first_name",
        "last_name",
        "url",
        "email",
        "company",
        "position",
        "connected_on",
    }
    norm = normalize(result)
    years = [c.connected_year for c in norm.connections]
    # pt month abbreviations parse: jan, mar, mai, ago, fev, out, dez.
    assert 2023 in years and 2019 in years and 2020 in years
    # The final row has an unparseable date -> None + a recorded issue.
    assert years[-1] is None
    assert any(i.column == "connected_on" for i in norm.issues)


@pytest.mark.parametrize(
    ("headers", "value"),
    [
        (b"Primeiro nome,Sobrenome,Empresa,Cargo", "Ana"),
        (b"Nombre,Apellidos,Empresa,Cargo", "Ana"),
    ],
)
def test_localized_header_aliases_map(tmp_path: Path, headers: bytes, value: str) -> None:
    path = _write(tmp_path / "loc.csv", headers + b"\n" + value.encode() + b",X,Acme,CEO\n")
    result = load_csv(path)
    assert set(result.columns_present) == {"first_name", "last_name", "company", "position"}
    assert result.rows[0].first_name == value


def test_localized_date_es_and_pt(tmp_path: Path) -> None:
    from content_machine.audience.normalize import parse_connected_year

    assert parse_connected_year("12 jan 2023") == 2023
    assert parse_connected_year("14 mai 2019") == 2019  # pt May
    assert parse_connected_year("01 dez 2022") == 2022  # pt Dec
    assert parse_connected_year("05 ene 2021") == 2021  # es Jan
    assert parse_connected_year("total nonsense") is None


def test_rejects_zip_xlsx_magic_bytes(tmp_path: Path) -> None:
    # ZIP/XLSX local-file-header magic.
    path = _write(tmp_path / "book.xlsx", b"PK\x03\x04\x14\x00\x06\x00salt-of-bytes")
    with pytest.raises(CsvLoadError) as exc:
        load_csv(path)
    assert "ZIP" in str(exc.value) or "XLSX" in str(exc.value)


def test_rejects_json_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "data.json", b'{"connections": [{"first": "Ana"}]}\n')
    with pytest.raises(CsvLoadError) as exc:
        load_csv(path)
    assert "JSON" in str(exc.value)


def test_rejects_json_array(tmp_path: Path) -> None:
    path = _write(tmp_path / "data.json", b'[{"first": "Ana"}]\n')
    with pytest.raises(CsvLoadError) as exc:
        load_csv(path)
    assert "JSON" in str(exc.value)


def test_rejects_alien_headers(tmp_path: Path) -> None:
    path = _write(tmp_path / "alien.csv", b"alpha,beta,gamma\n1,2,3\n")
    with pytest.raises(CsvLoadError) as exc:
        load_csv(path)
    assert "header" in str(exc.value).lower()
