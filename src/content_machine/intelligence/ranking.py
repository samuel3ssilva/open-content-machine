"""Explainable, deterministic ranking over intelligence topics.

Depends only on ``content_machine.intelligence.models``. This module must
NEVER import or receive a ``TopicCluster`` -- it sees only
:class:`~content_machine.intelligence.models.RankingInputs`, which
structurally excludes ``cluster_size``, member counts, ``source_type``,
``source_category``, and publisher lists. This is the core non-circularity
guarantee of Gate A: a topic cannot score higher merely for having many
sources, many emails, or a "popular" publisher.

All arithmetic is integer-only (no floats anywhere in this module).
``points(dim) = weight * effective_value`` (``effective_value`` in 0..5), so
``points_total`` is in 0..500 and ``score = points_total // 5`` is in 0..100,
exactly (``score`` is a floor division, never rounded).

Deviation from the ticket's literal signature, noted explicitly: the ticket
sketches ``rank_topics(inputs) -> list[RankedTopic]``, but ``RankedTopic``
embeds a ``TopicCluster`` -- constructing one here would violate the
"never import or receive TopicCluster" constraint, which is the stricter,
explicitly non-negotiable rule. :func:`rank_topics` therefore returns
``(RankingInputs, RankingBreakdown)`` pairs in final rank order; a caller that
holds the corresponding ``TopicCluster`` objects (matched by ``topic_id``) can
zip them together into ``RankedTopic`` instances if a materialized ranked
view is needed. No such assembly happens inside this package in Gate A.
"""

from __future__ import annotations

from datetime import date

from content_machine.intelligence.models import (
    DimensionScore,
    RankingBreakdown,
    RankingInputs,
    RelevanceProfile,
)


class DimensionOrderError(RuntimeError):
    """Raised if the fixed dimension order invariant is ever violated.

    A plain ``assert`` is stripped under ``python -O``, which would silently
    let a reordered/incomplete dimension list slip through the fixed-order
    guarantee this module promises callers -- so this is an explicit,
    always-enforced exception instead.
    """


RUBRIC_VERSION = "gate-a-1"
WEIGHTS_VERSION = "gate-a-1"
TAXONOMY_VERSION = "gate-a-1"

# Sum to exactly 100 (asserted in tests/test_intelligence_ranking.py).
WEIGHTS: dict[str, int] = {
    "relevance": 30,
    "magnitude": 20,
    "consequence": 15,
    "evidence": 15,
    "experiment": 10,
    "curiosity": 10,
}

_DIMENSION_ORDER = ("relevance", "magnitude", "consequence", "evidence", "experiment", "curiosity")

_MAGNITUDE_RAW: dict[str, int] = {
    "new_capability_class": 5,
    "breaking_change": 4,
    "material_change": 3,
    "incremental_update": 2,
    "restatement": 1,
    "announcement_of_intent": 1,
}

_CONSEQUENCE_RAW: dict[str, int] = {
    "migration_required": 5,
    "config_or_code_change": 4,
    "new_option_available": 4,
    "changes_how_to_think": 3,
    "none": 0,
}

# experiment anchor 5's evidence-type trigger set.
_EXPERIMENT_EVIDENCE_TRIGGER = frozenset(
    {"benchmark_with_methodology", "independent_implementation", "spec_change"}
)

# Human-readable text for each evidence_anchor_id produced by
# cluster._evidence_level_and_marketing_risk -- so a reader can re-derive why
# a topic landed at its evidence_level without re-running the rubric. This
# rubric is TOTAL (Gate A correction round 2, R1): every evidence_type, in
# either publisher polarity, produces a non-zero level for a single-member
# cluster except roundup/relay (never evidentiary) -- see
# test_evidence_rubric_totality_matrix in test_intelligence_cluster.py.
_EVIDENCE_ANCHOR_TEXT: dict[str, str] = {
    "evid_5_authoritative_plus_analysis_plus_independent_rigor": (
        "an authoritative source (first-party or non-subject) AND a non-subject "
        "independent_analysis AND a non-subject benchmark_with_methodology/"
        "independent_implementation/research_paper are all present"
    ),
    "evid_4_first_party_plus_independent": (
        "a first-party evidentiary source is present AND independent analysis or "
        "independent rigorous evidence is also present"
    ),
    "evid_4_independent_rigorous_alone": (
        "a non-subject benchmark_with_methodology, research_paper, or "
        "independent_implementation is present on its own"
    ),
    "evid_3_first_party_authoritative": (
        "an uncorroborated first-party-authoritative source "
        "(official_doc/spec_change/deprecation_notice/security_advisory)"
    ),
    "evid_3_non_subject_authoritative": (
        "a third-party authoritative source about the subject (e.g. a standards "
        "body's spec_change, or a security_advisory not published by the subject)"
    ),
    "evid_3_first_party_artifact": (
        "a self-published runnable/rigorous artifact (independent_implementation, "
        "benchmark_with_methodology, or research_paper published BY the subject) "
        "-- real evidence, but not independently corroborated"
    ),
    "evid_3_independent_only": (
        "independent evidence present with no first-party member in the cluster"
    ),
    "evid_2_first_party_promotional": (
        "first-party promotional source only (announcement/release_note), uncorroborated"
    ),
    "evid_2_first_party_commentary": (
        "the subject's own independent_analysis of itself (Founder decision D4) -- not "
        "independent, capped at level 2; marketing_risk is True only when the item's "
        "contains_benefit_or_performance_claim flag is set"
    ),
    "evid_1_secondary_news_uncorroborated": (
        "an isolated announcement/release_note about a non-subject, carried by a "
        "non-subject publisher, with no first-party or independent corroboration "
        "anywhere in the cluster (Founder decision D2)"
    ),
    "evid_1_rumor": "rumor only",
    "evid_0_no_qualifying_evidence": (
        "every evidence-counting member is roundup/relay (never evidentiary), or no "
        "evidence-counting members remain after excluding syndicated/duplicate roles"
    ),
}


def _score_relevance(inputs: RankingInputs, profile: RelevanceProfile) -> DimensionScore:
    """Score relevance as a JOIN between ``topic_tags`` and the profile.

    Intentional edge case (documented, not a bug): a tag EXPLICITLY listed in
    ``profile.territories`` at priority 0 sets ``any_territory_match = True``,
    so it falls through to the ``raw = 0`` "no match" branch even when it
    also appears in ``current_tooling`` -- an explicit zero-priority
    territory call is treated as "seen and deliberately deprioritized". A tag
    that is ABSENT from ``territories`` altogether but present in
    ``current_tooling`` instead reaches the ``raw = 1`` branch (no territory
    match, but tooling overlap). So an explicit priority-0 territory scores
    *lower* than a tag the Founder never mentioned at all -- the profile
    author's silence is treated more generously than an explicit "not now".
    """
    tags = set(inputs.topic_tags)
    matched_priorities = [t.priority for t in profile.territories if t.tag in tags]
    any_territory_match = bool(matched_priorities)
    max_priority = max(matched_priorities, default=0)
    tooling_overlap = bool(tags & set(profile.current_tooling))

    if max_priority == 5 and tooling_overlap:
        raw, anchor_id, anchor_text = (
            5,
            "rel_5",
            "max matched territory priority == 5 AND topic_tags overlap current_tooling",
        )
    elif max_priority >= 4:
        raw, anchor_id, anchor_text = (4, "rel_4", "max matched territory priority >= 4")
    elif max_priority == 3:
        raw, anchor_id, anchor_text = (3, "rel_3", "max matched territory priority == 3")
    elif max_priority in (1, 2):
        raw, anchor_id, anchor_text = (2, "rel_2", "max matched territory priority in {1, 2}")
    elif not any_territory_match and tooling_overlap:
        raw, anchor_id, anchor_text = (
            1,
            "rel_1",
            "no territory match, but topic_tags overlap current_tooling",
        )
    else:
        raw, anchor_id, anchor_text = (0, "rel_0", "no territory match and no tooling overlap")

    return DimensionScore(
        dimension="relevance",
        raw_value=raw,
        effective_value=raw,
        cap_applied=None,
        floor_applied=None,
        anchor_id=anchor_id,
        anchor_text=anchor_text,
        inputs={
            "max_territory_priority": str(max_priority),
            "any_territory_match": str(any_territory_match),
            "tooling_overlap": str(tooling_overlap),
        },
        rationale=(
            f"Max matched territory priority={max_priority} "
            f"(any_match={any_territory_match}), tooling_overlap={tooling_overlap}."
        ),
        weight=WEIGHTS["relevance"],
        points=WEIGHTS["relevance"] * raw,
    )


def _score_magnitude(inputs: RankingInputs) -> DimensionScore:
    raw = _MAGNITUDE_RAW[inputs.change_class]
    cap = inputs.evidence_level + 1
    effective = min(raw, cap)
    cap_applied = None
    if effective < raw:
        cap_applied = (
            f"mag_effective = min(raw {raw}, evidence {inputs.evidence_level} + 1) = {effective}"
        )

    return DimensionScore(
        dimension="magnitude",
        raw_value=raw,
        effective_value=effective,
        cap_applied=cap_applied,
        floor_applied=None,
        anchor_id=f"mag_{inputs.change_class}",
        anchor_text=(
            f"change_class={inputs.change_class} -> raw {raw}, capped at evidence_level + 1"
        ),
        inputs={
            "change_class": inputs.change_class,
            "evidence_level": str(inputs.evidence_level),
            "change_class_rationale": inputs.change_class_rationale,
        },
        rationale=(
            f"change_class '{inputs.change_class}' has raw magnitude {raw}; capped to "
            f"evidence_level ({inputs.evidence_level}) + 1 = {cap}."
        ),
        weight=WEIGHTS["magnitude"],
        points=WEIGHTS["magnitude"] * effective,
    )


def _score_consequence(inputs: RankingInputs) -> DimensionScore:
    """Founder decision D3 (supersedes the Gate A correction round 2, R4
    "KNOWN LIMITATION"/decision H note that used to live here): the
    breaking-change floor no longer fires unconditionally on ``change_class``
    alone. It fires ONLY when ALL of:

    1. ``change_class == "breaking_change"``;
    2. ``evidence_level >= 3``;
    3. ``has_direct_artifact_or_independent_source`` is True -- a first-party
       authoritative/artifact member or genuine independent evidence exists
       in the cluster (see ``cluster._evidence_level_and_marketing_risk``).

    Relay, roundup, or repetition alone can never satisfy (2) or (3): an
    all-relay cluster has ``evidence_level`` 0-1 and
    ``has_direct_artifact_or_independent_source`` False by construction, so
    authoring it as ``breaking_change`` no longer lifts consequence to 5.
    When the gate does not fire, consequence falls back to the normal
    ``action_required`` mapping and ``floor_applied`` stays ``None`` -- there
    is no partial/soft floor.
    """
    raw = _CONSEQUENCE_RAW[inputs.action_required]
    floor_gate = (
        inputs.change_class == "breaking_change"
        and inputs.evidence_level >= 3
        and inputs.has_direct_artifact_or_independent_source
    )
    floor_applied = None
    if floor_gate:
        effective = 5
        if raw != 5:
            floor_applied = (
                "consequence floored to 5 because change_class == breaking_change AND "
                f"evidence_level {inputs.evidence_level} >= 3 AND "
                "has_direct_artifact_or_independent_source is True "
                f"(raw was {raw})"
            )
    else:
        effective = raw

    if inputs.change_class == "breaking_change" and not floor_gate:
        floor_gate_note = (
            "breaking_change floor NOT applied: evidence_level "
            f"{inputs.evidence_level} >= 3 is "
            f"{'true' if inputs.evidence_level >= 3 else 'false'}, "
            "has_direct_artifact_or_independent_source is "
            f"{inputs.has_direct_artifact_or_independent_source} -- at least one gate "
            "condition failed"
        )
    elif floor_gate:
        floor_gate_note = (
            "breaking_change floor applied: evidence_level >= 3 and "
            "has_direct_artifact_or_independent_source are both true"
        )
    else:
        floor_gate_note = "breaking_change floor not applicable: change_class != breaking_change"

    return DimensionScore(
        dimension="consequence",
        raw_value=raw,
        effective_value=effective,
        cap_applied=None,
        floor_applied=floor_applied,
        anchor_id=f"con_{inputs.action_required}",
        anchor_text=f"action_required={inputs.action_required} -> raw {raw}",
        inputs={
            "action_required": inputs.action_required,
            "change_class": inputs.change_class,
            "evidence_level": str(inputs.evidence_level),
            "has_direct_artifact_or_independent_source": str(
                inputs.has_direct_artifact_or_independent_source
            ),
            "breaking_change_floor_gate": floor_gate_note,
        },
        rationale=(
            f"action_required '{inputs.action_required}' has raw consequence {raw}"
            + (
                "; floored to 5 (breaking_change, evidence >= 3, direct artifact/independent "
                "source present)."
                if floor_applied
                else "."
            )
        ),
        weight=WEIGHTS["consequence"],
        points=WEIGHTS["consequence"] * effective,
    )


def _score_evidence(inputs: RankingInputs) -> DimensionScore:
    # evidence_level is a fact already derived by cluster.py from the evidence
    # rubric (evidence_type + independence, never publisher class); this
    # dimension simply weights that pre-computed fact. evidence_anchor_id
    # names exactly which rubric branch produced it (see cluster.py), so a
    # reader can re-derive the level without re-running the rubric.
    value = inputs.evidence_level
    anchor_id = inputs.evidence_anchor_id
    anchor_text = _EVIDENCE_ANCHOR_TEXT.get(
        anchor_id, "evidence_level (0-5) derived from evidence_type + independence by cluster.py"
    )
    return DimensionScore(
        dimension="evidence",
        raw_value=value,
        effective_value=value,
        cap_applied=None,
        floor_applied=None,
        anchor_id=anchor_id or f"evid_{value}",
        anchor_text=anchor_text,
        inputs={
            "evidence_level": str(value),
            "has_independent_evidence": str(inputs.has_independent_evidence),
            "marketing_risk": str(inputs.marketing_risk),
            "evidence_anchor_id": anchor_id,
        },
        rationale=(
            f"evidence_level={value} ({anchor_text}); "
            f"has_independent_evidence={inputs.has_independent_evidence}, "
            f"marketing_risk={inputs.marketing_risk}."
        ),
        weight=WEIGHTS["evidence"],
        points=WEIGHTS["evidence"] * value,
    )


def _score_experiment(inputs: RankingInputs) -> DimensionScore:
    evidence_types = set(inputs.evidence_types)
    if inputs.experiment_affordance == "local_reproducible" and (
        evidence_types & _EXPERIMENT_EVIDENCE_TRIGGER
    ):
        raw, anchor_id, anchor_text = (
            5,
            "exp_5",
            "local_reproducible AND evidence_types intersects "
            "{benchmark_with_methodology, independent_implementation, spec_change}",
        )
    elif inputs.experiment_affordance == "local_reproducible":
        raw, anchor_id, anchor_text = (4, "exp_4", "local_reproducible")
    elif inputs.experiment_affordance == "requires_paid_service":
        raw, anchor_id, anchor_text = (2, "exp_2", "requires_paid_service")
    elif inputs.experiment_affordance == "not_testable" and "research_paper" in evidence_types:
        raw, anchor_id, anchor_text = (1, "exp_1", "not_testable AND research_paper present")
    else:
        raw, anchor_id, anchor_text = (0, "exp_0", "otherwise")

    return DimensionScore(
        dimension="experiment",
        raw_value=raw,
        effective_value=raw,
        cap_applied=None,
        floor_applied=None,
        anchor_id=anchor_id,
        anchor_text=anchor_text,
        inputs={
            "experiment_affordance": inputs.experiment_affordance,
            "evidence_types": ",".join(sorted(evidence_types)),
        },
        rationale=(
            f"experiment_affordance={inputs.experiment_affordance}, "
            f"evidence_types={sorted(evidence_types)}."
        ),
        weight=WEIGHTS["experiment"],
        points=WEIGHTS["experiment"] * raw,
    )


def _score_curiosity(inputs: RankingInputs, profile: RelevanceProfile) -> DimensionScore:
    tags = set(inputs.topic_tags)
    matched = sum(1 for q in profile.live_questions if set(q.tags) & tags)
    if matched >= 2:
        raw, anchor_id = 5, "cur_5"
    elif matched == 1:
        raw, anchor_id = 3, "cur_3"
    else:
        raw, anchor_id = 0, "cur_0"

    return DimensionScore(
        dimension="curiosity",
        raw_value=raw,
        effective_value=raw,
        cap_applied=None,
        floor_applied=None,
        anchor_id=anchor_id,
        anchor_text=f"{matched} live_question(s) matched by topic_tags",
        inputs={"matched_live_questions": str(matched)},
        rationale=f"{matched} live question(s) share a tag with this topic's topic_tags.",
        weight=WEIGHTS["curiosity"],
        points=WEIGHTS["curiosity"] * raw,
    )


# FOUNDER DECISION D1 -- RECORDED FOR M4, NOT IMPLEMENTED HERE. Tier
# admission itself (which tier a topic is placed into) is out of scope for
# this module; ranking.py computes ``tier1_eligible`` per the rule below and
# stops there. When M4 implements tier admission, it must honor this
# contract:
#
#   Tier 1 may waive the independent-source requirement only when
#   evidence_type in {deprecation_notice, security_advisory,
#   official_spec_change, official_api_behavior_change} AND evidence >= 4
#   AND practical_consequence >= 4 AND marketing_risk is False AND the claim
#   is directly verifiable in the artifact AND first_party_authoritative is
#   True. Benefit, performance, vendor self-benchmark, institutional opinion,
#   and promotional announcements never qualify. The absence of independent
#   analysis must remain explicit in the output.
#
# Nothing in this module implements the waiver above -- ``tier1_eligible``
# below still requires ``has_independent_evidence`` unconditionally; the
# ``first_party_authoritative_candidate`` diagnostic flags the narrow case
# the waiver is meant for without ever admitting it to Tier 1 itself.
def _tier1_eligibility(
    relevance_effective: int, evidence_effective: int, inputs: RankingInputs
) -> tuple[bool, list[str], bool]:
    """Founder-approved rule, implemented verbatim:

        tier1_eligible = relevance >= 4 AND evidence >= 3
                          AND has_independent_evidence AND not marketing_risk

    Every condition (pass or fail) is recorded in the returned reasons list.

    ``first_party_authoritative_candidate`` is a DIAGNOSTIC ONLY -- it never
    changes ``tier1_eligible``. It is True exactly when: independence is the
    ONLY failing Tier-1 condition (relevance, evidence, and marketing all
    pass); the topic's evidence is first-party-authoritative
    (``inputs.has_first_party_authoritative``, an explicit field computed by
    cluster.py -- NOT re-derived here algebraically, since after the evidence
    rubric fix (Gate A correction round 1) ``evidence_level == 3`` can also be
    reached via independent-only evidence with no first-party member, so a
    purely algebraic derivation from ``evidence_level`` alone would be far
    more fragile to reason about); AND there is no independent corroboration
    at all (``not inputs.has_independent_evidence``); AND the change itself
    is the kind that actually forces the Founder's hand -- a breaking change
    or a required migration. This narrows the diagnostic to the single case
    it was meant for (an uncorroborated vendor breaking-change/deprecation
    notice), rather than firing for any uncorroborated first-party-
    authoritative source regardless of how consequential the change is.
    """
    relevance_pass = relevance_effective >= 4
    evidence_pass = evidence_effective >= 3
    independent_pass = inputs.has_independent_evidence
    marketing_pass = not inputs.marketing_risk

    tier1_eligible = relevance_pass and evidence_pass and independent_pass and marketing_pass

    rel_status = "pass" if relevance_pass else "fail"
    evid_status = "pass" if evidence_pass else "fail"
    indep_status = "pass" if independent_pass else "fail"
    mktg_status = "pass" if marketing_pass else "fail"
    reasons = [
        f"relevance effective_value {relevance_effective} >= 4: {rel_status}",
        f"evidence effective_value {evidence_effective} >= 3: {evid_status}",
        f"has_independent_evidence: {indep_status}",
        f"not marketing_risk: {mktg_status}",
    ]

    breaking_or_migration = (
        inputs.change_class == "breaking_change" or inputs.action_required == "migration_required"
    )
    first_party_authoritative_candidate = (
        relevance_pass
        and evidence_pass
        and marketing_pass
        and not independent_pass
        and inputs.has_first_party_authoritative
        and breaking_or_migration
    )
    if first_party_authoritative_candidate:
        reasons.append(
            "first_party_authoritative_candidate: True -- barred from tier 1 solely for "
            "lacking independent corroboration of an official first-party source (e.g. a "
            "vendor deprecation notice); this is a PENDING FOUNDER DECISION, not an "
            "automatic override -- tier1_eligible remains False."
        )

    return tier1_eligible, reasons, first_party_authoritative_candidate


def _build_ranking_explanation(dimensions: list[DimensionScore]) -> str:
    """One short prose sentence naming the top two contributing dimensions
    and any cap/floor applied. The full machine-readable detail (raw/
    effective values, inputs, rationale) lives on each dimension itself --
    this string is a human-readable summary, not a re-derivation."""
    by_points = sorted(dimensions, key=lambda d: -d.points)
    top_two = by_points[:2]
    leaders = " and ".join(f"{d.dimension} ({d.points} pts)" for d in top_two)
    capped = [d.dimension for d in dimensions if d.cap_applied]
    floored = [d.dimension for d in dimensions if d.floor_applied]
    notes = []
    if capped:
        notes.append(f"{', '.join(capped)} capped")
    if floored:
        notes.append(f"{', '.join(floored)} floored")
    suffix = f"; {', '.join(notes)}." if notes else "."
    return f"Top contributors: {leaders}{suffix}"


def score_topic(inputs: RankingInputs, profile: RelevanceProfile) -> RankingBreakdown:
    """Score one topic. Pure and deterministic: same inputs, same output."""
    dimensions = [
        _score_relevance(inputs, profile),
        _score_magnitude(inputs),
        _score_consequence(inputs),
        _score_evidence(inputs),
        _score_experiment(inputs),
        _score_curiosity(inputs, profile),
    ]
    actual_order = tuple(d.dimension for d in dimensions)
    if actual_order != _DIMENSION_ORDER:
        raise DimensionOrderError(
            f"dimension order invariant violated: expected {_DIMENSION_ORDER}, got {actual_order}"
        )

    points_total = sum(d.points for d in dimensions)
    score = points_total // 5

    relevance_effective = dimensions[0].effective_value
    evidence_effective = dimensions[3].effective_value
    tier1_eligible, eligibility_reasons, first_party_authoritative_candidate = _tier1_eligibility(
        relevance_effective, evidence_effective, inputs
    )

    tie_break_key = (
        f"points={points_total} rel={relevance_effective} evid={evidence_effective} "
        f"first_seen={inputs.first_seen.isoformat()} topic_id={inputs.topic_id}"
    )
    ranking_explanation = _build_ranking_explanation(dimensions)

    return RankingBreakdown(
        dimensions=dimensions,
        points_total=points_total,
        score=score,
        rubric_version=RUBRIC_VERSION,
        weights_version=WEIGHTS_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        profile_version=profile.profile_version,
        tier1_eligible=tier1_eligible,
        eligibility_reasons=eligibility_reasons,
        first_party_authoritative_candidate=first_party_authoritative_candidate,
        tie_break_key=tie_break_key,
        ranking_explanation=ranking_explanation,
    )


def rank_topics(
    inputs: list[RankingInputs], profile: RelevanceProfile
) -> list[tuple[RankingInputs, RankingBreakdown]]:
    """Score and order every topic in ``inputs``.

    Returns ``(RankingInputs, RankingBreakdown)`` pairs, best first. Final
    ordering (ties broken in this exact sequence): ``points_total`` DESC ->
    relevance ``effective_value`` DESC -> evidence ``effective_value`` DESC ->
    ``first_seen`` ASC -> ``topic_id`` ASC. See the module docstring for why
    this returns pairs rather than ``RankedTopic`` instances.
    """
    scored = [(item, score_topic(item, profile)) for item in inputs]

    def _sort_key(pair: tuple[RankingInputs, RankingBreakdown]) -> tuple[int, int, int, date, str]:
        item, breakdown = pair
        relevance_effective = next(
            d.effective_value for d in breakdown.dimensions if d.dimension == "relevance"
        )
        evidence_effective = next(
            d.effective_value for d in breakdown.dimensions if d.dimension == "evidence"
        )
        return (
            -breakdown.points_total,
            -relevance_effective,
            -evidence_effective,
            item.first_seen,
            item.topic_id,
        )

    return sorted(scored, key=_sort_key)
