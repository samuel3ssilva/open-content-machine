"""Dedicated privacy-guarantee suite.

These tests exist independently of the unit-level tests in
``test_anonymizer.py`` / ``test_csv_loader.py`` / ``test_report.py`` so that
the project's core promises (SECURITY.md, docs/privacy.md) have a single,
obviously-named place to live and fail loudly:

- private-data paths are actually git-ignored;
- the full validate -> anonymize -> report pipeline never emits identifiers;
- CLI error paths never echo field values, only structural information;
- the anonymized model shape is a strict allowlist.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from content_machine.audience.normalize import normalize
from content_machine.audience.report import analyze, to_json, to_markdown
from content_machine.cli.main import app
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import AnonymizedConnection, anonymize, strip_for_model
from tests.conftest import REPO_ROOT, SYNTHETIC_CSV

runner = CliRunner()

# Paths that must be git-ignored per SECURITY.md's release checklist and
# .gitignore's PRIVACY-CRITICAL block. Deliberately synthetic/nonexistent
# paths: git check-ignore works on path syntax, not file existence.
_PRIVATE_PATHS = [
    "data/private/anything.csv",
    ".env",
    "SomeName_LinkedInDataExport_2026.zip/Connections.csv",
    "foo/Connections.csv",
]


def _git_usable() -> bool:
    """True if a ``git`` binary exists and REPO_ROOT is inside a work tree."""
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


_GIT_USABLE = _git_usable()


@pytest.mark.skipif(not _GIT_USABLE, reason="git is not available in this environment")
@pytest.mark.parametrize("relative_path", _PRIVATE_PATHS)
def test_private_paths_are_git_ignored(relative_path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative_path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{relative_path!r} is NOT git-ignored (git check-ignore exit "
        f"{result.returncode}); .gitignore may have regressed."
    )


def _synthetic_surnames() -> list[str]:
    """Distinct last names actually present in the shipped fixture.

    Read dynamically (via the real loader) rather than hardcoded so this test
    cannot silently rot if the fixture changes.
    """
    load = load_csv(SYNTHETIC_CSV)
    names = sorted({row.last_name for row in load.rows if row.last_name})
    assert names, "fixture unexpectedly has no last names to check against"
    return names


def test_full_pipeline_outputs_contain_no_identifiers(tmp_path: Path) -> None:
    load = load_csv(SYNTHETIC_CSV)
    norm = normalize(load)
    anon = anonymize(norm, salt="privacy-guarantee-test-salt")
    report = analyze(anon, load, norm)

    anonymized_json = json.dumps([c.model_dump() for c in anon.connections], indent=2)
    report_md = to_markdown(report)
    report_json = to_json(report)

    # Write to tmp_path outputs, mirroring what the CLI would produce on disk.
    (tmp_path / "anonymized.json").write_text(anonymized_json, encoding="utf-8")
    (tmp_path / "report.md").write_text(report_md, encoding="utf-8")
    (tmp_path / "report.json").write_text(report_json, encoding="utf-8")

    surnames = _synthetic_surnames()

    blobs = {
        "anonymized.json": (tmp_path / "anonymized.json").read_text(encoding="utf-8"),
        "report.md": (tmp_path / "report.md").read_text(encoding="utf-8"),
        "report.json": (tmp_path / "report.json").read_text(encoding="utf-8"),
    }
    for label, blob in blobs.items():
        assert "@" not in blob, f"{label} contains an '@' (possible email leak)"
        assert "http://" not in blob, f"{label} contains an http:// URL"
        assert "https://" not in blob, f"{label} contains an https:// URL"
        for surname in surnames:
            assert surname not in blob, f"synthetic surname {surname!r} leaked into {label}"

    parsed_rows = json.loads(blobs["anonymized.json"])
    forbidden_keys = {"first_name", "last_name", "email", "url"}
    for row in parsed_rows:
        leaked = forbidden_keys & row.keys()
        assert not leaked, f"anonymized JSON row has forbidden keys: {leaked}"


def test_validate_never_echoes_misplaced_email_like_value(tmp_path: Path) -> None:
    """A value that looks like an email, sitting in the wrong column, plus an
    unmapped extra column (both trigger issue messages) must never appear in
    CLI output -- only structural counts and column names may.
    """
    # Uses the example.com domain so this synthetic marker itself never trips
    # the "non-example email address" scan in the CI security checklist.
    marker = "leaky.value+marker@example.com"
    header = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
    # `marker` sits in the Company column (wrong place for an email-shaped
    # value) and again as a trailing, unmapped 8th column.
    body = f"Ana,Exemplo,,,{marker},CEO,01 Jan 2020,{marker}"
    path = tmp_path / "leaky.csv"
    path.write_text(header + "\n" + body + "\n", encoding="utf-8")

    result = runner.invoke(app, ["audience", "validate", str(path)])

    assert marker not in result.output
    # Sanity: the issue machinery did actually fire, so this is a real check.
    assert "parse_error" in result.output


def test_validate_failure_never_echoes_binary_content(tmp_path: Path) -> None:
    marker = b"super-secret-should-never-be-echoed"
    path = tmp_path / "garbage.dat"
    path.write_bytes(b"\x00\x01" + marker + b"\x00\x02")

    result = runner.invoke(app, ["audience", "validate", str(path)])

    assert result.exit_code == 1
    assert marker.decode() not in result.output


def test_strip_for_model_allowlist_only() -> None:
    load = load_csv(SYNTHETIC_CSV)
    norm = normalize(load)
    anon = anonymize(norm, salt="privacy-guarantee-test-salt")
    stripped = strip_for_model(anon.connections)
    assert stripped, "expected at least one row to survive stripping"
    for row in stripped:
        assert set(row.keys()) == {"company", "position"}


def test_anonymized_connection_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AnonymizedConnection(id="id_deadbeef00000000", company="Acme", email="x@example.com")  # type: ignore[call-arg]
