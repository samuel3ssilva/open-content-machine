"""Tests for the CSV loader: encoding fallback, preamble skip, header variants."""

from __future__ import annotations

from pathlib import Path

import pytest

from content_machine.ingestion.csv_loader import CsvLoadError, load_csv

_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_loads_synthetic_example(synthetic_csv: Path) -> None:
    result = load_csv(synthetic_csv)
    assert result.encoding_used == "utf-8-sig"
    assert result.skipped_preamble_lines == 3
    # 31 body lines minus the fully empty trailing row (collected as an issue).
    assert len(result.rows) == 30
    assert set(result.columns_present) == {
        "first_name",
        "last_name",
        "url",
        "email",
        "company",
        "position",
        "connected_on",
    }
    assert any(i.kind == "empty_row" for i in result.issues)


def test_encoding_fallback_latin1(tmp_path: Path) -> None:
    # A byte (0xE9 = 'é' in latin-1) that is not valid standalone UTF-8.
    content = (_HEADER + "\nJos\xe9,Silva,,,Acme,Engineer,01 Jan 2020\n").encode("latin-1")
    path = _write(tmp_path / "latin.csv", content)
    result = load_csv(path)
    assert result.encoding_used == "latin-1"
    assert result.rows[0].first_name == "Jos\xe9"


def test_utf8_sig_bom_stripped(tmp_path: Path) -> None:
    content = (_HEADER + "\nAna,Exemplo,,,Acme,CEO,01 Jan 2020\n").encode("utf-8-sig")
    path = _write(tmp_path / "bom.csv", content)
    result = load_csv(path)
    assert result.encoding_used == "utf-8-sig"
    # The BOM must not leak into the first header/field.
    assert result.rows[0].first_name == "Ana"


def test_preamble_skipped(tmp_path: Path) -> None:
    content = (
        "Notes:\n"
        '"Some informational line."\n'
        "\n"
        + _HEADER
        + "\nAna,Exemplo,,,Acme,CEO,01 Jan 2020\n"
    ).encode("utf-8")
    path = _write(tmp_path / "preamble.csv", content)
    result = load_csv(path)
    assert result.skipped_preamble_lines == 3
    assert len(result.rows) == 1


def test_missing_columns_tolerated(tmp_path: Path) -> None:
    # Only three columns present; the rest must be absent, not empty.
    content = (b"First Name,Last Name,Company\nAna,Exemplo,Acme\n")
    path = _write(tmp_path / "partial.csv", content)
    result = load_csv(path)
    assert set(result.columns_present) == {"first_name", "last_name", "company"}
    row = result.rows[0]
    assert row.first_name == "Ana"
    assert row.email is None
    assert row.url is None
    # No missing_value issues, because those columns were absent, not empty.
    assert all(i.column not in {"email", "url"} for i in result.issues)


def test_header_variants(tmp_path: Path) -> None:
    content = (
        b"first_name,LAST NAME,E-mail,Organization,Title,Connection Date\n"
        b"Ana,Exemplo,ana@example.com,Acme,Engineer,2020-01-01\n"
    )
    path = _write(tmp_path / "variants.csv", content)
    result = load_csv(path)
    assert set(result.columns_present) == {
        "first_name",
        "last_name",
        "email",
        "company",
        "position",
        "connected_on",
    }
    row = result.rows[0]
    assert row.email == "ana@example.com"
    assert row.company == "Acme"


def test_missing_value_issue_has_no_field_value(tmp_path: Path) -> None:
    content = (_HEADER + "\nAna,Exemplo,,,Acme,,01 Jan 2020\n").encode("utf-8")
    path = _write(tmp_path / "gap.csv", content)
    result = load_csv(path)
    position_issues = [i for i in result.issues if i.column == "position"]
    assert position_issues
    # Message must reference the column, never a value.
    assert "position" in position_issues[0].message
    assert "Acme" not in position_issues[0].message


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CsvLoadError):
        load_csv(tmp_path / "nope.csv")


def test_rejects_binary(tmp_path: Path) -> None:
    path = _write(tmp_path / "bin.dat", b"\x00\x01\x02binary\x00")
    with pytest.raises(CsvLoadError):
        load_csv(path)


def test_rejects_no_header(tmp_path: Path) -> None:
    path = _write(tmp_path / "nohdr.csv", b"just,some,random\nvalues,here,now\n")
    with pytest.raises(CsvLoadError):
        load_csv(path)
