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
    SCORE_CHANGE_TRIGGER_THRESHOLD,
    STALE_WEEKS,
    TopicLibraryEntry,
    append_audit_rows,
    append_score_history_rows,
    library_movements_for_brief,
    load_topics,
    save_topics,
    update_library,
)
from content_machine.intelligence.models import (
    ClaimAssessment,
    ClaimClass,
    ConfidenceLevel,
    DimensionScore,
    RankingBreakdown,
    RankingInputs,
    RecommendedAction,
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


def _make_cluster(topic_id: str, anchor_item_id: str, title: str) -> TopicCluster:
    return TopicCluster(
        topic_id=topic_id,
        cluster_fingerprint=f"fp_{topic_id}",
        canonical_title=title,
        anchor_item_id=anchor_item_id,
        member_ids=[anchor_item_id],
        member_roles={anchor_item_id: "primary"},
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
        d1_would_admit_at_evidence_3=False,
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
        clusters_by_topic_id[topic_id] = _make_cluster(topic_id, anchor.item_id, spec["title"])
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
    must never leak into the persisted entry."""
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

    # Week 2, 5 weeks later (>= STALE_WEEKS): t_stale is simply absent.
    week2_label = "2026-W25"
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

    # Every movement this week is represented with its own reason in the
    # brief-facing adapter.
    movements = library_movements_for_brief(week2)
    movements_by_topic = {m.topic_id: m for m in movements.movements}
    assert movements_by_topic["t_promote"].movement == "study_queue"
    assert movements_by_topic["t_promote"].reason
    assert movements_by_topic["t_reject"].movement == "rejected"
    assert movements_by_topic["t_reject"].reason
    assert movements_by_topic["t_stale"].movement == "stale"
    assert movements_by_topic["t_stale"].reason
    assert movements_by_topic["t_ordinary"].movement == "study_queue"
    assert movements_by_topic["t_ordinary"].reason
    assert isinstance(movements, LibraryMovementsSection)


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
