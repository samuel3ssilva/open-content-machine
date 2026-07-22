"""Edge-case coverage for the CSV loader: line endings, quoting, encodings,
empty bodies, and non-CSV binary input.

Complements tests/test_csv_loader.py, which covers the "happy path" plus
encoding fallback, preamble skipping, and header-variant matching.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_machine.ingestion.csv_loader import CsvLoadError, load_csv

_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
_ROW = "Ana,Exemplo,,,Acme,CEO,01 Jan 2020"


def _write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_crlf_line_endings(tmp_path: Path) -> None:
    content = (_HEADER + "\r\n" + _ROW + "\r\n").encode("utf-8")
    path = _write(tmp_path / "crlf.csv", content)
    result = load_csv(path)
    assert len(result.rows) == 1
    assert result.rows[0].first_name == "Ana"
    assert result.rows[0].last_name == "Exemplo"


def test_cr_only_line_endings(tmp_path: Path) -> None:
    content = (_HEADER + "\r" + _ROW + "\r").encode("utf-8")
    path = _write(tmp_path / "cr.csv", content)
    result = load_csv(path)
    assert len(result.rows) == 1
    assert result.rows[0].first_name == "Ana"


def test_embedded_quotes_and_commas(tmp_path: Path) -> None:
    # Last name has an embedded, doubled quote; Company has an embedded comma.
    # Both are legal CSV-quoted values and must survive parsing intact.
    row = 'Ana,"Ex""emplo, Jr.",https://x,,"Acme, Inc",CEO,01 Jan 2020'
    content = (_HEADER + "\n" + row + "\n").encode("utf-8")
    path = _write(tmp_path / "quoted.csv", content)
    result = load_csv(path)
    assert len(result.rows) == 1
    row0 = result.rows[0]
    assert row0.last_name == 'Ex"emplo, Jr.'
    assert row0.company == "Acme, Inc"


def test_utf8_bom_combined_with_preamble(tmp_path: Path) -> None:
    text = (
        "Notes:\n"
        '"Some informational preamble line."\n'
        "\n" + _HEADER + "\n" + _ROW + "\n"
    )
    content = text.encode("utf-8-sig")  # prepends the BOM bytes
    path = _write(tmp_path / "bom_preamble.csv", content)
    result = load_csv(path)
    assert result.encoding_used == "utf-8-sig"
    assert result.skipped_preamble_lines == 3
    assert len(result.rows) == 1
    # The BOM must not leak into the first cell of the preamble or the header.
    assert result.rows[0].first_name == "Ana"


def test_header_only_file_yields_zero_rows(tmp_path: Path) -> None:
    content = (_HEADER + "\n").encode("utf-8")
    path = _write(tmp_path / "header_only.csv", content)
    result = load_csv(path)
    assert result.rows == []
    assert result.issues == []
    assert set(result.columns_present) == {
        "first_name",
        "last_name",
        "url",
        "email",
        "company",
        "position",
        "connected_on",
    }


def test_header_only_file_no_trailing_newline(tmp_path: Path) -> None:
    # No trailing newline at all: still zero rows, not an error.
    content = _HEADER.encode("utf-8")
    path = _write(tmp_path / "header_only_no_nl.csv", content)
    result = load_csv(path)
    assert result.rows == []


def test_binary_garbage_with_nul_bytes_fails_gracefully(tmp_path: Path) -> None:
    content = b"\x00\x01\x02\xffPNG-ish-garbage\x00\x89\x50\x4e\x47"
    path = _write(tmp_path / "nul_garbage.bin", content)
    with pytest.raises(CsvLoadError):
        load_csv(path)


def test_binary_garbage_without_nul_bytes_fails_gracefully(tmp_path: Path) -> None:
    # No NUL bytes (so it clears the fast binary check and falls through the
    # encoding chain, decoding via the always-succeeding latin-1 fallback),
    # but it is not plausible CSV -- must fail cleanly, not raise or hang.
    random_bytes = bytes((i * 37 + 5) % 255 + 1 for i in range(300))
    path = _write(tmp_path / "no_nul_garbage.bin", random_bytes)
    with pytest.raises(CsvLoadError):
        load_csv(path)
