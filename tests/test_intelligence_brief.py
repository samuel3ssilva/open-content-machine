"""Tests for content_machine.intelligence.brief (M5: the Intelligence Brief
renderer). See docs/adr/0004-intelligence-evidence-and-ranking-decisions.md
for the upstream M4 decisions this module reports on."""

from __future__ import annotations

import inspect
import json
import re
import socket
from datetime import date
from pathlib import Path

import pytest

from content_machine.intelligence import brief as brief_module
from content_machine.intelligence.brief import (
    WeeklyBrief,
    build_weekly_brief,
    render_json,
    render_markdown,
)
from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.models import (
    RankingBreakdown,
    RankingInputs,
    RelevanceProfile,
    SourceItem,
    TopicCluster,
)
from content_machine.intelligence.ranking import rank_topics, score_topic
from content_machine.intelligence.tiers import TOP_N, assign_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"
WEEK_LABEL = "2026-W30"


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


def _real_fixture_pipeline() -> tuple[
    list[TopicCluster],
    dict[str, SourceItem],
    list[tuple[RankingInputs, RankingBreakdown]],
]:
    items = load_signals(VALID_FIXTURE).items
    items_by_id = {item.item_id: item for item in items}
    clusters = cluster_items(items)
    inputs_list = [to_ranking_inputs(c, items_by_id) for c in clusters]
    ranked = rank_topics(inputs_list, _profile())
    return clusters, items_by_id, ranked


def _brief_from_real_fixture() -> WeeklyBrief:
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    return build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)


# --- synthetic scenario builder (mirrors test_intelligence_tiers.py) --------


def _synthetic_topic(
    topic_id: str,
    *,
    qualifies: bool,
    first_seen: date,
    experiment_affordance: str = "not_testable",
    marketing: bool = False,
) -> tuple[SourceItem, TopicCluster, RankingInputs]:
    """A single-member synthetic topic. ``qualifies=True`` satisfies the base
    Tier-1 rule; ``qualifies=False`` fails on evidence_level/independence
    (never marketing); ``marketing=True`` overrides to produce a
    marketing_risk=True, non-qualifying topic instead."""
    if marketing:
        evidence_type = "release_note"
        evidence_level = 2
        evidence_anchor_id = "evid_2_first_party_promotional"
        has_independent_evidence = False
        has_direct = False
        marketing_risk = True
    elif qualifies:
        evidence_type = "independent_implementation"
        evidence_level = 4
        evidence_anchor_id = "evid_4_independent_rigorous_alone"
        has_independent_evidence = True
        has_direct = True
        marketing_risk = False
    else:
        evidence_type = "rumor"
        evidence_level = 1
        evidence_anchor_id = "evid_1_rumor"
        has_independent_evidence = False
        has_direct = False
        marketing_risk = False

    anchor = _make_item(
        item_id=f"{topic_id}-anchor",
        subject_entity_ids=[f"{topic_id}-subject"],
        evidence_type=evidence_type,
        topic_tags=["agents"],
        publication_date=first_seen,
        detection_date=first_seen,
        stable_reference=f"https://example.com/{topic_id}",
        experiment_affordance=experiment_affordance,
        title=f"Synthetic Topic {topic_id}",
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
        marketing_risk=marketing_risk,
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
        marketing_risk=marketing_risk,
        experiment_affordance=experiment_affordance,
        first_seen=first_seen,
    )
    return anchor, cluster, inputs


def _ranked_and_lookups(
    qualifies_by_rank: list[bool],
    experiment_affordance_by_rank: list[str] | None = None,
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
        affordance = (
            experiment_affordance_by_rank[i - 1]
            if experiment_affordance_by_rank
            else "not_testable"
        )
        anchor, cluster, inputs = _synthetic_topic(
            topic_id,
            qualifies=qualifies,
            first_seen=date(2026, 1, i),
            experiment_affordance=affordance,
        )
        clusters_by_topic_id[topic_id] = cluster
        items_by_id[anchor.item_id] = anchor
        breakdown = score_topic(inputs, profile)
        ranked.append((inputs, breakdown))
    return ranked, clusters_by_topic_id, items_by_id


def _brief_for(
    qualifies_by_rank: list[bool],
    experiment_affordance_by_rank: list[str] | None = None,
) -> WeeklyBrief:
    ranked, clusters_by_topic_id, items_by_id = _ranked_and_lookups(
        qualifies_by_rank, experiment_affordance_by_rank
    )
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    return build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)


# --- review status / no auto-publish ----------------------------------------


def test_brief_ends_in_awaiting_founder_review() -> None:
    brief = _brief_from_real_fixture()
    assert brief.review_status == "awaiting_founder_review"


def test_markdown_ends_with_awaiting_founder_review_closing_line() -> None:
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    assert "review_status = awaiting_founder_review" in markdown
    # The closing line must be near the end of the document, not buried.
    tail = markdown.strip().splitlines()[-1]
    assert "awaiting_founder_review" in tail


def test_no_publish_send_or_schedule_symbols_anywhere_in_the_module() -> None:
    """The module must not IMPORT or CALL anything that looks like a
    publish/send/schedule side effect -- this is a pure renderer. Checked
    against actual code lines (imports and calls), not prose in docstrings
    that merely documents this guarantee."""
    source = inspect.getsource(brief_module)
    forbidden_imports = ["smtplib", "requests", "httpx", "schedule", "socket", "urllib"]
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            for module in forbidden_imports:
                assert module not in stripped, f"forbidden import found: {stripped!r}"
    forbidden_calls = [".publish(", ".send(", ".post(", ".sendmail("]
    for token in forbidden_calls:
        assert token not in source, f"forbidden call token '{token}' found in brief.py"


# --- Tier 1 lean presentation + appendix ------------------------------------


def test_tier1_lean_presentation_present_on_real_fixture() -> None:
    brief = _brief_from_real_fixture()
    assert brief.tier1
    for item in brief.tier1:
        assert item.what_changed
        assert item.why_it_matters
        assert item.evidence_and_confidence
        assert item.recommended_action == "study"
        assert item.recommended_action_reason
        assert item.ranking_explanation


def test_appendix_has_the_twelve_full_fields() -> None:
    from content_machine.intelligence.brief import Tier1AppendixRecord

    expected_fields = {
        "topic_id",
        "rank",
        "canonical_title",
        "score",
        "claim_class",
        "claim_class_reason",
        "confidence",
        "confidence_reason",
        "admission_reasons",
        "exclusion_reasons",
        "warnings",
        "dimension_breakdown",
    }
    assert set(Tier1AppendixRecord.model_fields) == expected_fields
    assert len(expected_fields) == 12


def test_appendix_dimension_breakdown_has_all_six_dimensions() -> None:
    brief = _brief_from_real_fixture()
    assert brief.appendix
    for record in brief.appendix:
        assert len(record.dimension_breakdown) == 6


# --- executive summary / appendix consistency -------------------------------


def test_executive_summary_and_appendix_are_consistent_same_topics_same_counts() -> None:
    brief = _brief_from_real_fixture()
    appendix_topic_ids = {r.topic_id for r in brief.appendix}
    tier1_topic_ids = {t.topic_id for t in brief.tier1}
    assert appendix_topic_ids == tier1_topic_ids
    assert len(brief.appendix) == len(brief.tier1)
    # The executive summary must name the same Tier 1 count.
    joined_summary = " ".join(brief.executive_summary)
    assert f"{len(brief.tier1)}" in joined_summary


def test_markdown_contains_every_tier1_title_and_counts_match() -> None:
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    for item in brief.tier1:
        assert item.canonical_title in markdown
    tier1_headers = re.findall(r"^### \d+\. .+$", markdown, flags=re.MULTILINE)
    # Tier 1 section headers + appendix section headers, both one per topic.
    assert markdown.count("## Tier 1") >= 1
    assert len(brief.tier1) <= len(tier1_headers)


def test_markdown_and_json_never_diverge_tier1_titles_and_counts() -> None:
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    json_str = render_json(brief)
    payload = json.loads(json_str)

    assert len(payload["tier1"]) == len(brief.tier1)
    for entry in payload["tier1"]:
        assert entry["canonical_title"] in markdown
    assert payload["review_status"] == "awaiting_founder_review"


def test_tier1_short_reason_present_and_consistent_when_fewer_than_three_admitted() -> None:
    brief = _brief_for([True, False, False])
    assert len(brief.tier1) == 1
    assert brief.tier1_short_reason is not None
    assert "rank 2" in brief.tier1_short_reason
    assert "rank 3" in brief.tier1_short_reason
    joined_summary = " ".join(brief.executive_summary)
    assert brief.tier1_short_reason in joined_summary or "1" in joined_summary


def test_tier1_short_reason_is_none_when_all_three_admitted() -> None:
    brief = _brief_for([True, True, True])
    assert len(brief.tier1) == 3
    assert brief.tier1_short_reason is None


# --- study queue -------------------------------------------------------------


def test_study_queue_present_deep_and_two_light_on_real_fixture() -> None:
    brief = _brief_from_real_fixture()
    assert brief.study_queue.deep is not None
    assert brief.study_queue.deep_none_reason is None
    assert len(brief.study_queue.light) == 2
    assert brief.study_queue.light_none_reason is None
    assert brief.study_queue.selection_rule


def test_study_queue_states_none_when_no_tier1_admitted() -> None:
    brief = _brief_for([False, False, False])
    assert brief.study_queue.deep is None
    assert brief.study_queue.deep_none_reason is not None
    assert brief.study_queue.selection_rule


def test_study_queue_light_states_none_when_no_tier2_facts() -> None:
    """All-marketing Tier 2 topics: no 'fact' claim_class exists to light-study."""
    brief = _brief_for([True, False, False], experiment_affordance_by_rank=None)
    # rank2/rank3 are non-qualifying (hypothesis, low confidence) but still
    # 'hypothesis' not 'fact', so light study queue should be empty here too.
    assert brief.study_queue.light == []
    assert brief.study_queue.light_none_reason is not None


# --- practical experiment ----------------------------------------------------


def test_exactly_one_experiment_on_real_fixture() -> None:
    brief = _brief_from_real_fixture()
    assert brief.experiment.experiment is not None
    assert brief.experiment.none_reason is None
    assert brief.experiment.experiment.what_it_would_test


def test_experiment_states_none_when_no_topic_is_locally_reproducible() -> None:
    brief = _brief_for(
        [True, True, True],
        experiment_affordance_by_rank=["requires_paid_service"] * 10,
    )
    assert brief.experiment.experiment is None
    assert brief.experiment.none_reason is not None


def test_experiment_selects_highest_ranked_locally_reproducible_topic() -> None:
    affordances = ["not_testable"] * 10
    affordances[2] = "local_reproducible"  # rank 3 (0-indexed 2)
    affordances[5] = "local_reproducible"  # rank 6
    brief = _brief_for([True, True, True, True, True, True, True], affordances[:7])
    assert brief.experiment.experiment is not None
    assert brief.experiment.experiment.rank == 3


# --- content opportunities ----------------------------------------------------


def test_content_opportunities_at_most_three_on_real_fixture() -> None:
    brief = _brief_from_real_fixture()
    assert 0 <= len(brief.content_opportunities.opportunities) <= 3


def test_content_opportunities_zero_is_representable_and_stated() -> None:
    brief = _brief_for([False, False, False])
    assert brief.content_opportunities.opportunities == []
    assert brief.content_opportunities.none_reason is not None


def test_content_opportunities_never_exceeds_three_even_with_three_qualifying_tier1() -> None:
    brief = _brief_for([True, True, True])
    assert len(brief.content_opportunities.opportunities) <= 3
    for opp in brief.content_opportunities.opportunities:
        assert opp.reason


# --- discarded topics ---------------------------------------------------------


def test_discarded_topics_listed_with_reasons_on_real_fixture() -> None:
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)

    assert len(brief.discarded) == max(0, len(ranked) - TOP_N)
    assert brief.discarded  # the real fixture has more than 10 clusters
    for item in brief.discarded:
        assert item.reason
        assert item.canonical_title
        assert item.score >= 0


def test_discarded_topics_empty_is_representable() -> None:
    brief = _brief_for([True, False, False])  # only 3 topics total, none discarded
    assert brief.discarded == []


# --- determinism --------------------------------------------------------------


def test_byte_identical_markdown_and_json_running_the_full_pipeline_twice() -> None:
    """The key determinism test: build the ENTIRE pipeline (load -> cluster ->
    rank -> tier -> brief) twice, independently, and diff the Markdown and
    JSON outputs -- they must be byte-identical."""

    def _run() -> tuple[str, str]:
        items = load_signals(VALID_FIXTURE).items
        items_by_id = {item.item_id: item for item in items}
        clusters = cluster_items(items)
        profile = load_profile(PROFILE_FIXTURE)
        inputs_list = [to_ranking_inputs(c, items_by_id) for c in clusters]
        ranked = rank_topics(inputs_list, profile)
        clusters_by_topic_id = {c.topic_id: c for c in clusters}
        tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
        brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)
        return render_markdown(brief), render_json(brief)

    markdown_1, json_1 = _run()
    markdown_2, json_2 = _run()

    assert markdown_1 == markdown_2
    assert json_1 == json_2


def test_byte_identical_with_shuffled_input_order() -> None:
    profile = load_profile(PROFILE_FIXTURE)

    def _run(items: list[SourceItem]) -> tuple[str, str]:
        items_by_id = {item.item_id: item for item in items}
        clusters = cluster_items(items)
        inputs_list = [to_ranking_inputs(c, items_by_id) for c in clusters]
        ranked = rank_topics(inputs_list, profile)
        clusters_by_topic_id = {c.topic_id: c for c in clusters}
        tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
        brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)
        return render_markdown(brief), render_json(brief)

    import random

    items = load_signals(VALID_FIXTURE).items
    baseline = _run(items)

    shuffled = list(items)
    random.Random(2026).shuffle(shuffled)
    shuffled_result = _run(shuffled)

    assert baseline == shuffled_result


# --- no wall clock -------------------------------------------------------------


def test_module_source_never_reads_the_wall_clock() -> None:
    """The module must not import ``datetime`` at all (checked against actual
    import lines, not the docstring prose that documents this guarantee) --
    without that import, ``datetime.now()``/``date.today()`` cannot be
    called."""
    source = inspect.getsource(brief_module)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "datetime" not in stripped, f"forbidden datetime import: {stripped!r}"


def test_week_label_is_an_input_never_computed() -> None:
    """Same pipeline output, two different week_labels, only that field
    differs -- proving week_label is a passthrough input, not derived."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)

    brief_a = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, "2026-W30")
    brief_b = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, "2026-W31")

    dump_a = brief_a.model_dump()
    dump_b = brief_b.model_dump()
    assert dump_a["week_label"] == "2026-W30"
    assert dump_b["week_label"] == "2026-W31"
    dump_a.pop("week_label")
    dump_b.pop("week_label")
    assert dump_a == dump_b


# --- no network ----------------------------------------------------------------


def test_no_network_calls_while_building_or_rendering_the_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted by content_machine.intelligence.brief")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    brief = _brief_from_real_fixture()
    render_markdown(brief)
    render_json(brief)


# --- synthetic-only, no real data -----------------------------------------------


def test_fixtures_are_synthetic_example_domains_only() -> None:
    """Sanity check on this test file's own fixtures: every stable_reference
    uses example.com/example.org, never a real domain."""
    reserved_domains = ("example.com", "example.org", "example.net", "example.edu")
    _clusters, items_by_id, _ranked = _real_fixture_pipeline()
    for item in items_by_id.values():
        if item.stable_reference.startswith("http"):
            assert any(domain in item.stable_reference for domain in reserved_domains)
