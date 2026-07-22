"""Tests for the Typer CLI via CliRunner."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from content_machine import __version__
from content_machine.cli.main import app
from tests.conftest import SYNTHETIC_CSV

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Open Content Machine" in result.stdout


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_validate_exit_zero() -> None:
    result = runner.invoke(app, ["audience", "validate", str(SYNTHETIC_CSV)])
    assert result.exit_code == 0
    assert "Rows parsed: 30" in result.stdout
    assert "Duplicates detected: 2" in result.stdout


def test_validate_bad_file_exit_one() -> None:
    result = runner.invoke(app, ["audience", "validate", "does-not-exist.csv"])
    assert result.exit_code == 1


def test_report_stdout_markdown() -> None:
    result = runner.invoke(app, ["audience", "report", str(SYNTHETIC_CSV)])
    assert result.exit_code == 0
    assert "# Audience Report" in result.stdout


def test_report_writes_files(tmp_path: Path) -> None:
    md = tmp_path / "r.md"
    js = tmp_path / "r.json"
    result = runner.invoke(
        app,
        ["audience", "report", str(SYNTHETIC_CSV), "-o", str(md), "--json", str(js)],
    )
    assert result.exit_code == 0
    assert md.exists() and js.exists()
    assert "# Audience Report" in md.read_text()


def test_anonymize_writes_json(tmp_path: Path) -> None:
    out = tmp_path / "anon.json"
    result = runner.invoke(
        app, ["audience", "anonymize", str(SYNTHETIC_CSV), "-o", str(out)]
    )
    assert result.exit_code == 0
    content = out.read_text()
    assert "@" not in content
    assert "https://" not in content


def test_demo() -> None:
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0
    assert "# Audience Report" in result.stdout
