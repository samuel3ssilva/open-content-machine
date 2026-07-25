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

Lifecycle (exactly these eleven statuses): ``new``, ``ranked``,
``selected_for_brief``, ``study_queue``, ``experiment_candidate``,
``content_candidate``, ``deferred``, ``published``, ``rejected``, ``stale``,
``merged``.

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
- ``stale``: DATA-DRIVEN. A non-frozen (not rejected/published/merged) entry
  that does not appear in ``STALE_WEEKS`` or more consecutive weeks' Top-10
  signals is marked ``stale`` and stays there in history -- never presented
  as current news -- until it reappears in a future week's Top-10 (treated
  as new evidence, and re-derived like any other data-driven status).
- ``merged``: FROZEN, like ``published``. Set only by this module's own
  cross-week topic-merge pass (see below), never by upstream ranking/tiers
  facts. A ``merged`` entry never resurfaces as current, never re-derives,
  and never returns automatically (mirrors ``published``'s freeze, since a
  merge -- like a Founder publish decision -- is a durable structural fact
  about the library, not a data-driven bucket).

Every lifecycle transition and every score change appends an audit event
with a reason; score history is append-only (a later week's entry only ever
grows the ``score_history`` list, never edits or removes a prior row).

v0.2 additions (Opus orchestrator, Gate C -- see ADR 0004 D8 for the full
attributed design and constants):

- **Cross-week topic merge** (§12.1): two entries are the same subject when
  they share >= 1 ``subject_entity_ids`` AND their normalized-title Jaccard
  similarity is >= ``TOPIC_MERGE_JACCARD_THRESHOLD`` (0.7 -- deliberately
  higher than ``cluster.py``'s same-run 0.6 threshold; cross-week merging is
  a harder-to-undo decision than same-run clustering). See
  :func:`_apply_topic_merges`.
- **Decay and stale** (§12.2): :func:`effective_rank_score` is a DERIVED
  ordering/display value (``current_score`` minus a per-``freshness`` decay
  rate times :func:`weeks_since_last_evidence`) -- it never mutates the
  stored ``current_score``. The stale rule itself is unchanged (>= 8 weeks
  absence).
- **Structured relevance** (§12.3): ``relevance_reasons``,
  ``professional_connection``, and ``live_question_connections`` are derived
  from the already-computed ``RankingBreakdown`` (relevance/curiosity
  dimensions) -- never re-deriving a score. ``live_question_connections``
  additionally needs the run's ``RelevanceProfile`` to name concrete
  ``question_id``s (the breakdown only exposes a match COUNT); this is why
  :func:`update_library` gained an OPTIONAL keyword-only ``profile``
  parameter -- omitted (as every pre-v0.2 caller does), it is simply ``[]``.
- **Normalized summary** (§12.4, ADR 0004 D7): :func:`build_normalized_summary`
  derives a <= 280-character, markup-neutralized ``normalized_summary`` from
  the topic's own ``canonical_title``/``ranking_explanation`` -- never a raw
  article body.
- **Weekly deltas** (§12.5): :func:`compute_weekly_delta` builds one
  :class:`WeeklyDelta` per this week's tracked topic vs its previous library
  appearance. A topic ABSENT last week is ``is_new=True`` with
  ``score_delta=None`` -- NEVER treated as a drop from an implicit zero.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from content_machine.intelligence.brief import LibraryMovement, LibraryMovementsSection
from content_machine.intelligence.models import (
    ClaimClass,
    RankingBreakdown,
    RankingInputs,
    RelevanceProfile,
    SourceItem,
    TieredTopic,
    TopicCluster,
)
from content_machine.intelligence.normalize import jaccard, token_signature

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

#: v0.2 (Opus orchestrator, Gate C, §12.1; ADR 0004 D8): the Jaccard
#: similarity threshold, over normalized-title token sets, for two library
#: entries to be recognized as the SAME subject across weeks and merged.
#: Deliberately higher than cluster.py's same-run ``_JACCARD_TITLE_THRESHOLD``
#: (0.6) -- cross-week merging is a more consequential, harder-to-undo
#: decision than same-run clustering, so it demands stronger textual
#: evidence on top of the shared ``subject_entity_ids`` signal.
TOPIC_MERGE_JACCARD_THRESHOLD = 0.7

#: v0.2 (Opus orchestrator, Gate C, §12.2; ADR 0004 D8): decay rate, in
#: points per week of absence, applied by :func:`effective_rank_score` --
#: never mutates the stored ``current_score``.
DECAY_RATE_PER_WEEK: dict[str, int] = {"urgent": 20, "time_sensitive": 10, "evergreen": 2}

#: v0.2 (Opus orchestrator, Gate C, §12.4; ADR 0004 D7/D8): the maximum
#: length, in characters, of a persisted ``normalized_summary``.
NORMALIZED_SUMMARY_MAX_CHARS = 280

_MARKUP_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")

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
    "merged",
]

Freshness = Literal["urgent", "time_sensitive", "evergreen"]

#: Statuses this module never revisits once set: ``published``/``rejected``
#: are Founder-decision freezes; ``merged`` (v0.2) is this module's OWN
#: freeze, applied once a cross-week merge absorbs the entry -- see
#: :func:`_apply_topic_merges`.
_FROZEN_STATUSES = frozenset({"published", "rejected", "merged"})

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
    # --- v0.2 additions (Opus orchestrator, Gate C; ADR 0004 D8) -------------
    #: Opaque entity identifiers only (never names/emails/URLs) -- carried
    #: over from TopicCluster.subject_entity_ids so a future week can detect
    #: a cross-week merge (see _apply_topic_merges). Not prose.
    subject_entity_ids: list[str] = Field(default_factory=list)
    #: Former topic_id(s)/canonical_title(s) this entry absorbed via a
    #: cross-week merge (§12.1). Empty unless this entry is a merge survivor.
    aliases: list[str] = Field(default_factory=list)
    #: Set only on a `merged` (absorbed) entry: the surviving entry's
    #: topic_id. `None` for every entry that is not itself absorbed.
    merged_into: str | None = None
    #: Structured relevance (§12.3): human-readable reasons derived ONLY
    #: from the already-computed RankingBreakdown (relevance/curiosity
    #: dimensions) -- never a re-derived score.
    relevance_reasons: list[str] = Field(default_factory=list)
    #: One-line plain-language territory/tooling connection, or None when
    #: neither matched -- derived from the relevance dimension's own facts.
    professional_connection: str | None = None
    #: question_id(s) (from RelevanceProfile.live_questions) whose tags
    #: overlap this topic's topic_tags. Only populated when update_library
    #: is called with the run's `profile`; `[]` otherwise (documented
    #: limitation -- RankingBreakdown exposes only a match COUNT, not ids).
    live_question_connections: list[str] = Field(default_factory=list)
    #: The RelevanceProfile.profile_version that produced current_score.
    profile_version: str
    #: A <=280-char, markup-neutralized summary derived from canonical_title
    #: / ranking_explanation -- NEVER a raw article body (§12.4, ADR 0004 D7).
    normalized_summary: str
    #: This topic's rank the last week it appeared in the Top-N tiered
    #: signals; unchanged (not nulled) when the topic falls out of the Top-N
    #: -- used as `WeeklyDelta.previous_rank` on its next appearance.
    last_rank: int | None = None
    #: This topic's tier the last week it appeared in the Top-N tiered
    #: signals; same carry-over convention as `last_rank`.
    last_tier: str | None = None


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


class WeeklyDelta(BaseModel):
    """v0.2 (Opus orchestrator, Gate C, §12.5; ADR 0004 D8): one topic's
    week-over-week movement vs its PREVIOUS library appearance.

    CRITICAL (Founder-specified, never to regress): a topic absent from the
    prior library state is ``is_new=True`` with ``score_delta=None`` -- it is
    NEW, never treated as a drop from an implicit zero. A topic present
    before and now scoring/ranking lower reports a genuine negative delta.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    previous_score: int | None
    current_score: int
    score_delta: int | None
    previous_rank: int | None
    current_rank: int | None
    rank_delta: int | None
    tier_change: str | None
    new_evidence: bool
    is_new: bool
    movement_reason: str


class LibraryUpdateResult(BaseModel):
    """The result of one :func:`update_library` call: the FULL new library
    state (``entries``, one per tracked topic, sorted by ``topic_id``) plus
    only THIS week's new rows for the two append-only side logs, plus (v0.2)
    each Top-N topic's :class:`WeeklyDelta` vs its previous appearance."""

    model_config = ConfigDict(extra="forbid")

    entries: list[TopicLibraryEntry]
    new_score_history_rows: list[ScoreHistoryRow]
    new_audit_rows: list[AuditRow]
    deltas: list[WeeklyDelta] = Field(default_factory=list)


class MovementsDocument(BaseModel):
    """v0.2 (Opus orchestrator, Gate C, §13; ADR 0004 D8): the structured
    content behind the ``movements.md`` weekly output -- one bucket of
    ``"<canonical_title> (<topic_id>) -- <reason>"`` lines per movement
    category. See :func:`build_movements_document` /
    :func:`render_movements_markdown`."""

    model_config = ConfigDict(extra="forbid")

    new: list[str] = Field(default_factory=list)
    promoted: list[str] = Field(default_factory=list)
    demoted: list[str] = Field(default_factory=list)
    returning_from_deferred: list[str] = Field(default_factory=list)
    stale: list[str] = Field(default_factory=list)
    merged: list[str] = Field(default_factory=list)


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


# --- v0.2 structured relevance (§12.3, ADR 0004 D8): derived ONLY from the --
# already-computed RankingBreakdown -- never a re-derived score.


def _relevance_reasons(breakdown: RankingBreakdown, inputs: RankingInputs) -> list[str]:
    """Human-readable relevance reasons, built only from the already-scored
    relevance/curiosity dimensions -- never re-deriving a score."""
    relevance_dim = next(d for d in breakdown.dimensions if d.dimension == "relevance")
    curiosity_dim = next(d for d in breakdown.dimensions if d.dimension == "curiosity")
    reasons = [relevance_dim.anchor_text]
    matched = curiosity_dim.inputs.get("matched_live_questions", "0")
    if matched not in ("0", ""):
        tags = ", ".join(sorted(inputs.topic_tags)) if inputs.topic_tags else "no matched tag"
        reasons.append(f"{matched} live question(s) matched by topic_tags ({tags})")
    return reasons


def _professional_connection(breakdown: RankingBreakdown) -> str | None:
    """A one-line plain-language territory/tooling connection, derived only
    from the relevance dimension's own already-computed facts -- `None` when
    neither a territory nor a tooling match exists."""
    relevance_dim = next(d for d in breakdown.dimensions if d.dimension == "relevance")
    if relevance_dim.inputs.get("any_territory_match") == "True":
        return f"On-territory: {relevance_dim.anchor_text}"
    if relevance_dim.inputs.get("tooling_overlap") == "True":
        return f"Connects to current tooling: {relevance_dim.anchor_text}"
    return None


def _live_question_connections(
    inputs: RankingInputs, profile: RelevanceProfile | None
) -> list[str]:
    """question_id(s) whose tags overlap this topic's topic_tags. Requires
    the run's RelevanceProfile (RankingBreakdown only exposes a match COUNT,
    not ids) -- `[]` when `profile` is omitted, the documented v0.2
    limitation for callers that don't pass it (see the module docstring)."""
    if profile is None:
        return []
    tags = set(inputs.topic_tags)
    return sorted(q.question_id for q in profile.live_questions if set(q.tags) & tags)


def build_normalized_summary(canonical_title: str, ranking_explanation: str = "") -> str:
    """v0.2 (§12.4, ADR 0004 D7/D8): a <= 280-char, markup-neutralized
    summary derived from the topic's own canonical title and ranking
    explanation -- NEVER a raw article body. Any ``<...>`` markup (well
    formed or not) is stripped so no active HTML can survive; whitespace is
    collapsed; the result is truncated to ``NORMALIZED_SUMMARY_MAX_CHARS``.
    """
    raw = (
        f"{canonical_title}. {ranking_explanation}".strip()
        if ranking_explanation
        else canonical_title
    )
    without_tags = _MARKUP_TAG_RE.sub("", raw)
    # Belt-and-suspenders: strip any stray/unmatched angle brackets left over
    # from malformed markup, so no active tag can ever survive this function.
    without_brackets = without_tags.replace("<", "").replace(">", "")
    collapsed = _WHITESPACE_RE.sub(" ", without_brackets).strip()
    return collapsed[:NORMALIZED_SUMMARY_MAX_CHARS]


# --- v0.2 decay and stale (§12.2, ADR 0004 D8) -------------------------------


def weeks_since_last_evidence(entry: TopicLibraryEntry, current_week_label: str) -> int:
    """Week-label distance from ``entry.last_updated`` to ``current_week_label``
    -- reuses the library's existing week-distance helper (no ``datetime``)."""
    return _week_label_distance(entry.last_updated, current_week_label)


def effective_rank_score(entry: TopicLibraryEntry, current_week_label: str) -> int:
    """DERIVED ordering/display score: ``current_score`` minus this entry's
    ``freshness`` decay rate times weeks of absence, floored at 0. Never
    mutates the stored ``current_score`` -- callers that need a "what does
    this look like today" ordering call this; persistence is untouched."""
    weeks = weeks_since_last_evidence(entry, current_week_label)
    rate = DECAY_RATE_PER_WEEK[entry.freshness]
    return max(0, entry.current_score - rate * weeks)


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


#: Gate C correction round (Opus Finding 2): the exact reason text emitted
#: below when a topic simply leaves its one-time ``new`` status on its
#: second week with no other genuinely meaningful change -- routine
#: re-tracking, not a real movement. ``library_movements_for_brief`` matches
#: on this exact string to suppress these rows from the BRIEF-facing
#: movements list; the full audit trail (``audit.jsonl`` / ``audit_events``)
#: keeps every one of them regardless -- see the module docstring and
#: ``test_steady_state_second_week_declutters_routine_retracking_boilerplate``.
ROUTINE_RETRACKING_REASON = (
    "topic has been tracked for more than one week; recomputed from this week's data"
)


def _transition_reason(from_status: str, to_status: str, tier: str, claim_class: str) -> str:
    if from_status == "stale":
        return (
            "topic reappeared in this week's Top-10 signals after going stale -- treated "
            "as new evidence"
        )
    if from_status == "new":
        return ROUTINE_RETRACKING_REASON
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


# --- v0.2 cross-week topic merge (§12.1, ADR 0004 D8) ------------------------


def _merge_survivor_and_absorbed(
    a: TopicLibraryEntry, b: TopicLibraryEntry
) -> tuple[TopicLibraryEntry, TopicLibraryEntry]:
    """Deterministic tie-break (Opus orchestrator, Gate C): the entry with
    the earlier ``first_seen`` survives; ties broken by the lexicographically
    smaller ``topic_id``. Returns ``(survivor, absorbed)``."""
    if a.first_seen != b.first_seen:
        return (a, b) if a.first_seen < b.first_seen else (b, a)
    return (a, b) if a.topic_id < b.topic_id else (b, a)


def _dedup_sorted_score_history(
    a: list[ScoreHistoryEntry], b: list[ScoreHistoryEntry]
) -> list[ScoreHistoryEntry]:
    """The deduplicated, deterministically-sorted union of two score
    histories (by (week_label, score)) -- see the merge spec's requirement
    that a survivor's score_history become this union."""
    by_key: dict[tuple[str, int], ScoreHistoryEntry] = {}
    for entry in (*a, *b):
        by_key[(entry.week_label, entry.score)] = entry
    return [by_key[key] for key in sorted(by_key)]


def _dedup_sorted_audit_events(a: list[AuditEvent], b: list[AuditEvent]) -> list[AuditEvent]:
    """The deduplicated, deterministically-sorted union of two audit-event
    lists, keyed on every field (two events are "the same" only if every
    field matches) and sorted by (week_label, event_type, to_status,
    reason) for a stable, reproducible order."""
    by_key: dict[tuple[str, str, str | None, str, str], AuditEvent] = {}
    for event in (*a, *b):
        key = (
            event.week_label,
            event.event_type,
            event.from_status,
            event.to_status,
            event.reason,
        )
        by_key[key] = event
    ordered_keys = sorted(by_key, key=lambda k: (k[0], k[1], k[3], k[4]))
    return [by_key[key] for key in ordered_keys]


def _apply_topic_merges(
    entries: dict[str, TopicLibraryEntry], week_label: str
) -> tuple[dict[str, TopicLibraryEntry], list[AuditRow]]:
    """One deterministic merge pass over the FULL current library state
    (§12.1, ADR 0004 D8): every pair of non-``rejected``, non-``merged``
    entries that (a) share >= 1 ``subject_entity_ids`` AND (b) have
    normalized-title Jaccard similarity >= ``TOPIC_MERGE_JACCARD_THRESHOLD``
    is merged. Runs once per ``week_label`` over ``topic_id``-sorted
    candidates (a fixed snapshot taken before any merge is applied), so the
    result never depends on dict iteration order.

    On merge: the absorbed entry -> ``lifecycle_status="merged"``,
    ``merged_into=<survivor topic_id>``; the survivor gains the absorbed
    entry's ``topic_id``/``canonical_title`` in its ``aliases``, and its
    ``score_history``/``audit_events`` become the deduplicated,
    deterministically-sorted union of both (see
    :func:`_dedup_sorted_score_history` / :func:`_dedup_sorted_audit_events`).
    A ``merged`` audit event is emitted on the survivor naming the absorbed
    topic. A ``rejected`` entry never participates, either as survivor or
    absorbed.
    """
    candidates = sorted(
        (e for e in entries.values() if e.lifecycle_status not in ("rejected", "merged")),
        key=lambda e: e.topic_id,
    )
    merged_away: set[str] = set()
    new_audit_rows: list[AuditRow] = []
    updated = dict(entries)

    for index, a in enumerate(candidates):
        for b in candidates[index + 1 :]:
            if a.topic_id in merged_away:
                break
            if b.topic_id in merged_away or a.topic_id == b.topic_id:
                continue
            shared_subjects = set(a.subject_entity_ids) & set(b.subject_entity_ids)
            if not shared_subjects:
                continue
            similarity = jaccard(
                token_signature(a.canonical_title), token_signature(b.canonical_title)
            )
            if similarity < TOPIC_MERGE_JACCARD_THRESHOLD:
                continue

            survivor, absorbed = _merge_survivor_and_absorbed(a, b)
            current_survivor = updated[survivor.topic_id]
            current_absorbed = updated[absorbed.topic_id]
            shared_subjects_sorted = sorted(shared_subjects)

            survivor_reason = (
                f"merged topic '{current_absorbed.topic_id}' "
                f"('{current_absorbed.canonical_title}') into this entry: shared "
                f"subject_entity_id(s) {shared_subjects_sorted} and title Jaccard "
                f"similarity {similarity:.2f} >= {TOPIC_MERGE_JACCARD_THRESHOLD}"
            )
            survivor_event = AuditEvent(
                week_label=week_label,
                event_type="merged",
                from_status=None,
                to_status=current_survivor.lifecycle_status,
                reason=survivor_reason,
            )
            new_aliases = sorted(
                set(current_survivor.aliases)
                | {current_absorbed.topic_id, current_absorbed.canonical_title}
            )
            new_score_history = _dedup_sorted_score_history(
                current_survivor.score_history, current_absorbed.score_history
            )
            new_audit_events = [
                *_dedup_sorted_audit_events(
                    current_survivor.audit_events, current_absorbed.audit_events
                ),
                survivor_event,
            ]
            updated[current_survivor.topic_id] = current_survivor.model_copy(
                update={
                    "aliases": new_aliases,
                    "score_history": new_score_history,
                    "audit_events": new_audit_events,
                }
            )
            new_audit_rows.append(
                AuditRow(
                    topic_id=current_survivor.topic_id,
                    week_label=week_label,
                    event_type="merged",
                    from_status=None,
                    to_status=current_survivor.lifecycle_status,
                    reason=survivor_reason,
                )
            )

            absorbed_reason = (
                f"merged into topic '{current_survivor.topic_id}' "
                f"('{current_survivor.canonical_title}'): shared subject_entity_id(s) "
                f"{shared_subjects_sorted} and title Jaccard similarity "
                f"{similarity:.2f} >= {TOPIC_MERGE_JACCARD_THRESHOLD}"
            )
            absorbed_event = AuditEvent(
                week_label=week_label,
                event_type="lifecycle_transition",
                from_status=current_absorbed.lifecycle_status,
                to_status="merged",
                reason=absorbed_reason,
            )
            updated[current_absorbed.topic_id] = current_absorbed.model_copy(
                update={
                    "lifecycle_status": "merged",
                    "merged_into": current_survivor.topic_id,
                    "reason_not_selected": (
                        f"merged into topic '{current_survivor.topic_id}'; superseded, "
                        "never independently current"
                    ),
                    "reconsideration_condition": None,
                    "audit_events": [*current_absorbed.audit_events, absorbed_event],
                }
            )
            new_audit_rows.append(
                AuditRow(
                    topic_id=current_absorbed.topic_id,
                    week_label=week_label,
                    event_type="lifecycle_transition",
                    from_status=absorbed_event.from_status,
                    to_status="merged",
                    reason=absorbed_reason,
                )
            )
            merged_away.add(current_absorbed.topic_id)

    return updated, new_audit_rows


# ------------------------------ public API -----------------------------------


def update_library(
    tiered: list[TieredTopic],
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
    week_label: str,
    prior_entries: list[TopicLibraryEntry],
    *,
    profile: RelevanceProfile | None = None,
) -> LibraryUpdateResult:
    """Build this week's library state from ``tiered``/``ranked`` (the
    current week's already-tiered Top-10 topics and the full ranked list)
    plus ``prior_entries`` (the previously persisted state, or ``[]`` for the
    very first run). Pure and deterministic: same arguments (in particular
    the same ``prior_entries`` and the same ``week_label``), same result,
    field for field, byte for byte once serialized -- see
    ``test_deterministic_same_inputs_produce_byte_identical_output``.

    Never reads the wall clock: the only "week" input is ``week_label``.

    ``profile`` (v0.2, optional, keyword-only): the run's ``RelevanceProfile``,
    needed ONLY to name concrete ``live_question_connections`` ids (see the
    module docstring) -- every other v0.2 field is derived purely from
    already-computed ``RankingBreakdown``/``RankingInputs`` facts. Omitted by
    every pre-v0.2 caller; when omitted, ``live_question_connections`` is
    simply ``[]``.

    Also runs (v0.2) the cross-week topic-merge pass (see
    :func:`_apply_topic_merges`) and computes this week's per-topic
    :class:`WeeklyDelta` list (see :func:`compute_weekly_delta`) over the
    resulting ``LibraryUpdateResult.entries``/``.deltas``.
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

        if prior is not None and prior.lifecycle_status == "merged":
            # v0.2: frozen forever, same as `published` -- a merge is this
            # module's own durable structural fact, never re-derived even if
            # this topic_id resurfaces in a later week's signals.
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

        prior_aliases = list(prior.aliases) if prior is not None else []

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
            subject_entity_ids=list(cluster.subject_entity_ids),
            aliases=prior_aliases,
            merged_into=None,
            relevance_reasons=_relevance_reasons(breakdown, inputs),
            professional_connection=_professional_connection(breakdown),
            live_question_connections=_live_question_connections(inputs, profile),
            profile_version=breakdown.profile_version,
            normalized_summary=build_normalized_summary(
                topic.canonical_title, breakdown.ranking_explanation
            ),
            last_rank=topic.rank,
            last_tier=topic.tier_assignment.tier,
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

    merged_entries, merge_audit_rows = _apply_topic_merges(new_entries, week_label)
    audit_rows = [*audit_rows, *merge_audit_rows]

    deltas = [
        compute_weekly_delta(tiered_by_id[topic_id], prior_by_id.get(topic_id))
        for topic_id in sorted(tiered_by_id)
    ]

    all_entries = sorted(merged_entries.values(), key=lambda e: e.topic_id)
    return LibraryUpdateResult(
        entries=all_entries,
        new_score_history_rows=score_rows,
        new_audit_rows=audit_rows,
        deltas=deltas,
    )


# --- v0.2 weekly deltas (§12.5, ADR 0004 D8) ---------------------------------


def compute_weekly_delta(
    topic: TieredTopic, prior: TopicLibraryEntry | None
) -> WeeklyDelta:
    """One topic's week-over-week movement vs its previous library
    appearance (Opus orchestrator, Gate C, §12.5). CRITICAL: a topic absent
    from the prior library state (``prior is None``) is ``is_new=True`` with
    ``score_delta=None`` -- NEVER a drop from an implicit zero."""
    current_score = topic.score
    current_rank = topic.rank
    current_tier = topic.tier_assignment.tier

    if prior is None:
        return WeeklyDelta(
            topic_id=topic.topic_id,
            previous_score=None,
            current_score=current_score,
            score_delta=None,
            previous_rank=None,
            current_rank=current_rank,
            rank_delta=None,
            tier_change=None,
            new_evidence=True,
            is_new=True,
            movement_reason=(
                f"new topic this week (rank {current_rank}, score {current_score}/100)"
            ),
        )

    previous_score = prior.current_score
    previous_rank = prior.last_rank
    previous_tier = prior.last_tier
    score_delta = current_score - previous_score
    # Convention: POSITIVE rank_delta means IMPROVED (moved to a lower/better
    # rank number), e.g. rank 5 -> rank 2 is +3 -- mirrors how "promoted" reads.
    rank_delta = (previous_rank - current_rank) if previous_rank is not None else None
    tier_change = (
        f"{previous_tier} -> {current_tier}"
        if previous_tier is not None and previous_tier != current_tier
        else None
    )
    new_evidence = score_delta != 0

    parts: list[str] = []
    if score_delta > 0:
        parts.append(f"score rose by {score_delta} point(s)")
    elif score_delta < 0:
        parts.append(f"score fell by {abs(score_delta)} point(s)")
    else:
        parts.append("score unchanged")
    if rank_delta is not None:
        if rank_delta > 0:
            parts.append(f"rank improved from {previous_rank} to {current_rank}")
        elif rank_delta < 0:
            parts.append(f"rank fell from {previous_rank} to {current_rank}")
        else:
            parts.append(f"rank unchanged (#{current_rank})")
    if tier_change is not None:
        parts.append(f"tier changed: {tier_change}")

    return WeeklyDelta(
        topic_id=topic.topic_id,
        previous_score=previous_score,
        current_score=current_score,
        score_delta=score_delta,
        previous_rank=previous_rank,
        current_rank=current_rank,
        rank_delta=rank_delta,
        tier_change=tier_change,
        new_evidence=new_evidence,
        is_new=False,
        movement_reason="; ".join(parts),
    )


def _movement_bucket(delta: WeeklyDelta) -> str | None:
    """Classify one WeeklyDelta into a movements.md bucket name, or `None`
    when it represents no reportable movement (e.g. rank AND score both
    unchanged). Rank movement is the primary signal (matches "promoted"/
    "demoted" reading positions in the Top-N); score movement is the
    fallback when no previous rank is on record (the topic was tracked, but
    not previously in the Top-N).

    Gate C correction round (Opus Finding 1): a PURE rank-shuffle -- the
    score is unchanged AND no tier change occurred, and only the rank moved
    because other topics entered/left the Top-N -- is NOT a promotion or
    demotion; it overstates a mechanical composition change as a genuine
    ranking judgment about this topic. Such a delta is omitted from every
    bucket here (full "topic left the Top-N" fall-out reporting is
    deferred to v0.3 -- see ADR 0004's "Deferred to v0.3" note)."""
    if delta.is_new:
        return "new"
    if delta.score_delta == 0 and delta.tier_change is None:
        return None
    if delta.rank_delta is not None:
        if delta.rank_delta > 0:
            return "promoted"
        if delta.rank_delta < 0:
            return "demoted"
        return None
    if delta.score_delta is not None and delta.score_delta > 0:
        return "promoted"
    if delta.score_delta is not None and delta.score_delta < 0:
        return "demoted"
    return None


def build_movements_document(result: LibraryUpdateResult) -> MovementsDocument:
    """v0.2 (§13, ADR 0004 D8): the structured content for the ``movements.md``
    weekly output, built from ``result.deltas`` (new/promoted/demoted) and
    ``result.new_audit_rows`` (returning-from-deferred/stale/merged) --
    never re-deriving either."""
    entries_by_id = {e.topic_id: e for e in result.entries}

    new_items: list[str] = []
    promoted: list[str] = []
    demoted: list[str] = []
    for delta in sorted(result.deltas, key=lambda d: d.topic_id):
        if delta.topic_id not in entries_by_id:
            continue
        # A topic that was BOTH newly created and absorbed by a merge in the
        # SAME week (the realistic merge scenario) is reported only via the
        # `merged` bucket below -- never double-counted as `new` too.
        if entries_by_id[delta.topic_id].lifecycle_status == "merged":
            continue
        bucket = _movement_bucket(delta)
        title = entries_by_id[delta.topic_id].canonical_title
        line = f"{title} ({delta.topic_id}) -- {delta.movement_reason}"
        if bucket == "new":
            new_items.append(line)
        elif bucket == "promoted":
            promoted.append(line)
        elif bucket == "demoted":
            demoted.append(line)

    returning_from_deferred: list[str] = []
    stale: list[str] = []
    merged: list[str] = []
    for row in sorted(result.new_audit_rows, key=lambda r: (r.topic_id, r.week_label)):
        title = (
            entries_by_id[row.topic_id].canonical_title
            if row.topic_id in entries_by_id
            else row.topic_id
        )
        line = f"{title} ({row.topic_id}) -- {row.reason}"
        if row.from_status == "deferred" and row.to_status != "deferred":
            returning_from_deferred.append(line)
        if row.to_status == "stale":
            stale.append(line)
        if row.to_status == "merged":
            merged.append(line)

    return MovementsDocument(
        new=new_items,
        promoted=promoted,
        demoted=demoted,
        returning_from_deferred=returning_from_deferred,
        stale=stale,
        merged=merged,
    )


def render_movements_markdown(document: MovementsDocument, week_label: str) -> str:
    """Render :class:`MovementsDocument` to the ``movements.md`` Markdown
    text -- one section per movement category, in the documented order:
    new, promoted, demoted, returning-from-deferred, stale, merged."""

    def _section(title: str, items: list[str]) -> list[str]:
        lines = [f"## {title}"]
        if not items:
            lines.append(f"No {title.lower()} topics this week.")
        else:
            lines.extend(f"- {item}" for item in items)
        return lines

    lines = [f"# Library Movements -- Week {week_label}", ""]
    lines.extend(_section("New", document.new))
    lines.append("")
    lines.extend(_section("Promoted", document.promoted))
    lines.append("")
    lines.extend(_section("Demoted", document.demoted))
    lines.append("")
    lines.extend(_section("Returning From Deferred", document.returning_from_deferred))
    lines.append("")
    lines.extend(_section("Stale", document.stale))
    lines.append("")
    lines.extend(_section("Merged", document.merged))
    lines.append("")
    return "\n".join(lines) + "\n"


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
    never the M6-not-wired-in note (see ``NO_LIBRARY_MOVEMENTS_NOTE``).

    v0.2 (§13, ADR 0004 D8): also populates the bucketed
    ``new``/``promoted``/``demoted``/``returning_from_deferred``/``stale``/
    ``merged`` fields on the returned :class:`LibraryMovementsSection`, using
    ``result.deltas`` for the first three and ``result.new_audit_rows`` for
    the rest -- the SAME classification :func:`build_movements_document` uses
    for the ``movements.md`` output, so the brief and the file never diverge.

    Gate C correction round (Opus Finding 2): rows whose reason is the plain
    ``ROUTINE_RETRACKING_REASON`` boilerplate (a topic simply leaving its
    one-time ``new`` status with no other meaningful change) are ALSO
    suppressed here, on top of the plain ``score_change``/``created``
    suppression above -- they are routine re-tracking, not a genuine
    movement, and would otherwise clutter every steady-state week with one
    near-identical line per continuing topic. The full audit trail
    (``result.new_audit_rows`` / the persisted ``audit_events``) is
    unaffected and keeps every one of them.
    """
    entries_by_id = {e.topic_id: e for e in result.entries}
    movements = [
        LibraryMovement(
            topic_id=row.topic_id,
            canonical_title=entries_by_id[row.topic_id].canonical_title,
            movement=row.to_status,
            reason=row.reason,
        )
        for row in result.new_audit_rows
        if row.event_type not in ("score_change", "created")
        and row.to_status != "new"
        and row.reason != ROUTINE_RETRACKING_REASON
    ]
    deferred_note = NO_LIBRARY_MOVEMENTS_NOTE if not movements else None

    new_bucket: list[LibraryMovement] = []
    promoted_bucket: list[LibraryMovement] = []
    demoted_bucket: list[LibraryMovement] = []
    for delta in sorted(result.deltas, key=lambda d: d.topic_id):
        if delta.topic_id not in entries_by_id:
            continue
        # See build_movements_document: a topic both new and merged-away in
        # the same week is reported only via the `merged` bucket below.
        if entries_by_id[delta.topic_id].lifecycle_status == "merged":
            continue
        bucket_name = _movement_bucket(delta)
        if bucket_name is None:
            continue
        item = LibraryMovement(
            topic_id=delta.topic_id,
            canonical_title=entries_by_id[delta.topic_id].canonical_title,
            movement=bucket_name,
            reason=delta.movement_reason,
        )
        if bucket_name == "new":
            new_bucket.append(item)
        elif bucket_name == "promoted":
            promoted_bucket.append(item)
        elif bucket_name == "demoted":
            demoted_bucket.append(item)

    returning_bucket: list[LibraryMovement] = []
    stale_bucket: list[LibraryMovement] = []
    merged_bucket: list[LibraryMovement] = []
    for row in result.new_audit_rows:
        if row.topic_id not in entries_by_id:
            continue
        item = LibraryMovement(
            topic_id=row.topic_id,
            canonical_title=entries_by_id[row.topic_id].canonical_title,
            movement=row.to_status,
            reason=row.reason,
        )
        if row.from_status == "deferred" and row.to_status != "deferred":
            returning_bucket.append(item)
        if row.to_status == "stale":
            stale_bucket.append(item)
        if row.to_status == "merged":
            merged_bucket.append(item)

    return LibraryMovementsSection(
        movements=movements,
        deferred_note=deferred_note,
        new=new_bucket,
        promoted=promoted_bucket,
        demoted=demoted_bucket,
        returning_from_deferred=returning_bucket,
        stale=stale_bucket,
        merged=merged_bucket,
    )


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
