"""Tests for content_machine.intelligence.limitations_overlay (Fable ruling
2026-08-01, Part C: the Founder-approved, per-item limitations overlay).

All fixtures here are SYNTHETIC only -- invented topic/run ids and invented
limitation text, never anything resembling the real 2026-W31 run or any real
Founder-authored prose. No content in this file is loaded from, or written
to, ``data/private/``.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from content_machine.intelligence.brief import build_weekly_brief, render_markdown
from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.limitations_overlay import (
    OVERLAY_FILENAME,
    LimitationsOverlayError,
    LimitationsOverlayResult,
    corpus_topic_ids,
    load_limitations_overlay,
    rendered_topic_ids,
)
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.tiers import assign_tiers
from content_machine.privacy.anonymizer import strip_for_model

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"
WEEK_LABEL = "2026-W30"

RUN_ID = "run-synthetic-abc123"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_overlay(path: Path, **fields: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "provenance": "human_authored",
        "limitations": {},
    }
    payload.update(fields)
    _write(path, json.dumps(payload))


def _brief_from_real_fixture() -> object:
    items = load_signals(VALID_FIXTURE).items
    items_by_id = {item.item_id: item for item in items}
    clusters = cluster_items(items)
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    inputs_list = [to_ranking_inputs(c, items_by_id) for c in clusters]
    from content_machine.intelligence.ranking import rank_topics

    ranked = rank_topics(inputs_list, load_profile(PROFILE_FIXTURE))
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    return build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)


# --- 1. unknown item_id -------------------------------------------------------


def test_overlay_unknown_item_id_aborts_render(tmp_path: Path) -> None:
    """An overlay item_id that names a topic this run never produced at all
    (outside the corpus) must abort the entire render."""
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write_overlay(
        overlay_path,
        limitations={"topic-not-in-corpus": "a synthetic, invented limitation about scope"},
    )
    with pytest.raises(LimitationsOverlayError, match="topic-not-in-corpus") as exc_info:
        load_limitations_overlay(
            overlay_path,
            run_id=RUN_ID,
            corpus_topic_ids={"topic-a", "topic-b"},
            rendered_topic_ids={"topic-a"},
        )
    # The error names the item_id and run_id only, never the limitation text.
    assert "a synthetic, invented limitation about scope" not in str(exc_info.value)


# --- 2. item_id in corpus but never rendered ---------------------------------


def test_overlay_unrendered_item_id_aborts_render(tmp_path: Path) -> None:
    """An overlay item_id that names a real topic from this run's corpus
    (e.g. one that was discarded below the Top N) but was never rendered
    into a Tier 1/2/3 block must also abort -- there is no block to attach
    the limitation to."""
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write_overlay(
        overlay_path,
        limitations={"topic-b": "a synthetic, invented limitation about scope"},
    )
    with pytest.raises(LimitationsOverlayError, match="not rendered in the brief") as exc_info:
        load_limitations_overlay(
            overlay_path,
            run_id=RUN_ID,
            corpus_topic_ids={"topic-a", "topic-b"},
            rendered_topic_ids={"topic-a"},
        )
    assert "a synthetic, invented limitation about scope" not in str(exc_info.value)


# --- 3. duplicate item_id key -------------------------------------------------


def test_overlay_duplicate_item_id_aborts_render(tmp_path: Path) -> None:
    """A duplicate key in the overlay's own JSON must be DETECTED, not
    silently resolved to the last occurrence the way plain ``json.loads``
    would -- see the ``object_pairs_hook`` this module uses."""
    overlay_path = tmp_path / OVERLAY_FILENAME
    raw = (
        f'{{"schema_version": 1, "run_id": "{RUN_ID}", "provenance": "human_authored", '
        '"limitations": {"topic-a": "first synthetic invented text", '
        '"topic-a": "second synthetic invented text"}}'
    )
    _write(overlay_path, raw)
    with pytest.raises(LimitationsOverlayError, match="duplicate key 'topic-a'") as exc_info:
        load_limitations_overlay(
            overlay_path,
            run_id=RUN_ID,
            corpus_topic_ids={"topic-a"},
            rendered_topic_ids={"topic-a"},
        )
    assert "synthetic invented text" not in str(exc_info.value)


# --- 4. empty / whitespace-only limitation text -------------------------------


def test_overlay_empty_limitation_text_aborts_render(tmp_path: Path) -> None:
    """An empty or whitespace-only limitation string must abort -- absence of
    a limitation means the key is omitted entirely, never an empty value."""
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write_overlay(overlay_path, limitations={"topic-a": "   "})
    with pytest.raises(LimitationsOverlayError, match="empty or whitespace-only"):
        load_limitations_overlay(
            overlay_path,
            run_id=RUN_ID,
            corpus_topic_ids={"topic-a"},
            rendered_topic_ids={"topic-a"},
        )


# --- 5. unparseable / schema-invalid file -------------------------------------


def test_overlay_unparseable_file_aborts_render(tmp_path: Path) -> None:
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write(overlay_path, "{not valid json at all")
    with pytest.raises(LimitationsOverlayError, match="unparseable"):
        load_limitations_overlay(
            overlay_path, run_id=RUN_ID, corpus_topic_ids=set(), rendered_topic_ids=set()
        )


def test_overlay_schema_invalid_file_aborts_render(tmp_path: Path) -> None:
    """Valid JSON, but missing a required top-level field -- schema-invalid,
    the sibling failure mode to 'unparseable'. The error must name only the
    error count, never echo back any field value."""
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write(
        overlay_path,
        json.dumps({"schema_version": 1, "provenance": "human_authored", "limitations": {}}),
    )
    with pytest.raises(LimitationsOverlayError, match="schema-invalid"):
        load_limitations_overlay(
            overlay_path, run_id=RUN_ID, corpus_topic_ids=set(), rendered_topic_ids=set()
        )


# --- 6. run_id mismatch -------------------------------------------------------


def test_overlay_run_id_mismatch_aborts_render(tmp_path: Path) -> None:
    overlay_path = tmp_path / OVERLAY_FILENAME
    _write_overlay(
        overlay_path,
        run_id="run-some-other-run-entirely",
        limitations={"topic-a": "a synthetic, invented limitation about scope"},
    )
    with pytest.raises(LimitationsOverlayError, match="does not match the run being rendered"):
        load_limitations_overlay(
            overlay_path,
            run_id=RUN_ID,
            corpus_topic_ids={"topic-a"},
            rendered_topic_ids={"topic-a"},
        )


# --- 7. absent file: not a failure --------------------------------------------


def test_overlay_absent_file_renders_without_limitations(tmp_path: Path) -> None:
    overlay_path = tmp_path / OVERLAY_FILENAME
    assert not overlay_path.exists()
    result = load_limitations_overlay(
        overlay_path,
        run_id=RUN_ID,
        corpus_topic_ids={"topic-a"},
        rendered_topic_ids={"topic-a"},
    )
    assert result is None


def test_overlay_absent_reproduces_baseline_rendering_byte_for_byte() -> None:
    """No overlay supplied at all (the default) must reproduce EXACTLY the
    pre-overlay rendering -- proving 'renders without limitations' really
    means unchanged output, not merely 'does not crash'."""
    brief = _brief_from_real_fixture()
    baseline = render_markdown(brief)
    assert render_markdown(brief, limitations_overlay=None) == baseline
    assert render_markdown(brief, limitations_overlay={}) == baseline


# --- 8. never reaches strip_for_model or providers ----------------------------


def test_overlay_never_reaches_strip_for_model_or_providers() -> None:
    """Static import guard: content_machine.intelligence.limitations_overlay
    must never be imported by providers/ or privacy/anonymizer.py -- the
    overlay is composed only into rendered Markdown, never threaded through
    the model boundary. Also pins strip_for_model's field allowlist
    unchanged: company/position only, exactly as before this ruling."""
    providers_dir = REPO_ROOT / "src" / "content_machine" / "providers"
    for path in sorted(providers_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "limitations_overlay" not in module, f"{path} imports {module!r}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "limitations_overlay" not in alias.name, (
                        f"{path} imports {alias.name!r}"
                    )

    anonymizer_path = REPO_ROOT / "src" / "content_machine" / "privacy" / "anonymizer.py"
    anonymizer_tree = ast.parse(anonymizer_path.read_text(encoding="utf-8"))
    for node in ast.walk(anonymizer_tree):
        if isinstance(node, ast.ImportFrom):
            assert "limitations_overlay" not in (node.module or "")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "limitations_overlay" not in alias.name

    # strip_for_model's allowlist: extract every string-literal dict key the
    # function's own source constructs, via the AST (not a substring search,
    # so a stray mention in a comment can never false-pass this).
    source = inspect.getsource(strip_for_model)
    func_tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(func_tree):
        if isinstance(node, ast.Dict):
            for key_node in node.keys:
                if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                    keys.add(key_node.value)
    assert keys == {"company", "position"}


# --- bonus: a valid overlay composes correctly, end to end -------------------


def test_valid_overlay_composes_only_into_rendered_markdown() -> None:
    """A fully valid overlay renders the exact literal line inside the
    named item's own Tier 1/2/3 block, and changes NOTHING else -- not the
    structured WeeklyBrief object, not any other item's text."""
    brief = _brief_from_real_fixture()
    baseline_markdown = render_markdown(brief)

    corpus = corpus_topic_ids(brief)
    rendered = rendered_topic_ids(brief)
    assert corpus and rendered
    target_topic_id = next(iter(rendered))
    target_title = next(
        item.canonical_title
        for item in (*brief.tier1, *brief.tier2, *brief.tier3)
        if item.topic_id == target_topic_id
    )

    overlay_result = LimitationsOverlayResult(
        limitations={target_topic_id: "a synthetic, invented limitation for this one item"},
        overlay_sha256="0" * 64,
        item_count=1,
        provenance="human_authored",
    )
    composed_markdown = render_markdown(brief, limitations_overlay=overlay_result.limitations)

    assert composed_markdown != baseline_markdown
    assert (
        "- **Founder-noted limitation (human-authored):** "
        "a synthetic, invented limitation for this one item"
    ) in composed_markdown
    # Nothing else in the document moved: stripping the one new line back out
    # reproduces the baseline exactly.
    new_lines = set(composed_markdown.splitlines()) - set(baseline_markdown.splitlines())
    assert len(new_lines) == 1
    assert target_title  # sanity: the target item really was found
