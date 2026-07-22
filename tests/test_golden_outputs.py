"""Golden-output regression test.

Runs the full pipeline in-process on ``examples/synthetic-connections.csv``
and asserts the result equals the checked-in golden files in
``examples/expected-output/``.

## Salt provenance

The salt originally used to generate ``examples/expected-output/report.md``
and ``report.json`` (commit ee811f8, "feat: add synthetic dataset, expected
outputs and exported JSON Schemas") is not recoverable: it was never
committed (by design -- ADR 0003 requires the salt to be user-local and
private, kept only in a git-ignored ``.env``) and is not derivable from git
history.

However, ``AudienceReport`` (see ``content_machine.audience.report``) never
includes per-connection pseudonym ids -- only aggregate counts and
percentages -- so its rendered Markdown/JSON is salt-*independent* by
construction. Confirmed empirically: regenerating both golden files in-process
with the fixed salt below reproduced the existing checked-in files byte for
byte, so no regeneration was necessary in the end.

For future-proofing (e.g. if the report schema ever grows a salt-dependent
field), this test pins a documented canonical salt so the run is fully
reproducible:

    CONTENT_MACHINE_SALT = "open-content-machine-canonical-example-salt"

If a future change makes the report salt-dependent and this test starts
failing only because of the id value, regenerate the golden files with
exactly this salt and update this comment accordingly.
"""

from __future__ import annotations

import json

from content_machine.audience.normalize import normalize
from content_machine.audience.report import analyze, to_json, to_markdown
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import anonymize
from tests.conftest import REPO_ROOT, SYNTHETIC_CSV

_CANONICAL_SALT = "open-content-machine-canonical-example-salt"
_EXPECTED_DIR = REPO_ROOT / "examples" / "expected-output"


def _run_pipeline() -> tuple[str, str]:
    load = load_csv(SYNTHETIC_CSV)
    norm = normalize(load)
    anon = anonymize(norm, salt=_CANONICAL_SALT)
    report = analyze(anon, load, norm)
    return to_markdown(report), to_json(report)


def test_golden_markdown_matches() -> None:
    markdown, _json_text = _run_pipeline()
    expected = (_EXPECTED_DIR / "report.md").read_text(encoding="utf-8")
    assert markdown == expected


def test_golden_json_matches() -> None:
    _markdown, json_text = _run_pipeline()
    expected = (_EXPECTED_DIR / "report.json").read_text(encoding="utf-8")
    # Compare parsed structures (not raw text) so this is robust to a trailing
    # newline either file may or may not have on disk.
    assert json.loads(json_text) == json.loads(expected)


def test_golden_files_exist_and_are_nonempty() -> None:
    md_path = _EXPECTED_DIR / "report.md"
    json_path = _EXPECTED_DIR / "report.json"
    assert md_path.exists() and md_path.stat().st_size > 0
    assert json_path.exists() and json_path.stat().st_size > 0
