"""Tests for content_machine.intelligence.tiers (M4: claim classification and
Tier admission). See docs/adr/0004-intelligence-evidence-and-ranking-decisions.md
(D1) for the Founder decision this module implements."""

from __future__ import annotations

import random
import socket
from datetime import date
from pathlib import Path

import pytest

from content_machine.intelligence import brief as brief_module
from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.models import (
    RankingBreakdown,
    RankingInputs,
    RelevanceProfile,
    SecurityFlag,
    SourceItem,
    TopicCluster,
)
from content_machine.intelligence.ranking import rank_topics, score_topic
from content_machine.intelligence.tiers import (
    TOP_N,
    TierAssignmentError,
    assign_tiers,
    build_claim_assessment,
    build_tiered_topic,
    d1_exception_fires,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"


def _profile() -> RelevanceProfile:
    return load_profile(PROFILE_FIXTURE)


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


def _make_inputs(**overrides: object) -> RankingInputs:
    base: dict[str, object] = {
        "topic_id": "t_test",
        "topic_tags": [],
        "change_class": "incremental_update",
        "action_required": "none",
        "evidence_level": 0,
        "evidence_anchor_id": "evid_0_no_qualifying_evidence",
        "has_independent_evidence": False,
        "has_first_party_authoritative": False,
        "has_direct_artifact_or_independent_source": False,
        "marketing_risk": False,
        "experiment_affordance": "not_testable",
        "evidence_types": [],
        "first_seen": date(2026, 1, 1),
    }
    base.update(overrides)
    return RankingInputs.model_validate(base)


def _make_cluster(**overrides: object) -> TopicCluster:
    base: dict[str, object] = {
        "topic_id": "t_test",
        "cluster_fingerprint": "fp_test",
        "canonical_title": "Test Topic",
        "anchor_item_id": "base",
        "member_ids": ["base"],
        "member_roles": {"base": "primary"},
        "subject_entity_ids": [],
        "first_seen": date(2026, 1, 1),
        "last_seen": date(2026, 1, 1),
    }
    base.update(overrides)
    return TopicCluster.model_validate(base)


def _real_fixture_pipeline(
    items: list[SourceItem] | None = None,
) -> tuple[
    list[TopicCluster],
    dict[str, SourceItem],
    list[tuple[RankingInputs, RankingBreakdown]],
]:
    if items is None:
        items = load_signals(VALID_FIXTURE).items
    items_by_id = {item.item_id: item for item in items}
    clusters = cluster_items(items)
    inputs_list = [to_ranking_inputs(c, items_by_id) for c in clusters]
    ranked = rank_topics(inputs_list, _profile())
    return clusters, items_by_id, ranked


def _assign_tiers_for(items: list[SourceItem]) -> list:
    clusters, items_by_id, ranked = _real_fixture_pipeline(items)
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    return assign_tiers(ranked, clusters_by_topic_id, items_by_id)


# --- D1 exception: direct unit test of the predicate ------------------------


def test_d1_predicate_fires_when_every_conjunct_is_satisfied() -> None:
    """A direct, hand-constructed test of the D1 (ADR 0004) predicate,
    bypassing the cluster pipeline entirely, so the exception path is
    exercised even though it is measured to never occur on real data."""
    anchor = _make_item(
        evidence_type="deprecation_notice",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=4,
        has_independent_evidence=True,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_4_first_party_plus_independent",
    )
    breakdown = score_topic(inputs, _profile())

    assert d1_exception_fires(anchor, inputs, breakdown) is True


def test_d1_predicate_fails_if_any_single_conjunct_fails() -> None:
    """Flip each of the six conjuncts one at a time off of an otherwise-
    satisfying base case; the predicate must fail every time."""
    base_anchor_kwargs = {
        "evidence_type": "deprecation_notice",
        "claim_directly_verifiable_in_artifact": True,
        "change_class": "breaking_change",
        "action_required": "migration_required",
    }
    base_inputs_kwargs = {
        "evidence_level": 4,
        "has_independent_evidence": True,
        "has_first_party_authoritative": True,
        "has_direct_artifact_or_independent_source": True,
        "marketing_risk": False,
        "change_class": "breaking_change",
        "action_required": "migration_required",
        "evidence_anchor_id": "evid_4_first_party_plus_independent",
    }

    def _fires(anchor_overrides: dict[str, object], inputs_overrides: dict[str, object]) -> bool:
        anchor = _make_item(**{**base_anchor_kwargs, **anchor_overrides})
        inputs = _make_inputs(**{**base_inputs_kwargs, **inputs_overrides})
        breakdown = score_topic(inputs, _profile())
        return d1_exception_fires(anchor, inputs, breakdown)

    # wrong evidence_type
    assert _fires({"evidence_type": "official_doc"}, {}) is False
    # evidence_level below 3
    assert _fires({}, {"evidence_level": 2}) is False
    # consequence below 4 (action_required with raw < 4, non-breaking so no floor)
    assert (
        _fires(
            {"change_class": "material_change", "action_required": "none"},
            {"change_class": "material_change", "action_required": "none"},
        )
        is False
    )
    # marketing_risk True
    assert _fires({}, {"marketing_risk": True}) is False
    # claim not directly verifiable
    assert _fires({"claim_directly_verifiable_in_artifact": False}, {}) is False
    # not first-party authoritative
    assert _fires({}, {"has_first_party_authoritative": False}) is False


def test_d1_exception_fires_for_exactly_these_topics_on_the_real_fixture() -> None:
    """Founder final decision (ADR 0004, Gate C): the D1 exception fires at
    evidence_level >= 3, which is now the REAL admission rule (previously
    unreachable at the originally issued evidence_level >= 4). Measured
    against the real synthetic fixture: exactly the topics whose anchor is a
    first-party-authoritative deprecation_notice/security_advisory/
    spec_change/official_api_behavior_change at evidence_level >= 3 with
    consequence >= 4 and a directly-verifiable claim."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    assert ranked, "fixture produced no clusters"
    fired_anchor_ids = set()
    for inputs, breakdown in ranked:
        cluster = clusters_by_topic_id[inputs.topic_id]
        anchor = items_by_id[cluster.anchor_item_id]
        if d1_exception_fires(anchor, inputs, breakdown):
            fired_anchor_ids.add(cluster.anchor_item_id)
    assert fired_anchor_ids == {"item006", "item007", "item034", "item035", "item041"}


def test_marketing_never_uses_the_d1_exception() -> None:
    """Even when every other D1 conjunct is satisfied, marketing_risk=True
    must block the exception -- D1 explicitly excludes benefit/performance/
    promotional claims from ever qualifying for the waiver."""
    anchor = _make_item(
        evidence_type="security_advisory",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=4,
        has_independent_evidence=True,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=True,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_4_first_party_plus_independent",
    )
    breakdown = score_topic(inputs, _profile())

    assert d1_exception_fires(anchor, inputs, breakdown) is False


def test_d1_exception_fires_at_evidence_level_exactly_3() -> None:
    """The Founder's final Gate C ruling: evidence_level == 3 (not just >= 4)
    is now sufficient for the D1 exception to fire for real, when every
    other conjunct holds -- this is the whole point of the threshold change,
    admitting an uncorroborated first-party-authoritative source."""
    anchor = _make_item(
        evidence_type="spec_change",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=3,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_3_first_party_authoritative",
    )
    breakdown = score_topic(inputs, _profile())

    assert d1_exception_fires(anchor, inputs, breakdown) is True


def test_d1_admitted_topic_carries_the_no_independent_corroboration_marker() -> None:
    """Absence of independent analysis must remain visible (Founder
    requirement, ADR 0004 D1): a topic admitted to Tier 1 solely via the D1
    exception must carry an explicit marker in admission_reasons."""
    anchor = _make_item(
        evidence_type="spec_change",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=3,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_3_first_party_authoritative",
    )
    cluster = _make_cluster()
    breakdown = score_topic(inputs, _profile())
    assert d1_exception_fires(anchor, inputs, breakdown) is True
    assert breakdown.tier1_eligible is False  # base admission fails: no independent evidence

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)
    assert topic.tier_assignment.tier1_admitted is True
    assert topic.tier_assignment.tier == "tier_1"
    assert any(
        "no independent analysis" in r for r in topic.tier_assignment.admission_reasons
    )


def test_marketing_risk_blocks_d1_admission_even_with_every_other_conjunct_satisfied() -> None:
    """Benefit/performance/self-benchmark and other promotional claims never
    qualify for the D1 waiver (ADR 0004): marketing_risk=True blocks
    admission even when every other D1 conjunct would otherwise hold."""
    anchor = _make_item(
        evidence_type="deprecation_notice",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=3,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=True,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_3_first_party_authoritative",
    )
    cluster = _make_cluster()
    breakdown = score_topic(inputs, _profile())
    assert d1_exception_fires(anchor, inputs, breakdown) is False

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)
    assert topic.tier_assignment.tier1_admitted is False
    assert topic.tier_assignment.tier != "tier_1"


def test_non_d1_evidence_type_never_admitted_via_d1() -> None:
    """An announcement/release_note (not in the D1 evidence_type set) never
    qualifies for the D1 waiver, however strong its other facts are -- e.g. a
    vendor's own promotional announcement is exactly the "benefit claim"
    category D1 must never admit."""
    anchor = _make_item(
        evidence_type="announcement",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=3,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_3_first_party_authoritative",
    )
    cluster = _make_cluster()
    breakdown = score_topic(inputs, _profile())
    assert d1_exception_fires(anchor, inputs, breakdown) is False

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)
    assert topic.tier_assignment.tier1_admitted is False
    assert topic.tier_assignment.tier != "tier_1"


def test_claim_not_directly_verifiable_never_admitted_via_d1() -> None:
    """claim_directly_verifiable_in_artifact=False blocks D1 admission even
    when every other conjunct holds."""
    anchor = _make_item(
        evidence_type="deprecation_notice",
        claim_directly_verifiable_in_artifact=False,
        change_class="breaking_change",
        action_required="migration_required",
    )
    inputs = _make_inputs(
        evidence_level=3,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        change_class="breaking_change",
        action_required="migration_required",
        evidence_anchor_id="evid_3_first_party_authoritative",
    )
    cluster = _make_cluster()
    breakdown = score_topic(inputs, _profile())
    assert d1_exception_fires(anchor, inputs, breakdown) is False

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)
    assert topic.tier_assignment.tier1_admitted is False
    assert topic.tier_assignment.tier != "tier_1"


# --- Tier 1 admission: 0, 1, 2, 3 items (no backfill) -----------------------


def _synthetic_topic(
    topic_id: str, *, qualifies: bool, first_seen: date
) -> tuple[SourceItem, TopicCluster, RankingInputs]:
    """A single-member synthetic topic. ``qualifies=True`` satisfies the base
    Tier-1 rule (relevance>=4 via the 'agents' tag, evidence>=3, independent,
    not marketing); ``qualifies=False`` fails on evidence_level/independence.
    Neither ever satisfies the D1 exception (evidence_type is never in the
    D1 set), so admission is governed purely by the base rule here."""
    if qualifies:
        evidence_type = "independent_implementation"
        evidence_level = 4
        evidence_anchor_id = "evid_4_independent_rigorous_alone"
        has_independent_evidence = True
        has_direct = True
    else:
        evidence_type = "rumor"
        evidence_level = 1
        evidence_anchor_id = "evid_1_rumor"
        has_independent_evidence = False
        has_direct = False

    anchor = _make_item(
        item_id=f"{topic_id}-anchor",
        subject_entity_ids=[f"{topic_id}-subject"],
        evidence_type=evidence_type,
        topic_tags=["agents"],
        publication_date=first_seen,
        detection_date=first_seen,
        stable_reference=f"https://example.com/{topic_id}",
    )
    cluster = _make_cluster(
        topic_id=topic_id,
        cluster_fingerprint=f"fp_{topic_id}",
        canonical_title=f"Synthetic Topic {topic_id}",
        anchor_item_id=anchor.item_id,
        member_ids=[anchor.item_id],
        member_roles={anchor.item_id: "primary"},
        independent_publisher_count=1 if has_independent_evidence else 0,
        has_independent_evidence=has_independent_evidence,
        has_direct_artifact_or_independent_source=has_direct,
        evidence_level=evidence_level,
        evidence_anchor_id=evidence_anchor_id,
        marketing_risk=False,
        first_seen=first_seen,
        last_seen=first_seen,
        cluster_size=1,
        topic_tags=["agents"],
        evidence_types=[evidence_type],
    )
    inputs = _make_inputs(
        topic_id=topic_id,
        topic_tags=["agents"],
        change_class="material_change",
        action_required="new_option_available",
        evidence_level=evidence_level,
        evidence_anchor_id=evidence_anchor_id,
        has_independent_evidence=has_independent_evidence,
        has_direct_artifact_or_independent_source=has_direct,
        marketing_risk=False,
        first_seen=first_seen,
    )
    return anchor, cluster, inputs


def _ranked_and_lookups(
    qualifies_by_rank: list[bool],
) -> tuple[
    list[tuple[RankingInputs, RankingBreakdown]],
    dict[str, TopicCluster],
    dict[str, SourceItem],
]:
    profile = _profile()
    clusters_by_topic_id: dict[str, TopicCluster] = {}
    items_by_id: dict[str, SourceItem] = {}
    ranked: list[tuple[RankingInputs, RankingBreakdown]] = []
    for i, qualifies in enumerate(qualifies_by_rank, start=1):
        topic_id = f"t_rank{i}"
        anchor, cluster, inputs = _synthetic_topic(
            topic_id, qualifies=qualifies, first_seen=date(2026, 1, i)
        )
        clusters_by_topic_id[topic_id] = cluster
        items_by_id[anchor.item_id] = anchor
        breakdown = score_topic(inputs, profile)
        ranked.append((inputs, breakdown))
    return ranked, clusters_by_topic_id, items_by_id


@pytest.mark.parametrize(
    ("qualifies_by_rank", "expected_tier1_count"),
    [
        ([False, False, False], 0),
        ([True, False, False], 1),
        ([True, True, False], 2),
        ([True, True, True], 3),
    ],
)
def test_tier1_admits_exactly_0_1_2_3_items(
    qualifies_by_rank: list[bool], expected_tier1_count: int
) -> None:
    ranked, clusters_by_topic_id, items_by_id = _ranked_and_lookups(qualifies_by_rank)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)

    tier1_topics = [t for t in tiered if t.tier_assignment.tier == "tier_1"]
    assert len(tier1_topics) == expected_tier1_count
    for rank_index, qualifies in enumerate(qualifies_by_rank, start=1):
        topic = next(t for t in tiered if t.rank == rank_index)
        if qualifies:
            assert topic.tier_assignment.tier == "tier_1"
        else:
            assert topic.tier_assignment.tier == "tier_2"


def test_no_backfill_when_only_rank1_qualifies_ranks_2_and_3_are_not_promoted() -> None:
    """NO ARTIFICIAL BACKFILL: only rank 1 passes; ranks 2 and 3 fail and must
    land in Tier 2, never promoted to fill the empty Tier-1 slots. A 4th
    topic at rank 4 that ALSO fully satisfies the base Tier-1 rule must still
    land in Tier 2 -- rank position, not admission merit, gates candidacy."""
    qualifies_by_rank = [True, False, False, True]
    ranked, clusters_by_topic_id, items_by_id = _ranked_and_lookups(qualifies_by_rank)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)

    tier1_topics = [t for t in tiered if t.tier_assignment.tier == "tier_1"]
    assert len(tier1_topics) == 1
    assert tier1_topics[0].rank == 1

    rank2 = next(t for t in tiered if t.rank == 2)
    rank3 = next(t for t in tiered if t.rank == 3)
    rank4 = next(t for t in tiered if t.rank == 4)
    assert rank2.tier_assignment.tier == "tier_2"
    assert rank3.tier_assignment.tier == "tier_2"
    # rank4 fully qualifies for Tier 1 on the merits, but rank position (not
    # in 1-3) still gates it to Tier 2 -- no backfill in either direction.
    assert rank4.tier_assignment.tier == "tier_2"
    assert rank4.tier_assignment.tier1_admitted is True


def test_tier_boundaries_for_ranks_4_to_10() -> None:
    qualifies_by_rank = [True, True, True] + [True] * 7
    ranked, clusters_by_topic_id, items_by_id = _ranked_and_lookups(qualifies_by_rank)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)

    tiers_by_rank = {t.rank: t.tier_assignment.tier for t in tiered}
    assert tiers_by_rank[1] == "tier_1"
    assert tiers_by_rank[2] == "tier_1"
    assert tiers_by_rank[3] == "tier_1"
    for rank in (4, 5, 6, 7):
        assert tiers_by_rank[rank] == "tier_2"
    for rank in (8, 9, 10):
        assert tiers_by_rank[rank] == "tier_3"


# --- Top 10 determinism and duplicate-invariance ----------------------------


def _tiered_dumps(items: list[SourceItem]) -> list[dict[str, object]]:
    return [t.model_dump() for t in _assign_tiers_for(items)]


def test_top10_deterministic_same_input_same_output() -> None:
    items = load_signals(VALID_FIXTURE).items
    first = _tiered_dumps(items)
    second = _tiered_dumps(items)
    assert first == second


def test_top10_deterministic_shuffled_input_identical_output() -> None:
    items = load_signals(VALID_FIXTURE).items
    baseline = _tiered_dumps(items)

    shuffled = list(items)
    random.Random(12345).shuffle(shuffled)
    shuffled_result = _tiered_dumps(shuffled)

    assert shuffled_result == baseline


def test_duplicate_coverage_has_no_influence_on_tiering() -> None:
    """D6: appending a duplicate (same canonical reference) of an existing
    item must not change any topic's tier assignment, score, or rank."""
    items = load_signals(VALID_FIXTURE).items
    baseline = _tiered_dumps(items)

    duplicate = items[0].model_copy(update={"item_id": "item001-appended-duplicate"})
    augmented = [*items, duplicate]
    augmented_result = _tiered_dumps(augmented)

    assert augmented_result == baseline


# --- fact / hypothesis / marketing classification on real fixture topics ---


def _anchor_and_inputs_for_item_id(
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
    anchor_item_id: str,
) -> tuple[SourceItem, RankingInputs, RankingBreakdown]:
    cluster = next(c for c in clusters_by_topic_id.values() if c.anchor_item_id == anchor_item_id)
    inputs, breakdown = next(pair for pair in ranked if pair[0].topic_id == cluster.topic_id)
    return items_by_id[anchor_item_id], inputs, breakdown


def test_fact_classification_on_a_representative_fixture_topic() -> None:
    """item007 (VendorC's deprecation notice): first-party-authoritative,
    evidence_level 3, no independent corroboration, not marketing -- a
    textbook 'fact' (attested by an artifact) at medium confidence."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    _anchor, inputs, _breakdown = _anchor_and_inputs_for_item_id(
        ranked, clusters_by_topic_id, items_by_id, "item007"
    )
    cluster = clusters_by_topic_id[inputs.topic_id]
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert claim.claim_class_reason
    assert claim.confidence_reason


def test_marketing_classification_on_a_representative_fixture_topic() -> None:
    """item005 (VendorB's flashy, uncorroborated product announcement):
    first-party-promotional with no independent evidence -- marketing_risk is
    True, so claim_class is 'marketing' at low confidence."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    _anchor, inputs, _breakdown = _anchor_and_inputs_for_item_id(
        ranked, clusters_by_topic_id, items_by_id, "item005"
    )
    assert inputs.marketing_risk is True
    cluster = clusters_by_topic_id[inputs.topic_id]
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "marketing"
    assert claim.confidence == "low"
    assert claim.claim_class_reason
    assert claim.confidence_reason


def test_hypothesis_classification_on_a_representative_fixture_topic() -> None:
    """item040 (an unverified rumor about VendorM): evidence_level 1, no
    artifact, no independent source -- hypothesis at low confidence."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    _anchor, inputs, _breakdown = _anchor_and_inputs_for_item_id(
        ranked, clusters_by_topic_id, items_by_id, "item040"
    )
    cluster = clusters_by_topic_id[inputs.topic_id]
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "hypothesis"
    assert claim.confidence == "low"
    assert claim.claim_class_reason
    assert claim.confidence_reason


def test_hypothesis_classification_for_all_roundup_relay_cluster() -> None:
    """The wire-service standards-group cluster (items 020-023) is entirely
    roundup/relay -- evidence_level 0, no artifact, no independent source:
    hypothesis at low confidence."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    _anchor, inputs, _breakdown = _anchor_and_inputs_for_item_id(
        ranked, clusters_by_topic_id, items_by_id, "item020"
    )
    assert inputs.evidence_level == 0
    cluster = clusters_by_topic_id[inputs.topic_id]
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "hypothesis"
    assert claim.confidence == "low"


# --- Fable ruling 2026-08-01 (Part B): "independence" must stop being ------
# --- rendered as "corroboration" --------------------------------------------
#
# Defect: evidence_level >= 4 does NOT imply cross-source corroboration --
# the anchor evid_4_independent_rigorous_alone reaches level 4 with a SINGLE
# source. These tests pin the fixed behaviour directly against
# _assess_confidence's inputs (evidence_anchor_id, independent_publisher_count)
# rather than the old, incorrect has_independent_evidence-only rule.


def test_confidence_single_independent_rigorous_source_is_medium() -> None:
    """evid_4_independent_rigorous_alone with independent_publisher_count=1
    (a SINGLE structurally-independent publisher, zero corroboration) must be
    capped at medium, not high -- this is the exact defect the ruling
    fixes."""
    inputs = _make_inputs(
        change_class="material_change",
        action_required="none",
        evidence_level=4,
        evidence_anchor_id="evid_4_independent_rigorous_alone",
        has_independent_evidence=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=1)
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert "single-source independent evidence" in claim.confidence_reason
    assert "corrobor" not in claim.confidence_reason


def test_confidence_two_distinct_independent_publishers_is_high() -> None:
    """The SAME anchor (evid_4_independent_rigorous_alone) reaches high
    confidence once independent_publisher_count >= 2 -- two distinct,
    structurally-independent publishers is genuine cross-source
    corroboration, even though the anchor itself is not in
    _CORROBORATED_ANCHORS."""
    inputs = _make_inputs(
        change_class="material_change",
        action_required="none",
        evidence_level=4,
        evidence_anchor_id="evid_4_independent_rigorous_alone",
        has_independent_evidence=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=2)
    assert claim.claim_class == "fact"
    assert claim.confidence == "high"
    assert "cross-source corroboration is present" in claim.confidence_reason


def test_confidence_first_party_only_is_medium() -> None:
    """A single first-party-authoritative source (evidence_level 3, no
    independent evidence at all) is attested by an artifact but falls short
    of the evidence_level >= 4 bar high confidence requires -- medium, never
    high."""
    inputs = _make_inputs(
        change_class="breaking_change",
        action_required="migration_required",
        evidence_level=3,
        evidence_anchor_id="evid_3_first_party_authoritative",
        has_independent_evidence=False,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=0)
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    # Fable ruling 2026-08-01 (follow-up, Part C): the catch-all branch no
    # longer asserts anything about single-sourcing (it used to say "no
    # second, distinct source supports the claim", which is false whenever
    # independent_publisher_count >= 2 -- see
    # test_confidence_two_independent_publishers_below_level_4_does_not_claim_single_source
    # for that exact defect). It states only the true, count-independent
    # reason: evidence_level is below the >= 4 bar.
    #
    # Fable ruling 2026-08-01 (round 3, final recheck defect, F2): the old
    # "evidence_level is below the >= 4 bar" wording was ALSO false once
    # evidence_level == 4 could reach this branch (a registry-denied member
    # can select a level-4 anchor with zero countable corroboration -- see
    # test_denied_independence_plus_authoritative_does_not_contradict_itself).
    # The wording now names the real, two-part bar instead.
    assert (
        "does not meet the high-confidence bar (evidence_level >= 4 together with "
        "cross-source corroboration)." in claim.confidence_reason
    )
    assert "no second" not in claim.confidence_reason
    assert "single source" not in claim.confidence_reason
    assert "below the >= 4 bar" not in claim.confidence_reason


def test_confidence_two_independent_publishers_below_level_4_does_not_claim_single_source() -> (
    None
):
    """QA-constructed real case (Fable ruling 2026-08-01, follow-up, Part C):
    two independent_analysis items from DIFFERENT publishers, no first-party
    member -- evid_3_independent_only, independent_publisher_count=2,
    evidence_level=3. Confidence is correctly 'medium' (the untouched
    evidence_level >= 4 gate blocks 'high'), but the OLD catch-all text said
    "no second, distinct source supports the claim" -- FALSE, there are two.
    The fixed text must claim neither 'no second' single-sourcing NOR
    (wrongly) 'high'-grade corroboration -- it states only the true,
    count-independent reason evidence_level < 4."""
    inputs = _make_inputs(
        change_class="material_change",
        action_required="none",
        evidence_level=3,
        evidence_anchor_id="evid_3_independent_only",
        has_independent_evidence=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=2)
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert "no second" not in claim.confidence_reason
    assert "single source" not in claim.confidence_reason
    assert "independent_publisher_count=2" in claim.confidence_reason


def test_confidence_first_party_plus_independent_is_high() -> None:
    """evid_4_first_party_plus_independent already combines a first-party
    artifact AND independent evidence -- two distinct sources -- so it stays
    high even with independent_publisher_count == 1 (only one of the two
    sources is the 'independent' one; the first-party source is the other)."""
    inputs = _make_inputs(
        change_class="material_change",
        action_required="new_option_available",
        evidence_level=4,
        evidence_anchor_id="evid_4_first_party_plus_independent",
        has_independent_evidence=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=1)
    assert claim.claim_class == "fact"
    assert claim.confidence == "high"
    assert "cross-source corroboration is present" in claim.confidence_reason


def test_confidence_no_independent_evidence_is_low() -> None:
    """No artifact and no independent source at all: claim_class is
    'hypothesis' (not 'fact'), so confidence is low regardless of the
    independent-publisher count."""
    inputs = _make_inputs(
        change_class="incremental_update",
        action_required="none",
        evidence_level=1,
        evidence_anchor_id="evid_1_rumor",
        has_independent_evidence=False,
        has_direct_artifact_or_independent_source=False,
        marketing_risk=False,
    )
    claim = build_claim_assessment(inputs, independent_publisher_count=0)
    assert claim.claim_class == "hypothesis"
    assert claim.confidence == "low"


def test_confidence_same_publisher_twice_counts_once_stays_medium() -> None:
    """Integration test through the REAL (untouched) cluster.py publisher
    counting: two DISTINCT, non-syndicated independent_implementation
    members from the SAME publisher_id must collapse to
    independent_publisher_count == 1 (a set, not a member count) -- so
    confidence stays capped at medium, never inflated to high by simply
    having two member records from one publisher."""
    item_a = _make_item(
        item_id="samepub-a",
        publisher_id="indy-samepub",
        subject_entity_ids=["vendor-samepub"],
        title="SamePub Rigor Test Event",
        summary_normalized="an implementation write-up describing one specific reproduction",
        stable_reference="https://example.com/samepub/a",
        evidence_type="independent_implementation",
        topic_tags=["agents"],
    )
    item_b = _make_item(
        item_id="samepub-b",
        publisher_id="indy-samepub",
        subject_entity_ids=["vendor-samepub"],
        title="SamePub Rigor Test Event",
        summary_normalized="a completely unrelated follow-up benchmark covering different metrics",
        stable_reference="https://example.com/samepub/b",
        evidence_type="benchmark_with_methodology",
        topic_tags=["agents"],
    )
    items_by_id = {item_a.item_id: item_a, item_b.item_id: item_b}
    clusters = cluster_items([item_a, item_b])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_size == 2
    assert cluster.evidence_anchor_id == "evid_4_independent_rigorous_alone"
    assert cluster.independent_publisher_count == 1

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert "corrobor" not in claim.confidence_reason


def test_confidence_syndicated_copy_never_creates_second_source() -> None:
    """Integration test through the REAL (untouched) cluster.py syndication
    exclusion: a near-identical restatement of the same independent evidence
    -- even from a DIFFERENT publisher_id -- is excluded from evidence
    counting entirely (role='syndicated'), so it must never create a second
    distinct source. independent_publisher_count stays 1 and confidence
    stays capped at medium."""
    item_origin = _make_item(
        item_id="synd-origin",
        publisher_id="indy-orig",
        subject_entity_ids=["vendor-synd"],
        title="Synd Rigor Test Event",
        summary_normalized=(
            "the independent lab reproduced the benchmark and confirmed the reported numbers"
        ),
        stable_reference="https://example.com/synd/origin",
        evidence_type="independent_implementation",
        topic_tags=["agents"],
        publication_date=date(2026, 1, 1),
        detection_date=date(2026, 1, 1),
    )
    item_copy = _make_item(
        item_id="synd-copy",
        publisher_id="indy-copy",
        subject_entity_ids=["vendor-synd"],
        title="Synd Rigor Test Event",
        summary_normalized=(
            "the independent lab reproduced the benchmark and confirmed the reported numbers"
        ),
        stable_reference="https://example.com/synd/copy",
        evidence_type="independent_implementation",
        topic_tags=["agents"],
        publication_date=date(2026, 1, 2),
        detection_date=date(2026, 1, 2),
    )
    items_by_id = {item_origin.item_id: item_origin, item_copy.item_id: item_copy}
    clusters = cluster_items([item_origin, item_copy])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_size == 2
    assert cluster.member_roles["synd-copy"] == "syndicated"
    assert cluster.evidence_anchor_id == "evid_4_independent_rigorous_alone"
    assert cluster.independent_publisher_count == 1

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert "corrobor" not in claim.confidence_reason


# --- Fable ruling 2026-08-01 (round 3, final recheck defect): a Gate E0.3 --
# --- registry-denied member must never manufacture cross-source ------------
# --- corroboration ----------------------------------------------------------
#
# Root cause (cluster._evidence_level_and_marketing_risk, unchanged in this
# commit -- the deferral is deliberate, see the module's SCOPE FENCE):
# has_independent_rigorous/has_independent_analysis are set off the raw
# evidence_type/publisher check alone, never consulting _is_independent, so
# a member with may_supply_independence=False still raises evidence_level
# and can still select a _CORROBORATED_ANCHORS anchor, while being correctly
# excluded from independent_publishers (independent_publisher_count stays
# 0). These are integration tests through the REAL (untouched) cluster.py
# pipeline -- may_supply_independence=False is set on the denied member --
# so they exercise the actual disagreement between the evidence-level flags
# and independent_publisher_count, not a hand-built RankingInputs.


def test_denied_independence_plus_authoritative_does_not_contradict_itself() -> None:
    """S1: a non-subject AUTHORITATIVE member (authoritative types are never
    in _INDEPENDENT_EVIDENCE_TYPES, so it never counts as a publisher) plus
    one Gate-E0.3-DENIED non-subject rigorous member. This reaches
    evidence_level 4 via evid_4_independent_rigorous_alone (has_any_first_party
    is False, so 'has_independent_rigorous alone' is what fires) with
    independent_publisher_count == 0. Before this fix, the confidence
    catch-all said "evidence_level is below the >= 4 bar that high
    confidence requires" while evidence_level was literally 4 in the same
    sentence -- self-contradictory. The F2 fix must state the true, two-part
    bar instead."""
    authoritative = _make_item(
        item_id="s1-authoritative",
        publisher_id="s1-standards-body",
        subject_entity_ids=["s1-vendor"],
        title="S1 Round 3 Non-Subject Authoritative Plus Denied Rigor Event",
        summary_normalized="a standards body published a security advisory about the vendor",
        stable_reference="https://example.com/s1-standards-body/advisory",
        evidence_type="security_advisory",
    )
    denied_rigorous = _make_item(
        item_id="s1-denied-rigorous",
        publisher_id="s1-analyst",
        subject_entity_ids=["s1-vendor"],
        title="S1 Round 3 Non-Subject Authoritative Plus Denied Rigor Event",
        summary_normalized="an analyst published a reproduction of the vendor's reported numbers",
        stable_reference="https://example.org/s1-analyst/reproduction",
        evidence_type="independent_implementation",
        may_supply_independence=False,
    )
    items_by_id = {
        authoritative.item_id: authoritative,
        denied_rigorous.item_id: denied_rigorous,
    }
    clusters = cluster_items([authoritative, denied_rigorous])
    assert len(clusters) == 1
    cluster = clusters[0]

    # Pin the reachable state this test depends on, so a future cluster.py
    # change that shifts these facts fails loudly here rather than silently
    # invalidating the assertions below.
    assert cluster.evidence_level == 4
    assert cluster.evidence_anchor_id == "evid_4_independent_rigorous_alone"
    assert cluster.independent_publisher_count == 0
    assert cluster.has_independent_evidence is False
    assert cluster.independence_denied_by_registry is True

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"
    assert "evidence_level=4" in claim.confidence_reason
    # The old, self-contradictory wording must be gone.
    assert "below the >= 4 bar" not in claim.confidence_reason
    # The new F2 wording must be present, verbatim.
    assert (
        "does not meet the high-confidence bar (evidence_level >= 4 together with "
        "cross-source corroboration)." in claim.confidence_reason
    )


def test_denied_independence_never_yields_high_confidence() -> None:
    """S2 (the worse case, shipping with the round-2 merge before this fix):
    the subject's own rigorous artifact (has_first_party_artifact, no promo
    so marketing_risk stays False) plus one Gate-E0.3-DENIED non-subject
    independent-analysis member. This selects evid_4_first_party_plus_
    independent -- a _CORROBORATED_ANCHORS member -- with
    independent_publisher_count == 0, because the second ('independent') leg
    was explicitly denied by the registry. Before the F1 fix, the anchor
    alone was sufficient for 'high' confidence and the brief rendered
    "corroborated across multiple distinct sources" -- exactly the class of
    false corroboration claim this branch exists to eliminate. After F1,
    this must fall through to 'medium' with no affirmative corroboration
    wording anywhere -- not in the structured confidence_reason, and not in
    the brief's rendered human phrase (brief._human_evidence_sentence,
    which is keyed off claim.confidence and therefore self-corrects once
    confidence stops being 'high' -- untouched in this commit)."""
    first_party_artifact = _make_item(
        item_id="s2-first-party-artifact",
        publisher_id="s2-vendor",
        subject_entity_ids=["s2-vendor"],
        title="S2 Round 3 First-Party Artifact Plus Denied Independence Event",
        summary_normalized="the vendor published its own benchmark with full methodology",
        stable_reference="https://example.com/s2-vendor/benchmark",
        evidence_type="benchmark_with_methodology",
        contains_benefit_or_performance_claim=False,
    )
    denied_analysis = _make_item(
        item_id="s2-denied-analysis",
        publisher_id="s2-analyst",
        subject_entity_ids=["s2-vendor"],
        title="S2 Round 3 First-Party Artifact Plus Denied Independence Event",
        summary_normalized="an analyst wrote up an independent analysis of the vendor benchmark",
        stable_reference="https://example.org/s2-analyst/analysis",
        evidence_type="independent_analysis",
        may_supply_independence=False,
    )
    items_by_id = {
        first_party_artifact.item_id: first_party_artifact,
        denied_analysis.item_id: denied_analysis,
    }
    clusters = cluster_items([first_party_artifact, denied_analysis])
    assert len(clusters) == 1
    cluster = clusters[0]

    # Pin the reachable state this test depends on.
    assert cluster.evidence_level == 4
    assert cluster.evidence_anchor_id == "evid_4_first_party_plus_independent"
    assert cluster.independent_publisher_count == 0
    assert cluster.has_independent_evidence is False
    assert cluster.marketing_risk is False
    assert cluster.independence_denied_by_registry is True

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "medium"

    affirmative_corroboration_phrases = (
        "genuine independent corroboration is present",
        "cross-source corroboration is present",
        "corroborated across multiple distinct sources",
    )
    for phrase in affirmative_corroboration_phrases:
        assert phrase not in claim.confidence_reason

    # P1/P2 (product review round 4): renamed to _human_evidence_sentence and
    # shared by Tier 1 and Tier 2 (see brief.py) -- same semantics.
    human_phrase = brief_module._human_evidence_sentence(
        inputs, claim, cluster.independent_publisher_count
    )
    for phrase in affirmative_corroboration_phrases:
        assert phrase not in human_phrase


def test_denied_independence_does_not_over_tighten_a_genuine_two_publisher_cluster() -> (
    None
):
    """Regression guard for the F1 fix's own risk: two GENUINELY independent
    (non-denied) publishers must still reach 'high' -- F1 must not
    over-tighten the legitimate two-publisher case while closing the
    registry-denial gap. Two independent_implementation members from two
    distinct, non-denied publishers, no first-party member at all --
    independent_publisher_count == 2 clears 'high' on its own, with no
    _CORROBORATED_ANCHORS membership required."""
    item_a = _make_item(
        item_id="genuine-two-pub-a",
        publisher_id="genuine-lab-a",
        subject_entity_ids=["genuine-vendor"],
        title="Genuine Two Publisher Corroboration Event",
        summary_normalized="lab a reproduced the vendor's reported numbers independently",
        stable_reference="https://example.com/genuine-lab-a/reproduction",
        evidence_type="independent_implementation",
    )
    item_b = _make_item(
        item_id="genuine-two-pub-b",
        publisher_id="genuine-lab-b",
        subject_entity_ids=["genuine-vendor"],
        title="Genuine Two Publisher Corroboration Event",
        summary_normalized="lab b ran a separate benchmark covering different metrics entirely",
        stable_reference="https://example.org/genuine-lab-b/benchmark",
        evidence_type="benchmark_with_methodology",
    )
    items_by_id = {item_a.item_id: item_a, item_b.item_id: item_b}
    clusters = cluster_items([item_a, item_b])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.evidence_anchor_id == "evid_4_independent_rigorous_alone"
    assert cluster.independent_publisher_count == 2
    assert cluster.independence_denied_by_registry is False

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "high"
    assert "cross-source corroboration is present" in claim.confidence_reason


def test_denied_independence_does_not_over_tighten_a_genuine_corroborated_anchor() -> None:
    """Second regression guard: evid_4_first_party_plus_independent with a
    COUNTABLE (non-denied) independent leg must still reach 'high' -- this is
    the exact legitimate case F1's added independent_publisher_count >= 1
    conjunct must not kill. Mirrors the existing
    test_confidence_first_party_plus_independent_is_high (hand-built
    RankingInputs) but goes through the real cluster.py pipeline instead."""
    first_party_artifact = _make_item(
        item_id="genuine-corrob-first-party",
        publisher_id="genuine-corrob-vendor",
        subject_entity_ids=["genuine-corrob-vendor"],
        title="Genuine Corroborated Anchor Event",
        summary_normalized="the vendor published its own benchmark with full methodology",
        stable_reference="https://example.com/genuine-corrob-vendor/benchmark",
        evidence_type="benchmark_with_methodology",
    )
    independent_analysis = _make_item(
        item_id="genuine-corrob-analysis",
        publisher_id="genuine-corrob-analyst",
        subject_entity_ids=["genuine-corrob-vendor"],
        title="Genuine Corroborated Anchor Event",
        summary_normalized="an analyst wrote an independent analysis of the vendor benchmark",
        stable_reference="https://example.org/genuine-corrob-analyst/analysis",
        evidence_type="independent_analysis",
    )
    items_by_id = {
        first_party_artifact.item_id: first_party_artifact,
        independent_analysis.item_id: independent_analysis,
    }
    clusters = cluster_items([first_party_artifact, independent_analysis])
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.evidence_anchor_id == "evid_4_first_party_plus_independent"
    assert cluster.independent_publisher_count == 1
    assert cluster.independence_denied_by_registry is False

    inputs = to_ranking_inputs(cluster, items_by_id)
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    assert claim.claim_class == "fact"
    assert claim.confidence == "high"
    assert "cross-source corroboration is present" in claim.confidence_reason


# --- confidence explainability -----------------------------------------------


def test_every_top10_topic_confidence_reason_is_non_empty_and_names_inputs() -> None:
    tiered = _assign_tiers_for(load_signals(VALID_FIXTURE).items)
    assert tiered
    for topic in tiered:
        assert topic.claim.confidence_reason.strip()
        assert topic.claim.claim_class_reason.strip()
        # The reason must name the specific inputs it was derived from.
        assert "claim_class" in topic.claim.confidence_reason or (
            "evidence_level" in topic.claim.confidence_reason
        )


# --- recommended action -----------------------------------------------------


def test_tier1_topics_always_recommend_study() -> None:
    tiered = _assign_tiers_for(load_signals(VALID_FIXTURE).items)
    for topic in tiered:
        if topic.tier_assignment.tier == "tier_1":
            assert topic.tier_assignment.recommended_action == "study"
            assert topic.tier_assignment.recommended_action_reason


def test_marketing_topics_always_recommend_monitor() -> None:
    tiered = _assign_tiers_for(load_signals(VALID_FIXTURE).items)
    for topic in tiered:
        if topic.claim.claim_class == "marketing":
            assert topic.tier_assignment.recommended_action == "monitor"


# --- admission/exclusion reasons and warnings always populated --------------


def test_every_top10_topic_has_admission_and_exclusion_reasons_populated() -> None:
    """Every one of the base rule's four conditions (pass or fail) must be
    visible in one of admission_reasons/exclusion_reasons -- nothing dropped,
    regardless of overall outcome."""
    tiered = _assign_tiers_for(load_signals(VALID_FIXTURE).items)
    for topic in tiered:
        total_reasons = (
            topic.tier_assignment.admission_reasons + topic.tier_assignment.exclusion_reasons
        )
        # 4 base-rule reasons + 1 D1 reason, always both non-empty in total.
        assert len(total_reasons) >= 5
        if topic.tier_assignment.tier1_admitted:
            assert topic.tier_assignment.admission_reasons
        else:
            assert topic.tier_assignment.exclusion_reasons


def test_rank1_to_3_admission_failure_warns_about_no_backfill() -> None:
    qualifies_by_rank = [True, False, True]
    ranked, clusters_by_topic_id, items_by_id = _ranked_and_lookups(qualifies_by_rank)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    rank2 = next(t for t in tiered if t.rank == 2)
    assert any("no-backfill" in w.lower() for w in rank2.tier_assignment.warnings)


# --- TierAssignmentError invariants -----------------------------------------


def test_build_tiered_topic_rejects_rank_outside_top10_window() -> None:
    anchor, cluster, inputs = _synthetic_topic("t_oob", qualifies=True, first_seen=date(2026, 1, 1))
    breakdown = score_topic(inputs, _profile())
    with pytest.raises(TierAssignmentError):
        build_tiered_topic(11, cluster, inputs, breakdown, anchor)
    with pytest.raises(TierAssignmentError):
        build_tiered_topic(0, cluster, inputs, breakdown, anchor)


# --- no network, no real data, reproducible ---------------------------------


def test_no_network_calls_during_tiers_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted by content_machine.intelligence.tiers")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    tiered = _assign_tiers_for(load_signals(VALID_FIXTURE).items)
    assert len(tiered) == TOP_N


# --- Gate E0, E0.1: security_flags propagate cluster -> TieredTopic (R7) ----


def test_e0_1_tiered_topic_carries_the_clusters_security_flags() -> None:
    """R7: flags are attached "where clusters join ranked topics" --
    exactly ``build_tiered_topic``, which receives the ``TopicCluster``
    directly -- never through ``RankingInputs``/``RankingBreakdown`` (which
    ``ranking.py`` alone produces and must stay untouched)."""
    anchor = _make_item()
    inputs = _make_inputs()
    cluster = _make_cluster(
        security_flags=(SecurityFlag.credential_shaped_text, SecurityFlag.oversized_truncated)
    )
    breakdown = score_topic(inputs, _profile())

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)

    assert topic.security_flags == (
        SecurityFlag.credential_shaped_text,
        SecurityFlag.oversized_truncated,
    )


def test_e0_1_tiered_topic_security_flags_empty_when_cluster_clean() -> None:
    anchor = _make_item()
    inputs = _make_inputs()
    cluster = _make_cluster()  # security_flags defaults to ()
    breakdown = score_topic(inputs, _profile())

    topic = build_tiered_topic(1, cluster, inputs, breakdown, anchor)

    assert topic.security_flags == ()
