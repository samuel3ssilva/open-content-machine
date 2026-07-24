"""Tests for content_machine.intelligence.ranking."""

from __future__ import annotations

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
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    for anchor, inputs in inputs_by_anchor.items():
        breakdown = score_topic(inputs, profile)
        if inputs.marketing_risk:
            evidence_dim = next(d for d in breakdown.dimensions if d.dimension == "evidence")
            assert evidence_dim.effective_value == 2, (
                f"{anchor}: marketing_risk=True but evidence_level != 2"
            )


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


def test_quiet_spec_change_outranks_popular_vendor_announcement() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    quiet_spec_change = score_topic(inputs_by_anchor["item006"], profile)
    popular_announcement = score_topic(inputs_by_anchor["item010"], profile)
    assert quiet_spec_change.points_total > popular_announcement.points_total


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


def test_rank_topics_orders_best_first() -> None:
    inputs_by_anchor, _ = _ranking_inputs_by_anchor()
    profile = _profile()
    ranked = rank_topics(list(inputs_by_anchor.values()), profile)
    points = [bd.points_total for _, bd in ranked]
    assert points == sorted(points, reverse=True)
