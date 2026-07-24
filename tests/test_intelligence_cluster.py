"""Tests for content_machine.intelligence.normalize and .cluster."""

from __future__ import annotations

import random
import subprocess
import sys
from datetime import date
from pathlib import Path

from content_machine.intelligence.cluster import cluster_items
from content_machine.intelligence.loader import load_signals
from content_machine.intelligence.models import SourceItem
from content_machine.intelligence.normalize import (
    normalize_canonical_reference,
    normalize_text,
    parse_iso_date,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"


def _load_fixture_items() -> list[SourceItem]:
    return load_signals(VALID_FIXTURE).items


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
    }
    base.update(overrides)
    return SourceItem.model_validate(base)


# --- normalize.py idempotence -------------------------------------------------


def test_normalize_text_is_idempotent() -> None:
    samples = [
        "  VendorA Ships   New Agent-Harness! With Guardrails.  ",
        "already lowercase no punctuation",
        "",
        "ALL CAPS, WITH; PUNCTUATION!!",
        "Mixed CASE with the and a stopwords",
    ]
    for sample in samples:
        once = normalize_text(sample)
        twice = normalize_text(once)
        assert once == twice


def test_normalize_canonical_reference_is_idempotent() -> None:
    samples = [
        "HTTPS://WWW.Example.COM/Path/?utm_source=x#frag",
        "https://example.com/no-trailing-slash",
        "email:vendor-h-thread-1",
        "  email:Vendor-H-Thread-1  ",
    ]
    for sample in samples:
        once = normalize_canonical_reference(sample)
        twice = normalize_canonical_reference(once)
        assert once == twice


def test_normalize_canonical_reference_strips_scheme_www_query_fragment_slash() -> None:
    assert (
        normalize_canonical_reference("https://WWW.Example.com/Path/?q=1#frag")
        == "example.com/path"
    )
    assert normalize_canonical_reference("https://example.com") == "example.com"


def test_parse_iso_date_strict() -> None:
    assert parse_iso_date("2026-06-01") == date(2026, 6, 1)
    assert parse_iso_date("06/01/2026") is None
    assert parse_iso_date("not a date") is None
    assert parse_iso_date("") is None


# --- cluster.py merge rules ---------------------------------------------------


def test_duplicates_by_identical_canonical_reference_merge() -> None:
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item016" in c.member_ids)
    assert "item017" in cluster.member_ids
    assert cluster.member_roles["item017"] == "duplicate"
    assert "same_canonical_reference" in cluster.duplication_reasons


def test_duplicates_by_normalized_title_and_shared_subject_merge() -> None:
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item001" in c.member_ids)
    assert {"item002", "item003", "item004", "item009"} <= set(cluster.member_ids)
    assert "title_similarity_and_shared_subject" in cluster.duplication_reasons


def test_paraphrases_merge_and_collapse_as_syndicated() -> None:
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item001" in c.member_ids)
    assert cluster.member_roles["item002"] == "syndicated"
    assert cluster.member_roles["item003"] == "syndicated"
    # Syndicated members contribute zero to independent_publisher_count, even
    # though item004 (relay, NOT syndicated) is present in the same cluster.
    assert cluster.member_roles["item004"] == "relay"


def test_close_but_distinct_subjects_do_not_merge() -> None:
    """'VendorA Sandbox 1.0' and 'VendorB Sandbox 1.0' have title Jaccard >= 0.6
    but different subject_entity_ids -- the over-merge guard must hold."""
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster_a = next(c for c in clusters if "item018" in c.member_ids)
    cluster_b = next(c for c in clusters if "item019" in c.member_ids)
    assert cluster_a.topic_id != cluster_b.topic_id
    assert "item019" not in cluster_a.member_ids
    assert "item018" not in cluster_b.member_ids


def test_syndicated_wire_story_does_not_raise_independent_count() -> None:
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item020" in c.member_ids)
    assert {"item021", "item022", "item023"} <= set(cluster.member_ids)
    # roundup/relay evidence types never count toward independence, and the
    # syndicated members contribute zero regardless.
    assert cluster.independent_publisher_count == 0
    assert cluster.has_independent_evidence is False


def test_vendor_a_analysing_vendor_b_counts_as_independent() -> None:
    items = _load_fixture_items()
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item024" in c.member_ids)
    assert "item025" in cluster.member_ids
    assert cluster.member_roles["item025"] == "independent"
    assert cluster.has_independent_evidence is True
    assert cluster.independent_publisher_count == 1


def test_deterministic_cluster_and_topic_id() -> None:
    items = _load_fixture_items()
    clusters_a = cluster_items(items)
    clusters_b = cluster_items(list(items))
    assert [c.topic_id for c in clusters_a] == [c.topic_id for c in clusters_b]
    assert [c.member_ids for c in clusters_a] == [c.member_ids for c in clusters_b]


def test_order_independent_dedup_identical_output_order_after_shuffle() -> None:
    items = _load_fixture_items()
    shuffled = list(items)
    rng = random.Random(1234)
    rng.shuffle(shuffled)
    assert [i.item_id for i in shuffled] != [i.item_id for i in items]

    clusters_original = cluster_items(items)
    clusters_shuffled = cluster_items(shuffled)

    assert [c.topic_id for c in clusters_original] == [c.topic_id for c in clusters_shuffled]
    assert [c.member_ids for c in clusters_original] == [c.member_ids for c in clusters_shuffled]


def test_cross_process_topic_id_stability_under_different_hashseed() -> None:
    """topic_id must never depend on Python's randomized hash() seed -- it is
    built purely from sha256, never the hash() builtin."""
    script = (
        "import sys; sys.path.insert(0, 'src'); "
        "from content_machine.intelligence.loader import load_signals; "
        "from content_machine.intelligence.cluster import cluster_items; "
        "r = load_signals('examples/intelligence-signals-synthetic.json'); "
        "cs = cluster_items(r.items); "
        "print(','.join(c.topic_id for c in cs))"
    )
    env_a = {"PYTHONHASHSEED": "0"}
    env_b = {"PYTHONHASHSEED": "1"}
    import os

    result_a = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **env_a},
        check=True,
    )
    result_b = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **env_b},
        check=True,
    )
    assert result_a.stdout == result_b.stdout
    assert result_a.stdout.strip() != ""


def test_no_hash_builtin_used_anywhere_in_the_package() -> None:
    """Stable identifiers must come from hashlib.sha256, never Python's
    builtin hash() (PYTHONHASHSEED randomization would destroy determinism)."""
    package_dir = REPO_ROOT / "src" / "content_machine" / "intelligence"
    for path in sorted(package_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "hash(" not in text, f"builtin hash() found in {path}"


def test_syndication_first_member_can_never_be_syndicated() -> None:
    """A cluster whose earliest-sorted-by-item_id member has a summary later
    repeated verbatim must not mark that first member itself as syndicated."""
    a = _make_item(
        item_id="aa",
        subject_entity_ids=["shared-subject"],
        title="Shared Event Title One",
        summary_normalized="identical wording used to test syndication ordering",
        stable_reference="https://example.com/aa",
    )
    b = _make_item(
        item_id="bb",
        subject_entity_ids=["shared-subject"],
        title="Shared Event Title Two",
        summary_normalized="identical wording used to test syndication ordering",
        stable_reference="https://example.com/bb",
    )
    clusters = cluster_items([a, b])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.member_roles["aa"] != "syndicated"
