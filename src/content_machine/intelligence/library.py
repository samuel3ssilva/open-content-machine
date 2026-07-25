"""M6: a persistent, cross-run topic library with lifecycle and audit trail.

Per spec Sections 8/10 and Founder decision D (retention: minimum fields,
NEVER raw bodies), this module lets a :class:`TopicLibraryEntry` survive
across weekly runs. Everything here is deterministic and offline: the only
"time" input is the caller-supplied ``week_label`` (e.g. ``"2026-W30"``) --
this module never imports ``datetime`` and never reads the wall clock. Week
distance (used for the ``stale`` rule) is computed by plain integer
arithmetic on the ISO week label string, not by constructing real dates.

This module builds strictly ON TOP of M4/M5 (:mod:`content_machine.intelligence.tiers`
/ :mod:`content_machine.intelligence.ranking`): :func:`update_library` takes
the current week's already-tiered topics (``tiers.assign_tiers``'s output),
the full ranked list (for the score/evidence/tags of each topic), and the
PRIOR library state (a loaded JSONL, or an empty list for the very first
run) -- it never re-derives ranking, tiering, or evidence classification.

Persisted fields are deliberately minimal (Founder decision D): a
:class:`TopicLibraryEntry` stores the canonical title and, per source, only
``stable_reference``/``source_category`` -- NEVER ``summary_normalized``,
item titles beyond the topic's own canonical title, or any other prose that
could carry a full artifact body. See
``test_no_raw_bodies_or_prose_beyond_canonical_title_is_persisted``.

Lifecycle (exactly these ten statuses): ``new``, ``ranked``,
``selected_for_brief``, ``study_queue``, ``experiment_candidate``,
``content_candidate``, ``deferred``, ``published``, ``rejected``, ``stale``.

- ``new``: the topic's very first week in the library.
- ``ranked`` / ``selected_for_brief`` / ``study_queue`` /
  ``experiment_candidate`` / ``content_candidate``: DATA-DRIVEN, recomputed
  every week purely from this week's :class:`TieredTopic`/``RankingInputs``
  (tier, claim_class, confidence, experiment_affordance, relevance) -- see
  :func:`_derive_current_status`. A topic in one of these "active" buckets
  is re-derived fresh every week it appears; none of them is sticky.
- ``deferred`` / ``rejected`` / ``published``: FOUNDER-DECISION statuses.
  This module never assigns them on its own -- they only exist in a prior
  library state because something else (a future editorial tool, or a test
  fixture simulating one) set them. This module's job is only to apply the
  RETURN rules the Founder specified for them:
    * ``deferred`` returns to a data-driven status ONLY when a trigger
      fires. Of the six triggers the Founder named (new evidence, material
      change, score change >= threshold, relevance increase, experiment
      evidence, connection), only two are implemented here deterministically
      -- see :func:`_deferred_return_trigger`:
        1. score change >= ``SCORE_CHANGE_TRIGGER_THRESHOLD`` points, and
        2. evidence-status improvement (``evidence_level`` increases, or
           ``claim_class`` moves to ``fact`` from something else).
      The other four (material change, relevance increase, experiment
      evidence, editorial "connection") are Founder-judgment calls this
      deterministic engine cannot itself decide; a deferred topic simply
      stays deferred on those grounds until a human reconsiders it.
    * ``rejected`` NEVER returns automatically. If a rejected topic
      reappears in this week's signals, it stays ``rejected`` -- this is
      recorded as an audit event (so the reappearance isn't silently lost)
      but the topic is never treated as current.
    * ``published`` is frozen: once set, this module never changes it again
      (no new score history, no new audit events, no stale check).
- ``stale``: DATA-DRIVEN. A non-frozen (not rejected/published) entry that
  does not appear in ``STALE_WEEKS`` or more consecutive weeks' Top-10
  signals is marked ``stale`` and stays there in history -- never presented
  as current news -- until it reappears in a future week's Top-10 (treated
  as new evidence, and re-derived like any other data-driven status).

Every lifecycle transition and every score change appends an audit event
with a reason; score history is append-only (a later week's entry only ever
grows the ``score_history`` list, never edits or removes a prior row).

Deferred to v0.2 (not implemented in Gate B) -- deliberately, not forgotten:

- The ``merged`` lifecycle status and its ``merged_into`` cross-week merge
  (spec §8 F6): two entries later recognized as the same underlying topic
  are never consolidated here; each keeps its own independent history.
- Score **decay** by freshness (spec §8): ``freshness`` (``urgent`` /
  ``time_sensitive`` / ``evergreen``) is computed and persisted on every
  entry, but nothing in this module reduces ``current_score`` over time
  based on it -- it is intentionally unused for scoring/staleness today.
- A structured **professional-relevance** field on the entry: only
  ``evidence_level`` is a structured (non-prose) quality signal today;
  there is no separate structured field capturing relevance to the
  Founder's professional territory (``editorial_territory`` is a list of
  tag strings carried over from ``RankingInputs.topic_tags``, not a
  purpose-built relevance field).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from content_machine.intelligence.brief import LibraryMovement, LibraryMovementsSection
from content_machine.intelligence.models import (
    ClaimClass,
    RankingBreakdown,
    RankingInputs,
    SourceItem,
    TieredTopic,
    TopicCluster,
)

# --- constants (per approved spec §8: stale threshold = 8 weeks; -----------
# reconsideration score-change trigger = ±15 points; configurable) ----------

#: How many consecutive weeks of absence from the Top-10 signals before a
#: non-frozen entry is marked ``stale``. Per approved spec §8 (stale: 8
#: weeks); configurable.
STALE_WEEKS = 8

#: Minimum score increase (points) that automatically returns a ``deferred``
#: entry to a data-driven status on its own (one of the two implemented
#: Founder triggers -- see the module docstring). Per approved spec §8
#: (reconsideration score trigger: ±15 points); configurable.
SCORE_CHANGE_TRIGGER_THRESHOLD = 15

LifecycleStatus = Literal[
    "new",
    "ranked",
    "selected_for_brief",
    "study_queue",
    "experiment_candidate",
    "content_candidate",
    "deferred",
    "published",
    "rejected",
    "stale",
]

Freshness = Literal["urgent", "time_sensitive", "evergreen"]

_FROZEN_STATUSES = frozenset({"published", "rejected"})

_EXPERIMENT_POSSIBILITY_TEXT: dict[str, str] = {
    "local_reproducible": "locally reproducible -- can be tested offline without paid services",
    "requires_paid_service": "would require a paid service to test",
    "not_testable": "not practically testable as a local experiment",
}


# ------------------------------ models --------------------------------------


class SourceReferenceMinimal(BaseModel):
    """The ONLY per-source facts the library retains -- never a title,
    summary, or body. See the module docstring / Founder decision D."""

    model_config = ConfigDict(extra="forbid")

    stable_reference: str
    source_category: str


class ScoreHistoryEntry(BaseModel):
    """One append-only row of a topic's score over time (embedded in its
    :class:`TopicLibraryEntry`; ``topic_id`` is implied by the parent)."""

    model_config = ConfigDict(extra="forbid")

    week_label: str
    score: int


class AuditEvent(BaseModel):
    """One append-only audit row (embedded in its :class:`TopicLibraryEntry`;
    ``topic_id`` is implied by the parent). ``event_type`` is one of
    ``created``, ``lifecycle_transition``, ``score_change``, or
    ``rejected_reappearance``."""

    model_config = ConfigDict(extra="forbid")

    week_label: str
    event_type: str
    from_status: str | None
    to_status: str
    reason: str


class TopicLibraryEntry(BaseModel):
    """One topic's persistent library record. Minimum fields only (Founder
    decision D): NEVER a raw body or an item title beyond the topic's own
    ``canonical_title``. A normalized (generated) summary is NOT a raw body
    and spec Sections 3/8 authorize persisting one, but this module
    deliberately omits it in v0.1 as a conservative retention-scope choice,
    not a permanent prohibition -- see ADR 0004 D7 and
    ``test_no_raw_bodies_or_prose_beyond_canonical_title_is_persisted``,
    which pins today's (smaller) field set rather than declaring a
    normalized summary forbidden forever.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    source_references: list[SourceReferenceMinimal] = Field(default_factory=list)
    first_seen: str
    last_updated: str
    current_score: int
    score_history: list[ScoreHistoryEntry] = Field(default_factory=list)
    ranking_explanation: str
    editorial_territory: list[str] = Field(default_factory=list)
    evidence_level: int
    evidence_anchor_id: str
    claim_class: ClaimClass
    learning_value: Literal["high", "medium", "low"]
    experiment_possibility: str
    content_angle_possibilities: list[str] = Field(default_factory=list)
    reason_not_selected: str | None
    reconsideration_condition: str | None
    freshness: Freshness
    lifecycle_status: LifecycleStatus
    audit_events: list[AuditEvent] = Field(default_factory=list)


class ScoreHistoryRow(BaseModel):
    """One row of the flat, append-only ``score-history.jsonl`` file."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    week_label: str
    score: int


class AuditRow(BaseModel):
    """One row of the flat, append-only ``audit.jsonl`` file."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    week_label: str
    event_type: str
    from_status: str | None
    to_status: str
    reason: str


class LibraryUpdateResult(BaseModel):
    """The result of one :func:`update_library` call: the FULL new library
    state (``entries``, one per tracked topic, sorted by ``topic_id``) plus
    only THIS week's new rows for the two append-only side logs."""

    model_config = ConfigDict(extra="forbid")

    entries: list[TopicLibraryEntry]
    new_score_history_rows: list[ScoreHistoryRow]
    new_audit_rows: list[AuditRow]


# ------------------------------ deterministic helpers -----------------------


def _experiment_possibility(experiment_affordance: str) -> str:
    return _EXPERIMENT_POSSIBILITY_TEXT.get(
        experiment_affordance, f"experiment_affordance={experiment_affordance}"
    )


def _learning_value(recommended_action: str) -> Literal["high", "medium", "low"]:
    if recommended_action == "study":
        return "high"
    if recommended_action in ("read", "save"):
        return "medium"
    return "low"  # monitor, ignore


def _freshness(action_required: str, change_class: str) -> Freshness:
    if change_class == "breaking_change" or action_required == "migration_required":
        return "urgent"
    if action_required in ("config_or_code_change", "new_option_available"):
        return "time_sensitive"
    return "evergreen"


def _content_angle_possibilities(tier: str, claim_class: str, confidence: str) -> list[str]:
    """Deterministic, non-invented content angles: a pure function of
    already-computed structured fields (tier, claim_class, confidence)."""
    angles: list[str] = []
    if tier == "tier_1":
        angles.append("deep-dive study writeup")
    if claim_class == "fact" and confidence in ("high", "medium"):
        angles.append("explainer: what changed and why it matters")
    if claim_class == "hypothesis":
        angles.append("watch-and-verify piece pending corroboration")
    if claim_class == "marketing":
        angles.append("skeptical take pending independent verification")
    return angles


def _relevance_effective_value(breakdown: RankingBreakdown) -> int:
    return next(d.effective_value for d in breakdown.dimensions if d.dimension == "relevance")


def _derive_current_status(
    topic: TieredTopic, inputs: RankingInputs, breakdown: RankingBreakdown
) -> LifecycleStatus:
    """The DATA-DRIVEN status for a topic present in this week's Top-10,
    recomputed fresh every week purely from already-computed facts -- first
    match wins:

    1. Tier 1 (Must Understand) -> ``selected_for_brief``.
    2. Tier 2, claim_class == fact -> ``study_queue`` (mirrors
       ``brief._build_study_queue``'s light-study criterion).
    3. ``experiment_affordance == local_reproducible`` -> ``experiment_candidate``.
    4. fact, confidence in {high, medium}, relevance effective_value >= 4 ->
       ``content_candidate`` (mirrors ``brief._build_content_opportunities``'s
       editorial-gate proxy).
    5. otherwise -> ``ranked``.
    """
    if topic.tier_assignment.tier == "tier_1":
        return "selected_for_brief"
    if topic.tier_assignment.tier == "tier_2" and topic.claim.claim_class == "fact":
        return "study_queue"
    if inputs.experiment_affordance == "local_reproducible":
        return "experiment_candidate"
    if (
        topic.claim.claim_class == "fact"
        and topic.claim.confidence in ("high", "medium")
        and _relevance_effective_value(breakdown) >= 4
    ):
        return "content_candidate"
    return "ranked"


def _deferred_return_trigger(
    prior: TopicLibraryEntry,
    this_week_score: int,
    this_week_evidence_level: int,
    this_week_claim_class: str,
) -> str | None:
    """The two data-driven triggers (of the Founder's six) that automatically
    return a ``deferred`` entry to a data-driven status. Returns a reason
    string if a trigger fired, else ``None``."""
    score_delta = this_week_score - prior.current_score
    if score_delta >= SCORE_CHANGE_TRIGGER_THRESHOLD:
        return (
            f"score increased by {score_delta} points (>= threshold "
            f"{SCORE_CHANGE_TRIGGER_THRESHOLD}) since it was deferred"
        )
    if this_week_evidence_level > prior.evidence_level:
        return (
            f"evidence level improved from {prior.evidence_level} to "
            f"{this_week_evidence_level} since it was deferred"
        )
    if prior.claim_class != "fact" and this_week_claim_class == "fact":
        return f"claim reclassified from {prior.claim_class} to fact since it was deferred"
    return None


def _reason_not_selected(status: LifecycleStatus) -> str | None:
    if status in (
        "selected_for_brief",
        "study_queue",
        "experiment_candidate",
        "content_candidate",
        "published",
        "new",
    ):
        return None
    if status == "rejected":
        return "rejected by Founder decision; suppressed regardless of new signals"
    if status == "deferred":
        return "deferred by Founder decision; not currently prioritized"
    if status == "stale":
        return f"no new evidence for >= {STALE_WEEKS} week(s)"
    return (
        "ranked but not currently selected for the brief, study queue, experiment, or "
        "content pipeline"
    )


def _reconsideration_condition(status: LifecycleStatus) -> str | None:
    if status == "deferred":
        return (
            "returns to a data-driven status automatically if its score increases by >= "
            f"{SCORE_CHANGE_TRIGGER_THRESHOLD} points, or its evidence level/claim class "
            "improves (data-driven); other triggers (material change, relevance increase, "
            "experiment evidence, editorial connection) require Founder action."
        )
    if status == "rejected":
        return "never returns automatically; requires explicit Founder action to reconsider"
    if status == "stale":
        return (
            "returns to a data-driven status automatically if it reappears in a future "
            "week's Top-10 signals"
        )
    return None


def _transition_reason(from_status: str, to_status: str, tier: str, claim_class: str) -> str:
    if from_status == "stale":
        return (
            "topic reappeared in this week's Top-10 signals after going stale -- treated "
            "as new evidence"
        )
    if from_status == "new":
        return "topic has been tracked for more than one week; recomputed from this week's data"
    return (
        f"recomputed from this week's data: tier={tier}, claim_class={claim_class} -> "
        f"{to_status}"
    )


def _week_label_parts(week_label: str) -> tuple[int, int]:
    year_str, week_str = week_label.split("-W")
    return int(year_str), int(week_str)


def _week_label_distance(earlier: str, later: str) -> int:
    """Approximate distance, in weeks, between two ISO week labels (e.g.
    ``"2026-W30"`` -> ``"2026-W33"`` is 3). Deliberately simple integer
    arithmetic (52 weeks/year) -- no ``datetime`` import, no wall clock. Does
    not special-case 53-week years; documented, acceptable approximation for
    the staleness threshold this feeds."""
    earlier_year, earlier_week = _week_label_parts(earlier)
    later_year, later_week = _week_label_parts(later)
    return (later_year - earlier_year) * 52 + (later_week - earlier_week)


def _minimal_source_references(
    cluster: TopicCluster, items_by_id: dict[str, SourceItem]
) -> list[SourceReferenceMinimal]:
    return [
        SourceReferenceMinimal(
            stable_reference=items_by_id[member_id].stable_reference,
            source_category=items_by_id[member_id].source_category,
        )
        for member_id in cluster.member_ids
    ]


# ------------------------------ public API -----------------------------------


def update_library(
    tiered: list[TieredTopic],
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
    week_label: str,
    prior_entries: list[TopicLibraryEntry],
) -> LibraryUpdateResult:
    """Build this week's library state from ``tiered``/``ranked`` (the
    current week's already-tiered Top-10 topics and the full ranked list)
    plus ``prior_entries`` (the previously persisted state, or ``[]`` for the
    very first run). Pure and deterministic: same arguments (in particular
    the same ``prior_entries`` and the same ``week_label``), same result,
    field for field, byte for byte once serialized -- see
    ``test_deterministic_same_inputs_produce_byte_identical_output``.

    Never reads the wall clock: the only "week" input is ``week_label``.
    """
    inputs_by_topic_id = {inputs.topic_id: inputs for inputs, _b in ranked}
    breakdown_by_topic_id = {inputs.topic_id: b for inputs, b in ranked}
    tiered_by_id = {t.topic_id: t for t in tiered}
    prior_by_id = {e.topic_id: e for e in prior_entries}

    new_entries: dict[str, TopicLibraryEntry] = {}
    score_rows: list[ScoreHistoryRow] = []
    audit_rows: list[AuditRow] = []

    for topic_id in sorted(tiered_by_id):
        topic = tiered_by_id[topic_id]
        inputs = inputs_by_topic_id[topic_id]
        breakdown = breakdown_by_topic_id[topic_id]
        cluster = clusters_by_topic_id[topic_id]
        prior = prior_by_id.get(topic_id)

        if prior is not None and prior.lifecycle_status == "published":
            # Frozen forever: no score update, no new audit event.
            new_entries[topic_id] = prior
            continue

        if prior is not None and prior.lifecycle_status == "rejected":
            reason = (
                "topic reappeared in this week's Top-10 signals but stays suppressed "
                "(rejected, no automatic return)"
            )
            audit_row = AuditRow(
                topic_id=topic_id,
                week_label=week_label,
                event_type="rejected_reappearance",
                from_status="rejected",
                to_status="rejected",
                reason=reason,
            )
            audit_rows.append(audit_row)
            new_entries[topic_id] = prior.model_copy(
                update={
                    "audit_events": [
                        *prior.audit_events,
                        AuditEvent(
                            week_label=week_label,
                            event_type="rejected_reappearance",
                            from_status="rejected",
                            to_status="rejected",
                            reason=reason,
                        ),
                    ]
                }
            )
            continue

        from_status: str | None = None
        transition_reason: str | None = None

        if prior is None:
            status: LifecycleStatus = "new"
            first_seen = week_label
            from_status = None
            transition_reason = "topic entered the library for the first time"
            event_type = "created"
        elif prior.lifecycle_status == "deferred":
            trigger = _deferred_return_trigger(
                prior, breakdown.score, inputs.evidence_level, topic.claim.claim_class
            )
            first_seen = prior.first_seen
            if trigger is not None:
                status = _derive_current_status(topic, inputs, breakdown)
                from_status = "deferred"
                transition_reason = f"promoted from deferred: {trigger}"
                event_type = "lifecycle_transition"
            else:
                status = "deferred"
                event_type = "lifecycle_transition"  # unused unless transition_reason is set
        else:
            first_seen = prior.first_seen
            status = _derive_current_status(topic, inputs, breakdown)
            event_type = "lifecycle_transition"
            if status != prior.lifecycle_status:
                from_status = prior.lifecycle_status
                transition_reason = _transition_reason(
                    prior.lifecycle_status,
                    status,
                    topic.tier_assignment.tier,
                    topic.claim.claim_class,
                )

        entry_audit: list[AuditEvent] = list(prior.audit_events) if prior is not None else []
        if transition_reason is not None:
            entry_audit.append(
                AuditEvent(
                    week_label=week_label,
                    event_type=event_type,
                    from_status=from_status,
                    to_status=status,
                    reason=transition_reason,
                )
            )
            audit_rows.append(
                AuditRow(
                    topic_id=topic_id,
                    week_label=week_label,
                    event_type=event_type,
                    from_status=from_status,
                    to_status=status,
                    reason=transition_reason,
                )
            )

        if prior is not None and prior.current_score != breakdown.score:
            score_change_reason = f"score changed from {prior.current_score} to {breakdown.score}"
            entry_audit.append(
                AuditEvent(
                    week_label=week_label,
                    event_type="score_change",
                    from_status=str(prior.current_score),
                    to_status=str(breakdown.score),
                    reason=score_change_reason,
                )
            )
            audit_rows.append(
                AuditRow(
                    topic_id=topic_id,
                    week_label=week_label,
                    event_type="score_change",
                    from_status=str(prior.current_score),
                    to_status=str(breakdown.score),
                    reason=score_change_reason,
                )
            )

        score_rows.append(
            ScoreHistoryRow(topic_id=topic_id, week_label=week_label, score=breakdown.score)
        )
        prior_score_history = list(prior.score_history) if prior is not None else []
        new_score_history = [
            *prior_score_history,
            ScoreHistoryEntry(week_label=week_label, score=breakdown.score),
        ]

        new_entries[topic_id] = TopicLibraryEntry(
            topic_id=topic_id,
            canonical_title=topic.canonical_title,
            source_references=_minimal_source_references(cluster, items_by_id),
            first_seen=first_seen,
            last_updated=week_label,
            current_score=breakdown.score,
            score_history=new_score_history,
            ranking_explanation=breakdown.ranking_explanation,
            editorial_territory=list(inputs.topic_tags),
            evidence_level=inputs.evidence_level,
            evidence_anchor_id=inputs.evidence_anchor_id,
            claim_class=topic.claim.claim_class,
            learning_value=_learning_value(topic.tier_assignment.recommended_action),
            experiment_possibility=_experiment_possibility(inputs.experiment_affordance),
            content_angle_possibilities=_content_angle_possibilities(
                topic.tier_assignment.tier, topic.claim.claim_class, topic.claim.confidence
            ),
            reason_not_selected=_reason_not_selected(status),
            reconsideration_condition=_reconsideration_condition(status),
            freshness=_freshness(inputs.action_required, inputs.change_class),
            lifecycle_status=status,
            audit_events=entry_audit,
        )

    for topic_id in sorted(prior_by_id):
        if topic_id in tiered_by_id:
            continue
        prior = prior_by_id[topic_id]
        if prior.lifecycle_status in _FROZEN_STATUSES:
            new_entries[topic_id] = prior
            continue
        weeks_absent = _week_label_distance(prior.last_updated, week_label)
        if weeks_absent >= STALE_WEEKS and prior.lifecycle_status != "stale":
            reason = (
                f"no new evidence for {weeks_absent} week(s) (>= {STALE_WEEKS}) -- marked stale"
            )
            audit_rows.append(
                AuditRow(
                    topic_id=topic_id,
                    week_label=week_label,
                    event_type="lifecycle_transition",
                    from_status=prior.lifecycle_status,
                    to_status="stale",
                    reason=reason,
                )
            )
            new_entries[topic_id] = prior.model_copy(
                update={
                    "lifecycle_status": "stale",
                    "reason_not_selected": _reason_not_selected("stale"),
                    "reconsideration_condition": _reconsideration_condition("stale"),
                    "audit_events": [
                        *prior.audit_events,
                        AuditEvent(
                            week_label=week_label,
                            event_type="lifecycle_transition",
                            from_status=prior.lifecycle_status,
                            to_status="stale",
                            reason=reason,
                        ),
                    ],
                }
            )
        else:
            new_entries[topic_id] = prior

    all_entries = sorted(new_entries.values(), key=lambda e: e.topic_id)
    return LibraryUpdateResult(
        entries=all_entries, new_score_history_rows=score_rows, new_audit_rows=audit_rows
    )


#: Rendered when this week's audit rows contain no BRIEF-FACING movement
#: (e.g. the very first run, where every row is a plain ``created`` -> "new"
#: entry, or a week where nothing promoted/deferred/rejected/went stale).
NO_LIBRARY_MOVEMENTS_NOTE = "no library movements this week (first run / no transitions)"


def library_movements_for_brief(result: LibraryUpdateResult) -> LibraryMovementsSection:
    """Adapt this week's new audit rows into a
    :class:`content_machine.intelligence.brief.LibraryMovementsSection` for
    :func:`content_machine.intelligence.brief.build_weekly_brief`'s optional
    ``library_movements`` parameter -- the M5/M6 wiring point. ``brief.py``
    itself never imports this module; the CALLER runs :func:`update_library`
    and passes the adapted section in.

    Only MEANINGFUL transitions are brief-facing (promoted-from-deferred,
    deferred, rejected/rejected-reappearance, stale). Plain ``score_change``
    bookkeeping and plain ``created`` ("new") rows are suppressed here -- a
    first run would otherwise emit one "new" line per topic, duplicating the
    Tier 1/2/3 lists the brief already renders (they still live in the full
    audit log; this function only adapts what's brief-facing). When nothing
    meaningful happened this week (including the common first-run case),
    ``movements`` is empty and ``deferred_note`` states that explicitly --
    never the M6-not-wired-in note (see ``NO_LIBRARY_MOVEMENTS_NOTE``)."""
    entries_by_id = {e.topic_id: e for e in result.entries}
    movements = [
        LibraryMovement(
            topic_id=row.topic_id,
            canonical_title=entries_by_id[row.topic_id].canonical_title,
            movement=row.to_status,
            reason=row.reason,
        )
        for row in result.new_audit_rows
        if row.event_type not in ("score_change", "created") and row.to_status != "new"
    ]
    deferred_note = NO_LIBRARY_MOVEMENTS_NOTE if not movements else None
    return LibraryMovementsSection(movements=movements, deferred_note=deferred_note)


# ------------------------------ persistence -----------------------------------


def load_topics(path: Path) -> list[TopicLibraryEntry]:
    """Load the persisted topic library from ``path`` (one JSON object per
    line). Returns ``[]`` if the file does not exist -- the empty prior
    state for the very first run. PRIVATE output: the caller decides where
    ``path`` lives; this function never writes into the repo on its own."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(TopicLibraryEntry.model_validate_json(line))
    return entries


def save_topics(entries: list[TopicLibraryEntry], path: Path) -> None:
    """Overwrite ``path`` with the FULL current library state, one JSON
    object per line, sorted by ``topic_id`` for determinism."""
    lines = [entry.model_dump_json() for entry in sorted(entries, key=lambda e: e.topic_id)]
    text = "\n".join(lines) + ("\n" if lines else "")
    path.write_text(text, encoding="utf-8")


def append_score_history_rows(rows: list[ScoreHistoryRow], path: Path) -> None:
    """Append (never overwrite) this week's new score-history rows to
    ``path`` -- the flat, append-only ``score-history.jsonl`` log."""
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json())
            handle.write("\n")


def append_audit_rows(rows: list[AuditRow], path: Path) -> None:
    """Append (never overwrite) this week's new audit rows to ``path`` --
    the flat, append-only ``audit.jsonl`` log."""
    if not rows:
        return
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json())
            handle.write("\n")
