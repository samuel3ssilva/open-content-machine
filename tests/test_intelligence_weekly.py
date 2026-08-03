"""Tests for content_machine.intelligence.weekly (M7: the weekly Intelligence
Run engine + CLI foundation). Covers the 7-day window/timezone boundary
convention, the deterministic RunManifest (run_id/input_fingerprint), the
idempotency + atomicity contract of write_weekly_run_outputs, and the
cross-cutting no-network/no-wall-clock/synthetic-only guarantees this
task's ticket requires.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import socket
from datetime import date, timedelta
from pathlib import Path

import pytest

from content_machine.intelligence import weekly as weekly_module
from content_machine.intelligence.library import TopicLibraryEntry
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.models import RelevanceProfile, SourceItem
from content_machine.intelligence.weekly import (
    compute_input_fingerprint,
    compute_run_id,
    derive_week_label,
    filter_signals_to_window,
    is_within_window,
    resolve_window,
    run_weekly,
    write_weekly_run_outputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"

# The shipped synthetic fixture's dates span 2026-06-01 .. 2026-07-11 -- see
# examples/README.md. Reference dates chosen below deliberately land two
# different, both non-empty, 7-day windows over it.
W28_REFERENCE_DATE = "2026-07-12"  # window [2026-07-05, 2026-07-12) -> 7 signals
W27_REFERENCE_DATE = "2026-07-05"  # window [2026-06-28, 2026-07-05) -> 6 signals
TIMEZONE = "America/Sao_Paulo"


def _make_item(**overrides: object) -> SourceItem:
    base: dict[str, object] = {
        "item_id": "base",
        "source_type": "feed",
        "source_category": "vendor_blog",
        "publisher_id": "vendor-base",
        "subject_entity_ids": ["vendor-base"],
        "title": "Base Title",
        "summary_normalized": "a base summary used only for test scaffolding",
        "publication_date": date(2026, 1, 1),
        "detection_date": date(2026, 1, 1),
        "stable_reference": "https://example.com/base",
        "evidence_type": "announcement",
        "change_class": "material_change",
        "change_class_rationale": "n/a",
        "action_required": "none",
        "experiment_affordance": "not_testable",
        "topic_tags": [],
        "contains_benefit_or_performance_claim": False,
        "claim_directly_verifiable_in_artifact": False,
    }
    base.update(overrides)
    return SourceItem.model_validate(base)


def _profile() -> RelevanceProfile:
    return load_profile(PROFILE_FIXTURE)


def _signals() -> list[SourceItem]:
    return load_signals(VALID_FIXTURE).items


def _run(
    *,
    week_label: str = "2026-W28",
    reference_date: str = W28_REFERENCE_DATE,
    timezone: str = TIMEZONE,
    prior_library: list[TopicLibraryEntry] | None = None,
    signals: list[SourceItem] | None = None,
    profile: RelevanceProfile | None = None,
    execution_timestamp: str = "2026-07-12T18:00:00-03:00",
    code_version: str | None = None,
) -> weekly_module.WeeklyRunResult:
    return run_weekly(
        signals=signals if signals is not None else _signals(),
        profile=profile if profile is not None else _profile(),
        prior_library=prior_library if prior_library is not None else [],
        week_label=week_label,
        reference_date=reference_date,
        timezone=timezone,
        execution_timestamp=execution_timestamp,
        code_version=code_version,
    )


# ------------------------------ window + timezone ---------------------------


def test_resolve_window_boundary_convention() -> None:
    """window = [reference_date-7d 00:00, reference_date 00:00), both at
    local midnight in the given timezone -- the frozen convention documented
    in weekly.py's module docstring."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(TIMEZONE)
    window = resolve_window(W28_REFERENCE_DATE, TIMEZONE)
    assert window.end.year == 2026 and window.end.month == 7 and window.end.day == 12
    assert window.end.hour == 0 and window.end.tzinfo == tz
    assert window.start.year == 2026 and window.start.month == 7 and window.start.day == 5
    assert window.end - window.start == timedelta(days=7)


def test_window_boundary_inclusive_start_exclusive_end() -> None:
    """Exact-boundary test: one second before window_start is excluded;
    window_start itself is included; window_end is excluded."""
    window = resolve_window(W28_REFERENCE_DATE, TIMEZONE)

    assert is_within_window(window.start - timedelta(seconds=1), window) is False
    assert is_within_window(window.start, window) is True
    assert is_within_window(window.end - timedelta(seconds=1), window) is True
    assert is_within_window(window.end, window) is False


def test_reference_date_time_of_day_component_is_ignored() -> None:
    """A reference_date's time-of-day (if given) never shifts the window --
    only its calendar date matters (Saturday 18:00 is a CADENCE, not a
    boundary)."""
    date_only = resolve_window(W28_REFERENCE_DATE, TIMEZONE)
    with_time = resolve_window(f"{W28_REFERENCE_DATE}T18:00:00", TIMEZONE)
    assert date_only == with_time


def test_timezone_changes_resolved_window_bounds_deterministically() -> None:
    """Same reference_date, different timezone -> different resolved
    bounds, but the same window LENGTH; applied deterministically (calling
    twice with the same tz gives byte-identical bounds)."""
    window_sp = resolve_window(W28_REFERENCE_DATE, "America/Sao_Paulo")
    window_utc = resolve_window(W28_REFERENCE_DATE, "UTC")

    assert window_sp.start != window_utc.start
    assert window_sp.end != window_utc.end
    assert (window_sp.end - window_sp.start) == (window_utc.end - window_utc.start)

    # Deterministic: repeating with the same inputs gives the same result.
    assert resolve_window(W28_REFERENCE_DATE, "America/Sao_Paulo") == window_sp


def test_invalid_reference_date_raises_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_window("not-a-date", TIMEZONE)


# ------------------------------ signal-level window filtering ---------------


def test_signal_filtering_respects_date_boundary_inclusivity_exclusivity() -> None:
    window = resolve_window(W28_REFERENCE_DATE, TIMEZONE)  # [2026-07-05, 2026-07-12)

    in_at_start = _make_item(
        item_id="in_at_start", publication_date=date(2026, 7, 5), detection_date=date(2026, 7, 5)
    )
    out_before_start = _make_item(
        item_id="out_before_start",
        publication_date=date(2026, 7, 4),
        detection_date=date(2026, 7, 4),
    )
    in_last_day = _make_item(
        item_id="in_last_day",
        publication_date=date(2026, 7, 11),
        detection_date=date(2026, 7, 11),
    )
    out_at_end = _make_item(
        item_id="out_at_end", publication_date=date(2026, 7, 12), detection_date=date(2026, 7, 12)
    )

    kept = filter_signals_to_window(
        [in_at_start, out_before_start, in_last_day, out_at_end], window, TIMEZONE
    )
    assert {item.item_id for item in kept} == {"in_at_start", "in_last_day"}


def test_signal_filtering_falls_back_to_detection_date() -> None:
    window = resolve_window(W28_REFERENCE_DATE, TIMEZONE)
    item = _make_item(
        item_id="no_pub_date", publication_date=None, detection_date=date(2026, 7, 6)
    )
    kept = filter_signals_to_window([item], window, TIMEZONE)
    assert kept == [item]


def test_derive_week_label_deterministic() -> None:
    assert derive_week_label(W28_REFERENCE_DATE, TIMEZONE) == "2026-W28"
    assert derive_week_label(W28_REFERENCE_DATE, TIMEZONE) == derive_week_label(
        W28_REFERENCE_DATE, TIMEZONE
    )


# ------------------------------ run_id / input_fingerprint -------------------


def test_input_fingerprint_is_order_independent() -> None:
    signals = _signals()
    shuffled = list(reversed(signals))
    assert signals != shuffled  # sanity: the shuffle actually changed order
    assert compute_input_fingerprint(signals) == compute_input_fingerprint(shuffled)


def test_run_id_deterministic_same_inputs() -> None:
    fingerprint = compute_input_fingerprint(_signals())
    first = compute_run_id(
        "2026-W28", fingerprint, "0.1.0", "synthetic-1", "2026-07-05T00:00:00-03:00",
        "2026-07-12T00:00:00-03:00",
    )
    second = compute_run_id(
        "2026-W28", fingerprint, "0.1.0", "synthetic-1", "2026-07-05T00:00:00-03:00",
        "2026-07-12T00:00:00-03:00",
    )
    assert first == second


@pytest.mark.parametrize(
    ("changed_field", "other_value"),
    [
        ("week_label", "2026-W29"),
        ("input_fingerprint", "different-fingerprint"),
        ("code_version", "9.9.9"),
        ("profile_version", "synthetic-2"),
        ("window_start", "2026-07-06T00:00:00-03:00"),
        ("window_end", "2026-07-13T00:00:00-03:00"),
    ],
)
def test_run_id_changes_when_any_component_changes(changed_field: str, other_value: str) -> None:
    base_kwargs = {
        "week_label": "2026-W28",
        "input_fingerprint": compute_input_fingerprint(_signals()),
        "code_version": "0.1.0",
        "profile_version": "synthetic-1",
        "window_start": "2026-07-05T00:00:00-03:00",
        "window_end": "2026-07-12T00:00:00-03:00",
    }
    baseline = compute_run_id(**base_kwargs)  # type: ignore[arg-type]
    changed_kwargs = {**base_kwargs, changed_field: other_value}
    changed = compute_run_id(**changed_kwargs)  # type: ignore[arg-type]
    assert baseline != changed


def test_run_id_differs_for_same_week_label_but_different_reference_date() -> None:
    """QA Finding 1 (HIGH): compute_run_id previously omitted the resolved
    window bounds, so two runs whose week_label happened to collide (e.g.
    a Sunday reference_date landing in the same ISO week as the following
    Saturday's default cadence) but whose reference_date/window differed
    produced the SAME run_id despite analyzing DIFFERENT signals and
    producing a DIFFERENT brief -- the idempotency skip in
    write_weekly_run_outputs would then wrongly treat the second, correct
    run as already completed and skip writing it. window_start/window_end
    already encode both reference_date and timezone, so folding them into
    the run_id hash makes same-week/different-window runs get different
    ids."""
    signals = _signals()
    profile = _profile()

    result_sunday = run_weekly(
        signals=signals,
        profile=profile,
        prior_library=[],
        week_label="2026-W28",
        reference_date="2026-07-05",  # a Sunday -- still ISO week 27, chosen
        # deliberately distinct from the Saturday below to prove the two
        # windows (and thus run_ids) differ even though we force the SAME
        # week_label on both calls.
        timezone=TIMEZONE,
        execution_timestamp="2026-07-05T18:00:00-03:00",
    )
    result_saturday = run_weekly(
        signals=signals,
        profile=profile,
        prior_library=[],
        week_label="2026-W28",  # same week_label, forced, on purpose
        reference_date="2026-07-12",
        timezone=TIMEZONE,
        execution_timestamp="2026-07-12T18:00:00-03:00",
    )

    assert result_sunday.manifest.week_label == result_saturday.manifest.week_label
    assert result_sunday.manifest.window_start != result_saturday.manifest.window_start
    assert result_sunday.manifest.run_id != result_saturday.manifest.run_id


def test_idempotency_skip_does_not_mask_a_different_window_same_week_label_run(
    tmp_path: Path,
) -> None:
    """Direct end-to-end regression for QA Finding 1: writing the Sunday run
    first must NOT cause the Saturday run (same week_label, different
    window, different run_id) to be silently skipped as "already
    completed" -- each run_id gets its own write."""
    output_dir = tmp_path / "out"
    signals = _signals()
    profile = _profile()

    result_sunday = run_weekly(
        signals=signals,
        profile=profile,
        prior_library=[],
        week_label="2026-W28",
        reference_date="2026-07-05",
        timezone=TIMEZONE,
        execution_timestamp="2026-07-05T18:00:00-03:00",
    )
    result_saturday = run_weekly(
        signals=signals,
        profile=profile,
        prior_library=[],
        week_label="2026-W28",
        reference_date="2026-07-12",
        timezone=TIMEZONE,
        execution_timestamp="2026-07-12T18:00:00-03:00",
    )
    assert result_sunday.manifest.run_id != result_saturday.manifest.run_id

    first_outcome = write_weekly_run_outputs(result_sunday, output_dir)
    assert first_outcome.wrote is True

    second_outcome = write_weekly_run_outputs(result_saturday, output_dir)
    assert second_outcome.wrote is True  # NOT skipped -- a genuinely different run
    assert second_outcome.run_id == result_saturday.manifest.run_id

    manifest_on_disk = json.loads((output_dir / "run-manifest.json").read_text())
    assert manifest_on_disk["run_id"] == result_saturday.manifest.run_id

    # Re-writing the Saturday run again (the true "same run twice" case) IS
    # correctly skipped.
    third_outcome = write_weekly_run_outputs(result_saturday, output_dir)
    assert third_outcome.wrote is False


# --- R2 (2026-08-01 post-merge-gate round): brief_version/ ------------------
# --- confidence_rubric_version recorded additively, never in run identity --


def test_manifest_records_brief_version_and_confidence_rubric_version() -> None:
    from content_machine.intelligence.brief import BRIEF_VERSION
    from content_machine.intelligence.tiers import CONFIDENCE_RUBRIC_VERSION

    result = _run()
    assert result.manifest.brief_version == BRIEF_VERSION
    assert result.manifest.confidence_rubric_version == CONFIDENCE_RUBRIC_VERSION
    assert result.manifest.brief_version == result.brief.brief_version
    assert result.manifest.confidence_rubric_version == result.brief.confidence_rubric_version


def test_run_id_and_input_fingerprint_unaffected_by_version_markers() -> None:
    """R1/R2 requirement: brief_version/confidence_rubric_version must never
    enter compute_run_id/compute_input_fingerprint -- verified here by
    checking that run_id/input_fingerprint depend only on the six documented
    compute_run_id inputs, none of which is either version marker."""
    import inspect

    from content_machine.intelligence import weekly as weekly_module

    sig = inspect.signature(weekly_module.compute_run_id)
    assert set(sig.parameters) == {
        "week_label",
        "input_fingerprint",
        "code_version",
        "profile_version",
        "window_start",
        "window_end",
    }
    # Two runs with identical compute_run_id inputs must produce the same
    # run_id/input_fingerprint even though brief_version/
    # confidence_rubric_version are present (and identical) on both --
    # this is a smoke check that nothing upstream silently changed
    # compute_run_id's signature to fold either marker in.
    first = _run()
    second = _run()
    assert first.manifest.run_id == second.manifest.run_id
    assert first.manifest.input_fingerprint == second.manifest.input_fingerprint


# ------------------------------ full run_weekly integration -----------------


def test_run_weekly_signal_count_reflects_window_only() -> None:
    result = _run()
    assert result.manifest.signal_count == 7
    assert result.manifest.topic_count > 0


def test_run_weekly_final_state_awaiting_founder_review() -> None:
    result = _run()
    assert result.brief.review_status == "awaiting_founder_review"
    assert result.manifest.review_status == "awaiting_founder_review"


def test_run_weekly_prior_library_preserved_across_a_run() -> None:
    prior_untouched = TopicLibraryEntry(
        topic_id="t_untouched_published",
        canonical_title="An Already-Published Topic",
        source_references=[],
        first_seen="2026-W10",
        last_updated="2026-W10",
        current_score=77,
        score_history=[],
        ranking_explanation="n/a (test fixture)",
        editorial_territory=[],
        evidence_level=2,
        evidence_anchor_id="evid_2_first_party_promotional",
        claim_class="hypothesis",
        learning_value="low",
        experiment_possibility="not practically testable as a local experiment",
        content_angle_possibilities=[],
        reason_not_selected="published, frozen forever",
        reconsideration_condition=None,
        freshness="evergreen",
        lifecycle_status="published",
        audit_events=[],
        profile_version="test-1",
        normalized_summary="An Already-Published Topic.",
    )

    result = _run(prior_library=[prior_untouched])
    by_id = {entry.topic_id: entry for entry in result.library_entries}
    assert "t_untouched_published" in by_id
    assert by_id["t_untouched_published"].lifecycle_status == "published"
    assert by_id["t_untouched_published"].current_score == 77


def test_run_weekly_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted by content_machine.intelligence.weekly")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    result = _run()
    assert result.manifest.run_id  # ran to completion


def test_module_source_never_reads_the_wall_clock() -> None:
    """weekly.py legitimately imports `datetime`/`date` (to construct window
    boundaries from caller-supplied strings), but must never CALL
    `.now()`/`.today()`/`.utcnow()` anywhere in its actual code --
    `execution_timestamp` is always a caller-supplied input (see
    RunManifest's docstring). Checked via the AST (not a substring search)
    so mentions of these calls in prose/docstrings never false-positive."""
    source = inspect.getsource(weekly_module)
    tree = ast.parse(source)
    forbidden_attrs = {"now", "today", "utcnow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs, (
                f"forbidden wall-clock call found in weekly.py: .{node.func.attr}(...)"
            )


def test_fixtures_are_synthetic_example_domains_only() -> None:
    # IANA-reserved documentation domains only (RFC 2606) -- never a real
    # vendor or personal domain.
    reserved_domains = ("example.com", "example.org", "example.net", "example.edu")
    for item in _signals():
        if item.stable_reference.startswith("http"):
            assert any(domain in item.stable_reference for domain in reserved_domains)


# ------------------------------ idempotency + atomicity ----------------------


def test_dry_run_style_no_write_leaves_no_files(tmp_path: Path) -> None:
    """Computing a result (as --dry-run does) without calling
    write_weekly_run_outputs writes nothing."""
    output_dir = tmp_path / "out"
    _run()
    assert not output_dir.exists()


def test_write_creates_all_eight_core_outputs(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    outcome = write_weekly_run_outputs(result, output_dir)
    assert outcome.wrote is True
    assert set(outcome.files_written) == set(weekly_module.OUTPUT_FILENAMES)
    for name in weekly_module.OUTPUT_FILENAMES:
        assert (output_dir / name).exists()


# --- Fable ruling 2026-08-01 (Part A): item_topic_map -----------------------


def test_item_topic_map_covers_every_member_id_across_every_cluster() -> None:
    """item_topic_map (WeeklyRunResult, Part A) must map EVERY item_id that
    appears as a member_id of ANY cluster this run produced -- including
    clusters that end up discarded below the Top N -- to that cluster's own
    topic_id. Built from cluster.cluster_items directly here (independent of
    run_weekly's own construction) so this pins the CONTRACT, not just
    round-trips the implementation."""
    from content_machine.intelligence.cluster import cluster_items
    from content_machine.intelligence.weekly import filter_signals_to_window, resolve_window

    result = _run()
    window = resolve_window(W28_REFERENCE_DATE, TIMEZONE)
    windowed = filter_signals_to_window(_signals(), window, TIMEZONE)
    clusters = cluster_items(windowed)

    expected = {item_id: c.topic_id for c in clusters for item_id in c.member_ids}
    assert result.item_topic_map == expected
    assert expected  # sanity: the fixture window has real clusters


def test_item_topic_map_is_never_written_to_any_output_file(tmp_path: Path) -> None:
    """item_topic_map lives on WeeklyRunResult ONLY (Part A ruling): it must
    never leak into brief.json, topics.jsonl, run-manifest.json, or any
    other output -- raw item_ids paired with topic_ids are not part of any
    documented output schema."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)
    for name in weekly_module.OUTPUT_FILENAMES:
        on_disk = (output_dir / name).read_text(encoding="utf-8")
        assert "item_topic_map" not in on_disk


# --- v0.2 (Opus orchestrator, Gate C; ADR 0004 D8): movements.md/discarded.jsonl


def test_movements_markdown_is_wired_into_the_atomic_write_set(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)
    on_disk = (output_dir / "movements.md").read_text(encoding="utf-8")
    assert on_disk == result.movements_markdown
    assert on_disk.startswith("# Library Movements -- Week 2026-W28")
    headings = (
        "## New",
        "## Promoted",
        "## Demoted",
        "## Returning From Deferred",
        "## Stale",
        "## Merged",
    )
    for heading in headings:
        assert heading in on_disk


def test_discarded_jsonl_matches_the_brief_discarded_list_one_line_each(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)
    lines = (output_dir / "discarded.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.brief.discarded)
    on_disk_topic_ids = {json.loads(line)["topic_id"] for line in lines}
    assert on_disk_topic_ids == {d.topic_id for d in result.brief.discarded}
    if result.brief.discarded:
        first = json.loads(lines[0])
        assert set(first) == {"topic_id", "canonical_title", "score", "reason"}


def test_run_weekly_deltas_cover_every_top_n_topic() -> None:
    result = _run()
    assert {d.topic_id for d in result.deltas} == {t.topic_id for t in result.brief.tier1}.union(
        {t.topic_id for t in result.brief.tier2}, {t.topic_id for t in result.brief.tier3}
    )
    # First run against an empty prior_library: every topic is new.
    assert all(d.is_new for d in result.deltas)
    assert all(d.score_delta is None for d in result.deltas)


def test_same_run_twice_is_a_no_op_and_does_not_duplicate_rows(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"

    first = write_weekly_run_outputs(result, output_dir)
    assert first.wrote is True
    audit_before = (output_dir / "audit.jsonl").read_text()
    score_before = (output_dir / "score-history.jsonl").read_text()
    manifest_before = (output_dir / "run-manifest.json").read_text()

    second = write_weekly_run_outputs(result, output_dir)
    assert second.wrote is False
    assert second.run_id == result.manifest.run_id

    assert (output_dir / "audit.jsonl").read_text() == audit_before
    assert (output_dir / "score-history.jsonl").read_text() == score_before
    assert (output_dir / "run-manifest.json").read_text() == manifest_before


def test_regenerate_redoes_the_run_without_duplicating_append_only_rows(
    tmp_path: Path,
) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    audit_lines_before = (output_dir / "audit.jsonl").read_text().splitlines()
    score_lines_before = (output_dir / "score-history.jsonl").read_text().splitlines()
    assert audit_lines_before  # sanity: this week produced real rows

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=True)
    assert outcome.wrote is True

    audit_lines_after = (output_dir / "audit.jsonl").read_text().splitlines()
    score_lines_after = (output_dir / "score-history.jsonl").read_text().splitlines()

    assert len(audit_lines_after) == len(audit_lines_before)
    assert len(score_lines_after) == len(score_lines_before)
    assert sorted(audit_lines_after) == sorted(audit_lines_before)


def test_regenerate_preserves_other_weeks_append_only_rows(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"

    result_w27 = _run(
        week_label="2026-W27",
        reference_date=W27_REFERENCE_DATE,
        execution_timestamp="2026-07-05T18:00:00-03:00",
    )
    write_weekly_run_outputs(result_w27, output_dir)

    result_w28 = _run()
    write_weekly_run_outputs(result_w28, output_dir)

    audit_lines = (output_dir / "audit.jsonl").read_text().splitlines()
    week_labels_present = {json.loads(line)["week_label"] for line in audit_lines}
    assert {"2026-W27", "2026-W28"} <= week_labels_present

    # Regenerating W28 must leave W27's rows completely untouched.
    outcome = write_weekly_run_outputs(result_w28, output_dir, regenerate=True)
    assert outcome.wrote is True
    audit_lines_after = (output_dir / "audit.jsonl").read_text().splitlines()
    w27_rows_after = sorted(
        line for line in audit_lines_after if json.loads(line)["week_label"] == "2026-W27"
    )
    w27_rows_before = sorted(
        line for line in audit_lines if json.loads(line)["week_label"] == "2026-W27"
    )
    assert w27_rows_after == w27_rows_before


def test_simulated_write_failure_leaves_output_dir_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure mid-write (simulated here as the 4th temp file failing to
    stage) must leave every pre-existing destination file untouched and no
    stray temp file behind -- the all-or-nothing atomicity guarantee."""
    result = _run()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    sentinel = output_dir / "pre-existing.txt"
    sentinel.write_text("do not touch\n", encoding="utf-8")

    real_fdopen = os.fdopen
    call_count = {"n": 0}

    def _flaky_fdopen(fd: int, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 4:
            os.close(fd)
            raise OSError("simulated disk failure")
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(weekly_module.os, "fdopen", _flaky_fdopen)

    with pytest.raises(OSError, match="simulated disk failure"):
        write_weekly_run_outputs(result, output_dir)

    # Nothing from this run was written...
    for name in weekly_module.OUTPUT_FILENAMES:
        assert not (output_dir / name).exists()
    # ...no stray temp files were left behind...
    remaining = {p.name for p in output_dir.iterdir()}
    assert remaining == {"pre-existing.txt"}
    # ...and the pre-existing file is untouched.
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_rename_phase_failure_leaves_output_dir_unchanged_from_pre_run_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QA Finding 2 (MEDIUM): the old ``_atomic_write_all`` was all-or-nothing
    only during the STAGING phase -- if ``os.replace`` itself failed
    partway through the RENAME phase (e.g. after the first couple of files
    had already been renamed into place), the directory was left in a
    genuinely mixed state: some destination files reflecting the new run,
    others still the prior one. This simulates exactly that on a SECOND
    run over an output_dir that already holds a completed FIRST run: the
    rename phase is made to fail on its 2nd file (after the 1st file's
    rename already succeeded), and the whole output_dir -- every one of
    the six files -- must come back byte-for-byte identical to the state
    left by the first run. Not a mix of old and new content, and no stray
    temp/backup files."""
    output_dir = tmp_path / "out"
    first_result = _run()
    first_outcome = write_weekly_run_outputs(first_result, output_dir)
    assert first_outcome.wrote is True

    before_by_name = {
        name: (output_dir / name).read_text(encoding="utf-8")
        for name in weekly_module.OUTPUT_FILENAMES
    }

    second_result = _run(
        week_label="2026-W27",
        reference_date=W27_REFERENCE_DATE,
        execution_timestamp="2026-07-05T18:00:00-03:00",
    )
    assert second_result.manifest.run_id != first_result.manifest.run_id

    real_replace = os.replace
    call_count = {"n": 0}

    def _flaky_replace(src: object, dst: object) -> None:
        call_count["n"] += 1
        # Every pre-existing file gets a backup-replace then a
        # rename-replace (2 calls each) in OUTPUT_FILENAMES order,
        # so call #4 is brief.json's RENAME (its backup, call #3,
        # already succeeded) -- a failure squarely inside the rename
        # phase, after brief.md (calls #1-#2) already fully landed.
        if call_count["n"] == 4:
            raise OSError("simulated rename-phase failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(weekly_module.os, "replace", _flaky_replace)

    with pytest.raises(OSError, match="simulated rename-phase failure"):
        write_weekly_run_outputs(second_result, output_dir, regenerate=False)

    after_by_name = {
        name: (output_dir / name).read_text(encoding="utf-8")
        for name in weekly_module.OUTPUT_FILENAMES
    }
    assert after_by_name == before_by_name, (
        "output_dir must be restored to the pre-second-run state -- no partial batch"
    )

    remaining = {p.name for p in output_dir.iterdir()}
    assert remaining == set(weekly_module.OUTPUT_FILENAMES), (
        "no stray .tmp/.bak.tmp files may survive a rolled-back failure"
    )


def test_regenerate_flag_is_required_to_redo_a_completed_run(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    skipped = write_weekly_run_outputs(result, output_dir, regenerate=False)
    assert skipped.wrote is False
    assert skipped.skipped_reason is not None
    assert "regenerate" in skipped.skipped_reason.lower()


# --- R1 (2026-08-01 post-merge-gate round, ADR 0009): the idempotency skip --
# --- must verify its premise (byte-compare), not assume it from run_id -----


def test_clean_skip_is_not_flagged_stale(tmp_path: Path) -> None:
    """The ordinary case: nothing on disk has drifted from what current code
    would produce. The skip is silent (stale=False, stale_files=[]), and the
    skipped_reason text is UNCHANGED from before this round (ticket R1:
    "all equal -> today's clean skip, message unchanged")."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=False)
    assert outcome.wrote is False
    assert outcome.stale is False
    assert outcome.stale_files == []
    assert outcome.skipped_reason is not None
    assert outcome.skipped_reason == (
        f"a completed run with run_id={result.manifest.run_id} already exists in "
        f"{output_dir}; pass --regenerate to redo it"
    )


def test_two_genuine_reruns_with_different_execution_timestamps_is_a_silent_skip(
    tmp_path: Path,
) -> None:
    """Q1 (2026-08-01 QA blocker, "R1 cries wolf on every rerun"): the exact
    defect QA reproduced against the real CLI with two `runner.invoke` calls
    1.1s apart -- reproduced here directly against `run_weekly`/
    `write_weekly_run_outputs` instead, with TWO GENUINELY SEPARATE
    `WeeklyRunResult` objects (never the SAME in-memory result object
    written twice, which is what `test_clean_skip_is_not_flagged_stale`
    above does and is exactly why QA found it could not catch this: two
    calls against the same in-memory object make identical content true by
    construction, no matter what `execution_timestamp` is). Each result here
    is built by its own independent `_run()` call with its OWN
    `execution_timestamp`, mirroring two separate `weekly-run` CLI
    invocations a minute apart -- same code, same inputs, nothing else
    changed. Before Q1's fix, `execution_timestamp` alone (captured fresh on
    every CLI invocation, `cli/main.py`) always differed between the two, so
    the RAW byte-compare ALWAYS flagged `run-manifest.json` as stale on
    every routine rerun -- defeating R1's entire purpose. The second,
    genuinely independent invocation must be a SILENT, non-stale skip."""
    output_dir = tmp_path / "out"

    first_result = _run(execution_timestamp="2026-07-12T18:00:00-03:00")
    first_outcome = write_weekly_run_outputs(first_result, output_dir)
    assert first_outcome.wrote is True

    # A second, independently-built result for the identical run (same
    # signals, profile, week_label, reference_date, timezone, code_version)
    # -- only execution_timestamp differs, as it genuinely would across two
    # real CLI invocations.
    second_result = _run(execution_timestamp="2026-07-12T18:01:07-03:00")
    assert second_result.manifest.run_id == first_result.manifest.run_id
    assert (
        second_result.manifest.execution_timestamp != first_result.manifest.execution_timestamp
    )

    second_outcome = write_weekly_run_outputs(second_result, output_dir, regenerate=False)
    assert second_outcome.wrote is False
    assert second_outcome.stale is False
    assert second_outcome.stale_files == []
    assert second_outcome.skipped_reason is not None
    assert "stale" not in second_outcome.skipped_reason.lower()

    # The on-disk manifest still records the FIRST run's real
    # execution_timestamp verbatim -- Q1 changes only the COMPARISON, never
    # what actually landed on disk.
    on_disk_manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
    assert on_disk_manifest["execution_timestamp"] == "2026-07-12T18:00:00-03:00"


def test_manifest_comparison_ignores_execution_timestamp_but_nothing_else(tmp_path: Path) -> None:
    """Unit-level companion to the CLI-shaped test above: exercises
    `weekly._manifest_content_differs` directly. Only `execution_timestamp`
    is ignored -- any OTHER field changing (e.g. `signal_count`, standing in
    here for a genuine semantic drift) must still be detected, so Q1's fix
    cannot be satisfied by a comparison that ignores too much."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)
    on_disk = (output_dir / "run-manifest.json").read_text(encoding="utf-8")

    # Only execution_timestamp differs -> NOT differing.
    staged_same_except_timestamp = json.dumps(
        {**json.loads(on_disk), "execution_timestamp": "2099-01-01T00:00:00-03:00"}
    )
    assert (
        weekly_module._manifest_content_differs(on_disk, staged_same_except_timestamp) is False
    )

    # A genuinely different field (signal_count) -> still differing, even
    # though execution_timestamp is untouched.
    staged_genuinely_different = json.dumps({**json.loads(on_disk), "signal_count": 999})
    assert weekly_module._manifest_content_differs(on_disk, staged_genuinely_different) is True

    # Malformed on-disk content must still compare as differing against a
    # well-formed staged manifest.
    assert weekly_module._manifest_content_differs("not json at all", on_disk) is True


def test_unicode_decode_error_on_corrupt_on_disk_file_counts_as_differing(
    tmp_path: Path,
) -> None:
    """F2 (2026-08-01 Fable blocker, "verify, don't assume"): a corrupt or
    non-UTF-8 on-disk file must count as DIFFERING (the same treatment as a
    missing file), never crash the skip path with an unhandled
    UnicodeDecodeError -- `read_text(encoding="utf-8")` raises
    `UnicodeDecodeError` (a `ValueError`, not an `OSError`) for invalid
    UTF-8 bytes."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    # Corrupt brief.md with invalid UTF-8 bytes (a lone continuation byte).
    (output_dir / "brief.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=False)
    assert outcome.wrote is False
    assert outcome.stale is True
    assert "brief.md" in outcome.stale_files


def test_stale_on_disk_run_is_detected_and_still_skipped(tmp_path: Path) -> None:
    """Simulates exactly the headline defect this round fixes: an on-disk
    run whose run_id still matches (nothing about the INPUTS changed), but
    whose rendered brief.md no longer matches what CURRENT code renders
    (e.g. a rendering-semantics change never bumped code_version). The skip
    must still happen -- never write without --regenerate -- but it must be
    flagged stale, with the differing filename(s) named, so the caller can
    warn instead of silently serving a stale run."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    # Simulate drift: current code would render different brief.md content
    # for the SAME run_id/manifest (as if BRIEF_VERSION/rendering changed
    # without a code_version bump) by directly corrupting the on-disk file
    # -- this is exactly what a stale historical output directory looks
    # like from write_weekly_run_outputs' point of view.
    brief_md_path = output_dir / "brief.md"
    original_brief_md = brief_md_path.read_text(encoding="utf-8")
    brief_md_path.write_text("this is stale, pre-fix content\n", encoding="utf-8")

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=False)
    assert outcome.wrote is False
    assert outcome.stale is True
    assert outcome.stale_files == ["brief.md"]
    assert outcome.skipped_reason is not None
    assert "regenerate" in outcome.skipped_reason.lower()
    assert "stale" in outcome.skipped_reason.lower()
    assert "brief.md" in outcome.skipped_reason

    # Never overwritten without --regenerate: the tampered content is still
    # there, byte for byte.
    assert brief_md_path.read_text(encoding="utf-8") == "this is stale, pre-fix content\n"
    assert brief_md_path.read_text(encoding="utf-8") != original_brief_md


def test_stale_detection_lists_every_differing_file_not_just_one(tmp_path: Path) -> None:
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    (output_dir / "brief.md").write_text("stale\n", encoding="utf-8")
    (output_dir / "movements.md").write_text("stale\n", encoding="utf-8")

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=False)
    assert outcome.wrote is False
    assert outcome.stale is True
    assert outcome.stale_files == ["brief.md", "movements.md"]


def test_regenerate_bypasses_the_byte_compare_and_redoes_the_write(tmp_path: Path) -> None:
    """--regenerate never needs the byte-compare -- it always redoes the
    write, so a stale on-disk run is repaired, not just flagged."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)

    original_brief_md = (output_dir / "brief.md").read_text(encoding="utf-8")
    (output_dir / "brief.md").write_text("stale\n", encoding="utf-8")

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=True)
    assert outcome.wrote is True
    assert (output_dir / "brief.md").read_text(encoding="utf-8") == original_brief_md


def test_stale_check_never_touches_run_id_or_input_fingerprint(tmp_path: Path) -> None:
    """R1/R2 requirement: the byte-compare and its stale flag must never
    feed back into run_id/input_fingerprint computation -- re-running
    run_weekly on the exact same inputs after a stale on-disk mutation must
    still compute the identical run_id."""
    result = _run()
    output_dir = tmp_path / "out"
    write_weekly_run_outputs(result, output_dir)
    (output_dir / "brief.md").write_text("stale\n", encoding="utf-8")

    write_weekly_run_outputs(result, output_dir, regenerate=False)

    recomputed = _run()
    assert recomputed.manifest.run_id == result.manifest.run_id
    assert recomputed.manifest.input_fingerprint == result.manifest.input_fingerprint
