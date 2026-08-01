"""CliRunner regression tests for `content-machine intelligence weekly-run`,
guarding the fail-closed limitations-overlay wiring THROUGH the CLI (QA gap,
Part D of the 2026-08-01 follow-up ruling).

Before this file, `tests/test_limitations_overlay.py` only unit-tested
`load_limitations_overlay` directly -- nothing in CI proved the CLI command
itself actually aborts BEFORE any output file is written on a bad overlay,
or that the Part B idempotency skip-with-overlay warning actually fires when
invoked end to end. This file closes that gap.

All fixtures are SYNTHETIC only: invented vendor names, example.com-style
references, and invented limitation text -- never anything resembling the
real 2026-W31 run or any real Founder-authored prose.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from content_machine.cli.main import app
from content_machine.intelligence.limitations_overlay import OVERLAY_FILENAME
from content_machine.intelligence.loader import load_profile
from content_machine.intelligence.models import SourceItem
from content_machine.intelligence.weekly import (
    OUTPUT_FILENAMES,
    WeeklyRunResult,
    derive_week_label,
    run_weekly,
)

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"

REFERENCE_DATE = "2026-08-01"
TIMEZONE = "America/Sao_Paulo"
EXECUTION_TIMESTAMP = "2026-08-01T18:00:00-03:00"

# 12 signals, each with its own unique subject_entity_ids/stable_reference,
# so cluster.cluster_items never merges any two of them (see
# test_intelligence_cluster.py for the merge rule: same canonical reference,
# OR shared subject AND title similarity -- neither ever holds here). 12
# single-item topics > TOP_N (10), so exactly 2 are discarded -- needed to
# exercise the "item's containing topic is discarded, not rendered" abort
# condition through a real run, not a hand-built brief.
_SIGNAL_COUNT = 12


def _make_item_dict(n: int) -> dict[str, object]:
    return {
        "item_id": f"cli-item-{n:02d}",
        "source_type": "feed",
        "source_category": "vendor_blog",
        "publisher_id": f"vendor-cli-{n:02d}",
        "subject_entity_ids": [f"vendor-cli-{n:02d}"],
        "title": f"Vendor CLI Test {n:02d} Ships A New Thing",
        "summary_normalized": f"a synthetic summary describing test item number {n:02d}",
        "publication_date": "2026-07-27",
        "detection_date": "2026-07-27",
        "stable_reference": f"https://example.com/vendor-cli-{n:02d}/item",
        "evidence_type": "announcement",
        "change_class": "material_change",
        "change_class_rationale": "synthetic rationale for CLI regression test",
        "action_required": "none",
        "experiment_affordance": "not_testable",
        "topic_tags": [],
        "contains_benefit_or_performance_claim": False,
        "claim_directly_verifiable_in_artifact": False,
    }


def _write_signals(path: Path) -> None:
    payload = [_make_item_dict(n) for n in range(1, _SIGNAL_COUNT + 1)]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _compute_result() -> WeeklyRunResult:
    """Independently compute the same WeeklyRunResult the CLI invocation
    below will compute, so overlay fixtures can be built against REAL
    run_id/item_topic_map/rendered-topic values rather than guessed
    constants. execution_timestamp never feeds run_id (see
    weekly.compute_run_id), so this is safe to compute out of band."""
    signals = [SourceItem.model_validate(_make_item_dict(n)) for n in range(1, _SIGNAL_COUNT + 1)]
    profile = load_profile(PROFILE_FIXTURE)
    week_label = derive_week_label(REFERENCE_DATE, TIMEZONE)
    return run_weekly(
        signals=signals,
        profile=profile,
        prior_library=[],
        week_label=week_label,
        reference_date=REFERENCE_DATE,
        timezone=TIMEZONE,
        execution_timestamp=EXECUTION_TIMESTAMP,
    )


def _invoke(signals_path: Path, output_dir: Path, *, regenerate: bool = False) -> object:
    args = [
        "intelligence",
        "weekly-run",
        "--signals",
        str(signals_path),
        "--profile",
        str(PROFILE_FIXTURE),
        "--reference-date",
        REFERENCE_DATE,
        "--timezone",
        TIMEZONE,
        "--output-dir",
        str(output_dir),
    ]
    if regenerate:
        args.append("--regenerate")
    return runner.invoke(app, args)


def _write_overlay(path: Path, **fields: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": "placeholder",
        "provenance": "human_authored",
        "limitations": {},
    }
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _assert_no_output_files_written(output_dir: Path) -> None:
    for name in OUTPUT_FILENAMES:
        assert not (output_dir / name).exists(), f"{name} must not exist after an aborted run"


# --- shared fixture state (computed once per test via the helper above) ----


def _setup(tmp_path: Path) -> tuple[Path, Path, object]:
    signals_path = tmp_path / "signals.json"
    _write_signals(signals_path)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    result = _compute_result()
    return signals_path, output_dir, result


# --- six abort conditions, through the real CLI -----------------------------


def test_cli_aborts_on_unknown_item_id(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id=result.manifest.run_id,
        limitations={"item-does-not-exist": "a synthetic invented limitation"},
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "item-does-not-exist" in invocation.output
    assert "a synthetic invented limitation" not in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_item_whose_containing_topic_was_discarded(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    rendered_topic_ids = {i.topic_id for i in result.brief.tier1}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier2}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier3}
    discarded_item_id = next(
        item_id
        for item_id, topic_id in result.item_topic_map.items()
        if topic_id not in rendered_topic_ids
    )
    assert discarded_item_id  # sanity: the 12-signal fixture always discards 2

    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id=result.manifest.run_id,
        limitations={discarded_item_id: "a synthetic invented limitation"},
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "not rendered in the brief" in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_duplicate_item_id_key(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    target_item_id = next(iter(result.item_topic_map))
    raw = (
        f'{{"schema_version": 1, "run_id": "{result.manifest.run_id}", '
        '"provenance": "human_authored", "limitations": '
        f'{{"{target_item_id}": "first text", "{target_item_id}": "second text"}}}}'
    )
    (output_dir / OVERLAY_FILENAME).write_text(raw, encoding="utf-8")
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "duplicate key" in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_empty_limitation_text(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    target_item_id = next(iter(result.item_topic_map))
    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id=result.manifest.run_id,
        limitations={target_item_id: "   "},
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "empty or whitespace-only" in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_unparseable_overlay_file(tmp_path: Path) -> None:
    signals_path, output_dir, _result = _setup(tmp_path)
    (output_dir / OVERLAY_FILENAME).write_text("{not valid json at all", encoding="utf-8")
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "unparseable" in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_schema_invalid_overlay_file(tmp_path: Path) -> None:
    signals_path, output_dir, _result = _setup(tmp_path)
    (output_dir / OVERLAY_FILENAME).write_text(
        json.dumps({"schema_version": 1, "provenance": "human_authored", "limitations": {}}),
        encoding="utf-8",
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "schema-invalid" in invocation.output
    _assert_no_output_files_written(output_dir)


def test_cli_aborts_on_run_id_mismatch(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    target_item_id = next(iter(result.item_topic_map))
    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id="run-some-other-run-entirely",
        limitations={target_item_id: "a synthetic invented limitation"},
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code != 0
    assert "does not match the run being rendered" in invocation.output
    _assert_no_output_files_written(output_dir)


# --- valid-overlay path: composes into brief.md, brief.json unaffected -----


def test_cli_valid_overlay_writes_limitation_into_brief_md_only(tmp_path: Path) -> None:
    signals_path, output_dir, result = _setup(tmp_path)
    rendered_topic_ids = {i.topic_id for i in result.brief.tier1}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier2}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier3}
    target_item_id = next(
        item_id
        for item_id, topic_id in result.item_topic_map.items()
        if topic_id in rendered_topic_ids
    )

    # Baseline: a no-overlay run over the SAME signals, into a sibling dir.
    baseline_dir = tmp_path / "baseline_out"
    baseline_dir.mkdir()
    baseline_invocation = _invoke(signals_path, baseline_dir)
    assert baseline_invocation.exit_code == 0
    baseline_brief_json = (baseline_dir / "brief.json").read_text(encoding="utf-8")

    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id=result.manifest.run_id,
        limitations={target_item_id: "a synthetic invented limitation for this one item"},
    )
    invocation = _invoke(signals_path, output_dir)
    assert invocation.exit_code == 0

    brief_md = (output_dir / "brief.md").read_text(encoding="utf-8")
    assert (
        "- **Founder-noted limitation (human-authored):** "
        "a synthetic invented limitation for this one item"
    ) in brief_md

    brief_json = (output_dir / "brief.json").read_text(encoding="utf-8")
    assert brief_json == baseline_brief_json

    manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["limitations_overlay"]["present"] is True
    assert manifest["limitations_overlay"]["item_count"] == 1


# --- Part B: the skip-with-overlay warning actually fires -------------------


def test_cli_skip_with_overlay_warns_loudly_and_names_regenerate(tmp_path: Path) -> None:
    """A completed run already on disk WITHOUT the overlay applied; a
    Founder then places a valid overlay and re-runs WITHOUT --regenerate.
    write_weekly_run_outputs skips (idempotent, matching run_id) -- the CLI
    must warn loudly that the on-disk brief.md does NOT carry the overlay
    and name --regenerate as the remedy, per the Part B ruling."""
    signals_path, output_dir, result = _setup(tmp_path)

    # First run: no overlay present yet -- writes the baseline run to disk.
    first = _invoke(signals_path, output_dir)
    assert first.exit_code == 0
    manifest_before = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest_before["limitations_overlay"]["present"] is False
    brief_md_before = (output_dir / "brief.md").read_text(encoding="utf-8")

    # Now a Founder places a valid overlay and re-runs WITHOUT --regenerate.
    rendered_topic_ids = {i.topic_id for i in result.brief.tier1}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier2}
    rendered_topic_ids |= {i.topic_id for i in result.brief.tier3}
    target_item_id = next(
        item_id
        for item_id, topic_id in result.item_topic_map.items()
        if topic_id in rendered_topic_ids
    )
    _write_overlay(
        output_dir / OVERLAY_FILENAME,
        run_id=result.manifest.run_id,
        limitations={target_item_id: "a synthetic invented limitation for this one item"},
    )

    second = _invoke(signals_path, output_dir)
    assert second.exit_code == 0  # a WARNING, never an error
    assert "WARNING" in second.output
    assert "--regenerate" in second.output
    assert "does NOT carry it" in second.output or "does not carry" in second.output.lower()

    # The skip really was a no-op: brief.md on disk is untouched.
    assert (output_dir / "brief.md").read_text(encoding="utf-8") == brief_md_before
    assert (
        "a synthetic invented limitation for this one item"
        not in (output_dir / "brief.md").read_text(encoding="utf-8")
    )

    # --regenerate actually applies it and clears the divergence.
    third = _invoke(signals_path, output_dir, regenerate=True)
    assert third.exit_code == 0
    assert "WARNING" not in third.output
    assert (
        "a synthetic invented limitation for this one item"
        in (output_dir / "brief.md").read_text(encoding="utf-8")
    )
    manifest_after = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest_after["limitations_overlay"]["present"] is True
