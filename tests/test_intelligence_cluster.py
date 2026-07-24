"""Tests for content_machine.intelligence.normalize and .cluster."""

from __future__ import annotations

import random
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import get_args

from content_machine.intelligence.cluster import cluster_items
from content_machine.intelligence.loader import load_signals
from content_machine.intelligence.models import EvidenceType, SourceItem
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


def test_anchor_prefers_non_relay_member_over_earlier_roundup() -> None:
    """B3: an earlier-dated roundup/relay copy must not become the anchor
    when a non-relay member exists in the same cluster -- change_class,
    action_required, and experiment_affordance (read off the anchor by
    to_ranking_inputs) must come from the real primary source, not from
    whichever copy happened to surface first."""
    roundup_copy = _make_item(
        item_id="roundup-early",
        subject_entity_ids=["shared-anchor-subject"],
        title="Shared Anchor Test Event Roundup",
        summary_normalized=(
            "a brief roundup mention of the shared anchor test event with no independent detail"
        ),
        publication_date=date(2026, 1, 1),
        detection_date=date(2026, 1, 1),
        stable_reference="https://example.com/roundup/shared-anchor-test-event",
        evidence_type="roundup",
        change_class="incremental_update",
        action_required="none",
        experiment_affordance="not_testable",
    )
    primary_source = _make_item(
        item_id="primary-later",
        subject_entity_ids=["shared-anchor-subject"],
        title="Shared Anchor Test Event Primary",
        summary_normalized=(
            "the primary source publishes full detail on the shared anchor test event "
            "with a required migration step"
        ),
        publication_date=date(2026, 1, 3),
        detection_date=date(2026, 1, 3),
        stable_reference="https://example.com/primary/shared-anchor-test-event",
        evidence_type="official_doc",
        change_class="breaking_change",
        action_required="migration_required",
        experiment_affordance="local_reproducible",
    )
    clusters = cluster_items([roundup_copy, primary_source])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.anchor_item_id == "primary-later"
    assert cluster.member_roles["roundup-early"] == "relay"


def test_anchor_falls_back_to_earliest_when_all_members_are_relay() -> None:
    """B3 fallback: when EVERY member in a cluster is roundup/relay, the
    anchor is still the earliest by date (there is no better candidate)."""
    earlier_relay = _make_item(
        item_id="relay-early",
        subject_entity_ids=["all-relay-subject"],
        title="All Relay Test Event Wire",
        summary_normalized="a wire pickup of the all relay test event with generic framing",
        publication_date=date(2026, 1, 1),
        detection_date=date(2026, 1, 1),
        stable_reference="https://example.net/relay-early/all-relay-test-event",
        evidence_type="relay",
    )
    later_roundup = _make_item(
        item_id="roundup-later",
        subject_entity_ids=["all-relay-subject"],
        title="All Relay Test Event Roundup",
        summary_normalized=(
            "a weekly roundup mention of the all relay test event with generic framing"
        ),
        publication_date=date(2026, 1, 2),
        detection_date=date(2026, 1, 2),
        stable_reference="https://example.net/roundup-later/all-relay-test-event",
        evidence_type="roundup",
    )
    clusters = cluster_items([later_roundup, earlier_relay])
    assert len(clusters) == 1
    assert clusters[0].anchor_item_id == "relay-early"


def test_topic_tags_and_evidence_types_exclude_roundup_and_relay_members() -> None:
    """B2/S1: a roundup/relay member's topic_tags and evidence_type must not
    leak into the cluster's topic_tags/evidence_types union when a real
    (primary/independent) member is present -- only coverage, no signal."""
    primary_source = _make_item(
        item_id="tags-primary",
        subject_entity_ids=["tags-subject"],
        title="Tags Test Event Announcement",
        summary_normalized="the primary source publishes detail on the tags test event",
        publication_date=date(2026, 1, 1),
        detection_date=date(2026, 1, 1),
        stable_reference="https://example.com/tags-primary/tags-test-event",
        evidence_type="announcement",
        topic_tags=["agents"],
    )
    roundup_copy = _make_item(
        item_id="tags-roundup",
        subject_entity_ids=["tags-subject"],
        title="Tags Test Event Roundup",
        summary_normalized="a distinct roundup mention of the tags test event with its own framing",
        publication_date=date(2026, 1, 2),
        detection_date=date(2026, 1, 2),
        stable_reference="https://example.com/tags-roundup/tags-test-event",
        evidence_type="roundup",
        topic_tags=["agents", "evals"],
    )
    clusters = cluster_items([primary_source, roundup_copy])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.member_roles["tags-roundup"] == "relay"
    assert cluster.topic_tags == ["agents"]
    assert cluster.evidence_types == ["announcement"]


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


# --- evidence rubric totality (Gate A correction round 2, R1) -----------------


def test_evidence_rubric_totality_matrix() -> None:
    """The evidence rubric must be a TOTAL function over evidence_type x
    publisher-polarity: every one of the 14 evidence types (13 plus
    ``official_api_behavior_change``, added for Founder decision D1), in
    EITHER polarity (publisher is/isn't the cluster's own subject), must
    produce evidence_level > 0 for a single-member cluster with an anchor id
    that is NOT ``evid_0_*``, EXCEPT roundup/relay (never evidentiary in any
    polarity). Before the original fix, 10 of these cells silently fell to
    level 0 (and the magnitude cap then crushed them to 1); this is the
    structural guarantee that the hole cannot reopen. Levels themselves
    shifted under D2/D4 (self-authored independent_analysis and isolated
    non-subject announcement/release_note now land at 2 and 1 respectively,
    not 3) but every non-roundup/relay cell must still clear level 0."""
    all_types = get_args(EvidenceType)
    assert len(all_types) == 14  # guards against the taxonomy silently growing unnoticed

    for evidence_type in all_types:
        for is_first_party, label in ((True, "first-party"), (False, "non-subject")):
            item = _make_item(
                item_id=f"totality-{evidence_type}-{label}",
                publisher_id="totality-subject" if is_first_party else "totality-other",
                subject_entity_ids=["totality-subject"],
                title=f"Totality Matrix Probe {evidence_type} {label}",
                summary_normalized=f"totality matrix probe summary for {evidence_type} {label}",
                stable_reference=f"https://example.com/totality/{evidence_type}/{label}",
                evidence_type=evidence_type,
            )
            cluster = cluster_items([item])[0]
            if evidence_type in ("roundup", "relay"):
                assert cluster.evidence_level == 0, (evidence_type, label)
                assert cluster.evidence_anchor_id.startswith("evid_0_"), (evidence_type, label)
            else:
                assert cluster.evidence_level > 0, (evidence_type, label)
                assert not cluster.evidence_anchor_id.startswith("evid_0_"), (
                    evidence_type,
                    label,
                )


def test_self_published_independent_implementation_reaches_level_3_not_0() -> None:
    """R1, the item015 class: a self-published (publisher IS the subject)
    independent_implementation is still real evidence -- a runnable
    artifact -- even though it is not independently corroborated. Before the
    fix this fell all the way to evidence_level 0 (and the magnitude cap then
    crushed it to 1); it must now land at level 3 as first_party_artifact,
    and it must NOT count toward has_independent_evidence."""
    item = _make_item(
        item_id="self-published-impl",
        publisher_id="sandbox-project",
        subject_entity_ids=["sandbox-project"],
        title="Sandbox Project Ships A Runnable Reference Implementation",
        summary_normalized=(
            "the sandbox project published its own runnable reference implementation"
        ),
        stable_reference="https://example.com/sandbox-project/impl",
        evidence_type="independent_implementation",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_first_party_artifact"
    assert cluster.has_independent_evidence is False
    assert cluster.independent_publisher_count == 0


def test_third_party_security_advisory_reaches_level_3_not_0() -> None:
    """R1: a security_advisory published by a THIRD PARTY (not the vendor
    itself) is real, uncorroborated evidence about the subject -- the exact
    "third-party security advisory" case the ticket names as being
    inverted below a vendor's own press release before this fix. It must
    land at level 3 as non_subject_authoritative, and (CRITICAL) must NOT
    count toward has_independent_evidence -- only raising the evidence level,
    never Tier-1 admission on its own."""
    item = _make_item(
        item_id="third-party-advisory",
        publisher_id="security-research-lab",
        subject_entity_ids=["vendor-advisory-subject"],
        title="Security Research Lab Publishes Advisory About Vendor Advisory Subject",
        summary_normalized=(
            "a third party security research lab published an advisory about a vendor"
        ),
        stable_reference="https://example.com/security-research-lab/advisory",
        evidence_type="security_advisory",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_non_subject_authoritative"
    assert cluster.has_independent_evidence is False
    assert cluster.independent_publisher_count == 0


def test_non_subject_authoritative_and_first_party_artifact_never_flip_independence() -> None:
    """CRITICAL invariant from the R1 fix: raising the evidence LEVEL for
    non_subject_authoritative and first_party_artifact cells must never flip
    has_independent_evidence (and therefore Tier-1 eligibility) -- those two
    cells were deliberately NOT added to _INDEPENDENT_EVIDENCE_TYPES. This
    checks both cells together in one cluster, alongside a plain first-party
    promotional item, so the only way has_independent_evidence could become
    True is if one of the two new cells were (incorrectly) counted."""
    # Identical titles guarantee the top-level merge rule (title Jaccard >=
    # 0.6 AND shared subject_entity_ids) fires, putting all three in ONE
    # cluster; the summaries are deliberately distinct so none of them
    # collapse into each other as "syndicated" (Jaccard < 0.85 pairwise) --
    # this test is about evidence accounting, not the syndication mechanism.
    shared_title = "Critical Vendor Platform Update Coverage"
    promotional = _make_item(
        item_id="critical-promo",
        publisher_id="critical-vendor",
        subject_entity_ids=["critical-vendor"],
        title=shared_title,
        summary_normalized="critical vendor announced a brand new feature for its platform",
        stable_reference="https://example.com/critical-vendor/announcement",
        evidence_type="announcement",
    )
    third_party_advisory = _make_item(
        item_id="critical-advisory",
        publisher_id="critical-third-party",
        subject_entity_ids=["critical-vendor"],
        title=shared_title,
        summary_normalized=(
            "a third party published a distinct security advisory about critical vendor"
        ),
        stable_reference="https://example.com/critical-third-party/advisory",
        evidence_type="security_advisory",
    )
    self_published_artifact = _make_item(
        item_id="critical-artifact",
        publisher_id="critical-vendor",
        subject_entity_ids=["critical-vendor"],
        title=shared_title,
        summary_normalized=(
            "critical vendor published its own benchmark with a distinct methodology"
        ),
        stable_reference="https://example.com/critical-vendor/benchmark",
        evidence_type="benchmark_with_methodology",
    )
    clusters = cluster_items([promotional, third_party_advisory, self_published_artifact])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.has_independent_evidence is False
    assert cluster.independent_publisher_count == 0
    assert cluster.evidence_level >= 3


def test_evidence_level_5_methodology_leg_rejects_self_published_benchmark() -> None:
    """R6: the methodology leg of evidence level 5 must come from a
    NON-subject publisher -- a subject's own benchmark_with_methodology must
    never satisfy it, even alongside a first-party-authoritative source and a
    genuine third-party independent_analysis. Spec Section 6: "benchmark
    exige metodologia ... vendor com cautela"."""
    shared_title = "R6 Vendor Spec Change Coverage"
    authoritative = _make_item(
        item_id="r6-authoritative",
        publisher_id="r6-vendor",
        subject_entity_ids=["r6-vendor"],
        title=shared_title,
        summary_normalized="r6 vendor published an official spec change document",
        stable_reference="https://example.com/r6-vendor/spec-change",
        evidence_type="spec_change",
    )
    self_published_benchmark = _make_item(
        item_id="r6-self-benchmark",
        publisher_id="r6-vendor",
        subject_entity_ids=["r6-vendor"],
        title=shared_title,
        summary_normalized="r6 vendor published its own benchmark with a claimed methodology",
        stable_reference="https://example.com/r6-vendor/self-benchmark",
        evidence_type="benchmark_with_methodology",
    )
    third_party_analysis = _make_item(
        item_id="r6-analysis",
        publisher_id="r6-independent-analyst",
        subject_entity_ids=["r6-vendor"],
        title=shared_title,
        summary_normalized="an independent analyst reviewed the r6 vendor spec change in detail",
        stable_reference="https://example.com/r6-independent-analyst/review",
        evidence_type="independent_analysis",
    )
    clusters = cluster_items([authoritative, self_published_benchmark, third_party_analysis])
    assert len(clusters) == 1
    cluster = clusters[0]
    # Must NOT reach level 5: the only benchmark present is self-published,
    # so it cannot serve as the independent-rigor leg.
    assert cluster.evidence_level == 4
    assert cluster.evidence_anchor_id == "evid_4_first_party_plus_independent"


def test_independent_analysis_alone_with_no_first_party_member_is_level_3(
) -> None:
    """G1 coverage gap (from the QA auditor): a cluster whose ONLY member is
    a non-subject independent_analysis -- with NO first-party member present
    at all -- must yield evidence_level == 3 at the independent-only anchor.
    This is distinct from test_vendor_a_analysing_vendor_b_counts_as_independent,
    which has a first-party member present and hits the level-4 branch
    instead; before this test existed, the entire 401-test suite still
    passed with the evid_3_independent_only branch mutated away."""
    item = _make_item(
        item_id="g1-lone-analysis",
        publisher_id="g1-independent-analyst",
        subject_entity_ids=["g1-ecosystem-subject"],
        title="G1 Independent Analyst Reviews An Ecosystem Wide Trend",
        summary_normalized="an independent analyst reviewed an ecosystem wide trend on its own",
        stable_reference="https://example.com/g1-independent-analyst/review",
        evidence_type="independent_analysis",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_independent_only"
    assert cluster.has_independent_evidence is True
    assert cluster.independent_publisher_count == 1


# --- Founder decision D2: isolated secondary news is level 1 -----------------


def test_isolated_secondary_news_uncorroborated_is_level_1() -> None:
    """D2: a single non-subject announcement/release_note about someone
    else, with no primary artifact and no corroboration anywhere in the
    cluster, must land at evidence_level 1 with its own anchor id --
    distinct from rumor and no longer sharing evid_3_other_uncorroborated's
    level 3. Before this fix this scored level 3, level with a third-party
    security advisory; it is weaker than that and must not be conflated with
    it."""
    item = _make_item(
        item_id="d2-secondary-news",
        publisher_id="third-party-outlet",
        subject_entity_ids=["d2-subject"],
        title="D2 Secondary News Test Event",
        summary_normalized="a third party outlet reported that d2 subject shipped something new",
        stable_reference="https://example.com/third-party-outlet/d2-report",
        evidence_type="announcement",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 1
    assert cluster.evidence_anchor_id == "evid_1_secondary_news_uncorroborated"
    assert cluster.marketing_risk is False
    assert cluster.has_independent_evidence is False


def test_isolated_secondary_news_release_note_is_also_level_1() -> None:
    """D2 applies to release_note exactly as it does to announcement -- both
    are in _SECONDARY_NEWS_TYPES."""
    item = _make_item(
        item_id="d2-secondary-release-note",
        publisher_id="third-party-outlet-two",
        subject_entity_ids=["d2-subject-two"],
        title="D2 Secondary Release Note Test Event",
        summary_normalized="a third party outlet republished release notes about d2 subject two",
        stable_reference="https://example.com/third-party-outlet-two/d2-report",
        evidence_type="release_note",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 1
    assert cluster.evidence_anchor_id == "evid_1_secondary_news_uncorroborated"


def test_secondary_news_corroborated_by_first_party_authoritative_reaches_level_3() -> None:
    """D2's carve-out: when the SAME kind of isolated secondary-news item is
    clustered WITH a first-party authoritative artifact, the existing higher
    branch fires -- the secondary-news signal never suppresses genuine
    evidence, it only decides the outcome when it is the cluster's best
    signal."""
    shared_title = "D2 Corroborated By Authoritative Test Event"
    secondary_news = _make_item(
        item_id="d2-corrob-news",
        publisher_id="third-party-outlet-three",
        subject_entity_ids=["d2-corrob-subject"],
        title=shared_title,
        summary_normalized="a third party outlet briefly covered the d2 corroborated test event",
        stable_reference="https://example.com/third-party-outlet-three/d2-corrob-report",
        evidence_type="announcement",
    )
    authoritative = _make_item(
        item_id="d2-corrob-authoritative",
        publisher_id="d2-corrob-subject",
        subject_entity_ids=["d2-corrob-subject"],
        title=shared_title,
        summary_normalized=(
            "d2 corrob subject published an official spec change document for this event"
        ),
        stable_reference="https://example.com/d2-corrob-subject/spec-change",
        evidence_type="spec_change",
    )
    clusters = cluster_items([secondary_news, authoritative])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_first_party_authoritative"


def test_secondary_news_corroborated_by_independent_analysis_reaches_level_3() -> None:
    """D2's carve-out, independent-evidence variant: an isolated secondary
    news item clustered with genuine (non-subject) independent analysis of
    the same subject reaches level 3 via the independent-only branch, not
    level 1 -- corroboration, not the secondary-news item itself, decides
    the outcome."""
    shared_title = "D2 Corroborated By Independent Analysis Test Event"
    secondary_news = _make_item(
        item_id="d2-corrob-news-2",
        publisher_id="third-party-outlet-four",
        subject_entity_ids=["d2-corrob-subject-2"],
        title=shared_title,
        summary_normalized=(
            "a third party outlet briefly covered the second d2 corroborated test event"
        ),
        stable_reference="https://example.com/third-party-outlet-four/d2-corrob-report",
        evidence_type="announcement",
    )
    independent_analysis = _make_item(
        item_id="d2-corrob-analysis",
        publisher_id="independent-analyst-d2",
        subject_entity_ids=["d2-corrob-subject-2"],
        title=shared_title,
        summary_normalized=(
            "an independent analyst reviewed the second d2 corroborated test event in detail"
        ),
        stable_reference="https://example.org/independent-analyst-d2/review",
        evidence_type="independent_analysis",
    )
    clusters = cluster_items([secondary_news, independent_analysis])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_independent_only"
    assert cluster.has_independent_evidence is True


# --- repetition is not evidence (D6 clause 3) ---------------------------------


def test_repetition_of_relay_copies_is_not_evidence() -> None:
    """D6: repetition is not evidence. Five distinct relay pickups of the
    exact same non-evidentiary wire story must produce the SAME
    evidence_level (0) as a single relay item -- repeating a relay pickup
    can never manufacture evidentiary weight, no matter how many times it is
    repeated."""
    single = _make_item(
        item_id="repeat-relay-1",
        publisher_id="relay-outlet-1",
        subject_entity_ids=["repeat-subject"],
        title="Repetition Test Event Wire Pickup One",
        summary_normalized="a wire pickup of the repetition test event with generic framing one",
        stable_reference="https://example.net/relay-outlet-1/repetition-test-event",
        evidence_type="relay",
    )
    many = [single] + [
        _make_item(
            item_id=f"repeat-relay-{i}",
            publisher_id=f"relay-outlet-{i}",
            subject_entity_ids=["repeat-subject"],
            title=f"Repetition Test Event Wire Pickup {i}",
            summary_normalized=(
                f"a wire pickup of the repetition test event with generic framing {i}"
            ),
            stable_reference=f"https://example.net/relay-outlet-{i}/repetition-test-event",
            evidence_type="relay",
        )
        for i in range(2, 6)
    ]
    single_cluster = cluster_items([single])[0]
    many_cluster = cluster_items(many)[0]
    assert len(many_cluster.member_ids) == 5
    assert single_cluster.evidence_level == many_cluster.evidence_level == 0
    assert (
        single_cluster.evidence_anchor_id
        == many_cluster.evidence_anchor_id
        == "evid_0_no_qualifying_evidence"
    )


# --- Founder decision D4: self-authored analysis is first_party_commentary ---


def test_self_authored_independent_analysis_is_first_party_commentary_capped_at_2() -> None:
    """D4: an independent_analysis published BY the cluster's own subject is
    not independent -- it must be classified as first_party_commentary,
    capped at evidence_level 2 (never 3+, replacing the old
    evid_3_other_uncorroborated branch's self-analysis half), and must NOT
    set marketing_risk when contains_benefit_or_performance_claim is False
    (the default)."""
    item = _make_item(
        item_id="d4-self-analysis",
        publisher_id="d4-subject",
        subject_entity_ids=["d4-subject"],
        title="D4 Self Authored Analysis Test Event",
        summary_normalized="d4 subject published its own analysis of its recent announcement",
        stable_reference="https://example.com/d4-subject/self-analysis",
        evidence_type="independent_analysis",
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 2
    assert cluster.evidence_anchor_id == "evid_2_first_party_commentary"
    assert cluster.marketing_risk is False
    assert cluster.has_independent_evidence is False
    assert cluster.independent_publisher_count == 0


def test_self_authored_independent_analysis_with_claim_sets_marketing_risk_true() -> None:
    """D4(c): the SAME self-authored-analysis case, but with
    contains_benefit_or_performance_claim=True on the authoring item, must
    set marketing_risk=True -- the flag is the only thing that decides this,
    since a deterministic pipeline cannot infer claim type from prose."""
    item = _make_item(
        item_id="d4-self-analysis-claim",
        publisher_id="d4-subject-claim",
        subject_entity_ids=["d4-subject-claim"],
        title="D4 Self Authored Analysis With Claim Test Event",
        summary_normalized=(
            "d4 subject claim published its own analysis claiming faster performance"
        ),
        stable_reference="https://example.com/d4-subject-claim/self-analysis",
        evidence_type="independent_analysis",
        contains_benefit_or_performance_claim=True,
    )
    cluster = cluster_items([item])[0]
    assert cluster.evidence_level == 2
    assert cluster.evidence_anchor_id == "evid_2_first_party_commentary"
    assert cluster.marketing_risk is True


def test_self_authored_analysis_never_contributes_to_independence_or_level_4() -> None:
    """D4(a) regression: a self-authored independent_analysis alongside a
    genuine first-party-authoritative member must NOT elevate the cluster to
    level 4 (which requires independent_analysis/independent_rigorous from a
    NON-subject) and must NOT contribute to has_independent_evidence or
    independent_publisher_count -- confirming _is_independent's existing
    publisher-based exclusion holds for this specific case."""
    shared_title = "D4 Regression Authoritative Plus Self Analysis Test Event"
    authoritative = _make_item(
        item_id="d4-regression-authoritative",
        publisher_id="d4-regression-subject",
        subject_entity_ids=["d4-regression-subject"],
        title=shared_title,
        summary_normalized="d4 regression subject published an official spec change document",
        stable_reference="https://example.com/d4-regression-subject/spec-change",
        evidence_type="spec_change",
    )
    self_analysis = _make_item(
        item_id="d4-regression-self-analysis",
        publisher_id="d4-regression-subject",
        subject_entity_ids=["d4-regression-subject"],
        title=shared_title,
        summary_normalized="d4 regression subject published its own analysis of the spec change",
        stable_reference="https://example.com/d4-regression-subject/self-analysis",
        evidence_type="independent_analysis",
    )
    clusters = cluster_items([authoritative, self_analysis])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_first_party_authoritative"
    assert cluster.has_independent_evidence is False
    assert cluster.independent_publisher_count == 0


# --- Founder decision D1: official_api_behavior_change is authoritative -----


def test_official_api_behavior_change_is_authoritative_like_spec_change() -> None:
    """D1 (schema-only prep for M4): official_api_behavior_change was added
    to the authoritative evidence types alongside official_doc/spec_change/
    deprecation_notice/security_advisory so M4 can implement the Tier-1
    waiver contract without a schema break. Exercised here via the real
    fixture item041 (see examples/intelligence-signals-synthetic.json),
    published first-party by its own subject."""
    items = _load_fixture_items()
    item041 = next(i for i in items if i.item_id == "item041")
    assert item041.evidence_type == "official_api_behavior_change"
    clusters = cluster_items(items)
    cluster = next(c for c in clusters if "item041" in c.member_ids)
    assert cluster.evidence_level == 3
    assert cluster.evidence_anchor_id == "evid_3_first_party_authoritative"
    assert cluster.has_first_party_authoritative is True
