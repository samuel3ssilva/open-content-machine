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
    BRIEF_VERSION,
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
    SecurityFlag,
    SourceItem,
    TopicCluster,
)
from content_machine.intelligence.ranking import rank_topics, score_topic
from content_machine.intelligence.tiers import (
    TOP_N,
    assign_tiers,
    build_tiered_topic,
    d1_exception_fires,
)

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


def _extract_markdown_section(markdown: str, heading_prefix: str) -> str:
    """Return only the lines of ``markdown`` belonging to the FIRST section
    whose heading starts with ``heading_prefix``, up to (but excluding) the
    next ``## `` heading. Used so a consistency check can assert a title
    appears in a SPECIFIC section, not merely somewhere in the whole
    document (see QA-2: a substring-anywhere check still passes even if the
    Tier 1/Appendix renderer stops emitting the title, because it also
    leaks via the Content Opportunities section)."""
    lines = markdown.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading_prefix)), None)
    assert start is not None, f"heading {heading_prefix!r} not found in markdown"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith(heading_prefix):
            end = j
            break
    return "\n".join(lines[start:end])


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


# --- Product F1: human-readable Tier 1/2 lean lines, no rubric mechanics ----

_RUBRIC_JARGON_TOKENS = (
    "raw magnitude",
    "capped to evidence_level",
    "raw consequence",
    "effective_value",
    "floored to 5",
    "evidence_level=",
    "marketing_risk=",
    "has_independent_evidence=",
)


def test_tier1_what_changed_is_the_authored_rationale_not_rubric_mechanics() -> None:
    """F1: what_changed must be the anchor's authored change_class_rationale
    (falling back to summary_normalized), never the magnitude dimension's
    rationale (which reads like a debug trace, e.g. "change_class
    'material_change' has raw magnitude 3; capped to evidence_level+1=5")."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)
    assert brief.tier1
    for item in brief.tier1:
        cluster = clusters_by_topic_id[item.topic_id]
        anchor = items_by_id[cluster.anchor_item_id]
        expected = anchor.change_class_rationale or anchor.summary_normalized
        assert item.what_changed == expected
        for token in _RUBRIC_JARGON_TOKENS:
            assert token not in item.what_changed


def test_tier1_why_it_matters_is_a_human_template_not_rubric_mechanics() -> None:
    """F1: why_it_matters must be the deterministic human template (territory
    tags, action_required phrase, claim_class, confidence), never the
    consequence dimension's rationale."""
    brief = _brief_from_real_fixture()
    assert brief.tier1
    for item in brief.tier1:
        assert item.why_it_matters.startswith("On-territory (")
        for token in _RUBRIC_JARGON_TOKENS:
            assert token not in item.why_it_matters


def test_tier2_explanation_is_the_authored_rationale_not_rubric_mechanics() -> None:
    """F1: Tier 2's 'explanation' line has the same rubric-jargon problem as
    Tier 1's what_changed (both were `magnitude.rationale`) -- fixed the same
    way."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)
    assert brief.tier2
    for item in brief.tier2:
        cluster = clusters_by_topic_id[item.topic_id]
        anchor = items_by_id[cluster.anchor_item_id]
        expected = anchor.change_class_rationale or anchor.summary_normalized
        assert item.explanation == expected
        for token in _RUBRIC_JARGON_TOKENS:
            assert token not in item.explanation


def test_tier2_practical_consequence_and_principal_evidence_are_human_not_rubric_mechanics() -> (
    None
):
    """F-B: 'practical_consequence' and 'principal_evidence' used to be the
    consequence/evidence dimensions' raw rationale strings verbatim (e.g.
    "action_required 'migration_required' has raw consequence 5" and
    "evidence_level=4 (...); has_independent_evidence=True,
    marketing_risk=False") -- the same jargon class the Founder already
    rejected once for Tier 1. Both must now be human templates built from
    already-computed structured fields, with no rubric-jargon substrings."""
    brief = _brief_from_real_fixture()
    assert brief.tier2
    for item in brief.tier2:
        for token in _RUBRIC_JARGON_TOKENS:
            assert token not in item.practical_consequence
            assert token not in item.principal_evidence
        # Sanity: both are non-empty, human-authored sentences, not blank.
        assert item.practical_consequence
        assert item.principal_evidence
        assert item.confidence in item.principal_evidence


def test_dimension_rationales_for_tier2_still_live_in_the_appendix_dimension_breakdown() -> None:
    """F-B: the raw consequence/evidence rationale strings must not be LOST
    -- Tier 2 topics that also happen to be Tier 1 don't exist (disjoint
    tiers), so this asserts the raw mechanics survive in ranking's own
    RankingBreakdown -- available to any caller via the same dimensions the
    Appendix already renders for Tier 1 (see
    test_dimension_rationales_still_live_in_the_appendix)."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    for _inputs, breakdown in ranked:
        consequence = next(d for d in breakdown.dimensions if d.dimension == "consequence")
        evidence = next(d for d in breakdown.dimensions if d.dimension == "evidence")
        assert consequence.rationale
        assert evidence.rationale


def test_dimension_rationales_still_live_in_the_appendix() -> None:
    """F1: the raw dimension mechanics must not be LOST -- only moved out of
    the lean Tier 1 lines and into the Appendix's dimension_breakdown."""
    brief = _brief_from_real_fixture()
    assert brief.appendix
    for record in brief.appendix:
        joined = " ".join(record.dimension_breakdown)
        assert "raw=" in joined and "effective=" in joined


def test_appendix_has_the_thirteen_full_fields() -> None:
    """Gate E0 (E0.1): adversarial_security_flags is a NEW, thirteenth field
    (product ruling P1) -- was twelve before this gate."""
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
        "adversarial_security_flags",
    }
    assert set(Tier1AppendixRecord.model_fields) == expected_fields
    assert len(expected_fields) == 13


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


def test_tier1_section_specifically_contains_each_admitted_tier1_title() -> None:
    """QA-2: a substring-anywhere check on the whole document still passes if
    the Tier 1 renderer stops emitting the title (it also leaks via Content
    Opportunities). This test parses ONLY the '## Tier 1' section and must
    fail if that specific renderer stops emitting a title."""
    brief = _brief_from_real_fixture()
    assert brief.tier1  # otherwise this test would vacuously pass
    markdown = render_markdown(brief)
    section = _extract_markdown_section(markdown, "## Tier 1")
    for item in brief.tier1:
        assert item.canonical_title in section


def test_appendix_section_specifically_contains_each_appendix_record_title() -> None:
    """QA-2: same strengthening as above, for the Appendix section
    specifically -- must fail if the Appendix renderer stops emitting a
    title even though it still appears elsewhere in the document."""
    brief = _brief_from_real_fixture()
    assert brief.appendix  # otherwise this test would vacuously pass
    markdown = render_markdown(brief)
    section = _extract_markdown_section(markdown, "## Appendix")
    for record in brief.appendix:
        assert record.canonical_title in section


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


def test_tier1_short_reason_states_the_failing_condition_and_is_not_doubled() -> None:
    """F4: the no-backfill reason must state WHY the fell-through rank failed
    admission (its failing condition, e.g. 'insufficient evidence level'),
    and must not repeat the phrase 'no-backfill rule' twice for the SAME
    failing rank (the old text was tiers.py's own warning, "No-backfill rule
    applied: ... per the no-backfill rule ..."). Exactly one rank (3) fails
    here, so the whole reason must contain the phrase exactly once."""
    brief = _brief_for([True, True, False])
    assert len(brief.tier1) == 2
    assert brief.tier1_short_reason is not None
    # Exactly one occurrence of "no-backfill rule" (one failing rank), not
    # two -- the old text doubled it within a single rank's own reason.
    assert brief.tier1_short_reason.lower().count("no-backfill rule") == 1
    # The reason names the actual failing base-rule category (rank 3 is a
    # synthetic 'hypothesis' topic that fails on evidence/independence).
    assert (
        "insufficient evidence level" in brief.tier1_short_reason
        or "lack of independent corroboration" in brief.tier1_short_reason
    )


def test_tier1_short_reason_one_occurrence_of_no_backfill_rule_per_failing_rank() -> None:
    """When MULTIPLE ranks fall through (ranks 2 and 3 both fail here), each
    contributes its own single "no-backfill rule applied" mention -- one per
    failing rank, never doubled within any one rank's own reason text."""
    brief = _brief_for([True, False, False])
    assert len(brief.tier1) == 1
    assert brief.tier1_short_reason is not None
    # Two failing ranks -> two per-rank mentions, not four (doubled).
    assert brief.tier1_short_reason.lower().count("no-backfill rule applied") == 2


def test_tier1_short_reason_is_none_on_real_fixture_since_d1_gate_c() -> None:
    """Since the Founder's final D1 ruling (ADR 0004, Gate C: evidence >= 3),
    rank 2 ('Core Project Spec: Breaking Change To Tool Call Framing') no
    longer falls through to Tier 2 -- it fails the base rule (no independent
    evidence) but is now admitted via the real D1 exception, so all three
    rank-1-3 candidates are Tier-1 admitted on the real fixture and no
    short-reason text is generated. Before this decision (D1 unreachable at
    evidence >= 4), this rank fell through and this test asserted a
    'lack of independent corroboration' short reason instead."""
    brief = _brief_from_real_fixture()
    assert len(brief.tier1) == 3
    assert brief.tier1_short_reason is None


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


def test_content_opportunities_exact_count_matches_documented_selection_rule_on_real_fixture() -> (
    None
):
    """QA-1: the previous version of this test (constructing exactly 3
    qualifying Tier 1 topics) was tautological -- Tier 1 never exceeds 3
    topics by construction (tiers._tier_bucket_for_rank only ever offers
    ranks 1-3 as Tier 1 candidates), so the `== 3: break` cap it claimed to
    exercise was structurally unreachable regardless of whether the
    safeguard existed. This test instead measures the REAL fixture: since
    the Founder's final D1 ruling (ADR 0004, Gate C: evidence >= 3), all 3
    Tier 1 topics there are high-confidence facts with on-territory
    relevance (all three admitted Tier 1 topics qualify -- rank 2 newly via
    the D1 exception), independently verifiable against the fixture data,
    not backfilled to hit a quota."""
    brief = _brief_from_real_fixture()
    assert len(brief.content_opportunities.opportunities) == 3
    assert len(brief.tier1) == 3
    for opp in brief.content_opportunities.opportunities:
        assert opp.reason
        assert "claim_class=fact" in opp.reason


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


# --- Fable ruling 2026-08-01 (Part B): single-reference topics must never --
# --- claim corroboration in the rendered brief ------------------------------


def test_brief_single_reference_topics_never_claim_corroboration() -> None:
    """The real 2026-W31 defect this ruling fixes: topics whose cluster has
    exactly ONE member ('each have exactly one reference', in Fable's own
    words) can structurally never combine two distinct sources -- at most one
    publisher can ever be independent, and evid_4_first_party_plus_independent
    (which needs a first-party member too) is impossible with a single
    member. Confidence must therefore never be 'high' for such a topic, and
    neither the structured claim reason nor the rendered Markdown may ever
    contain an affirmative cross-source-corroboration claim for it."""
    clusters, items_by_id, ranked = _real_fixture_pipeline()
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)
    markdown = render_markdown(brief)

    single_reference_topic_ids = {
        topic_id
        for topic_id, cluster in clusters_by_topic_id.items()
        if cluster.cluster_size == 1
    }
    assert single_reference_topic_ids  # the real fixture has single-item clusters

    affirmative_corroboration_phrases = (
        "genuine independent corroboration is present",
        "cross-source corroboration is present",
        "corroborated across multiple distinct sources",
    )

    single_reference_fact_topics = [
        topic
        for topic in tiered
        if topic.topic_id in single_reference_topic_ids and topic.claim.claim_class == "fact"
    ]
    assert single_reference_fact_topics  # the real fixture exercises this path

    for topic in single_reference_fact_topics:
        assert topic.claim.confidence != "high"
        for phrase in affirmative_corroboration_phrases:
            assert phrase not in topic.claim.confidence_reason

    # Structured Tier 1/2 fields interpolate confidence_reason verbatim into
    # the rendered Markdown block (see brief._render_tier1_section's
    # evidence_and_confidence line and _render_tier2_section's
    # principal_evidence line) -- re-check at the rendered-text level too, so
    # this proves the DOCUMENT a Founder actually reads, not just the
    # structured object it was built from.
    single_reference_titles = {
        topic.canonical_title for topic in single_reference_fact_topics
    }
    for item in brief.tier1:
        if item.canonical_title in single_reference_titles:
            for phrase in affirmative_corroboration_phrases:
                assert phrase not in item.evidence_and_confidence
    for item in brief.tier2:
        if item.canonical_title in single_reference_titles:
            for phrase in affirmative_corroboration_phrases:
                assert phrase not in item.principal_evidence

    # Defense-in-depth: re-check at the level of each item's OWN rendered
    # Markdown block (### heading up to the next heading of the same or
    # higher level), not just the structured fields it was assembled from.
    # Tier 3/Radar items render as a single "- **N. Title**" list line, not a
    # "### " heading (see brief._render_tier3_section) -- their
    # signal_paragraph is built by a wholly separate template
    # (_radar_future_event) that structurally never emits these phrases, so
    # only Tier 1/2 topics (the ones with a "### " block) are checked here.
    markdown_lines = markdown.splitlines()
    tier1_and_tier2_titles = {item.canonical_title for item in brief.tier1} | {
        item.canonical_title for item in brief.tier2
    }
    for rank_title in (
        f"{topic.rank}. {topic.canonical_title}"
        for topic in single_reference_fact_topics
        if topic.canonical_title in tier1_and_tier2_titles
    ):
        heading = f"### {rank_title}"
        start = next((i for i, line in enumerate(markdown_lines) if line == heading), None)
        assert start is not None, f"heading {heading!r} not found in rendered brief.md"
        end = len(markdown_lines)
        for j in range(start + 1, len(markdown_lines)):
            if markdown_lines[j].startswith("#"):
                end = j
                break
        block = "\n".join(markdown_lines[start:end])
        for phrase in affirmative_corroboration_phrases:
            assert phrase not in block


# --- D1 first-party-authoritative exception (ADR 0004, Founder final -------
# --- decision, Gate C: real admission at evidence_level >= 3) --------------


def test_d1_admitted_topic_appears_in_tier1_with_marker_on_real_fixture() -> None:
    """Founder final decision (Gate C): the D1 exception now fires for real
    at evidence_level >= 3. On the real synthetic fixture, rank 2 ('Core
    Project Spec: Breaking Change To Tool Call Framing', spec_change,
    evidence_level 3) fails the base Tier-1 rule (no independent evidence)
    but is admitted via the D1 exception and appears in Tier 1 carrying the
    explicit 'no independent corroboration' marker -- absence of independent
    analysis must remain visible, per the Founder's requirement."""
    brief = _brief_from_real_fixture()
    by_title = {item.canonical_title: item for item in brief.tier1}
    assert "Core Project Spec: Breaking Change To Tool Call Framing" in by_title
    item = by_title["Core Project Spec: Breaking Change To Tool Call Framing"]
    assert item.rank == 2
    assert item.first_party_authoritative_note is not None
    assert "no independent corroboration" in item.first_party_authoritative_note.lower()


def test_d1_marker_is_rendered_in_the_tier1_markdown_section() -> None:
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    section = _extract_markdown_section(markdown, "## Tier 1")
    assert "no independent corroboration" in section.lower()


def test_topics_admitted_by_the_base_rule_carry_no_d1_marker() -> None:
    """A topic admitted by the base Tier-1 rule (has independent evidence)
    must never carry the D1 'no independent corroboration' marker -- it is
    reserved for topics admitted solely via the D1 exception."""
    brief = _brief_from_real_fixture()
    for item in brief.tier1:
        if item.canonical_title != "Core Project Spec: Breaking Change To Tool Call Framing":
            assert item.first_party_authoritative_note is None


def test_d1_admission_still_respects_the_rank_window_no_backfill() -> None:
    """A topic satisfying the D1 exception outside the rank-1-3 Tier-1
    candidate window is tier1_admitted=True but must still land in Tier 2,
    never promoted -- rank position, not admission merit, gates Tier-1
    candidacy. Unchanged by the Founder's D1 threshold decision."""
    anchor = _make_item(
        evidence_type="spec_change",
        claim_directly_verifiable_in_artifact=True,
        change_class="breaking_change",
        action_required="migration_required",
    )
    d1_inputs = _make_inputs(
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
    breakdown = score_topic(d1_inputs, _profile())
    assert d1_exception_fires(anchor, d1_inputs, breakdown) is True
    assert breakdown.tier1_eligible is False  # base admission fails: no independent evidence

    topic = build_tiered_topic(5, cluster, d1_inputs, breakdown, anchor)
    assert topic.tier_assignment.tier1_admitted is True
    assert topic.tier_assignment.tier == "tier_2"


# --- Product F7: TL;DR block, study time, library-movements deferral -------


def test_tldr_present_at_most_three_lines_names_top_tier1_actions() -> None:
    brief = _brief_from_real_fixture()
    assert brief.tldr
    assert len(brief.tldr) <= 3
    assert len(brief.tldr) == len(brief.tier1)
    for line, item in zip(brief.tldr, brief.tier1, strict=True):
        assert item.canonical_title in line
        assert item.recommended_action in line


def test_tldr_states_explicitly_when_no_tier1_admitted() -> None:
    brief = _brief_for([False, False, False])
    assert brief.tier1 == []
    assert len(brief.tldr) == 1
    assert "No topics were admitted to Tier 1" in brief.tldr[0]


def test_tldr_rendered_at_the_very_top_distinct_from_executive_summary() -> None:
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    tldr_index = markdown.index("## If You Read Nothing Else")
    summary_index = markdown.index("## Executive Summary")
    assert tldr_index < summary_index
    for line in brief.tldr:
        assert line in markdown[tldr_index:summary_index]


def test_estimated_study_time_is_distinct_from_reading_time_and_documented() -> None:
    brief = _brief_from_real_fixture()
    assert isinstance(brief.estimated_study_minutes, int)
    assert brief.estimated_study_minutes != brief.estimated_reading_minutes
    assert brief.estimated_study_time_method
    assert brief.estimated_study_time_method != brief.estimated_reading_time_method


def test_estimated_study_time_is_zero_when_study_queue_is_empty() -> None:
    brief = _brief_for([False, False, False])
    assert brief.study_queue.deep is None
    assert brief.study_queue.light == []
    assert brief.estimated_study_minutes == 0


def test_library_movements_states_deferred_to_m6_when_none_wired_in() -> None:
    brief = _brief_from_real_fixture()
    assert brief.library_movements.movements == []
    assert brief.library_movements.deferred_note is not None
    assert "deferred to the topic library (M6)" in brief.library_movements.deferred_note
    markdown = render_markdown(brief)
    section = _extract_markdown_section(markdown, "## Library Movements")
    assert "deferred to the topic library (M6)" in section


def test_library_movements_render_end_to_end_when_wired_in() -> None:
    """F-C(b): when build_weekly_brief receives a REAL library_movements
    argument (the M5/M6 wiring point), it renders those movements -- not the
    M6-deferral note -- in both Markdown and JSON. Reuses
    test_intelligence_library.py's two-week simulation building blocks so
    this exercises a genuine update_library() result (promotion, a rejected
    reappearance, and staleness), not a hand-rolled movements list."""
    from content_machine.intelligence.library import (
        SCORE_CHANGE_TRIGGER_THRESHOLD,
        STALE_WEEKS,
        library_movements_for_brief,
        update_library,
    )
    from tests.test_intelligence_library import _build_week

    week1_tiered, week1_ranked, week1_clusters, week1_items = _build_week(
        [
            {
                "topic_id": "t_promote",
                "title": "Deferred Then Promoted",
                "rank": 4,
                "tier": "tier_2",
                "score": 55,
            },
            {
                "topic_id": "t_reject",
                "title": "Rejected Topic",
                "rank": 1,
                "tier": "tier_1",
                "score": 70,
            },
            {
                "topic_id": "t_stale",
                "title": "Will Go Stale",
                "rank": 8,
                "tier": "tier_3",
                "score": 40,
            },
        ]
    )
    week1 = update_library(week1_tiered, week1_ranked, week1_clusters, week1_items, "2026-W20", [])
    by_id = {e.topic_id: e for e in week1.entries}
    prior_entries = [
        by_id["t_promote"].model_copy(update={"lifecycle_status": "deferred"}),
        by_id["t_reject"].model_copy(update={"lifecycle_status": "rejected"}),
        by_id["t_stale"],  # left as-is; will go stale from absence
    ]

    week2_label = f"2026-W{20 + STALE_WEEKS}"
    week2_tiered, week2_ranked, week2_clusters, week2_items = _build_week(
        [
            {
                "topic_id": "t_promote",
                "title": "Deferred Then Promoted",
                "rank": 4,
                "tier": "tier_2",
                "score": 55 + SCORE_CHANGE_TRIGGER_THRESHOLD,
                "claim_class": "fact",
            },
            {
                "topic_id": "t_reject",
                "title": "Rejected Topic",
                "rank": 1,
                "tier": "tier_1",
                "score": 95,
            },
            # t_stale is absent this week.
        ]
    )
    week2 = update_library(
        week2_tiered, week2_ranked, week2_clusters, week2_items, week2_label, prior_entries
    )
    movements_section = library_movements_for_brief(week2)

    brief = build_weekly_brief(
        week2_tiered,
        week2_ranked,
        week2_clusters,
        week2_items,
        week2_label,
        library_movements=movements_section,
    )
    assert brief.library_movements.deferred_note is None
    assert brief.library_movements.movements

    movements_by_topic = {m.topic_id: m for m in brief.library_movements.movements}
    assert movements_by_topic["t_promote"].movement == "study_queue"
    assert "promoted from deferred" in movements_by_topic["t_promote"].reason
    assert movements_by_topic["t_reject"].movement == "rejected"
    assert "stays suppressed" in movements_by_topic["t_reject"].reason
    assert movements_by_topic["t_stale"].movement == "stale"
    assert "no new evidence" in movements_by_topic["t_stale"].reason

    markdown = render_markdown(brief)
    section = _extract_markdown_section(markdown, "## Library Movements")
    for movement in movements_by_topic.values():
        assert movement.canonical_title in section
        assert movement.reason in section

    payload = json.loads(render_json(brief))
    rendered = payload["library_movements"]["movements"]
    assert len(rendered) == len(movements_by_topic)
    rendered_reasons = {m["reason"] for m in rendered}
    for movement in movements_by_topic.values():
        assert movement.reason in rendered_reasons


# --- Product F9/F10/F12: appendix method placement, radar title, list ------
# --- rendering ---------------------------------------------------------------


def test_header_shows_compact_reading_and_study_time_pointing_to_appendix() -> None:
    """F9: the verbose method paragraph must live in the appendix, not the
    header -- the header shows only the figure and a pointer."""
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    header = markdown[: markdown.index("## Executive Summary")]
    assert f"{brief.estimated_reading_minutes} minutes (see appendix for method)" in header
    assert f"{brief.estimated_study_minutes} minutes (see appendix for method)" in header
    assert brief.estimated_reading_time_method not in header
    assert brief.estimated_study_time_method not in header
    appendix_section = _extract_markdown_section(markdown, "## Appendix")
    assert brief.estimated_reading_time_method in appendix_section
    assert brief.estimated_study_time_method in appendix_section


def test_radar_paragraph_does_not_repeat_the_title() -> None:
    """F10: the bullet already shows the title ("- **8. Title** -- ...");
    the signal_paragraph itself must not repeat it a second time."""
    brief = _brief_from_real_fixture()
    assert brief.tier3
    for item in brief.tier3:
        assert not item.signal_paragraph.startswith(item.canonical_title)


def test_appendix_lists_render_as_joined_text_not_python_repr() -> None:
    """F12: admission_reasons/exclusion_reasons/warnings must render as
    human-readable '; '-joined text (or '(none)' when empty), never a Python
    list repr like "['a', 'b']"."""
    brief = _brief_from_real_fixture()
    markdown = render_markdown(brief)
    appendix_section = _extract_markdown_section(markdown, "## Appendix")
    record_blocks = re.split(r"(?=^### \d+\. )", appendix_section, flags=re.MULTILINE)
    for record in brief.appendix:
        block = next(b for b in record_blocks if f"({record.topic_id})" in b.splitlines()[0])
        for field_name, value in (
            ("admission_reasons", record.admission_reasons),
            ("exclusion_reasons", record.exclusion_reasons),
            ("warnings", record.warnings),
        ):
            # The field's Python list repr (e.g. "['a', 'b']") must never
            # appear as the rendered value for this field -- only the
            # "; "-joined human-readable form (or "(none)" when empty).
            line = next(
                line for line in block.splitlines() if line.startswith(f"- {field_name}:")
            )
            assert line == f"- {field_name}: {'; '.join(value) if value else '(none)'}"


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


# =============================================================================
# Gate E0: security_flags propagation (E0.1), may_supply_independence (E0.3),
# and product rulings P1/P3/P4/P5.
# =============================================================================


def _security_flag_topic(
    topic_id: str,
    *,
    first_seen: date,
    security_flags: tuple[SecurityFlag, ...] = (),
    independence_denied_by_registry: bool = False,
    qualifies: bool = True,
) -> tuple[SourceItem, TopicCluster, RankingInputs]:
    """A local, single-member synthetic-topic builder for Gate E0 tests,
    deliberately kept separate from ``_synthetic_topic`` above (which has no
    security_flags/independence_denied_by_registry kwargs) so these new
    tests can never affect that helper's existing callers."""
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
        title=f"Security Flag Topic {topic_id}",
        security_flags=security_flags,
    )
    cluster = _make_cluster(
        topic_id=topic_id,
        cluster_fingerprint=f"fp_{topic_id}",
        canonical_title=f"Security Flag Topic {topic_id}",
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
        security_flags=security_flags,
        independence_denied_by_registry=independence_denied_by_registry,
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
        experiment_affordance="not_testable",
        first_seen=first_seen,
    )
    return anchor, cluster, inputs


def _security_flag_brief(specs: list[dict[str, object]]) -> WeeklyBrief:
    profile = _profile()
    clusters_by_topic_id: dict[str, TopicCluster] = {}
    items_by_id: dict[str, SourceItem] = {}
    ranked: list[tuple[RankingInputs, RankingBreakdown]] = []
    for i, spec in enumerate(specs, start=1):
        topic_id = f"t_secrank{i}"
        anchor, cluster, inputs = _security_flag_topic(
            topic_id, first_seen=date(2026, 3, i), **spec
        )
        clusters_by_topic_id[topic_id] = cluster
        items_by_id[anchor.item_id] = anchor
        ranked.append((inputs, score_topic(inputs, profile)))
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    return build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)


# --- product ruling P1: two display classes ---------------------------------


def test_p1_adversarial_flag_is_named_per_topic_on_tier1_and_its_appendix_record() -> None:
    brief = _security_flag_brief(
        [
            {"security_flags": (SecurityFlag.instruction_shaped_text,)},
            {"security_flags": (SecurityFlag.active_markup_neutralized,)},
        ]
    )
    assert len(brief.tier1) == 2
    by_rank = {t.rank: t for t in brief.tier1}
    assert by_rank[1].adversarial_security_flags == ("instruction_shaped_text",)
    # A HYGIENE flag (active_markup_neutralized) must NEVER be named per-topic,
    # even on an otherwise-adversarial-eligible Tier 1 entry.
    assert by_rank[2].adversarial_security_flags == ()

    appendix_by_rank = {r.rank: r for r in brief.appendix}
    assert appendix_by_rank[1].adversarial_security_flags == ("instruction_shaped_text",)
    assert appendix_by_rank[2].adversarial_security_flags == ()
    # Distinct field from `warnings` (P1: "Do NOT reuse TierAssignment.warnings").
    assert "instruction_shaped_text" not in appendix_by_rank[1].warnings


def test_p1_adversarial_flag_named_per_topic_on_tier2_too() -> None:
    brief = _security_flag_brief(
        [
            {"qualifies": True},
            {"qualifies": True},
            {"qualifies": False, "security_flags": (SecurityFlag.credential_shaped_text,)},
        ]
    )
    tier2_by_rank = {t.rank: t for t in brief.tier2}
    assert 3 in tier2_by_rank  # rank 3 failed admission -> falls through to Tier 2
    assert tier2_by_rank[3].adversarial_security_flags == ("credential_shaped_text",)


def test_p1_hygiene_flags_are_a_single_run_level_count_never_per_topic() -> None:
    brief = _security_flag_brief(
        [
            {"security_flags": (SecurityFlag.instruction_shaped_text,)},
            {"security_flags": (SecurityFlag.active_markup_neutralized,)},
        ]
    )
    # Exactly 1 of the 2 items carries a hygiene flag.
    assert "1 of 2" in brief.security_flag_summary.hygiene_note
    markdown = render_markdown(brief)
    # The hygiene flag's NAME is never rendered anywhere -- only the count.
    assert "active_markup_neutralized" not in markdown
    # The adversarial flag IS named, in the Tier 1 section.
    assert "instruction_shaped_text" in markdown


def test_p1_tier3_and_discarded_adversarial_flags_are_appendix_count_only() -> None:
    """Tier 3 (Radar)/discarded topics get a COUNT only -- never a per-topic
    name -- even for an adversarial flag (P1: "Tier 3 / discarded: appendix
    count only")."""
    specs: list[dict[str, object]] = [{"qualifies": True} for _ in range(7)]
    specs.append({"qualifies": True, "security_flags": (SecurityFlag.instruction_shaped_text,)})
    brief = _security_flag_brief(specs)

    assert len(brief.tier3) == 1
    radar_item = brief.tier3[0]
    assert not hasattr(radar_item, "adversarial_security_flags")
    assert "instruction_shaped_text" not in radar_item.signal_paragraph

    assert brief.security_flag_summary.tier3_and_discarded_adversarial_count == 1
    markdown = render_markdown(brief)
    # Named nowhere in the Radar section itself -- only counted in the
    # Appendix's security flag summary.
    radar_section = _extract_markdown_section(markdown, "## Radar")
    assert "instruction_shaped_text" not in radar_section


# --- product ruling P3: distinct exclusion category for registry denial ----


def _p3_scenario(*, independence_denied_by_registry: bool) -> WeeklyBrief:
    """A single rank-1 topic that fails Tier-1 admission on independence
    ALONE (relevance/evidence/marketing all pass) -- so the no-backfill
    short reason isolates exactly the exclusion category this test
    targets. ``evidence_type='official_doc'`` is deliberately excluded from
    D1's evidence-type set (tiers.py), so the D1 exception can never rescue
    this topic either."""
    first_seen = date(2026, 4, 1)
    anchor = _make_item(
        item_id="t_p3-anchor",
        subject_entity_ids=["t_p3-subject"],
        evidence_type="official_doc",
        topic_tags=["agents"],
        publication_date=first_seen,
        detection_date=first_seen,
        stable_reference="https://example.com/t_p3",
        title="P3 Registry Independence Topic",
        change_class="breaking_change",
        action_required="migration_required",
    )
    cluster = _make_cluster(
        topic_id="t_p3",
        cluster_fingerprint="fp_t_p3",
        canonical_title="P3 Registry Independence Topic",
        anchor_item_id=anchor.item_id,
        member_ids=[anchor.item_id],
        member_roles={anchor.item_id: "primary"},
        independent_publisher_count=0,
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        evidence_level=3,
        evidence_anchor_id="evid_3_first_party_authoritative",
        marketing_risk=False,
        first_seen=first_seen,
        last_seen=first_seen,
        cluster_size=1,
        topic_tags=["agents"],
        evidence_types=["official_doc"],
        independence_denied_by_registry=independence_denied_by_registry,
    )
    inputs = _make_inputs(
        topic_id="t_p3",
        topic_tags=["agents"],
        change_class="breaking_change",
        action_required="migration_required",
        evidence_level=3,
        evidence_anchor_id="evid_3_first_party_authoritative",
        has_independent_evidence=False,
        has_first_party_authoritative=True,
        has_direct_artifact_or_independent_source=True,
        marketing_risk=False,
        experiment_affordance="not_testable",
        first_seen=first_seen,
    )
    profile = _profile()
    breakdown = score_topic(inputs, profile)
    # Sanity: independence must be the ONLY failing base condition, or this
    # test would not isolate the category it targets.
    assert breakdown.tier1_eligible is False
    failing = [r for r in breakdown.eligibility_reasons if r.endswith(": fail")]
    assert len(failing) == 1
    assert failing[0].startswith("has_independent_evidence")

    clusters_by_topic_id = {"t_p3": cluster}
    items_by_id = {anchor.item_id: anchor}
    ranked = [(inputs, breakdown)]
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    return build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, WEEK_LABEL)


def test_p3_registry_denied_independence_gets_a_distinct_exclusion_category() -> None:
    brief = _p3_scenario(independence_denied_by_registry=True)
    assert brief.tier1 == []
    assert len(brief.tier2) == 1
    assert brief.tier1_short_reason is not None
    assert "registry-authoris" in brief.tier1_short_reason
    assert "authorise an independent/community source" in brief.tier1_short_reason
    # The generic (pre-E0.3) wording must NOT appear -- this is the whole
    # point of P3's distinct category (opposite actions: authorise the
    # existing source vs. find a new one).
    assert "lack of independent corroboration" not in brief.tier1_short_reason

    discarded_reason_source = brief.discarded[0].reason if brief.discarded else None
    assert discarded_reason_source is None  # only 1 topic exists; nothing is discarded here


def test_p3_ordinary_independence_failure_keeps_the_generic_category() -> None:
    """The complementary case: when the registry did NOT deny independence
    (no corroborating source has simply been found yet), the ORIGINAL,
    generic category is unchanged -- proving P3 only ADDS a category, it
    never removes or renames the existing one."""
    brief = _p3_scenario(independence_denied_by_registry=False)
    assert brief.tier1_short_reason is not None
    assert "lack of independent corroboration" in brief.tier1_short_reason
    assert "registry-authoris" not in brief.tier1_short_reason


# --- product ruling P4: BRIEF_VERSION bump -----------------------------------


def test_p4_brief_version_was_bumped_for_the_new_security_field() -> None:
    assert BRIEF_VERSION != "gate-b-m5-1"
    brief = _brief_from_real_fixture()
    assert brief.brief_version == BRIEF_VERSION


# --- product ruling P5: empty Tier 1 tldr names the cause, not just effect -


def test_p5_empty_tier1_tldr_names_the_governed_source_count_and_expected_framing() -> None:
    brief = _brief_for([False, False, False])
    assert brief.tier1 == []
    assert len(brief.tldr) == 1
    line = brief.tldr[0]
    # The old, effect-only copy is preserved verbatim (existing test pins
    # this substring) ...
    assert "No topics were admitted to Tier 1" in line
    # ... but P5 requires the CAUSE to be named too: this is the expected,
    # correct result, and the governed-source count explains why.
    assert "expected" in line.lower()
    assert "registry-governed" in line
    assert "Of 3 source item(s) considered" in line


def test_p5_governed_source_counts_reflect_real_may_supply_independence_values() -> None:
    """The counts in the P5 tldr line are not just decorative prose -- they
    must actually reflect ``SourceItem.may_supply_independence`` across
    ``items_by_id``."""
    from content_machine.intelligence.brief import _build_tldr

    items_by_id = {
        "a": _make_item(item_id="a", may_supply_independence=True),
        "b": _make_item(item_id="b", may_supply_independence=False),
        "c": _make_item(item_id="c", may_supply_independence=None),
    }
    tldr = _build_tldr([], items_by_id)
    assert len(tldr) == 1
    line = tldr[0]
    assert "Of 3 source item(s) considered" in line
    assert "2 are registry-governed" in line
    assert "only 1 of those are authorised" in line
