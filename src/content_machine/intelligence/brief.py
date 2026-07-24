"""M5: renders the weekly Intelligence Brief (Markdown + JSON) from M4's
already-tiered output.

This module is a PURE RENDERER. It never re-ranks, re-tiers, or re-derives
evidence/claim classification -- it only reads the facts already produced by
``ranking.rank_topics`` (the full ordered ``(RankingInputs, RankingBreakdown)``
list), ``tiers.assign_tiers`` (the Top-``TOP_N`` :class:`TieredTopic` list),
``cluster.cluster_items`` (``TopicCluster`` records, needed for the canonical
title of topics that fall OUTSIDE the Top N and therefore have no
``TieredTopic``), and the ``SourceItem`` anchors. See
``content_machine.intelligence.tiers`` and
``docs/adr/0004-intelligence-evidence-and-ranking-decisions.md`` for the
upstream decisions this brief reports on.

Determinism (no wall clock): the ISO week label is an INPUT parameter
(``week_label``), never computed from ``datetime.now()``/``date.today()`` --
this module does not import ``datetime`` at all. Given the same pipeline
output and the same ``week_label``, :func:`build_weekly_brief` and
:func:`render_markdown` are byte-identical across runs, and across process
restarts and item-shuffles that do not change ``ranking.rank_topics``'s
output order (already proven stable in ``test_intelligence_tiers.py`` /
``test_intelligence_guarantees.py``).

The Markdown is rendered FROM the same :class:`WeeklyBrief` structured object
that is also serialized to JSON (via ``WeeklyBrief.model_dump_json``) -- so
the two representations can never diverge; see
``tests/test_intelligence_brief.py::test_markdown_and_json_never_diverge`` for
the consistency check the ticket calls out explicitly.

The brief NEVER auto-publishes anything: it always ends in
``review_status = "awaiting_founder_review"`` (JSON) and a matching closing
line (Markdown). No code path in this module sends, posts, schedules, or
writes outside of returning plain Python objects/strings to its caller.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from content_machine.intelligence.models import (
    ClaimAssessment,
    ClaimClass,
    ConfidenceLevel,
    DimensionScore,
    RankingBreakdown,
    RankingInputs,
    RecommendedAction,
    SourceItem,
    TieredTopic,
    TopicCluster,
)
from content_machine.intelligence.tiers import TOP_N

BRIEF_VERSION = "gate-b-m5-1"

REVIEW_STATUS = "awaiting_founder_review"

# --- deterministic reading-time budget (Founder spec: "state the method") --
#
# Word-count-from-rendered-Markdown was deliberately rejected: the Markdown is
# rendered FROM the WeeklyBrief object, so if the brief itself embedded a
# reading-time figure derived from that same Markdown's word count, building
# the object would require the Markdown, and rendering the Markdown would
# require the object -- a circular dependency. Instead this uses a FIXED
# per-section-item minute budget, a function purely of already-computed
# structural counts (how many Tier 1/2/3 items, whether a deep/light study
# topic or experiment was selected, how many content opportunities, whether
# any topics were discarded) -- never of prose length.
_READING_MINUTES_EXECUTIVE_SUMMARY = 1
_READING_MINUTES_TIER1_ITEM = 1
_READING_MINUTES_TIER2_ITEM = 1
_READING_MINUTES_TIER3_ITEM = 1
_READING_MINUTES_DEEP_STUDY = 2
_READING_MINUTES_LIGHT_STUDY_ITEM = 1
_READING_MINUTES_EXPERIMENT = 1
_READING_MINUTES_CONTENT_OPPORTUNITY_ITEM = 1
_READING_MINUTES_DISCARDED_SECTION = 1
_READING_MINUTES_RANKING_EXPLANATION_POINTER = 1
_READING_MINUTES_APPENDIX_TIER1_ITEM = 2

ESTIMATED_READING_TIME_METHOD = (
    "Fixed per-section-item minute budget (never a word count of the rendered "
    "Markdown, to avoid a circular dependency between the brief object and its "
    "own rendered text): "
    f"executive summary {_READING_MINUTES_EXECUTIVE_SUMMARY}; "
    f"each Tier 1 item {_READING_MINUTES_TIER1_ITEM}; "
    f"each Tier 2 item {_READING_MINUTES_TIER2_ITEM}; "
    f"each Tier 3/radar item {_READING_MINUTES_TIER3_ITEM}; "
    f"deep-study topic (if any) {_READING_MINUTES_DEEP_STUDY}; "
    f"each light-study topic {_READING_MINUTES_LIGHT_STUDY_ITEM}; "
    f"the practical experiment (if any) {_READING_MINUTES_EXPERIMENT}; "
    f"each content opportunity {_READING_MINUTES_CONTENT_OPPORTUNITY_ITEM}; "
    f"the discarded-topics section (if non-empty) {_READING_MINUTES_DISCARDED_SECTION}; "
    f"the ranking-explanation pointer {_READING_MINUTES_RANKING_EXPLANATION_POINTER}; "
    f"each Tier 1 appendix record {_READING_MINUTES_APPENDIX_TIER1_ITEM}. Summed, integer minutes."
)

# --- deterministic STUDY-time budget (spec 4.2: "tempo de leitura E de       --
# estudo" -- reading time and study time are two distinct figures). Reading
# time (above) estimates how long it takes to READ the brief document itself;
# study time estimates the separate, larger effort of actually working
# through the study-queue topics it points to. Same fixed-budget method as
# reading time, for the same circularity reason.
_STUDY_MINUTES_DEEP_STUDY_TOPIC = 20
_STUDY_MINUTES_LIGHT_STUDY_TOPIC = 10

ESTIMATED_STUDY_TIME_METHOD = (
    "Fixed per-study-queue-item minute budget, distinct from the reading-time estimate above "
    "(spec: reading time AND study time are separate figures): the deep-study topic (if any) "
    f"{_STUDY_MINUTES_DEEP_STUDY_TOPIC}; each light-study topic "
    f"{_STUDY_MINUTES_LIGHT_STUDY_TOPIC}. Summed, integer minutes; zero when the study queue "
    "is empty."
)

# --- human phrase for action_required, used by the Tier 1/2 lean "why it ----
# matters" template (no rubric jargon).
_ACTION_REQUIRED_HUMAN_PHRASE: dict[str, str] = {
    "migration_required": "requires a migration",
    "config_or_code_change": "requires a config or code change",
    "new_option_available": "makes a new option available",
    "changes_how_to_think": "changes how you should think about this area",
    "none": "requires no immediate action",
}

# --- human phrases for Tier 2's practical_consequence/principal_evidence ----
# lines (F-B fix): plain-English severity/evidence-quality words keyed off
# already-computed structured values (the consequence dimension's
# effective_value, and evidence_level) -- never the dimension rationale's
# debug-trace prose ("action_required '...' has raw consequence N",
# "evidence_level=N (...); has_independent_evidence=..., marketing_risk=...").
_CONSEQUENCE_SEVERITY_PHRASE: dict[int, str] = {
    5: "high-impact",
    4: "significant",
    3: "moderate",
    2: "minor",
    1: "minor",
    0: "negligible",
}

_EVIDENCE_LEVEL_HUMAN_PHRASE: dict[int, str] = {
    5: "very strong evidence: an authoritative source plus rigorous independent analysis",
    4: "strong evidence: a first-party source or rigorous independent evidence",
    3: "a single first-party authoritative source",
    2: "limited evidence",
    1: "weak, largely uncorroborated evidence",
    0: "no meaningful evidence",
}

# --- D1 threshold-watch diagnostic (decision-support only, ADR 0004/D1) -----
D1_THRESHOLD_WATCH_NOTE = (
    "Would become Tier-1 eligible if D1's threshold were evidence_level >= 3 instead of >= 4 "
    "(ADR 0004). Currently inert: decision-support only -- does NOT admit this topic to Tier "
    "1 and never will unless the Founder rules to change the threshold."
)

# --- library movements (M6 deferral note for a standalone M5 brief) --------
LIBRARY_MOVEMENTS_DEFERRED_NOTE = (
    "Library movements (new / promoted-from-deferred / deferred / rejected / stale, with "
    "reasons) are deferred to the topic library (M6): no persistent topic library is wired "
    "into this brief yet."
)

# --- editorial-gate proxy for content opportunities (Founder spec example) --
CONTENT_OPPORTUNITY_SELECTION_RULE = (
    "Tier 1 admitted AND claim_class == 'fact' AND confidence in {high, medium} AND "
    "the relevance dimension's effective_value >= 4 (on-territory) -- the exact proxy "
    "named in the Founder spec. At most 3 topics are selected, in rank order; zero is a "
    "valid outcome and is stated explicitly rather than backfilled."
)

STUDY_SELECTION_RULE = (
    "Deep-study: the highest-ranked (lowest rank number) Tier 1 topic whose "
    "recommended_action == 'study' (only Tier 1 topics ever recommend 'study', per "
    "tiers.py's recommended-action rule chain). Light-study: the next two Tier 2 "
    "topics, in rank order, whose claim_class == 'fact' (marketing and hypothesis "
    "claims are excluded from the light-study queue) -- Tier 1 topics are excluded "
    "from the light picks since they already receive full 'study' treatment above."
)

EXPERIMENT_SELECTION_RULE = (
    "The highest-ranked (lowest rank number) Top-10 topic whose "
    "experiment_affordance == 'local_reproducible', regardless of tier. If no Top-10 "
    "topic is locally reproducible, no experiment is suggested."
)


# ------------------------------ nested models -------------------------------


class DiscardedTopic(BaseModel):
    """One topic ranked below the Top N, with a one-line reason."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    score: int
    reason: str


class Tier1LeanItem(BaseModel):
    """Lean, ~5-line Tier 1 ("Must Understand") presentation of one topic."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    topic_id: str
    canonical_title: str
    what_changed: str
    why_it_matters: str
    evidence_and_confidence: str
    recommended_action: RecommendedAction
    recommended_action_reason: str
    score: int
    ranking_explanation: str


class Tier2Item(BaseModel):
    """Concise "Should Know" presentation of one Tier 2 topic."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    topic_id: str
    canonical_title: str
    explanation: str
    practical_consequence: str
    principal_evidence: str
    confidence: ConfidenceLevel
    recommended_action: RecommendedAction


class RadarItem(BaseModel):
    """One-paragraph "Radar" presentation of one Tier 3 topic."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    topic_id: str
    canonical_title: str
    signal_paragraph: str


class StudyTopicRef(BaseModel):
    """A pointer to one topic selected into the study queue."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    rank: int
    reason: str


class StudyQueue(BaseModel):
    """One deep-study topic (optional) plus up to two light-study topics."""

    model_config = ConfigDict(extra="forbid")

    selection_rule: str
    deep: StudyTopicRef | None
    deep_none_reason: str | None
    light: list[StudyTopicRef]
    light_none_reason: str | None


class ExperimentSuggestion(BaseModel):
    """One locally-reproducible practical experiment derived from a topic."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    rank: int
    what_it_would_test: str


class ExperimentSelection(BaseModel):
    """The (at most one) practical-experiment suggestion, or a stated 'none'."""

    model_config = ConfigDict(extra="forbid")

    selection_rule: str
    experiment: ExperimentSuggestion | None
    none_reason: str | None


class ContentOpportunity(BaseModel):
    """One topic that passed the content-opportunity editorial-gate proxy."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    rank: int
    reason: str


class ContentOpportunitySelection(BaseModel):
    """Up to three content opportunities. Zero is a valid, stated outcome --
    never backfilled to hit a quota."""

    model_config = ConfigDict(extra="forbid")

    selection_rule: str
    opportunities: list[ContentOpportunity]
    none_reason: str | None


class Tier1AppendixRecord(BaseModel):
    """The full twelve-field record for one Tier 1 topic -- every fact the
    lean Tier 1 presentation omits. Exactly twelve named fields (see
    ``tests/test_intelligence_brief.py`` for the field-count assertion):
    ``topic_id``, ``rank``, ``canonical_title``, ``score``, ``claim_class``,
    ``claim_class_reason``, ``confidence``, ``confidence_reason``,
    ``admission_reasons``, ``exclusion_reasons``, ``warnings``, and
    ``dimension_breakdown`` (one summary line per ``RankingBreakdown``
    dimension, so the full per-dimension detail is reachable without
    re-running the rubric). ``tier1_admitted`` is deliberately not a
    thirteenth field: every appendix record is for an admitted Tier 1 topic
    by construction.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    rank: int
    canonical_title: str
    score: int
    claim_class: ClaimClass
    claim_class_reason: str
    confidence: ConfidenceLevel
    confidence_reason: str
    admission_reasons: list[str]
    exclusion_reasons: list[str]
    warnings: list[str]
    dimension_breakdown: list[str]


class D1ThresholdWatchItem(BaseModel):
    """One Top-10 topic flagged by ``tiers.d1_would_admit_at_evidence_3`` --
    decision-support only for the Founder's D1 threshold ruling (ADR 0004).
    Never affects ``tier1_admitted``; see
    ``test_d1_threshold_watch_never_changes_tier_assignment``."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    rank: int
    canonical_title: str
    evidence_type: str
    evidence_level: int
    note: str


class LibraryMovement(BaseModel):
    """One topic's lifecycle movement in the persistent topic library (M6),
    e.g. newly seen, promoted from deferred, deferred, rejected, or gone
    stale -- with the reason for the movement."""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    canonical_title: str
    movement: str
    reason: str


class LibraryMovementsSection(BaseModel):
    """Library movements for this week. ``deferred_note`` is populated (and
    ``movements`` empty) when no library-update result is wired into this
    brief -- e.g. a standalone M5 brief with no M6 library yet -- rather than
    silently omitting the section."""

    model_config = ConfigDict(extra="forbid")

    movements: list[LibraryMovement] = Field(default_factory=list)
    deferred_note: str | None = None


class WeeklyBrief(BaseModel):
    """The full structured Intelligence Brief -- the single source of truth
    both the Markdown and the JSON outputs are derived from.

    Always ends in ``review_status = "awaiting_founder_review"``: nothing in
    this module or its caller may set this to anything else, and no code path
    here publishes, sends, or schedules anything.
    """

    model_config = ConfigDict(extra="forbid")

    week_label: str
    brief_version: str
    rubric_version: str
    weights_version: str
    taxonomy_version: str
    profile_version: str
    tldr: list[str]
    executive_summary: list[str]
    estimated_reading_minutes: int
    estimated_reading_time_method: str
    estimated_study_minutes: int
    estimated_study_time_method: str
    tier1: list[Tier1LeanItem]
    tier1_short_reason: str | None
    tier2: list[Tier2Item]
    tier3: list[RadarItem]
    study_queue: StudyQueue
    experiment: ExperimentSelection
    content_opportunities: ContentOpportunitySelection
    discarded: list[DiscardedTopic]
    d1_threshold_watch: list[D1ThresholdWatchItem]
    library_movements: LibraryMovementsSection
    ranking_explanation_pointer: str
    appendix: list[Tier1AppendixRecord]
    review_status: str = REVIEW_STATUS


# ------------------------------ internal helpers -----------------------------


def _breakdown_lookup(
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
) -> dict[str, RankingBreakdown]:
    return {inputs.topic_id: breakdown for inputs, breakdown in ranked}


def _dimension(breakdown: RankingBreakdown, name: str) -> DimensionScore:
    return next(d for d in breakdown.dimensions if d.dimension == name)


def _dimension_summary_line(breakdown: RankingBreakdown) -> list[str]:
    lines = []
    for d in breakdown.dimensions:
        lines.append(
            f"{d.dimension}: raw={d.raw_value} effective={d.effective_value} "
            f"({d.points} pts, weight {d.weight}) -- {d.rationale}"
        )
    return lines


def _primary_exclusion_category(eligibility_reasons: list[str]) -> str:
    """Deterministically pick the FIRST failing base-rule condition (fixed
    order: relevance, evidence, independence, marketing -- the order
    ``ranking._tier1_eligibility`` always emits them in) and map it to a
    human-readable category. If every condition passes (the topic actually
    met the Tier 1 bar but simply ranked below the Top N by score), a fifth,
    distinct category is returned instead."""
    category_by_prefix = (
        ("relevance effective_value", "insufficient relevance to current territory priorities"),
        ("evidence effective_value", "insufficient evidence level"),
        ("has_independent_evidence", "lack of independent corroboration"),
        ("not marketing_risk", "marketing risk not cleared by independent evidence"),
    )
    for reason in eligibility_reasons:
        if reason.endswith(": fail"):
            for prefix, category in category_by_prefix:
                if reason.startswith(prefix):
                    return category
    return "met the Tier 1 admission bar but ranked below the Top 10 by score alone"


def _anchor_for_topic(
    topic_id: str,
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
) -> SourceItem:
    cluster = clusters_by_topic_id[topic_id]
    return items_by_id[cluster.anchor_item_id]


def _human_what_changed(anchor: SourceItem) -> str:
    """Human-authored prose about the actual change, never rubric mechanics:
    the anchor's authored ``change_class_rationale``, falling back to its
    ``summary_normalized`` only when the rationale was left empty."""
    return anchor.change_class_rationale or anchor.summary_normalized


def _human_why_it_matters(inputs: RankingInputs, claim: ClaimAssessment) -> str:
    """Deterministic, human-readable template built ONLY from already-computed
    structured fields (topic_tags, action_required, claim_class, confidence)
    -- no rubric jargon (raw/effective values, weights, caps/floors)."""
    territory = ", ".join(sorted(inputs.topic_tags)) if inputs.topic_tags else "no matched tag"
    action_phrase = _ACTION_REQUIRED_HUMAN_PHRASE.get(
        inputs.action_required, inputs.action_required
    )
    return (
        f"On-territory ({territory}); {action_phrase}. "
        f"{claim.claim_class} at {claim.confidence} confidence."
    )


def _human_practical_consequence(inputs: RankingInputs, consequence: DimensionScore) -> str:
    """F-B: Tier 2's practical-consequence line, human prose built only from
    already-computed structured fields (the action_required phrase, the
    consequence dimension's effective_value as a severity word, and whether
    a breaking-change floor applied) -- never the dimension's raw rationale
    ("action_required '...' has raw consequence N")."""
    action_phrase = _ACTION_REQUIRED_HUMAN_PHRASE.get(
        inputs.action_required, inputs.action_required
    )
    severity = _CONSEQUENCE_SEVERITY_PHRASE.get(consequence.effective_value, "unclear")
    sentence = f"{action_phrase.capitalize()} -- {severity} practical consequence."
    if consequence.floor_applied is not None:
        sentence += (
            " Treated as high-impact because it is a breaking change confirmed by a direct "
            "artifact or an independent source."
        )
    return sentence


def _human_principal_evidence(inputs: RankingInputs, claim: ClaimAssessment) -> str:
    """F-B: Tier 2's principal-evidence line, human prose built only from
    already-computed structured fields (evidence_level in words,
    has_independent_evidence, claim_class, confidence) -- never the
    dimension's raw rationale ("evidence_level=N (...); "
    "has_independent_evidence=..., marketing_risk=...")."""
    evidence_phrase = _EVIDENCE_LEVEL_HUMAN_PHRASE.get(
        inputs.evidence_level, f"evidence level {inputs.evidence_level}"
    )
    independence_phrase = (
        "independently corroborated"
        if inputs.has_independent_evidence
        else "not independently corroborated"
    )
    return (
        f"{evidence_phrase.capitalize()}, {independence_phrase}. "
        f"Classified as {claim.claim_class} at {claim.confidence} confidence."
    )


def _build_tier1_lean(
    topic: TieredTopic, breakdown: RankingBreakdown, inputs: RankingInputs, anchor: SourceItem
) -> Tier1LeanItem:
    return Tier1LeanItem(
        rank=topic.rank,
        topic_id=topic.topic_id,
        canonical_title=topic.canonical_title,
        what_changed=_human_what_changed(anchor),
        why_it_matters=_human_why_it_matters(inputs, topic.claim),
        evidence_and_confidence=(
            f"{topic.claim.claim_class} claim, {topic.claim.confidence} confidence -- "
            f"{topic.claim.confidence_reason}"
        ),
        recommended_action=topic.tier_assignment.recommended_action,
        recommended_action_reason=topic.tier_assignment.recommended_action_reason,
        score=topic.score,
        ranking_explanation=breakdown.ranking_explanation,
    )


def _build_tier2(
    topic: TieredTopic, breakdown: RankingBreakdown, inputs: RankingInputs, anchor: SourceItem
) -> Tier2Item:
    consequence = _dimension(breakdown, "consequence")
    return Tier2Item(
        rank=topic.rank,
        topic_id=topic.topic_id,
        canonical_title=topic.canonical_title,
        explanation=_human_what_changed(anchor),
        practical_consequence=_human_practical_consequence(inputs, consequence),
        principal_evidence=_human_principal_evidence(inputs, topic.claim),
        confidence=topic.claim.confidence,
        recommended_action=topic.tier_assignment.recommended_action,
    )


def _radar_future_event(claim_class: str, confidence: str) -> str:
    """Deterministic, exhaustive function of (claim_class, confidence) only --
    no new facts invented. Covers every reachable combination (see
    ``tiers._assess_confidence``: non-'fact' claims are always 'low')."""
    if claim_class == "marketing":
        return "independent, non-promotional evidence corroborates the claim"
    if claim_class == "hypothesis":
        return "the claim is corroborated by an artifact or an independent source"
    if confidence == "medium":
        return "independent corroboration raises confidence from medium to high"
    return "its relevance or consequence increases relative to other ranked topics"


def _build_radar(topic: TieredTopic) -> RadarItem:
    if topic.tier_assignment.tier1_admitted:
        entered_because = "it already meets the Tier 1 admission bar but ranks outside the Top 3"
    elif topic.tier_assignment.exclusion_reasons:
        entered_because = topic.tier_assignment.exclusion_reasons[0]
    else:
        entered_because = "it ranked outside the Top 3 window"
    future_event = _radar_future_event(topic.claim.claim_class, topic.claim.confidence)
    paragraph = (
        f"Currently ranked #{topic.rank} (score {topic.score}/100), "
        f"classified as {topic.claim.claim_class} at {topic.claim.confidence} confidence. "
        f"It entered the radar because {entered_because}. It would warrant closer attention if "
        f"{future_event}."
    )
    return RadarItem(
        rank=topic.rank,
        topic_id=topic.topic_id,
        canonical_title=topic.canonical_title,
        signal_paragraph=paragraph,
    )


def _build_appendix_record(
    topic: TieredTopic, breakdown: RankingBreakdown
) -> Tier1AppendixRecord:
    return Tier1AppendixRecord(
        topic_id=topic.topic_id,
        rank=topic.rank,
        canonical_title=topic.canonical_title,
        score=topic.score,
        claim_class=topic.claim.claim_class,
        claim_class_reason=topic.claim.claim_class_reason,
        confidence=topic.claim.confidence,
        confidence_reason=topic.claim.confidence_reason,
        admission_reasons=list(topic.tier_assignment.admission_reasons),
        exclusion_reasons=list(topic.tier_assignment.exclusion_reasons),
        warnings=list(topic.tier_assignment.warnings),
        dimension_breakdown=_dimension_summary_line(breakdown),
    )


def _build_study_queue(
    tier1_topics: list[TieredTopic], tier2_topics: list[TieredTopic]
) -> StudyQueue:
    deep = None
    deep_none_reason = None
    deep_source = next(
        (t for t in tier1_topics if t.tier_assignment.recommended_action == "study"), None
    )
    if deep_source is not None:
        deep = StudyTopicRef(
            topic_id=deep_source.topic_id,
            canonical_title=deep_source.canonical_title,
            rank=deep_source.rank,
            reason=(
                "highest-ranked Tier 1 topic recommended for full study "
                f"(rank {deep_source.rank}, score {deep_source.score}/100)"
            ),
        )
    else:
        deep_none_reason = "no Tier 1 topic was admitted this week, so no deep-study topic exists"

    light_candidates = [
        t for t in tier2_topics if t.claim.claim_class == "fact"
    ]
    light_selected = light_candidates[:2]
    light = [
        StudyTopicRef(
            topic_id=t.topic_id,
            canonical_title=t.canonical_title,
            rank=t.rank,
            reason=(
                f"Tier 2 fact-classified topic, rank {t.rank}, "
                f"{t.claim.confidence} confidence"
            ),
        )
        for t in light_selected
    ]
    light_none_reason = None
    if not light:
        light_none_reason = "no Tier 2 topic had claim_class == 'fact' this week"

    return StudyQueue(
        selection_rule=STUDY_SELECTION_RULE,
        deep=deep,
        deep_none_reason=deep_none_reason,
        light=light,
        light_none_reason=light_none_reason,
    )


def _build_experiment(
    top_n_topics: list[TieredTopic],
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
) -> ExperimentSelection:
    inputs_by_topic_id = {inputs.topic_id: inputs for inputs, _breakdown in ranked}
    candidate = next(
        (
            t
            for t in top_n_topics
            if inputs_by_topic_id[t.topic_id].experiment_affordance == "local_reproducible"
        ),
        None,
    )
    if candidate is None:
        return ExperimentSelection(
            selection_rule=EXPERIMENT_SELECTION_RULE,
            experiment=None,
            none_reason="no Top-10 topic has experiment_affordance == 'local_reproducible'",
        )

    anchor = _anchor_for_topic(candidate.topic_id, clusters_by_topic_id, items_by_id)
    what_it_would_test = (
        f"Reproduce locally: verify the {anchor.change_class} described in "
        f"'{candidate.canonical_title}' ({anchor.change_class_rationale}); "
        f"action_required={anchor.action_required}."
    )
    return ExperimentSelection(
        selection_rule=EXPERIMENT_SELECTION_RULE,
        experiment=ExperimentSuggestion(
            topic_id=candidate.topic_id,
            canonical_title=candidate.canonical_title,
            rank=candidate.rank,
            what_it_would_test=what_it_would_test,
        ),
        none_reason=None,
    )


def _build_content_opportunities(
    tier1_topics: list[TieredTopic],
    breakdown_by_topic_id: dict[str, RankingBreakdown],
) -> ContentOpportunitySelection:
    opportunities: list[ContentOpportunity] = []
    for topic in tier1_topics:
        if topic.claim.claim_class != "fact":
            continue
        if topic.claim.confidence not in ("high", "medium"):
            continue
        breakdown = breakdown_by_topic_id[topic.topic_id]
        relevance = _dimension(breakdown, "relevance")
        if relevance.effective_value < 4:
            continue
        opportunities.append(
            ContentOpportunity(
                topic_id=topic.topic_id,
                canonical_title=topic.canonical_title,
                rank=topic.rank,
                reason=(
                    f"Tier 1, claim_class=fact, confidence={topic.claim.confidence}, "
                    f"relevance effective_value={relevance.effective_value} >= 4 "
                    "(on-territory)"
                ),
            )
        )
        # Defensive cap, currently structurally unreachable: Tier 1 never
        # admits more than 3 topics (tiers._tier_bucket_for_rank only ever
        # offers ranks 1-3 as Tier 1 candidates), so `tier1_topics` itself
        # never has more than 3 elements today. Kept explicit rather than
        # removed in case Tier 1's size ever changes upstream -- see
        # test_content_opportunities_exact_count_matches_documented_selection_rule_on_real_fixture
        # for the real-fixture test that exercises the actual documented
        # behaviour instead of a tautological one.
        if len(opportunities) == 3:
            break
    none_reason = None
    if not opportunities:
        none_reason = (
            "no Tier 1 topic is a high/medium-confidence fact with on-territory relevance "
            "(effective_value >= 4) this week"
        )
    return ContentOpportunitySelection(
        selection_rule=CONTENT_OPPORTUNITY_SELECTION_RULE,
        opportunities=opportunities,
        none_reason=none_reason,
    )


def _build_discarded(
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
) -> list[DiscardedTopic]:
    discarded = []
    for inputs, breakdown in ranked[TOP_N:]:
        cluster = clusters_by_topic_id[inputs.topic_id]
        discarded.append(
            DiscardedTopic(
                topic_id=inputs.topic_id,
                canonical_title=cluster.canonical_title,
                score=breakdown.score,
                reason=_primary_exclusion_category(breakdown.eligibility_reasons),
            )
        )
    return discarded


def _tier1_short_reason(top_n_topics: list[TieredTopic], tier1_count: int) -> str | None:
    if tier1_count >= 3:
        return None
    failed = [
        t
        for t in top_n_topics
        if t.rank <= 3 and t.tier_assignment.tier != "tier_1"
    ]
    if not failed:
        return (
            f"Tier 1 shipped with only {tier1_count} of 3 possible slots because fewer than "
            "3 topics were ranked overall this week -- never backfilled from Tier 2."
        )
    reasons = []
    for t in failed:
        failing_condition = _primary_exclusion_category(t.tier_assignment.exclusion_reasons)
        reasons.append(
            f"rank {t.rank} ('{t.canonical_title}'): no-backfill rule applied -- fell through "
            f"to Tier 2 because it failed admission on {failing_condition}"
        )
    joined = "; ".join(reasons)
    return (
        f"Tier 1 shipped with only {tier1_count} of 3 possible slots -- never backfilled "
        f"from Tier 2 (no-artificial-backfill rule, ADR 0004/tiers.py): {joined}"
    )


def _build_executive_summary(
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    top_n_topics: list[TieredTopic],
    tier1_topics: list[TieredTopic],
    tier2_topics: list[TieredTopic],
    tier3_topics: list[TieredTopic],
    discarded: list[DiscardedTopic],
    study_queue: StudyQueue,
    experiment: ExperimentSelection,
    content_opportunities: ContentOpportunitySelection,
    tier1_short_reason: str | None,
) -> list[str]:
    sentences: list[str] = []

    sentences.append(
        f"This week's pipeline evaluated {len(ranked)} ranked topic(s) and reviewed the "
        f"top {len(top_n_topics)} in detail."
    )

    if tier1_short_reason is not None:
        sentences.append(
            f"Only {len(tier1_topics)} of 3 possible slots were admitted to Tier 1 (Must "
            "Understand) this week -- never backfilled from Tier 2 (see the Tier 1 section "
            "and appendix for the per-topic no-backfill reasons)."
        )
    else:
        sentences.append(
            f"{len(tier1_topics)} topic(s) were admitted to Tier 1 (Must Understand)."
        )

    sentences.append(
        f"{len(tier2_topics)} topic(s) land in Tier 2 (Should Know) and "
        f"{len(tier3_topics)} in Tier 3 (Radar)."
    )

    if top_n_topics:
        leader = top_n_topics[0]
        sentences.append(
            f"The top-ranked topic, '{leader.canonical_title}', scored {leader.score}/100 "
            f"and was classified as {leader.claim.claim_class} at {leader.claim.confidence} "
            "confidence."
        )

    marketing_count = sum(1 for t in top_n_topics if t.claim.claim_class == "marketing")
    sentences.append(
        f"{marketing_count} of the Top {len(top_n_topics)} topics were classified as "
        "marketing-risk claims that still require independent corroboration before acting."
    )

    if discarded:
        categories = [d.reason for d in discarded]
        most_common = max(sorted(set(categories)), key=categories.count)
        sentences.append(
            f"{len(discarded)} topic(s) ranked below the Top {TOP_N} and were discarded; "
            f"the most common reason was: {most_common}."
        )
    else:
        sentences.append(f"No topics ranked below the Top {TOP_N} this week.")

    study_bits = []
    if study_queue.deep is not None:
        study_bits.append("1 deep-study topic")
    else:
        study_bits.append("no deep-study topic")
    study_bits.append(f"{len(study_queue.light)} light-study topic(s)")
    experiment_bits = (
        "1 practical experiment" if experiment.experiment is not None else "no experiment"
    )
    opportunity_count = len(content_opportunities.opportunities)
    opportunity_noun = "opportunity" if opportunity_count == 1 else "opportunities"
    sentences.append(
        f"The study queue carries {' and '.join(study_bits)}; {experiment_bits} was identified; "
        f"{opportunity_count} content {opportunity_noun} passed the editorial gate."
    )

    sentences.append(
        "This brief is a draft for Founder review only -- nothing in it is auto-published."
    )

    return sentences


def _estimate_reading_minutes(brief_parts: dict[str, int]) -> int:
    total = _READING_MINUTES_EXECUTIVE_SUMMARY
    total += _READING_MINUTES_TIER1_ITEM * brief_parts["tier1"]
    total += _READING_MINUTES_TIER2_ITEM * brief_parts["tier2"]
    total += _READING_MINUTES_TIER3_ITEM * brief_parts["tier3"]
    if brief_parts["deep"]:
        total += _READING_MINUTES_DEEP_STUDY
    total += _READING_MINUTES_LIGHT_STUDY_ITEM * brief_parts["light"]
    if brief_parts["experiment"]:
        total += _READING_MINUTES_EXPERIMENT
    total += _READING_MINUTES_CONTENT_OPPORTUNITY_ITEM * brief_parts["content_opportunities"]
    if brief_parts["discarded"]:
        total += _READING_MINUTES_DISCARDED_SECTION
    total += _READING_MINUTES_RANKING_EXPLANATION_POINTER
    total += _READING_MINUTES_APPENDIX_TIER1_ITEM * brief_parts["tier1"]
    return total


def _estimate_study_minutes(*, deep_present: bool, light_count: int) -> int:
    total = _STUDY_MINUTES_DEEP_STUDY_TOPIC if deep_present else 0
    total += _STUDY_MINUTES_LIGHT_STUDY_TOPIC * light_count
    return total


def _build_tldr(tier1_topics: list[TieredTopic]) -> list[str]:
    """The 3-line (at most, since Tier 1 never exceeds 3 items) "If you read
    nothing else" summary -- distinct from the executive summary, naming only
    the top Tier 1 action(s) in rank order."""
    if not tier1_topics:
        return [
            "No topics were admitted to Tier 1 this week -- see Tier 2 (Should Know) for the "
            "highest-priority items instead."
        ]
    return [
        f"{t.rank}. {t.canonical_title} -- {t.tier_assignment.recommended_action}: "
        f"{t.tier_assignment.recommended_action_reason}"
        for t in tier1_topics
    ]


def _build_d1_threshold_watch(
    tiered: list[TieredTopic],
    inputs_by_topic_id: dict[str, RankingInputs],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
) -> list[D1ThresholdWatchItem]:
    """Every Top-10 topic flagged by the ``d1_would_admit_at_evidence_3``
    diagnostic that is NOT already Tier-1 admitted -- decision-support only
    for the Founder's D1 threshold ruling (ADR 0004). Reads
    ``tier_assignment.d1_would_admit_at_evidence_3`` (computed in
    ``tiers.py``) and never itself decides or changes admission."""
    watch: list[D1ThresholdWatchItem] = []
    for t in tiered:
        if not t.tier_assignment.d1_would_admit_at_evidence_3:
            continue
        if t.tier_assignment.tier1_admitted:
            continue
        anchor = _anchor_for_topic(t.topic_id, clusters_by_topic_id, items_by_id)
        inputs = inputs_by_topic_id[t.topic_id]
        watch.append(
            D1ThresholdWatchItem(
                topic_id=t.topic_id,
                rank=t.rank,
                canonical_title=t.canonical_title,
                evidence_type=anchor.evidence_type,
                evidence_level=inputs.evidence_level,
                note=D1_THRESHOLD_WATCH_NOTE,
            )
        )
    return watch


# ------------------------------ public API -----------------------------------


def build_weekly_brief(
    tiered: list[TieredTopic],
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
    week_label: str,
    library_movements: LibraryMovementsSection | None = None,
) -> WeeklyBrief:
    """Build the full structured Intelligence Brief.

    ``tiered`` must be exactly ``tiers.assign_tiers``'s output for ``ranked``
    (the Top ``TOP_N`` topics, tier-annotated); ``ranked`` must be the FULL
    ``ranking.rank_topics`` output (used here only to report topics ranked
    below the Top N -- ``discarded``). ``clusters_by_topic_id`` and
    ``items_by_id`` mirror ``tiers.assign_tiers``'s own parameters, needed
    here to reach the canonical title of a discarded (non-Top-N) topic and
    the anchor ``SourceItem`` for the experiment suggestion. ``week_label`` is
    the caller-supplied ISO week label (e.g. ``"2026-W30"``) -- this function
    never reads the wall clock.

    ``library_movements`` is OPTIONAL: this module never imports or depends
    on ``content_machine.intelligence.library`` (M6) -- a caller that has run
    the M6 library update may build a :class:`LibraryMovementsSection` from
    its result and pass it in; when omitted (the default, and the only path
    exercised by an M5-only caller), this brief states explicitly that
    library movements are deferred to M6 rather than silently omitting the
    section.

    Pure and deterministic: same arguments, same :class:`WeeklyBrief` (field
    for field, including nested lists in the same order).
    """
    breakdown_by_topic_id = _breakdown_lookup(ranked)
    inputs_by_topic_id = {inputs.topic_id: inputs for inputs, _breakdown in ranked}

    tier1_topics = [t for t in tiered if t.tier_assignment.tier == "tier_1"]
    tier2_topics = [t for t in tiered if t.tier_assignment.tier == "tier_2"]
    tier3_topics = [t for t in tiered if t.tier_assignment.tier == "tier_3"]

    tier1 = [
        _build_tier1_lean(
            t,
            breakdown_by_topic_id[t.topic_id],
            inputs_by_topic_id[t.topic_id],
            _anchor_for_topic(t.topic_id, clusters_by_topic_id, items_by_id),
        )
        for t in tier1_topics
    ]
    tier2 = [
        _build_tier2(
            t,
            breakdown_by_topic_id[t.topic_id],
            inputs_by_topic_id[t.topic_id],
            _anchor_for_topic(t.topic_id, clusters_by_topic_id, items_by_id),
        )
        for t in tier2_topics
    ]
    tier3 = [_build_radar(t) for t in tier3_topics]

    appendix = [
        _build_appendix_record(t, breakdown_by_topic_id[t.topic_id]) for t in tier1_topics
    ]

    discarded = _build_discarded(ranked, clusters_by_topic_id)
    study_queue = _build_study_queue(tier1_topics, tier2_topics)
    experiment = _build_experiment(tiered, ranked, clusters_by_topic_id, items_by_id)
    content_opportunities = _build_content_opportunities(tier1_topics, breakdown_by_topic_id)
    tier1_short_reason = _tier1_short_reason(tiered, len(tier1_topics))
    d1_threshold_watch = _build_d1_threshold_watch(
        tiered, inputs_by_topic_id, clusters_by_topic_id, items_by_id
    )
    if library_movements is None:
        library_movements = LibraryMovementsSection(
            movements=[], deferred_note=LIBRARY_MOVEMENTS_DEFERRED_NOTE
        )

    executive_summary = _build_executive_summary(
        ranked=ranked,
        top_n_topics=tiered,
        tier1_topics=tier1_topics,
        tier2_topics=tier2_topics,
        tier3_topics=tier3_topics,
        discarded=discarded,
        study_queue=study_queue,
        experiment=experiment,
        content_opportunities=content_opportunities,
        tier1_short_reason=tier1_short_reason,
    )

    tldr = _build_tldr(tier1_topics)

    estimated_reading_minutes = _estimate_reading_minutes(
        {
            "tier1": len(tier1),
            "tier2": len(tier2),
            "tier3": len(tier3),
            "deep": 1 if study_queue.deep is not None else 0,
            "light": len(study_queue.light),
            "experiment": 1 if experiment.experiment is not None else 0,
            "content_opportunities": len(content_opportunities.opportunities),
            "discarded": len(discarded),
        }
    )
    estimated_study_minutes = _estimate_study_minutes(
        deep_present=study_queue.deep is not None,
        light_count=len(study_queue.light),
    )

    first_breakdown = ranked[0][1] if ranked else None
    ranking_explanation_pointer = (
        "Each Tier 1 topic's full per-dimension breakdown (raw/effective value, weight, "
        "points, rationale for every one of the six ranking dimensions) is in the Appendix, "
        "field 'dimension_breakdown'. Each lean Tier 1/2 entry above also carries its own "
        "one-line 'ranking_explanation' / dimension rationale."
    )

    return WeeklyBrief(
        week_label=week_label,
        brief_version=BRIEF_VERSION,
        rubric_version=first_breakdown.rubric_version if first_breakdown else "",
        weights_version=first_breakdown.weights_version if first_breakdown else "",
        taxonomy_version=first_breakdown.taxonomy_version if first_breakdown else "",
        profile_version=first_breakdown.profile_version if first_breakdown else "",
        tldr=tldr,
        executive_summary=executive_summary,
        estimated_reading_minutes=estimated_reading_minutes,
        estimated_reading_time_method=ESTIMATED_READING_TIME_METHOD,
        estimated_study_minutes=estimated_study_minutes,
        estimated_study_time_method=ESTIMATED_STUDY_TIME_METHOD,
        tier1=tier1,
        tier1_short_reason=tier1_short_reason,
        tier2=tier2,
        tier3=tier3,
        study_queue=study_queue,
        experiment=experiment,
        content_opportunities=content_opportunities,
        discarded=discarded,
        d1_threshold_watch=d1_threshold_watch,
        library_movements=library_movements,
        ranking_explanation_pointer=ranking_explanation_pointer,
        appendix=appendix,
        review_status=REVIEW_STATUS,
    )


def _render_tier1_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Tier 1 -- Must Understand"]
    if brief.tier1_short_reason:
        lines.append(f"_{brief.tier1_short_reason}_")
    if not brief.tier1:
        lines.append("No topics were admitted to Tier 1 this week.")
    for item in brief.tier1:
        lines.append(f"### {item.rank}. {item.canonical_title}")
        lines.append(f"- **What changed:** {item.what_changed}")
        lines.append(f"- **Why it matters:** {item.why_it_matters}")
        lines.append(f"- **Evidence & confidence:** {item.evidence_and_confidence}")
        lines.append(
            f"- **Recommended action:** {item.recommended_action} -- "
            f"{item.recommended_action_reason}"
        )
        lines.append(f"- **Score:** {item.score}/100 -- {item.ranking_explanation}")
    return lines


def _render_tier2_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Should Know"]
    if not brief.tier2:
        lines.append("No topics in this tier this week.")
    for item in brief.tier2:
        lines.append(f"### {item.rank}. {item.canonical_title}")
        lines.append(f"- **Explanation:** {item.explanation}")
        lines.append(f"- **Practical consequence:** {item.practical_consequence}")
        lines.append(f"- **Principal evidence:** {item.principal_evidence}")
        lines.append(f"- **Confidence:** {item.confidence}")
        lines.append(f"- **Recommended action:** {item.recommended_action}")
    return lines


def _render_tier3_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Radar"]
    if not brief.tier3:
        lines.append("No topics in this tier this week.")
    for item in brief.tier3:
        lines.append(f"- **{item.rank}. {item.canonical_title}** -- {item.signal_paragraph}")
    return lines


def _render_study_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Study Queue", f"_Selection rule: {brief.study_queue.selection_rule}_"]
    if brief.study_queue.deep is not None:
        d = brief.study_queue.deep
        lines.append(f"- **Deep study:** {d.canonical_title} (rank {d.rank}) -- {d.reason}")
    else:
        lines.append(f"- **Deep study:** none -- {brief.study_queue.deep_none_reason}")
    if brief.study_queue.light:
        for item in brief.study_queue.light:
            lines.append(
                f"- **Light study:** {item.canonical_title} (rank {item.rank}) -- {item.reason}"
            )
    else:
        lines.append(f"- **Light study:** none -- {brief.study_queue.light_none_reason}")
    return lines


def _render_experiment_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Practical Experiment", f"_Selection rule: {brief.experiment.selection_rule}_"]
    if brief.experiment.experiment is not None:
        e = brief.experiment.experiment
        lines.append(f"- **{e.canonical_title}** (rank {e.rank}): {e.what_it_would_test}")
    else:
        lines.append(f"- None -- {brief.experiment.none_reason}")
    return lines


def _render_content_opportunities_section(brief: WeeklyBrief) -> list[str]:
    lines = [
        "## Content Opportunities",
        f"_Selection rule: {brief.content_opportunities.selection_rule}_",
    ]
    if brief.content_opportunities.opportunities:
        for item in brief.content_opportunities.opportunities:
            lines.append(f"- **{item.canonical_title}** (rank {item.rank}): {item.reason}")
    else:
        lines.append(f"- None -- {brief.content_opportunities.none_reason}")
    return lines


def _render_discarded_section(brief: WeeklyBrief) -> list[str]:
    lines = [f"## Discarded Topics ({len(brief.discarded)})"]
    if not brief.discarded:
        lines.append("No topics were discarded this week.")
    for item in brief.discarded:
        lines.append(f"- {item.canonical_title} (score {item.score}/100): {item.reason}")
    return lines


def _render_string_list(values: list[str]) -> str:
    """Render a list of strings as human-readable ``; ``-joined text, never a
    Python list repr (``['a', 'b']``) -- see Product F12."""
    return "; ".join(values) if values else "(none)"


def _render_d1_threshold_watch_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## D1 Threshold Watch (Decision Support)"]
    if not brief.d1_threshold_watch:
        lines.append(
            "No Top-10 topic is currently flagged by the d1_would_admit_at_evidence_3 "
            "diagnostic."
        )
    for item in brief.d1_threshold_watch:
        lines.append(
            f"- **{item.canonical_title}** (rank {item.rank}, evidence_type="
            f"{item.evidence_type}, evidence_level={item.evidence_level}): {item.note}"
        )
    return lines


def _render_library_movements_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Library Movements"]
    movements = brief.library_movements
    if movements.deferred_note is not None and not movements.movements:
        lines.append(f"_{movements.deferred_note}_")
    elif not movements.movements:
        lines.append("No library movements this week.")
    else:
        for movement in movements.movements:
            lines.append(
                f"- **{movement.canonical_title}** -- {movement.movement}: {movement.reason}"
            )
    return lines


def _render_appendix_section(brief: WeeklyBrief) -> list[str]:
    lines = ["## Appendix -- Full Tier 1 Records"]
    if not brief.appendix:
        lines.append("No Tier 1 topics were admitted this week.")
    for record in brief.appendix:
        lines.append(f"### {record.rank}. {record.canonical_title} ({record.topic_id})")
        lines.append(f"- topic_id: {record.topic_id}")
        lines.append(f"- rank: {record.rank}")
        lines.append(f"- canonical_title: {record.canonical_title}")
        lines.append(f"- score: {record.score}")
        lines.append(f"- claim_class: {record.claim_class}")
        lines.append(f"- claim_class_reason: {record.claim_class_reason}")
        lines.append(f"- confidence: {record.confidence}")
        lines.append(f"- confidence_reason: {record.confidence_reason}")
        lines.append(f"- admission_reasons: {_render_string_list(record.admission_reasons)}")
        lines.append(f"- exclusion_reasons: {_render_string_list(record.exclusion_reasons)}")
        lines.append(f"- warnings: {_render_string_list(record.warnings)}")
        lines.append("- dimension_breakdown:")
        for dim_line in record.dimension_breakdown:
            lines.append(f"  - {dim_line}")
    lines.append("")
    lines.append("### Reading & Study Time Method")
    lines.append(f"- Reading time method: {brief.estimated_reading_time_method}")
    lines.append(f"- Study time method: {brief.estimated_study_time_method}")
    return lines


def render_markdown(brief: WeeklyBrief) -> str:
    """Render the Markdown Intelligence Brief FROM the structured
    :class:`WeeklyBrief` object -- never re-derived from raw pipeline output,
    so the Markdown and the JSON (``brief.model_dump_json()``) can never
    diverge. Pure and deterministic: same ``brief``, same string, always."""
    lines: list[str] = []
    lines.append(f"# Intelligence Brief -- Week {brief.week_label}")
    lines.append("")
    lines.append("## If You Read Nothing Else")
    for line in brief.tldr:
        lines.append(f"- {line}")
    lines.append("")
    lines.append("**Status:** DRAFT -- awaiting Founder review. Nothing here is published.")
    lines.append(
        f"**Estimated reading time:** {brief.estimated_reading_minutes} minutes "
        "(see appendix for method)."
    )
    lines.append(
        f"**Estimated study time:** {brief.estimated_study_minutes} minutes "
        "(see appendix for method)."
    )
    lines.append("")
    lines.append("## Executive Summary")
    for sentence in brief.executive_summary:
        lines.append(sentence)
    lines.append("")
    lines.extend(_render_tier1_section(brief))
    lines.append("")
    lines.extend(_render_tier2_section(brief))
    lines.append("")
    lines.extend(_render_tier3_section(brief))
    lines.append("")
    lines.extend(_render_study_section(brief))
    lines.append("")
    lines.extend(_render_experiment_section(brief))
    lines.append("")
    lines.extend(_render_content_opportunities_section(brief))
    lines.append("")
    lines.extend(_render_discarded_section(brief))
    lines.append("")
    lines.extend(_render_d1_threshold_watch_section(brief))
    lines.append("")
    lines.extend(_render_library_movements_section(brief))
    lines.append("")
    lines.append("## Ranking Explanation")
    lines.append(brief.ranking_explanation_pointer)
    lines.append("")
    lines.extend(_render_appendix_section(brief))
    lines.append("")
    lines.append("---")
    lines.append(
        f"**review_status = {brief.review_status}.** This brief is a draft only: nothing in "
        "it has been published, sent, or scheduled. Founder review is required before any "
        "content is produced from it."
    )
    return "\n".join(lines) + "\n"


def render_json(brief: WeeklyBrief) -> str:
    """Serialize the same structured object rendered by
    :func:`render_markdown` to JSON. Deterministic: Pydantic dumps model
    fields in declaration order, so the same ``brief`` always yields the same
    JSON string."""
    return brief.model_dump_json(indent=2) + "\n"
