"""Tests for `content-machine audience inspect FILE --dry-run`.

The inspection must reveal STRUCTURE (types, sizes, column names, counts) but
never a single cell value, and must require the --dry-run flag.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from content_machine.cli.main import app

runner = CliRunner()

_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On,Extra Notes"
# Every cell is a sentinel that must NOT appear in the inspection output.
_ROW = (
    "SENTINELFIRST,SENTINELLAST,https://sentinel.example/u,"
    "sentinel@example.com,SENTINELCO,SENTINELTITLE,01 Jan 2020,SENTINELNOTE"
)

_SENTINELS = [
    "SENTINELFIRST",
    "SENTINELLAST",
    "SENTINELCO",
    "SENTINELTITLE",
    "SENTINELNOTE",
    "sentinel",
]


def _fixture(tmp_path: Path) -> Path:
    path = tmp_path / "external.csv"
    path.write_text(_HEADER + "\n" + _ROW + "\n", encoding="utf-8")
    return path


def test_inspect_requires_dry_run(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path)])
    assert result.exit_code == 1
    assert "--dry-run" in result.output


def test_inspect_dry_run_exits_zero_and_shows_structure(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    assert result.exit_code == 0
    out = result.output
    # Structural facts are present.
    assert "File type: CSV" in out
    assert "Data rows: 1" in out
    assert "Network access: none (offline by design)" in out
    assert "Source file copied: no" in out
    # Column names (safe) appear.
    assert "First Name" in out
    assert "Extra Notes" in out  # the unmapped/ignored column
    # Transformations listed.
    for step in ("normalize", "dedup", "pseudonymize", "classify", "aggregate"):
        assert step in out
    # Generic output names, never the input path baked into a persisted name.
    assert "./audience-report.md" in out


def test_inspect_never_prints_any_cell_value(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    assert result.exit_code == 0
    out = result.output
    for sentinel in _SENTINELS:
        assert sentinel not in out, f"leaked cell value: {sentinel}"
    # No email or URL characters from any value.
    assert "@" not in out
    assert "http" not in out


def test_inspect_reports_removed_identifiers(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    out = result.output
    assert "REMOVED at anonymization" in out
    for ident in ("first_name", "last_name", "email", "url"):
        assert ident in out


def test_inspect_bad_file_exits_one(tmp_path: Path) -> None:
    missing = tmp_path / "nope.csv"
    result = runner.invoke(app, ["audience", "inspect", str(missing), "--dry-run"])
    assert result.exit_code == 1
