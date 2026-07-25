"""Tests for content_machine.intelligence.library (M6: the persistent topic
library with lifecycle and audit trail). Builds directly-constructed
TieredTopic/RankingInputs/RankingBreakdown fixtures (mirroring
test_intelligence_tiers.py's approach) rather than running the full
ranking pipeline, since library.py's own contract is that it consumes
already-computed facts and never re-derives ranking/tiering/evidence."""

from __future__ import annotations

import inspect
import socket
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from content_machine.intelligence import library as library_module
from content_machine.intelligence.brief import LibraryMovementsSection
from content_machine.intelligence.library import (
    NORMALIZED_SUMMARY_MAX_CHARS,
    SCORE_CHANGE_TRIGGER_THRESHOLD,
    STALE_WEEKS,
    TOPIC_MERGE_JACCARD_THRESHOLD,
    TopicLibraryEntry,
    append_audit_rows,
    append_score_history_rows,
    build_movements_document,
    build_normalized_summary,
    effective_rank_score,
    library_movements_for_brief,
    load_topics,
    render_movements_markdown,
    save_topics,
    update_library,
)
from content_machine.intelligence.models import (
    ClaimAssessment,
    ClaimClass,
    ConfidenceLevel,
    DimensionScore,
    LiveQuestion,
    RankingBreakdown,
    RankingInputs,
    RecommendedAction,
    RelevanceProfile,
    SourceItem,
    TierAssignment,
    TieredTopic,
    TierName,
    TopicCluster,
)

_DIMENSION_NAMES = ("relevance", "magnitude", "consequence", "evidence", "experiment", "curiosity")


def _dimension(name: str, effective: int, weight: int = 10) -> DimensionScore:
    return DimensionScore(
        dimension=name,
        raw_value=effective,
        effective_value=effective,
        cap_applied=None,
        floor_applied=None,
        anchor_id=f"{name}_anchor",
        anchor_text=f"{name} anchor text",
        inputs={},
        rationale=f"{name} dimension rationale",
        weight=weight,
        points=weight * effective,
    )


def _breakdown(*, score: int, relevance_effective: int = 5) -> RankingBreakdown:
    dimensions = [
        _dimension(name, relevance_effective if name == "relevance" else 4)
        for name in _DIMENSION_NAMES
    ]
    return RankingBreakdown(
        dimensions=dimensions,
        points_total=score,
        score=score,
        rubric_version="test-1",
        weights_version="test-1",
        taxonomy_version="test-1",
        profile_version="test-1",
        tier1_eligible=True,
        eligibility_reasons=[],
        first_party_authoritative_candidate=False,
        tie_break_key="tie",
        ranking_explanation=f"Test ranking explanation for score={score}.",
    )


def _make_item(item_id: str, stable_reference: str) -> SourceItem:
    return SourceItem(
        item_id=item_id,
        source_type="feed",
        source_category="vendor_blog",
        publisher_id=f"pub-{item_id}",
        subject_entity_ids=[f"subject-{item_id}"],
        title=f"UNPERSISTABLE TITLE PROSE for {item_id}",
        summary_normalized=f"UNPERSISTABLE SUMMARY PROSE for {item_id}",
        publication_date=date(2026, 1, 1),
        detection_date=date(2026, 1, 1),
        stable_reference=stable_reference,
        evidence_type="announcement",
        change_class="material_change",
        change_class_rationale="UNPERSISTABLE RATIONALE PROSE",
        action_required="none",
        experiment_affordance="not_testable",
        topic_tags=["agents"],
        contains_benefit_or_performance_claim=False,
        claim_directly_verifiable_in_artifact=False,
    )


def _make_cluster(
    topic_id: str,
    anchor_item_id: str,
    title: str,
    subject_entity_ids: list[str] | None = None,
) -> TopicCluster:
    return TopicCluster(
        topic_id=topic_id,
        cluster_fingerprint=f"fp_{topic_id}",
        canonical_title=title,
        anchor_item_id=anchor_item_id,
        member_ids=[anchor_item_id],
        member_roles={anchor_item_id: "primary"},
        subject_entity_ids=subject_entity_ids if subject_entity_ids is not None else [],
        first_seen=date(2026, 1, 1),
        last_seen=date(2026, 1, 1),
    )


def _make_inputs(
    topic_id: str,
    *,
    evidence_level: int = 4,
    experiment_affordance: str = "not_testable",
    action_required: str = "none",
    change_class: str = "material_change",
    has_independent_evidence: bool = True,
) -> RankingInputs:
    return RankingInputs(
        topic_id=topic_id,
        topic_tags=["agents"],
        change_class=change_class,
        action_required=action_required,
        evidence_level=evidence_level,
        evidence_anchor_id=f"evid_{evidence_level}",
        has_independent_evidence=has_independent_evidence,
        has_first_party_authoritative=False,
        has_direct_artifact_or_independent_source=has_independent_evidence,
        marketing_risk=False,
        experiment_affordance=experiment_affordance,
        evidence_types=["announcement"],
        first_seen=date(2026, 1, 1),
    )


def _make_tiered(
    topic_id: str,
    title: str,
    *,
    rank: int,
    tier: TierName,
    score: int,
    claim_class: ClaimClass = "fact",
    confidence: ConfidenceLevel = "high",
    recommended_action: RecommendedAction = "study",
) -> TieredTopic:
    claim = ClaimAssessment(
        claim_class=claim_class,
        claim_class_reason="test claim reason",
        confidence=confidence,
        confidence_reason="test confidence reason",
    )
    tier_assignment = TierAssignment(
        tier=tier,
        tier_label=tier,
        rank=rank,
        tier1_admitted=(tier == "tier_1"),
        admission_reasons=[],
        exclusion_reasons=[],
        warnings=[],
        d1_exception_fired=False,
        recommended_action=recommended_action,
        recommended_action_reason="test recommended action reason",
    )
    return TieredTopic(
        rank=rank,
        topic_id=topic_id,
        canonical_title=title,
        score=score,
        claim=claim,
        tier_assignment=tier_assignment,
    )


def _build_week(
    specs: list[dict[str, Any]],
) -> tuple[
    list[TieredTopic],
    list[tuple[RankingInputs, RankingBreakdown]],
    dict[str, TopicCluster],
    dict[str, SourceItem],
]:
    tiered: list[TieredTopic] = []
    ranked: list[tuple[RankingInputs, RankingBreakdown]] = []
    clusters_by_topic_id: dict[str, TopicCluster] = {}
    items_by_id: dict[str, SourceItem] = {}
    for spec in specs:
        topic_id = spec["topic_id"]
        anchor = _make_item(
            f"{topic_id}-item", spec.get("stable_reference", f"https://example.com/{topic_id}")
        )
        items_by_id[anchor.item_id] = anchor
        clusters_by_topic_id[topic_id] = _make_cluster(
            topic_id, anchor.item_id, spec["title"], spec.get("subject_entity_ids")
        )
        inputs = _make_inputs(
            topic_id,
            evidence_level=spec.get("evidence_level", 4),
            experiment_affordance=spec.get("experiment_affordance", "not_testable"),
            action_required=spec.get("action_required", "none"),
            change_class=spec.get("change_class", "material_change"),
            has_independent_evidence=spec.get("has_independent_evidence", True),
        )
        breakdown = _breakdown(
            score=spec["score"], relevance_effective=spec.get("relevance_effective", 5)
        )
        ranked.append((inputs, breakdown))
        tiered.append(
            _make_tiered(
                topic_id,
                spec["title"],
                rank=spec["rank"],
                tier=spec["tier"],
                score=spec["score"],
                claim_class=spec.get("claim_class", "fact"),
                confidence=spec.get("confidence", "high"),
                recommended_action=spec.get("recommended_action", "study"),
            )
        )
    return tiered, ranked, clusters_by_topic_id, items_by_id


def _lifecycle_transitions(entry: TopicLibraryEntry) -> list[Any]:
    return [e for e in entry.audit_events if e.event_type == "lifecycle_transition"]


# --- basic lifecycle: new / recompute ---------------------------------------


def test_new_topic_gets_new_status_with_a_created_audit_event() -> None:
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.lifecycle_status == "new"
    assert entry.first_seen == "2026-W10"
    assert entry.last_updated == "2026-W10"
    assert len(entry.audit_events) == 1
    assert entry.audit_events[0].event_type == "created"
    assert entry.audit_events[0].reason
    assert len(result.new_audit_rows) == 1
    assert len(result.new_score_history_rows) == 1


def test_recompute_promotes_out_of_new_on_the_second_week() -> None:
    """A 'new' topic that keeps appearing is recomputed to a data-driven
    status (never stays 'new' forever)."""
    spec = [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    tiered, ranked, clusters, items = _build_week(spec)
    week1 = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    week2 = update_library(tiered, ranked, clusters, items, "2026-W11", week1.entries)
    entry = week2.entries[0]
    assert entry.lifecycle_status == "selected_for_brief"  # tier_1 -> selected_for_brief
    transitions = _lifecycle_transitions(entry)
    assert len(transitions) == 1
    assert transitions[0].from_status == "new"
    assert transitions[0].to_status == "selected_for_brief"
    assert transitions[0].reason


# --- score history: append-only, audited ------------------------------------


def test_score_history_is_append_only_and_score_changes_are_audited() -> None:
    spec1 = [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    spec2 = [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 91}]
    tiered1, ranked1, clusters1, items1 = _build_week(spec1)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(spec2)
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)

    entry = week2.entries[0]
    assert [(h.week_label, h.score) for h in entry.score_history] == [
        ("2026-W10", 80),
        ("2026-W11", 91),
    ]
    score_change_events = [e for e in entry.audit_events if e.event_type == "score_change"]
    assert len(score_change_events) == 1
    assert score_change_events[0].from_status == "80"
    assert score_change_events[0].to_status == "91"
    assert "80" in score_change_events[0].reason and "91" in score_change_events[0].reason


def test_every_audit_event_has_a_non_empty_reason() -> None:
    spec1 = [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    spec2 = [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 60}]
    tiered1, ranked1, clusters1, items1 = _build_week(spec1)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(spec2)
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    for entry in week2.entries:
        for event in entry.audit_events:
            assert event.reason
            assert event.to_status


# --- no raw bodies persisted -------------------------------------------------


def test_no_raw_bodies_or_prose_beyond_canonical_title_is_persisted() -> None:
    """Only the allowlisted fields may appear; item titles, summaries, and
    change_class_rationale text (which could carry a full artifact body)
    must never leak into the persisted entry.

    This pins the v0.1 field set as a conservative RETENTION-SCOPE choice,
    not a declaration that a normalized summary is forbidden forever: spec
    Sections 3/8 authorize persisting a normalized (generated) summary
    (it is not a raw body under Founder decision D), and one may be added
    in v0.2 for editorial reconsideration -- see ADR 0004 D7. If that
    happens, this test's ``forbidden_fields`` set is expected to gain a
    new entry rather than stay frozen at today's field list."""
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    entry = result.entries[0]
    serialized = entry.model_dump_json()

    forbidden_fields = {
        "topic_id",
        "canonical_title",
        "source_references",
        "first_seen",
        "last_updated",
        "current_score",
        "score_history",
        "ranking_explanation",
        "editorial_territory",
        "evidence_level",
        "evidence_anchor_id",
        "claim_class",
        "learning_value",
        "experiment_possibility",
        "content_angle_possibilities",
        "reason_not_selected",
        "reconsideration_condition",
        "freshness",
        "lifecycle_status",
        "audit_events",
        # v0.2 (ADR 0004 D7/D8): normalized_summary is the authorized,
        # length-bounded, markup-neutralized derived summary; the rest are
        # structural facts (ids/tags/rank/tier), never raw prose.
        "subject_entity_ids",
        "aliases",
        "merged_into",
        "relevance_reasons",
        "professional_connection",
        "live_question_connections",
        "profile_version",
        "normalized_summary",
        "last_rank",
        "last_tier",
    }
    assert set(TopicLibraryEntry.model_fields) == forbidden_fields
    assert "UNPERSISTABLE TITLE PROSE" not in serialized
    assert "UNPERSISTABLE SUMMARY PROSE" not in serialized
    assert "UNPERSISTABLE RATIONALE PROSE" not in serialized
    # source_references carry ONLY stable_reference + source_category.
    for ref in entry.source_references:
        assert set(type(ref).model_fields) == {"stable_reference", "source_category"}


# --- determinism --------------------------------------------------------------


def test_deterministic_same_inputs_produce_byte_identical_output() -> None:
    spec1 = [
        {"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80},
        {"topic_id": "t2", "title": "Topic Two", "rank": 4, "tier": "tier_2", "score": 60},
    ]
    tiered1, ranked1, clusters1, items1 = _build_week(spec1)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    spec2 = [
        {"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 85},
        {"topic_id": "t2", "title": "Topic Two", "rank": 5, "tier": "tier_2", "score": 58},
    ]

    def _run_week2() -> tuple[str, str]:
        tiered2, ranked2, clusters2, items2 = _build_week(spec2)
        result = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
        entries_json = "\n".join(e.model_dump_json() for e in result.entries)
        audit_json = "\n".join(a.model_dump_json() for a in result.new_audit_rows)
        return entries_json, audit_json

    run_a = _run_week2()
    run_b = _run_week2()
    assert run_a == run_b


# --- deferred: returns only under a trigger ---------------------------------


def test_deferred_stays_deferred_without_a_trigger() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 60}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    deferred_entry = week1.entries[0].model_copy(update={"lifecycle_status": "deferred"})

    # Week 2: same score, same evidence level, same claim class -- no trigger.
    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 61}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [deferred_entry])
    entry = week2.entries[0]
    assert entry.lifecycle_status == "deferred"
    lifecycle_transitions = _lifecycle_transitions(entry)
    assert lifecycle_transitions == []


def test_deferred_returns_on_score_change_trigger() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 60}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    deferred_entry = week1.entries[0].model_copy(update={"lifecycle_status": "deferred"})

    # Week 2: score jumps by >= SCORE_CHANGE_TRIGGER_THRESHOLD points.
    bumped_score = 60 + SCORE_CHANGE_TRIGGER_THRESHOLD
    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "t1",
                "title": "Topic One",
                "rank": 4,
                "tier": "tier_2",
                "score": bumped_score,
                "claim_class": "fact",
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [deferred_entry])
    entry = week2.entries[0]
    assert entry.lifecycle_status != "deferred"
    assert entry.lifecycle_status == "study_queue"  # tier_2 + fact -> study_queue
    transitions = _lifecycle_transitions(entry)
    assert len(transitions) == 1
    assert transitions[0].from_status == "deferred"
    assert "promoted from deferred" in transitions[0].reason
    assert "score increased by" in transitions[0].reason


def test_deferred_returns_on_evidence_status_trigger() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "t1",
                "title": "Topic One",
                "rank": 4,
                "tier": "tier_2",
                "score": 60,
                "claim_class": "hypothesis",
                "evidence_level": 1,
            }
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    deferred_entry = week1.entries[0].model_copy(update={"lifecycle_status": "deferred"})

    # Week 2: same score (no score trigger), but claim reclassified to fact
    # with higher evidence -- the evidence-status trigger.
    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "t1",
                "title": "Topic One",
                "rank": 4,
                "tier": "tier_2",
                "score": 60,
                "claim_class": "fact",
                "evidence_level": 4,
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [deferred_entry])
    entry = week2.entries[0]
    assert entry.lifecycle_status != "deferred"
    transitions = _lifecycle_transitions(entry)
    assert len(transitions) == 1
    assert "promoted from deferred" in transitions[0].reason
    assert (
        "evidence level improved" in transitions[0].reason
        or "reclassified" in transitions[0].reason
    )


# --- rejected: never returns automatically ----------------------------------


def test_rejected_never_returns_automatically_even_with_a_large_score_increase() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 40}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    rejected_entry = week1.entries[0].model_copy(update={"lifecycle_status": "rejected"})

    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 99}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [rejected_entry])
    entry = week2.entries[0]
    assert entry.lifecycle_status == "rejected"
    reappearance_events = [e for e in entry.audit_events if e.event_type == "rejected_reappearance"]
    assert len(reappearance_events) == 1
    assert "stays suppressed" in reappearance_events[0].reason
    # A rejected topic's score is never updated -- it isn't tracked as current.
    assert entry.current_score == 40


def test_rejected_topic_never_resurfaces_across_many_weeks() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 40}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    rejected_entry = week1.entries[0].model_copy(update={"lifecycle_status": "rejected"})
    state = [rejected_entry]
    for week_number in range(11, 20):
        tiered_n, ranked_n, clusters_n, items_n = _build_week(
            [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 90}]
        )
        result = update_library(
            tiered_n, ranked_n, clusters_n, items_n, f"2026-W{week_number}", state
        )
        assert result.entries[0].lifecycle_status == "rejected"
        state = result.entries


# --- published: stays in history, frozen ------------------------------------


def test_published_stays_frozen_no_score_or_audit_update() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    published_entry = week1.entries[0].model_copy(update={"lifecycle_status": "published"})
    audit_count_before = len(published_entry.audit_events)

    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 20}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [published_entry])
    entry = week2.entries[0]
    assert entry.lifecycle_status == "published"
    assert entry.current_score == 80  # unchanged despite this week's score=20
    assert len(entry.audit_events) == audit_count_before  # no new event
    assert week2.new_audit_rows == []
    assert week2.new_score_history_rows == []


# --- stale: absence-based, never presented as current -----------------------


def test_stale_after_n_weeks_of_absence_and_not_before() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    # One week later (< STALE_WEEKS): topic absent, but not yet stale.
    week2 = update_library([], [], {}, {}, "2026-W11", week1.entries)
    assert week2.entries[0].lifecycle_status != "stale"

    # STALE_WEEKS later from week1's last_updated: now stale.
    week_n_label = f"2026-W{10 + STALE_WEEKS}"
    week_n = update_library([], [], {}, {}, week_n_label, week1.entries)
    entry = week_n.entries[0]
    assert entry.lifecycle_status == "stale"
    transitions = _lifecycle_transitions(entry)
    assert len(transitions) == 1
    assert transitions[0].to_status == "stale"
    assert "no new evidence" in transitions[0].reason


def test_stale_topic_never_presented_as_current_by_the_library_movements_adapter() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    week_n_label = f"2026-W{10 + STALE_WEEKS}"
    stale_week = update_library([], [], {}, {}, week_n_label, week1.entries)
    assert stale_week.entries[0].lifecycle_status == "stale"
    movements = library_movements_for_brief(stale_week)
    # The stale movement IS reported (as a movement, with its reason) -- but
    # its lifecycle_status is 'stale', never a "current" status.
    assert any(m.movement == "stale" for m in movements.movements)
    for movement in movements.movements:
        assert movement.movement != "selected_for_brief" or movement.topic_id != "t1"


def test_stale_topic_reappearing_is_treated_as_new_evidence() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    week_n_label = f"2026-W{10 + STALE_WEEKS}"
    stale_week = update_library([], [], {}, {}, week_n_label, week1.entries)
    assert stale_week.entries[0].lifecycle_status == "stale"

    reappear_label = f"2026-W{10 + STALE_WEEKS + 1}"
    tiered_r, ranked_r, clusters_r, items_r = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 82}]
    )
    reappear_week = update_library(
        tiered_r, ranked_r, clusters_r, items_r, reappear_label, stale_week.entries
    )
    entry = reappear_week.entries[0]
    assert entry.lifecycle_status == "selected_for_brief"
    transitions = _lifecycle_transitions(entry)
    assert transitions[-1].from_status == "stale"
    assert "stale" in transitions[-1].reason


# --- persistence: load/save, append-only side logs --------------------------


def test_save_and_load_topics_roundtrip(tmp_path: Path) -> None:
    tiered, ranked, clusters, items = _build_week(
        [
            {"topic_id": "t2", "title": "Topic Two", "rank": 4, "tier": "tier_2", "score": 60},
            {"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80},
        ]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    path = tmp_path / "topics.jsonl"
    save_topics(result.entries, path)
    loaded = load_topics(path)
    assert loaded == sorted(result.entries, key=lambda e: e.topic_id)


def test_load_topics_returns_empty_list_when_file_does_not_exist(tmp_path: Path) -> None:
    assert load_topics(tmp_path / "does-not-exist.jsonl") == []


def test_append_score_history_and_audit_rows_are_append_only_across_two_runs(
    tmp_path: Path,
) -> None:
    score_path = tmp_path / "score-history.jsonl"
    audit_path = tmp_path / "audit.jsonl"

    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    append_score_history_rows(week1.new_score_history_rows, score_path)
    append_audit_rows(week1.new_audit_rows, audit_path)

    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 91}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    append_score_history_rows(week2.new_score_history_rows, score_path)
    append_audit_rows(week2.new_audit_rows, audit_path)

    score_lines = score_path.read_text(encoding="utf-8").splitlines()
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(score_lines) == len(week1.new_score_history_rows) + len(week2.new_score_history_rows)
    assert len(audit_lines) == len(week1.new_audit_rows) + len(week2.new_audit_rows)
    # The week 1 row is still present after the week 2 append (not truncated).
    assert any('"score":80' in line for line in score_lines)
    assert any('"score":91' in line for line in score_lines)


# --- library_movements_for_brief: brief-facing adapter suppresses noise -----


def test_first_run_created_rows_are_suppressed_from_brief_movements() -> None:
    """F-C: a first run's plain 'created' -> 'new' rows must NOT surface as
    brief-facing library movements (they'd otherwise duplicate the Tier
    1/2/3 lists the brief already renders). They still exist in the full
    audit log (new_audit_rows) -- only the brief-facing adapter suppresses
    them."""
    tiered, ranked, clusters, items = _build_week(
        [
            {"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80},
            {"topic_id": "t2", "title": "Topic Two", "rank": 4, "tier": "tier_2", "score": 60},
        ]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    # The full audit log still records both 'created' events.
    assert len(result.new_audit_rows) == 2
    assert all(row.event_type == "created" for row in result.new_audit_rows)

    movements = library_movements_for_brief(result)
    assert movements.movements == []
    assert movements.deferred_note is not None
    assert "first run" in movements.deferred_note
    assert "no library movements this week" in movements.deferred_note
    # This is NOT the "M6 not wired in" deferral note -- the library IS wired
    # in here, there just happened to be nothing brief-worthy this week.
    assert "not wired" not in movements.deferred_note
    assert "M6" not in movements.deferred_note


def test_a_week_with_only_score_changes_also_states_no_movements() -> None:
    """A continuing topic whose data-driven status is already stable (only
    its score changes) must not surface a movement either -- score_change
    bookkeeping was already suppressed before this fix; this confirms it
    still is. Week 1 is always 'new'; week 2 always transitions away from
    'new' to the data-driven bucket (recomputed every week by construction),
    so the "nothing changed" case is only reachable from week 3 onward, once
    the data-driven bucket itself is stable."""
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 60}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 61}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    assert week2.entries[0].lifecycle_status == "study_queue"  # tier_2 + fact -> study_queue

    tiered3, ranked3, clusters3, items3 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 4, "tier": "tier_2", "score": 62}]
    )
    week3 = update_library(tiered3, ranked3, clusters3, items3, "2026-W12", week2.entries)
    # Same data-driven bucket both weeks (tier_2 + 'fact' -> study_queue) --
    # only the score changed, so THIS WEEK's new rows are score_change only,
    # not a lifecycle_transition (checked on new_audit_rows, not cumulative
    # audit_events, since week 2's transition is still in history).
    assert week3.entries[0].lifecycle_status == week2.entries[0].lifecycle_status
    assert all(row.event_type == "score_change" for row in week3.new_audit_rows)
    assert len(week3.new_audit_rows) == 1
    movements = library_movements_for_brief(week3)
    assert movements.movements == []
    assert movements.deferred_note == "no library movements this week (first run / no transitions)"


# --- two-week simulation (Founder mandatory test) ---------------------------


def test_two_week_simulation_promotion_deferral_rejection_and_stale() -> None:
    """Week 1 seeds the library with five topics. Between weeks 1 and 2, two
    entries are hand-set to 'deferred' and 'rejected' -- simulating a
    Founder editorial decision this deterministic engine never makes on its
    own. Week 2 then exercises: promotion (the deferred topic's score jumps
    past the trigger threshold), a rejected topic reappearing and staying
    suppressed, a topic going stale after a >= STALE_WEEKS absence, and an
    ordinary 'new' -> data-driven recompute for the remaining topic."""
    week1_specs = [
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
        {
            "topic_id": "t_ordinary",
            "title": "Ordinary Continuation",
            "rank": 5,
            "tier": "tier_2",
            "score": 58,
        },
    ]
    tiered1, ranked1, clusters1, items1 = _build_week(week1_specs)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W20", [])
    assert {e.topic_id for e in week1.entries} == {
        "t_promote",
        "t_reject",
        "t_stale",
        "t_ordinary",
    }
    for entry in week1.entries:
        assert entry.lifecycle_status == "new"

    # Simulate a Founder editorial decision made between week 1 and week 2.
    by_id = {e.topic_id: e for e in week1.entries}
    prior_entries = [
        by_id["t_promote"].model_copy(update={"lifecycle_status": "deferred"}),
        by_id["t_reject"].model_copy(update={"lifecycle_status": "rejected"}),
        by_id["t_stale"],  # left as-is; will go stale from absence, not a Founder action
        by_id["t_ordinary"],  # left as-is
    ]

    # Week 2, STALE_WEEKS later (>= STALE_WEEKS): t_stale is simply absent.
    week2_label = f"2026-W{20 + STALE_WEEKS}"
    week2_specs = [
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
        {
            "topic_id": "t_ordinary",
            "title": "Ordinary Continuation",
            "rank": 4,
            "tier": "tier_2",
            "score": 58,
            "claim_class": "fact",
        },
        # t_stale is absent this week.
    ]
    tiered2, ranked2, clusters2, items2 = _build_week(week2_specs)
    week2 = update_library(tiered2, ranked2, clusters2, items2, week2_label, prior_entries)
    by_id_2 = {e.topic_id: e for e in week2.entries}

    # 1. Promotion: t_promote left 'deferred' and returns via the score trigger.
    promoted = by_id_2["t_promote"]
    assert promoted.lifecycle_status == "study_queue"
    promoted_transitions = _lifecycle_transitions(promoted)
    assert len(promoted_transitions) == 1
    assert promoted_transitions[0].from_status == "deferred"
    assert "promoted from deferred" in promoted_transitions[0].reason
    assert "score increased by" in promoted_transitions[0].reason

    # 2. Rejection: t_reject stays rejected despite reappearing with a much
    #    higher score -- recorded, never resurfaced as current.
    rejected = by_id_2["t_reject"]
    assert rejected.lifecycle_status == "rejected"
    reappearance = [e for e in rejected.audit_events if e.event_type == "rejected_reappearance"]
    assert len(reappearance) == 1
    assert "stays suppressed" in reappearance[0].reason

    # 3. Stale: t_stale, absent for exactly STALE_WEEKS, is marked stale.
    stale = by_id_2["t_stale"]
    assert stale.lifecycle_status == "stale"
    stale_transitions = _lifecycle_transitions(stale)
    assert len(stale_transitions) == 1
    assert "no new evidence" in stale_transitions[0].reason

    # 4. Ordinary recompute: t_ordinary just continues, 'new' -> data-driven.
    ordinary = by_id_2["t_ordinary"]
    assert ordinary.lifecycle_status == "study_queue"  # tier_2 + fact
    ordinary_transitions = _lifecycle_transitions(ordinary)
    assert len(ordinary_transitions) == 1
    assert ordinary_transitions[0].from_status == "new"

    # Every GENUINE movement this week is represented with its own reason in
    # the brief-facing adapter.
    movements = library_movements_for_brief(week2)
    movements_by_topic = {m.topic_id: m for m in movements.movements}
    assert movements_by_topic["t_promote"].movement == "study_queue"
    assert movements_by_topic["t_promote"].reason
    assert movements_by_topic["t_reject"].movement == "rejected"
    assert movements_by_topic["t_reject"].reason
    assert movements_by_topic["t_stale"].movement == "stale"
    assert movements_by_topic["t_stale"].reason
    # t_ordinary's own score (58) and tier (tier_2) are UNCHANGED between
    # weeks -- its only "change" is leaving the one-time 'new' status, and
    # its rank moved only because t_stale vacated the Top-N (a mechanical
    # composition shuffle, not a judgment about t_ordinary itself). Gate C
    # correction round: this is deliberately NOT a brief-facing movement
    # (FIX 1: routine re-tracking boilerplate is dropped) and NOT classified
    # promoted/demoted (FIX 2: a pure rank-shuffle with score_delta == 0 and
    # no tier change is not a promotion/demotion) -- see
    # test_steady_state_second_week_declutters_routine_retracking_boilerplate
    # and test_pure_rank_shuffle_with_unchanged_score_is_not_promoted_or_demoted
    # for the fixes in isolation.
    assert "t_ordinary" not in movements_by_topic
    assert not any(m.topic_id == "t_ordinary" for m in movements.promoted)
    assert not any(m.topic_id == "t_ordinary" for m in movements.demoted)
    assert isinstance(movements, LibraryMovementsSection)


# --- v0.2: cross-week topic merge (Opus orchestrator, Gate C; ADR 0004 D8) --


def test_merge_detects_same_subject_across_weeks_and_preserves_aliases_and_history() -> None:
    """Week 1 creates topic_a. Week 2's pipeline produces a DIFFERENT
    topic_id (topic_b) for what is actually the same underlying subject
    (shared subject_entity_ids, near-identical title) -- the real-world edge
    case cluster.py's per-run identity can't avoid on its own (see
    models.py's TopicCluster docstring). The merge pass must recognize this,
    absorb topic_b into topic_a (topic_a's earlier first_seen wins), and
    preserve aliases + the unioned score history."""
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "topic_a",
                "title": "New Agent Framework Launch",
                "rank": 1,
                "tier": "tier_1",
                "score": 70,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "topic_b",
                "title": "New Agent Framework Launch Update",
                "rank": 2,
                "tier": "tier_2",
                "score": 65,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)

    by_id = {e.topic_id: e for e in week2.entries}
    assert by_id["topic_b"].lifecycle_status == "merged"
    assert by_id["topic_b"].merged_into == "topic_a"

    survivor = by_id["topic_a"]
    assert survivor.lifecycle_status != "merged"
    assert "topic_b" in survivor.aliases
    assert "New Agent Framework Launch Update" in survivor.aliases

    history_pairs = {(h.week_label, h.score) for h in survivor.score_history}
    assert ("2026-W10", 70) in history_pairs
    assert ("2026-W11", 65) in history_pairs

    merged_events = [e for e in survivor.audit_events if e.event_type == "merged"]
    assert len(merged_events) == 1
    assert "topic_b" in merged_events[0].reason

    absorbed_transitions = [e for e in by_id["topic_b"].audit_events if e.to_status == "merged"]
    assert len(absorbed_transitions) == 1
    assert "topic_a" in absorbed_transitions[0].reason


def test_merged_absorbed_entry_excluded_from_current_library_and_surfaced_as_a_merge_movement() -> (
    None
):
    """A merged (absorbed) entry contributes NOTHING to current ranking: it
    is excluded from any 'current' filter over lifecycle_status, and
    library_movements_for_brief surfaces it ONLY in the 'merged' bucket --
    never as a live tier bucket."""
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "topic_a",
                "title": "New Agent Framework Launch",
                "rank": 1,
                "tier": "tier_1",
                "score": 70,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "topic_b",
                "title": "New Agent Framework Launch Update",
                "rank": 2,
                "tier": "tier_2",
                "score": 65,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)

    excluded_statuses = ("merged", "stale", "rejected")
    current_topics = {
        e.topic_id for e in week2.entries if e.lifecycle_status not in excluded_statuses
    }
    assert current_topics == {"topic_a"}

    movements = library_movements_for_brief(week2)
    assert any(m.movement == "merged" and m.topic_id == "topic_b" for m in movements.merged)
    assert not any(m.topic_id == "topic_b" for m in movements.promoted)
    assert not any(m.topic_id == "topic_b" for m in movements.new)


def test_rejected_entry_never_participates_in_a_merge() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "topic_a",
                "title": "New Agent Framework Launch",
                "rank": 1,
                "tier": "tier_1",
                "score": 70,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    rejected_entry = week1.entries[0].model_copy(update={"lifecycle_status": "rejected"})

    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "topic_b",
                "title": "New Agent Framework Launch Update",
                "rank": 2,
                "tier": "tier_2",
                "score": 65,
                "subject_entity_ids": ["vendor-x"],
                "claim_class": "fact",
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", [rejected_entry])
    by_id = {e.topic_id: e for e in week2.entries}
    assert by_id["topic_a"].lifecycle_status == "rejected"
    assert by_id["topic_b"].lifecycle_status != "merged"
    assert by_id["topic_b"].merged_into is None


def test_dissimilar_titles_with_shared_subject_do_not_merge() -> None:
    """Sharing subject_entity_ids alone is NOT sufficient -- the title
    Jaccard similarity must also clear TOPIC_MERGE_JACCARD_THRESHOLD."""
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "topic_a",
                "title": "New Agent Framework Launch",
                "rank": 1,
                "tier": "tier_1",
                "score": 70,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "topic_b",
                "title": "Completely Unrelated Pricing Change",
                "rank": 2,
                "tier": "tier_2",
                "score": 65,
                "subject_entity_ids": ["vendor-x"],
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    by_id = {e.topic_id: e for e in week2.entries}
    assert by_id["topic_b"].lifecycle_status != "merged"
    assert TOPIC_MERGE_JACCARD_THRESHOLD == 0.7  # documents the constant this test relies on


# --- Gate C correction round: constants pinned against mutation (QA Finding 1) -


def test_stale_weeks_and_score_change_trigger_threshold_are_pinned_to_spec() -> None:
    """QA Finding 1 (MEDIUM + INFO), Gate C correction round: the stale
    tests derive their week offsets from STALE_WEEKS itself, and two of the
    three deferred-return tests derive their score bump from
    SCORE_CHANGE_TRIGGER_THRESHOLD itself -- so a regression that changes
    either constant's VALUE (e.g. STALE_WEEKS 8 -> 9) would not be caught by
    any of them; every self-referential test would still pass. This mirrors
    the TOPIC_MERGE_JACCARD_THRESHOLD == 0.7 literal-value pin above (which
    DID catch its own mutation) to close the same blind spot for the other
    two Founder-specified constants."""
    assert STALE_WEEKS == 8
    assert SCORE_CHANGE_TRIGGER_THRESHOLD == 15


# --- Gate C correction round: declutter brief-facing movements (Opus F2) ----


def test_steady_state_second_week_declutters_routine_retracking_boilerplate() -> None:
    """FIX 1, Gate C correction round (Opus Finding 2): on a topic's SECOND
    week, leaving the one-time 'new' status with no other genuine change
    (same score, same rank, same tier -- purely routine re-tracking) used to
    emit one "topic has been tracked for more than one week; recomputed
    from this week's data" line per topic in the brief's flat movements
    list -- ~9 near-identical boilerplate lines every steady-state week.
    These rows must no longer be brief-facing. The full audit trail
    (new_audit_rows / audit_events) is unaffected and keeps every one of
    them, so nothing is silently lost -- only what's SHOWN in the brief
    changes."""
    specs = [
        {"topic_id": f"t{i}", "title": f"Topic {i}", "rank": i, "tier": "tier_2", "score": 50}
        for i in range(1, 4)
    ]
    tiered1, ranked1, clusters1, items1 = _build_week(specs)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    assert all(e.lifecycle_status == "new" for e in week1.entries)

    # Week 2: identical inputs -- no genuine change anywhere in score, rank,
    # or tier, just routine re-tracking out of 'new'.
    tiered2, ranked2, clusters2, items2 = _build_week(specs)
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)

    # The full audit trail still records all three transitions, in full.
    assert len(week2.new_audit_rows) == 3
    assert all(row.event_type == "lifecycle_transition" for row in week2.new_audit_rows)
    assert all(row.from_status == "new" for row in week2.new_audit_rows)
    assert all(
        row.reason == library_module.ROUTINE_RETRACKING_REASON for row in week2.new_audit_rows
    )

    # But NONE of it is brief-facing: pure boilerplate, not a real movement.
    movements = library_movements_for_brief(week2)
    assert movements.movements == []
    assert movements.deferred_note == "no library movements this week (first run / no transitions)"
    assert movements.new == []
    assert movements.promoted == []
    assert movements.demoted == []


def test_pure_rank_shuffle_with_unchanged_score_is_not_promoted_or_demoted() -> None:
    """FIX 2, Gate C correction round (Opus Finding 1): a topic whose OWN
    score and tier are unchanged, but whose rank moved only because another
    topic left the Top-N (a mechanical composition shuffle, not a ranking
    judgment about this topic), must NOT be reported as 'promoted' or
    'demoted' -- that would overstate a shuffle as a genuine movement.
    Full "topic left the Top-N" fall-out reporting stays deferred to v0.3
    (see ADR 0004's 'Deferred to v0.3' note) -- this test does not invent
    it."""
    week1_specs = [
        {
            "topic_id": "t_shuffle",
            "title": "Steady Topic",
            "rank": 3,
            "tier": "tier_2",
            "score": 60,
        },
        {
            "topic_id": "t_leaving",
            "title": "Topic Leaving The Top N",
            "rank": 1,
            "tier": "tier_1",
            "score": 90,
        },
    ]
    tiered1, ranked1, clusters1, items1 = _build_week(week1_specs)
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    # Week 2: t_leaving is simply absent; t_shuffle's OWN score and tier are
    # unchanged, but its rank improves purely because t_leaving vacated rank 1.
    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "t_shuffle",
                "title": "Steady Topic",
                "rank": 1,
                "tier": "tier_2",
                "score": 60,
            }
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)

    delta = next(d for d in week2.deltas if d.topic_id == "t_shuffle")
    assert delta.score_delta == 0
    assert delta.tier_change is None
    assert delta.rank_delta is not None
    assert delta.rank_delta > 0  # rank genuinely moved (3 -> 1)

    movements = library_movements_for_brief(week2)
    assert not any(m.topic_id == "t_shuffle" for m in movements.promoted)
    assert not any(m.topic_id == "t_shuffle" for m in movements.demoted)

    document = build_movements_document(week2)
    assert not any("t_shuffle" in line for line in document.promoted)
    assert not any("t_shuffle" in line for line in document.demoted)


# --- v0.2: decay and effective_rank_score (Opus orchestrator, Gate C) --------


def test_time_sensitive_decays_faster_than_evergreen() -> None:
    tiered_ts, ranked_ts, clusters_ts, items_ts = _build_week(
        [
            {
                "topic_id": "t_ts",
                "title": "Config Change Needed",
                "rank": 1,
                "tier": "tier_1",
                "score": 80,
                "action_required": "config_or_code_change",
            }
        ]
    )
    week1_ts = update_library(tiered_ts, ranked_ts, clusters_ts, items_ts, "2026-W10", [])
    entry_ts = week1_ts.entries[0]
    assert entry_ts.freshness == "time_sensitive"

    tiered_ev, ranked_ev, clusters_ev, items_ev = _build_week(
        [
            {
                "topic_id": "t_ev",
                "title": "Evergreen Concept Piece",
                "rank": 1,
                "tier": "tier_1",
                "score": 80,
                "action_required": "none",
                "change_class": "restatement",
            }
        ]
    )
    week1_ev = update_library(tiered_ev, ranked_ev, clusters_ev, items_ev, "2026-W10", [])
    entry_ev = week1_ev.entries[0]
    assert entry_ev.freshness == "evergreen"

    later_label = "2026-W15"  # 5 weeks later
    score_ts = effective_rank_score(entry_ts, later_label)
    score_ev = effective_rank_score(entry_ev, later_label)
    assert score_ts == 80 - 10 * 5
    assert score_ev == 80 - 2 * 5
    assert score_ts < score_ev
    # current_score itself must never be mutated by this derived computation.
    assert entry_ts.current_score == 80
    assert entry_ev.current_score == 80


def test_effective_rank_score_floors_at_zero_for_urgent_after_many_weeks() -> None:
    tiered, ranked, clusters, items = _build_week(
        [
            {
                "topic_id": "t_urgent",
                "title": "Breaking Change",
                "rank": 1,
                "tier": "tier_1",
                "score": 30,
                "action_required": "migration_required",
                "change_class": "breaking_change",
            }
        ]
    )
    week1 = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    entry = week1.entries[0]
    assert entry.freshness == "urgent"
    later = "2026-W20"  # 10 weeks later; rate 20/week would go negative
    assert effective_rank_score(entry, later) == 0
    assert entry.current_score == 30


def test_stale_at_exactly_stale_weeks_and_never_shown_as_current() -> None:
    """Reuses the STALE_WEEKS constant already pinned by
    test_stale_after_n_weeks_of_absence_and_not_before; this test adds the
    explicit "never shown as current" check via a plain lifecycle filter."""
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    week1 = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    week_n_label = f"2026-W{10 + STALE_WEEKS}"
    stale_week = update_library([], [], {}, {}, week_n_label, week1.entries)
    entry = stale_week.entries[0]
    assert entry.lifecycle_status == "stale"
    current_topics = [
        e for e in stale_week.entries if e.lifecycle_status not in ("stale", "merged", "rejected")
    ]
    assert current_topics == []


# --- v0.2: structured relevance (Opus orchestrator, Gate C) ------------------


def test_relevance_reasons_and_profile_version_derived_from_breakdown() -> None:
    tiered, ranked, clusters, items = _build_week(
        [
            {
                "topic_id": "t1",
                "title": "Topic One",
                "rank": 1,
                "tier": "tier_1",
                "score": 80,
                "relevance_effective": 5,
            }
        ]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    entry = result.entries[0]
    assert entry.relevance_reasons  # non-empty, derived from the relevance dimension
    assert entry.profile_version == "test-1"  # from the test breakdown fixture


def test_live_question_connections_populated_only_when_profile_is_passed() -> None:
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    without_profile = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    assert without_profile.entries[0].live_question_connections == []

    profile = RelevanceProfile(
        profile_version="p1",
        territories=[],
        live_questions=[LiveQuestion(question_id="q_agents", tags=["agents"])],
        current_tooling=[],
        experiment_budget="low",
    )
    with_profile = update_library(
        tiered, ranked, clusters, items, "2026-W10", [], profile=profile
    )
    # _make_inputs' default topic_tags is ["agents"] -- matches q_agents' tags.
    assert with_profile.entries[0].live_question_connections == ["q_agents"]


# --- v0.2: normalized summary (Opus orchestrator, Gate C; ADR 0004 D7/D8) ---


def test_normalized_summary_neutralizes_markup_and_is_length_bounded() -> None:
    dirty = "<script>alert(1)</script> Breaking: <b>Vendor X</b> ships <unterminated"
    summary = build_normalized_summary(dirty)
    assert "<" not in summary
    assert ">" not in summary

    long_title = "x" * 500
    long_summary = build_normalized_summary(long_title)
    assert len(long_summary) == NORMALIZED_SUMMARY_MAX_CHARS


def test_normalized_summary_persisted_and_never_a_raw_body() -> None:
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic <b>One</b>", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    entry = result.entries[0]
    assert "<" not in entry.normalized_summary
    assert len(entry.normalized_summary) <= NORMALIZED_SUMMARY_MAX_CHARS
    # canonical_title itself is untouched (neutralization is a property of
    # the DERIVED normalized_summary only, not of canonical_title).
    assert entry.canonical_title == "Topic <b>One</b>"


# --- v0.2: weekly deltas (Opus orchestrator, Gate C; ADR 0004 D8) -----------


def test_weekly_delta_for_a_brand_new_topic_is_new_never_a_drop() -> None:
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 3, "tier": "tier_2", "score": 60}]
    )
    result = update_library(tiered, ranked, clusters, items, "2026-W10", [])
    assert len(result.deltas) == 1
    delta = result.deltas[0]
    assert delta.is_new is True
    assert delta.score_delta is None
    assert delta.previous_score is None
    assert delta.previous_rank is None
    assert delta.new_evidence is True


def test_weekly_delta_reports_negative_score_and_rank_delta_for_a_fallen_topic() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 90}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 5, "tier": "tier_2", "score": 55}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    delta = week2.deltas[0]
    assert delta.is_new is False
    assert delta.score_delta == 55 - 90
    assert delta.score_delta < 0
    assert delta.previous_rank == 1
    assert delta.current_rank == 5
    assert delta.rank_delta == 1 - 5
    assert delta.rank_delta < 0
    assert delta.tier_change == "tier_1 -> tier_2"


def test_weekly_delta_reports_positive_deltas_for_a_promoted_topic() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 5, "tier": "tier_2", "score": 50}]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])
    tiered2, ranked2, clusters2, items2 = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 85}]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    delta = week2.deltas[0]
    assert delta.score_delta == 35
    assert delta.rank_delta == 4
    assert delta.tier_change == "tier_2 -> tier_1"


# --- v0.2: movements.md document (Opus orchestrator, Gate C; ADR 0004 D8) ---


def test_build_movements_document_buckets_new_promoted_demoted() -> None:
    tiered1, ranked1, clusters1, items1 = _build_week(
        [
            {
                "topic_id": "t_promoted",
                "title": "Promoted Topic",
                "rank": 5,
                "tier": "tier_2",
                "score": 50,
            },
            {
                "topic_id": "t_demoted",
                "title": "Demoted Topic",
                "rank": 1,
                "tier": "tier_1",
                "score": 90,
            },
        ]
    )
    week1 = update_library(tiered1, ranked1, clusters1, items1, "2026-W10", [])

    tiered2, ranked2, clusters2, items2 = _build_week(
        [
            {
                "topic_id": "t_promoted",
                "title": "Promoted Topic",
                "rank": 1,
                "tier": "tier_1",
                "score": 85,
            },
            {
                "topic_id": "t_demoted",
                "title": "Demoted Topic",
                "rank": 5,
                "tier": "tier_2",
                "score": 55,
            },
            {"topic_id": "t_new", "title": "New Topic", "rank": 3, "tier": "tier_2", "score": 60},
        ]
    )
    week2 = update_library(tiered2, ranked2, clusters2, items2, "2026-W11", week1.entries)
    document = build_movements_document(week2)
    assert any("New Topic" in line for line in document.new)
    assert any("Promoted Topic" in line for line in document.promoted)
    assert any("Demoted Topic" in line for line in document.demoted)

    markdown = render_movements_markdown(document, "2026-W11")
    assert "New Topic" in markdown
    assert "Promoted Topic" in markdown
    assert "Demoted Topic" in markdown
    assert markdown.startswith("# Library Movements -- Week 2026-W11")


# --- no network, no wall clock, no real data ---------------------------------


def test_no_network_calls_while_updating_the_library(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted by content_machine.intelligence.library")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    update_library(tiered, ranked, clusters, items, "2026-W10", [])


def test_module_source_never_reads_the_wall_clock() -> None:
    source = inspect.getsource(library_module)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "datetime" not in stripped, f"forbidden datetime import: {stripped!r}"


def test_fixtures_are_synthetic_example_domains_only() -> None:
    tiered, ranked, clusters, items = _build_week(
        [{"topic_id": "t1", "title": "Topic One", "rank": 1, "tier": "tier_1", "score": 80}]
    )
    for item in items.values():
        if item.stable_reference.startswith("http"):
            assert "example.com" in item.stable_reference
