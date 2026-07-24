"""Tests for content_machine.intelligence.ranking."""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.models import RankingInputs, RelevanceProfile
from content_machine.intelligence.ranking import WEIGHTS, rank_topics, score_topic

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"


def _profile() -> RelevanceProfile:
    return load_profile(PROFILE_FIXTURE)


def _ranking_inputs_by_anchor() -> tuple[dict[str, RankingInputs], dict[str, str]]:
    """Load the fixture and return (RankingInputs keyed by anchor item_id,
    topic_id keyed by anchor item_id)."""
    result = load_signals(VALID_FIXTURE)
    items_by_id = {item.item_id: item for item in result.items}
    clusters = cluster_items(result.items)
    inputs_by_anchor = {}
    topic_by_anchor = {}
    for cluster in clusters:
        inputs = to_ranking_inputs(cluster, items_by_id)
        inputs_by_anchor[cluster.anchor_item_id] = inputs
        topic_by_anchor[cluster.anchor_item_id] = cluster.topic_id
    return inputs_by_anchor, topic_by_anchor


def _make_inputs(**overrides: object) -> RankingInputs:
    base: dict[str, object] = {
        "topic_id": "t_test",
        "topic_tags": [],
        "change_class": "incremental_update",
        "action_required": "none",
        "evidence_level": 0,
        "has_independent_evidence": False,
        "marketing_risk": False,
        "experiment_affordance": "not_testable",
        "evidence_types": [],
        "first_seen": date(2026, 1, 1),
    }
    base.update(overrides)
    return RankingInputs.model_validate(base)


# --- pure arithmetic invariants ----------------------------------------------


def test_weights_sum_to_100() -> None:
    assert sum(WEIGHTS.values()) == 100


def test_all_fives_yields_exactly_100() -> None:
    inputs = _make_inputs(
        topic_tags=["agents", "harnesses"],
        change_class="new_capability_class",
        action_required="migration_required",
        evidence_level=5,
        has_independent_evidence=True,
        marketing_risk=False,
        experiment_affordance="local_reproducible",
        evidence_types=["benchmark_with_methodology"],
    )
    profile = RelevanceProfile.model_validate(
        {
            "profile_version": "v1",
            "territories": [{"tag": "agents", "priority": 5}],
            "live_questions": [
                {"question_id": "q1", "tags": ["agents"]},
                {"question_id": "q2", "tags": ["harnesses"]},
            ],
            "current_tooling": ["agents"],
            "experiment_budget": "high",
        }
    )
    breakdown = score_topic(inputs, profile)
    for dim in breakdown.dimensions:
        assert dim.effective_value == 5
    assert breakdown.points_total == 500
    assert breakdown.score == 100


def test_score_is_within_0_and_100_across_the_fixture() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    for inputs in inputs_by_anchor.values():
        breakdown = score_topic(inputs, profile)
        assert 0 <= breakdown.score <= 100


def test_points_total_equals_sum_of_dimension_points_exactly() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    for inputs in inputs_by_anchor.values():
        breakdown = score_topic(inputs, profile)
        assert sum(d.points for d in breakdown.dimensions) == breakdown.points_total


def test_score_equals_points_total_floor_div_5_exactly() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    for inputs in inputs_by_anchor.values():
        breakdown = score_topic(inputs, profile)
        assert breakdown.score == breakdown.points_total // 5


def test_dimension_order_is_fixed() -> None:
    inputs = _make_inputs()
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    assert [d.dimension for d in breakdown.dimensions] == [
        "relevance",
        "magnitude",
        "consequence",
        "evidence",
        "experiment",
        "curiosity",
    ]


# --- magnitude cap boundary ---------------------------------------------------


def test_magnitude_effective_capped_when_raw_exceeds_evidence_plus_one() -> None:
    # new_capability_class raw=5, evidence_level=1 -> cap 2 -> effective=2, capped.
    inputs = _make_inputs(change_class="new_capability_class", evidence_level=1)
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    mag = next(d for d in breakdown.dimensions if d.dimension == "magnitude")
    assert mag.raw_value == 5
    assert mag.effective_value == 2
    assert mag.cap_applied is not None


def test_magnitude_effective_uncapped_when_raw_equals_evidence_plus_one() -> None:
    # breaking_change raw=4, evidence_level=3 -> cap 4 -> effective=4, boundary equal.
    inputs = _make_inputs(change_class="breaking_change", evidence_level=3)
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    mag = next(d for d in breakdown.dimensions if d.dimension == "magnitude")
    assert mag.raw_value == 4
    assert mag.effective_value == 4
    assert mag.cap_applied is None


def test_magnitude_effective_uncapped_when_raw_below_evidence_plus_one() -> None:
    # incremental_update raw=2, evidence_level=5 -> cap 6 -> effective stays 2.
    inputs = _make_inputs(change_class="incremental_update", evidence_level=5)
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    mag = next(d for d in breakdown.dimensions if d.dimension == "magnitude")
    assert mag.raw_value == 2
    assert mag.effective_value == 2
    assert mag.cap_applied is None


# --- consequence floor --------------------------------------------------------


def test_consequence_floored_to_5_on_breaking_change() -> None:
    inputs = _make_inputs(change_class="breaking_change", action_required="none")
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    con = next(d for d in breakdown.dimensions if d.dimension == "consequence")
    assert con.raw_value == 0
    assert con.effective_value == 5
    assert con.floor_applied is not None


# --- marketing_risk / tier1 ----------------------------------------------------


def test_marketing_risk_set_only_at_evidence_anchor_2() -> None:
    """S2: this must be a POSITIVE claim, not one that's vacuously true when
    no fixture topic has marketing_risk at all -- assert at least one fixture
    topic actually carries it, in addition to the "only at level 2" property."""
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    saw_marketing_risk = False
    for anchor, inputs in inputs_by_anchor.items():
        breakdown = score_topic(inputs, profile)
        if inputs.marketing_risk:
            saw_marketing_risk = True
            evidence_dim = next(d for d in breakdown.dimensions if d.dimension == "evidence")
            assert evidence_dim.effective_value == 2, (
                f"{anchor}: marketing_risk=True but evidence_level != 2"
            )
    assert saw_marketing_risk, "no fixture topic has marketing_risk=True; test is vacuous"


def test_marketing_risk_is_false_for_a_rumor_only_topic() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    breakdown = score_topic(inputs_by_anchor["item040"], profile)
    evidence_dim = next(d for d in breakdown.dimensions if d.dimension == "evidence")
    assert evidence_dim.effective_value == 1
    assert inputs_by_anchor["item040"].marketing_risk is False


def test_marketing_risk_is_false_for_an_evidence_level_3_topic() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    breakdown = score_topic(inputs_by_anchor["item006"], profile)
    evidence_dim = next(d for d in breakdown.dimensions if d.dimension == "evidence")
    assert evidence_dim.effective_value == 3
    assert inputs_by_anchor["item006"].marketing_risk is False


def test_tier1_fails_only_on_marketing_risk_when_crafted_directly() -> None:
    """cluster.py never actually produces marketing_risk=True together with
    evidence_level >= 3 (marketing_risk is set ONLY at evidence anchor 2) --
    but ranking.py's Tier-1 gate must still react correctly to that
    combination on its own terms, as a pure function of RankingInputs. This
    constructs the case directly (bypassing cluster.py) so the marketing_risk
    condition is provably not a dead branch in _tier1_eligibility."""
    inputs = _make_inputs(
        topic_tags=["agents"],
        change_class="material_change",
        action_required="new_option_available",
        evidence_level=3,
        has_independent_evidence=True,
        marketing_risk=True,
        experiment_affordance="not_testable",
    )
    profile = RelevanceProfile.model_validate(
        {
            "profile_version": "v1",
            "territories": [{"tag": "agents", "priority": 5}],
            "live_questions": [],
            "current_tooling": ["agents"],
            "experiment_budget": "medium",
        }
    )
    breakdown = score_topic(inputs, profile)
    relevance_dim = next(d for d in breakdown.dimensions if d.dimension == "relevance")
    evidence_dim = next(d for d in breakdown.dimensions if d.dimension == "evidence")
    assert relevance_dim.effective_value >= 4
    assert evidence_dim.effective_value >= 3
    assert inputs.has_independent_evidence is True
    assert breakdown.tier1_eligible is False
    assert any("not marketing_risk: fail" in reason for reason in breakdown.eligibility_reasons)


def test_vendor_only_marketing_item_not_tier1_eligible() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    breakdown = score_topic(inputs_by_anchor["item005"], profile)
    assert breakdown.tier1_eligible is False


def test_independent_source_lifts_topic_out_of_marketing_and_into_tier1() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    breakdown = score_topic(inputs_by_anchor["item001"], profile)
    assert inputs_by_anchor["item001"].marketing_risk is False
    assert inputs_by_anchor["item001"].has_independent_evidence is True
    assert breakdown.tier1_eligible is True


def test_coverage_contributes_zero_points_to_the_vendor_announcement() -> None:
    """S3: the honest pair is item006 (quiet, uncorroborated first-party spec
    change) vs item001 (the heavily-covered vendor announcement) -- NOT
    item010 (an off-territory cosmetic redesign at territory priority 0),
    which is a rigged comparison that proves nothing about coverage.

    Measured after the Gate A correction-round-1 fixes: item006 and item001
    BOTH score exactly 400 points, and item001 wins the tie-break on
    relevance (5 vs 4) -- so the quiet spec change does NOT outrank the
    corroborated announcement. This test names that truth directly, and
    proves WHY the tie exists: item001's extra syndicated/relay coverage
    (item002 syndicated, item003 syndicated, item004 relay) contributes
    exactly zero additional points over item001 alone -- only item009's
    genuine independent evidence would change the score, and it is
    deliberately excluded from the "covered" cluster below."""
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    quiet_spec_change = score_topic(inputs_by_anchor["item006"], profile)
    vendor_announcement = score_topic(inputs_by_anchor["item001"], profile)
    assert quiet_spec_change.points_total == vendor_announcement.points_total == 400
    rel_quiet = next(
        d.effective_value for d in quiet_spec_change.dimensions if d.dimension == "relevance"
    )
    rel_vendor = next(
        d.effective_value for d in vendor_announcement.dimensions if d.dimension == "relevance"
    )
    assert rel_vendor > rel_quiet

    ranked = rank_topics(
        [inputs_by_anchor["item006"], inputs_by_anchor["item001"]], profile
    )
    assert ranked[0][0].topic_id == inputs_by_anchor["item001"].topic_id


def test_duplicate_coverage_members_change_item001_score_by_exactly_zero() -> None:
    """S3: item001 scored alone vs item001 scored with its syndicated
    (item002, item003) and relay (item004) coverage must be byte-identical --
    coverage volume must never leak into the score. item009 (genuine
    independent evidence) is deliberately excluded from the "covered" set:
    it's the one member that SHOULD change the score, and does (see
    test_independent_source_lifts_topic_out_of_marketing_and_into_tier1)."""
    result = load_signals(VALID_FIXTURE)
    items_by_id = {item.item_id: item for item in result.items}
    profile = _profile()

    solo_cluster = cluster_items([items_by_id["item001"]])[0]
    covered_cluster = cluster_items(
        [items_by_id[i] for i in ("item001", "item002", "item003", "item004")]
    )[0]

    solo_inputs = to_ranking_inputs(solo_cluster, {"item001": items_by_id["item001"]})
    covered_inputs = to_ranking_inputs(covered_cluster, items_by_id)

    solo_breakdown = score_topic(solo_inputs, profile)
    covered_breakdown = score_topic(covered_inputs, profile)

    assert solo_breakdown.model_dump() == covered_breakdown.model_dump()


def test_off_territory_cosmetic_item_ranks_last() -> None:
    """The off-territory (priority-0), zero-evidence-corroboration cosmetic
    redesign (item010) is the lowest-scoring topic in the whole fixture."""
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    ranked = rank_topics(list(inputs_by_anchor.values()), profile)
    assert ranked[-1][0].topic_id == inputs_by_anchor["item010"].topic_id


def test_off_territory_high_magnitude_item_held_out_by_relevance_gate() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    off_territory = score_topic(inputs_by_anchor["item027"], profile)
    relevance_dim = next(d for d in off_territory.dimensions if d.dimension == "relevance")
    magnitude_dim = next(d for d in off_territory.dimensions if d.dimension == "magnitude")
    assert relevance_dim.effective_value == 0
    assert magnitude_dim.effective_value >= 4  # genuinely high magnitude
    assert off_territory.tier1_eligible is False
    # And it must not out-rank a well-covered, in-territory topic.
    in_territory = score_topic(inputs_by_anchor["item001"], profile)
    assert in_territory.points_total > off_territory.points_total


def test_first_party_authoritative_candidate_true_and_tier1_false_for_deprecation() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    breakdown = score_topic(inputs_by_anchor["item007"], profile)
    assert breakdown.first_party_authoritative_candidate is True
    assert breakdown.tier1_eligible is False
    assert any("pending" in reason.lower() for reason in breakdown.eligibility_reasons)


def test_first_party_authoritative_candidate_fires_exactly_for_the_intended_topics() -> None:
    """S7: before the narrowing fix, ANY uncorroborated first-party-
    authoritative topic with relevance>=4 and no marketing_risk was flagged
    (4 of 19 fixture topics: item006, item007, item034, item035) -- but only
    the genuinely consequential (breaking-change-or-required-migration) cases
    were meant. item006 and item007 are both breaking_change +
    migration_required and remain flagged; item034 and item035 are ordinary
    material_change/config_or_code_change spec changes and must NOT be."""
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()

    flagged = {"item006", "item007"}
    not_flagged = {"item034", "item035"}
    for anchor in flagged:
        breakdown = score_topic(inputs_by_anchor[anchor], profile)
        assert breakdown.first_party_authoritative_candidate is True, anchor
    for anchor in not_flagged:
        breakdown = score_topic(inputs_by_anchor[anchor], profile)
        assert breakdown.first_party_authoritative_candidate is False, anchor


# --- tie-break and determinism ------------------------------------------------


def test_deterministic_tie_break_on_near_tie_pair() -> None:
    inputs_by_anchor, topic_by_anchor = _ranking_inputs_by_anchor()
    profile = _profile()
    a = inputs_by_anchor["item034"]
    b = inputs_by_anchor["item035"]
    breakdown_a = score_topic(a, profile)
    breakdown_b = score_topic(b, profile)
    assert breakdown_a.points_total == breakdown_b.points_total
    rel_a = next(d.effective_value for d in breakdown_a.dimensions if d.dimension == "relevance")
    rel_b = next(d.effective_value for d in breakdown_b.dimensions if d.dimension == "relevance")
    evid_a = next(d.effective_value for d in breakdown_a.dimensions if d.dimension == "evidence")
    evid_b = next(d.effective_value for d in breakdown_b.dimensions if d.dimension == "evidence")
    assert rel_a == rel_b
    assert evid_a == evid_b
    assert a.first_seen < b.first_seen

    ranked = rank_topics([a, b], profile)
    assert [inp.topic_id for inp, _ in ranked] == [
        topic_by_anchor["item034"],
        topic_by_anchor["item035"],
    ]


def test_rank_topics_same_input_same_output() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    all_inputs = list(inputs_by_anchor.values())
    first = rank_topics(all_inputs, profile)
    second = rank_topics(list(all_inputs), profile)
    assert [inp.topic_id for inp, _ in first] == [inp.topic_id for inp, _ in second]
    assert [bd.model_dump() for _, bd in first] == [bd.model_dump() for _, bd in second]


def test_rank_topics_shuffled_input_same_output() -> None:
    """S5: a plain list copy (as in test_rank_topics_same_input_same_output)
    preserves input order, so it cannot catch an order-dependent bug. This
    shuffles with a seeded RNG and asserts byte-identical ranked output."""
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    all_inputs = list(inputs_by_anchor.values())
    shuffled = list(all_inputs)
    rng = random.Random(20260724)
    rng.shuffle(shuffled)
    assert [i.topic_id for i in shuffled] != [i.topic_id for i in all_inputs]

    original_ranked = rank_topics(all_inputs, profile)
    shuffled_ranked = rank_topics(shuffled, profile)
    assert [inp.topic_id for inp, _ in original_ranked] == [
        inp.topic_id for inp, _ in shuffled_ranked
    ]
    assert [bd.model_dump() for _, bd in original_ranked] == [
        bd.model_dump() for _, bd in shuffled_ranked
    ]


def test_rank_topics_orders_best_first() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    ranked = rank_topics(list(inputs_by_anchor.values()), profile)
    points = [bd.points_total for _, bd in ranked]
    assert points == sorted(points, reverse=True)
