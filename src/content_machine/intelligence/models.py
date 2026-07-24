"""Data contracts for the Intelligence Brief module (Gate A).

Three separate object families keep the ranking from being circular
(non-negotiable, see the Gate A ticket):

* :class:`SourceItem` -- observable facts about one artifact (email, feed
  entry, or doc), fillable by someone who has never met the Founder, does not
  know the ranking weights, and cannot see the desired ordering. Deliberately
  has NO relevance/marketing_risk/body field.
* :class:`TopicCluster` -- facts DERIVED from the corpus by
  :mod:`content_machine.intelligence.cluster`. Never authored in a fixture.
* :class:`RelevanceProfile` -- the Founder's priors, authored once per run.

Relevance and curiosity are computed as a JOIN between an item's
``topic_tags`` and the profile's tags in :mod:`content_machine.intelligence.ranking`
-- never asserted directly on an item.

This package imports only the standard library and Pydantic (see the module
docstrings in ``loader.py``/``cluster.py``/``ranking.py`` for the intra-package
dependency direction). Nothing outside this package imports it in Gate A: no
network code, no provider imports, no LLM, no CLI command.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Closed, generic tag vocabulary. Unknown tags are a validation error at load
# time (content_machine.intelligence.loader), never silently accepted.
TOPIC_TAXONOMY: frozenset[str] = frozenset(
    {
        "agents",
        "harnesses",
        "skills",
        "agent-cli",
        "mcp",
        "browser-agents",
        "computer-use",
        "evals",
        "multi-agent",
        "hooks-guardrails",
        "memory-context",
        "security-boundaries",
        "local-first",
        "careers-skills",
    }
)

SourceType = Literal["email", "feed", "doc"]

EvidenceType = Literal[
    "roundup",
    "relay",
    "rumor",
    "announcement",
    "release_note",
    "official_doc",
    "spec_change",
    "deprecation_notice",
    "security_advisory",
    "official_api_behavior_change",
    "independent_analysis",
    "benchmark_with_methodology",
    "independent_implementation",
    "research_paper",
]

ChangeClass = Literal[
    "new_capability_class",
    "breaking_change",
    "material_change",
    "incremental_update",
    "restatement",
    "announcement_of_intent",
]

ActionRequired = Literal[
    "migration_required",
    "config_or_code_change",
    "new_option_available",
    "changes_how_to_think",
    "none",
]

ExperimentAffordance = Literal["local_reproducible", "requires_paid_service", "not_testable"]


class SourceItem(BaseModel):
    """One observable fact-sheet about a single artifact (email/feed/doc item).

    Fillable by someone who has never met the Founder and cannot see the
    RELEVANCE or EVIDENCE rubric: every field describes the artifact itself,
    never how relevant or important it is, and there is deliberately no
    ``relevance``, ``marketing_risk``, or ``body`` field here. ``change_class``,
    ``action_required``, and ``experiment_affordance`` are, however, AUTHORED
    enums -- a human judgment call about the artifact, not an observable fact
    in the same sense as ``publisher_id`` or ``evidence_type``. That is a
    known, documented Gate A limitation: the only check on an authored enum
    is the free-text ``change_class_rationale`` field (surfaced in the
    magnitude dimension's ``inputs`` -- see ``ranking.py``), which a human
    reviewer can audit but the system does not itself verify.

    ``contains_benefit_or_performance_claim`` (Founder decision D4) is a
    similarly AUTHORED-but-checkable observable fact, not an inference: a
    deterministic pipeline cannot itself classify prose as a benefit/
    performance claim, so a human fills this in the same way they fill
    ``change_class`` -- but unlike ``change_class`` it is a narrow yes/no
    question anyone reading the artifact can verify ("does this text claim a
    benefit or a performance characteristic?"), not a judgment call about
    significance. It is REQUIRED, with no default: an unfilled field must
    never be silently interpreted as "clean" (Opus F2/F3) -- every authored
    item must state whether it carries such a claim, even when the answer is
    False. It feeds ``marketing_risk`` (see
    ``cluster._evidence_level_and_marketing_risk``) whenever this item is the
    cluster's ``first_party_commentary`` member (the subject analysing
    itself), and is otherwise inert.

    Authoring guidance for downstream release notes: a non-subject
    ``release_note`` can genuinely be a PRIMARY ARTIFACT of the publishing
    project, not mere secondary news about someone else -- e.g. a downstream
    project's own release note stating "upgraded to VendorA 3.0, requires
    migration" is that downstream project's first-party artifact, even
    though it is also coverage of VendorA. To be scored as such (rather than
    D2's ``secondary_news_uncorroborated``), the downstream project MUST be
    listed in that item's own ``subject_entity_ids`` -- publisher_id alone is
    not enough, since evidence polarity is judged per (item, cluster-subject)
    pair, and a cluster's ``subject_entity_ids`` is the union of every
    member's.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    source_type: SourceType
    source_category: str
    publisher_id: str
    subject_entity_ids: list[str] = Field(default_factory=list)
    title: str
    summary_normalized: str
    publication_date: date | None = None
    detection_date: date
    # Opaque; URL-shaped for feeds, "email:<slug>" for email. Do NOT require
    # URL shape -- see content_machine.intelligence.normalize.normalize_canonical_reference.
    stable_reference: str
    evidence_type: EvidenceType
    change_class: ChangeClass
    change_class_rationale: str
    action_required: ActionRequired
    experiment_affordance: ExperimentAffordance
    topic_tags: list[str] = Field(default_factory=list)
    # D4: whether this artifact itself contains a benefit-or-performance
    # claim -- an authored-but-observable fact (see class docstring).
    # REQUIRED, no default (Opus F2/F3): an unfilled field must never
    # silently mean "no claim". Only consulted when this item is the
    # cluster's first_party_commentary (self-authored independent_analysis)
    # member; inert otherwise.
    contains_benefit_or_performance_claim: bool


class TerritoryPriority(BaseModel):
    """One tag the Founder has assigned a priority (0-5) within a profile."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    priority: int = Field(ge=0, le=5)


class LiveQuestion(BaseModel):
    """A question the Founder is currently trying to answer, tagged by topic."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    tags: list[str] = Field(default_factory=list)


class RelevanceProfile(BaseModel):
    """Founder priors, authored once and versioned, ONE per ranking run.

    The real profile is private and must never enter this repo; see
    ``examples/intelligence-profile-synthetic.json`` for the synthetic
    stand-in used by fixtures, tests, and the loader's documented default.
    """

    model_config = ConfigDict(extra="forbid")

    profile_version: str
    territories: list[TerritoryPriority] = Field(default_factory=list)
    live_questions: list[LiveQuestion] = Field(default_factory=list)
    current_tooling: list[str] = Field(default_factory=list)
    # Loaded and validated, but intentionally not consulted by ranking.py in
    # Gate A -- no dimension reads experiment_budget yet.
    experiment_budget: Literal["low", "medium", "high"]


class TopicCluster(BaseModel):
    """Facts DERIVED from the corpus by :mod:`content_machine.intelligence.cluster`.

    Never authored directly in a fixture -- every field here is computed from
    a group of merged :class:`SourceItem` records. ``topic_id`` identifies the
    cluster only within a single run (see the identity docstring on
    :func:`content_machine.intelligence.cluster.cluster_items`); persistent
    cross-run identity is out of scope for Gate A and lands at M6.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    cluster_fingerprint: str
    canonical_title: str
    anchor_item_id: str
    member_ids: list[str] = Field(default_factory=list)
    member_roles: dict[str, str] = Field(default_factory=dict)
    duplication_reasons: list[str] = Field(default_factory=list)
    subject_entity_ids: list[str] = Field(default_factory=list)
    independent_publisher_count: int = 0
    has_independent_evidence: bool = False
    has_first_party_authoritative: bool = False
    # D3: a derived corpus FACT (never a count) -- True when the cluster has
    # a first-party authoritative/artifact member OR genuine independent
    # evidence. This is the only thing that may gate the breaking-change
    # consequence floor in ranking.py; see
    # cluster._evidence_level_and_marketing_risk for how it is computed.
    has_direct_artifact_or_independent_source: bool = False
    evidence_level: int = Field(default=0, ge=0, le=5)
    # Which branch of the evidence rubric produced evidence_level (e.g.
    # "evid_3_independent_only"); lets a reader re-derive the level without
    # re-running the rubric. See cluster._evidence_level_and_marketing_risk.
    evidence_anchor_id: str = ""
    marketing_risk: bool = False
    first_seen: date
    last_seen: date
    cluster_size: int = 0
    topic_tags: list[str] = Field(default_factory=list)
    evidence_types: list[str] = Field(default_factory=list)


class RankingInputs(BaseModel):
    """The ONLY thing ``ranking.py`` receives about a topic.

    Structurally excludes ``cluster_size``, member count, ``source_type``,
    ``source_category``, and publisher lists -- so the ranking arithmetic
    cannot become a popularity count. ``first_seen`` is used ONLY for
    tie-breaking, never for points.
    """

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    topic_tags: list[str] = Field(default_factory=list)
    change_class: str
    change_class_rationale: str = ""
    action_required: str
    evidence_level: int = Field(ge=0, le=5)
    evidence_anchor_id: str = ""
    has_independent_evidence: bool
    has_first_party_authoritative: bool = False
    # D3: see TopicCluster.has_direct_artifact_or_independent_source -- the
    # ONLY input allowed to gate the breaking-change consequence floor in
    # ranking.py. A derived fact, never a count.
    has_direct_artifact_or_independent_source: bool = False
    marketing_risk: bool
    experiment_affordance: str
    evidence_types: list[str] = Field(default_factory=list)
    first_seen: date


class DimensionScore(BaseModel):
    """One scored dimension of a :class:`RankingBreakdown`, fully explained."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    raw_value: int
    effective_value: int
    cap_applied: str | None = None
    floor_applied: str | None = None
    anchor_id: str
    anchor_text: str
    inputs: dict[str, str] = Field(default_factory=dict)
    rationale: str
    weight: int
    points: int


class RankingBreakdown(BaseModel):
    """Full explanation of one topic's score, in fixed dimension order."""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[DimensionScore] = Field(default_factory=list)
    points_total: int
    score: int = Field(ge=0, le=100)
    rubric_version: str
    weights_version: str
    taxonomy_version: str
    profile_version: str
    tier1_eligible: bool
    eligibility_reasons: list[str] = Field(default_factory=list)
    first_party_authoritative_candidate: bool
    tie_break_key: str
    ranking_explanation: str


class RankedTopic(BaseModel):
    """A :class:`TopicCluster` paired with its rank and full score breakdown.

    Not constructed anywhere in Gate A code (``ranking.py`` must never import
    or receive ``TopicCluster`` -- see its module docstring); this model
    exists so the shape is frozen and its JSON Schema can be published ahead
    of the milestone (M4+) that assembles it.
    """

    model_config = ConfigDict(extra="forbid")

    rank: int
    cluster: TopicCluster
    breakdown: RankingBreakdown
