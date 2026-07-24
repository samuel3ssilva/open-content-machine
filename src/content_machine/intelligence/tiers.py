"""M4: claim classification and Tier admission over ranked intelligence topics.

Depends on ``content_machine.intelligence.models`` and reads (never
recomputes) facts already produced by ``cluster.py`` and ``ranking.py``:
``TopicCluster``, ``RankingInputs``, ``RankingBreakdown``, and the anchor
``SourceItem``. This module builds strictly ON TOP of those -- it never
re-derives the evidence rubric, the six ranking weights, or the base Tier-1
rule (``RankingBreakdown.tier1_eligible`` / ``eligibility_reasons``), and it
never re-sorts: :func:`assign_tiers` takes the list already produced by
``ranking.rank_topics`` (final rank order, ties already broken) and only
slices and annotates it.

See ``docs/adr/0004-intelligence-evidence-and-ranking-decisions.md`` (D1) for
the Founder decision this module implements, including the MEASURED
CONSTRAINT that the D1 exception is unreachable as issued: reaching
``evidence_level >= 4`` already implies ``has_independent_evidence`` in every
current rubric branch (``cluster._evidence_level_and_marketing_risk``), so a
waiver meant to admit an UNCORROBORATED first-party-authoritative source can
never actually need to fire -- whenever its six conjuncts hold, the topic
already has independent evidence anyway. This module implements the predicate
VERBATIM regardless: it is not this module's place to "fix" the threshold,
reinterpret it, or add a new rubric branch to make it reachable (that is a
Founder/architecture decision, not implementation work). A separate
diagnostic, ``d1_would_admit_at_evidence_3``, reports (for Founder
decision-support only) which topics would satisfy the same predicate if the
threshold were lowered to ``evidence_level >= 3`` -- it never affects
admission.

Claim classification (fact / hypothesis / marketing) and confidence
(high / medium / low) are deterministic functions of existing structured
``RankingInputs`` fields only -- no model call, no prose parsing of titles or
summaries.

Tier bucketing (Founder decision, M4 ticket): ranks 1-3 are Tier 1 ("Must
Understand") CANDIDATES -- a candidate is only actually placed in Tier 1 if
it passes admission (the base rule, or the D1 exception). NO ARTIFICIAL
BACKFILL: an admission failure at rank 1-3 is never promoted, and the tier
boundaries are never reshuffled to compensate -- the failing item simply
becomes a Tier 2 item at its own true rank (falls through "by rank", per the
ticket), and Tier 1 ships with fewer than 3 items. Ranks 4-7 are always Tier 2
("Should Know") and ranks 8-10 are always Tier 3 ("Radar"), regardless of
admission outcomes elsewhere in the Top 10.
"""

from __future__ import annotations

from content_machine.intelligence.models import (
    ClaimAssessment,
    ClaimClass,
    ConfidenceLevel,
    RankingBreakdown,
    RankingInputs,
    RecommendedAction,
    SourceItem,
    TierAssignment,
    TieredTopic,
    TierName,
    TopicCluster,
)

TOP_N = 10

_TIER_LABELS: dict[TierName, str] = {
    "tier_1": "Must Understand",
    "tier_2": "Should Know",
    "tier_3": "Radar",
}

# D1 (ADR 0004): "official_spec_change" in the decision text denotes the
# existing "spec_change" evidence-type literal -- no separate literal exists.
# official_doc is deliberately EXCLUDED: D1 names four specific evidence
# types, not the whole _AUTHORITATIVE_TYPES set in cluster.py.
_D1_EVIDENCE_TYPES = frozenset(
    {
        "deprecation_notice",
        "security_advisory",
        "spec_change",
        "official_api_behavior_change",
    }
)


class TierAssignmentError(RuntimeError):
    """Raised when a tiers.py invariant is violated: a rank outside the
    Top-10 window, a missing ``consequence`` dimension on a
    ``RankingBreakdown``, or a recommended-action combination the rule chain
    does not (and, by construction, should never) cover. A plain ``assert``
    would be stripped under ``python -O``; this is explicit and always
    enforced, mirroring ``ranking.DimensionOrderError``.
    """


def _classify_claim_class(inputs: RankingInputs) -> tuple[ClaimClass, str]:
    """Deterministic fact/hypothesis/marketing classification.

    Order matters: marketing is checked FIRST, so a self-serving or
    unresolved promotional claim is flagged even in a cluster whose evidence
    level would otherwise qualify as ``fact`` (e.g. a vendor announcement
    accompanied only by its own uncorroborated self-published artifact/
    commentary) -- ``marketing_risk`` already encodes exactly this per
    Founder decision D4 / spec Section 5.2 (see
    ``cluster._evidence_level_and_marketing_risk``): true whenever a
    first-party-promotional announcement/release_note, or the subject's own
    benefit/performance-claiming self-analysis, is present AND not cleared by
    genuine independent evidence.

    ``fact`` reuses ``has_direct_artifact_or_independent_source`` (Founder
    decision D3) unchanged -- that fact is already exactly "the change is
    attested by an artifact": a first-party or non-subject authoritative
    source, a self-published rigorous artifact, or genuine independent
    corroboration.

    Everything else is ``hypothesis`` -- rumor, isolated uncorroborated
    secondary news (D2), or any evidentiary basis that does not establish the
    claim.
    """
    if inputs.marketing_risk:
        return (
            "marketing",
            (
                f"marketing_risk=True (evidence_anchor_id={inputs.evidence_anchor_id}, "
                f"evidence_level={inputs.evidence_level}): a first-party-promotional "
                "announcement/release_note, or the subject's own benefit/performance-"
                "claiming self-analysis, is present and no independent evidence has "
                "cleared it (Founder decision D4 / spec Section 5.2)."
            ),
        )
    if inputs.evidence_level >= 3 and inputs.has_direct_artifact_or_independent_source:
        return (
            "fact",
            (
                f"evidence_level={inputs.evidence_level} >= 3 AND "
                "has_direct_artifact_or_independent_source=True "
                f"(evidence_anchor_id={inputs.evidence_anchor_id}): the change is attested "
                "by a first-party or non-subject authoritative artifact, a self-published "
                "rigorous artifact, or genuine independent corroboration (Founder decision "
                "D3)."
            ),
        )
    return (
        "hypothesis",
        (
            f"evidence_level={inputs.evidence_level}, "
            "has_direct_artifact_or_independent_source="
            f"{inputs.has_direct_artifact_or_independent_source}, "
            f"marketing_risk={inputs.marketing_risk} "
            f"(evidence_anchor_id={inputs.evidence_anchor_id}): no artifact and no "
            "independent source attests the claim -- rumor, isolated uncorroborated "
            "secondary news (Founder decision D2), or otherwise unestablished evidence."
        ),
    )


def _assess_confidence(
    claim_class: ClaimClass, inputs: RankingInputs
) -> tuple[ConfidenceLevel, str]:
    """Deterministic confidence, built only from ``evidence_level``,
    ``has_independent_evidence``, and whether ``claim_class == "fact"`` -- no
    new inputs invented. A non-``fact`` claim (hypothesis or marketing) is
    always ``low`` confidence, regardless of evidence_level: an unestablished
    or self-serving claim does not become more trustworthy just because its
    evidence_level happens to be high (e.g. a marketing_risk topic can still
    reach evidence_level 3+, see D4/hardening)."""
    if claim_class != "fact":
        return (
            "low",
            f"claim_class={claim_class} (not 'fact'): confidence is low regardless of "
            "evidence_level.",
        )
    if inputs.evidence_level >= 4 and inputs.has_independent_evidence:
        return (
            "high",
            (
                f"claim_class=fact, evidence_level={inputs.evidence_level} >= 4 AND "
                "has_independent_evidence=True: genuine independent corroboration is "
                "present."
            ),
        )
    return (
        "medium",
        (
            f"claim_class=fact, evidence_level={inputs.evidence_level}, "
            f"has_independent_evidence={inputs.has_independent_evidence}: attested by an "
            "artifact but not (yet) independently corroborated at the high-confidence bar."
        ),
    )


def build_claim_assessment(inputs: RankingInputs) -> ClaimAssessment:
    """Build the full M4 claim assessment (classification + confidence) for
    one topic's ``RankingInputs``. Pure and deterministic."""
    claim_class, claim_reason = _classify_claim_class(inputs)
    confidence, confidence_reason = _assess_confidence(claim_class, inputs)
    return ClaimAssessment(
        claim_class=claim_class,
        claim_class_reason=claim_reason,
        confidence=confidence,
        confidence_reason=confidence_reason,
    )


def _consequence_effective(breakdown: RankingBreakdown) -> int:
    for dimension in breakdown.dimensions:
        if dimension.dimension == "consequence":
            return dimension.effective_value
    raise TierAssignmentError(
        "RankingBreakdown is missing its 'consequence' dimension -- cannot evaluate the D1 "
        "predicate's practical_consequence >= 4 conjunct."
    )


def _d1_predicate(
    anchor: SourceItem, inputs: RankingInputs, breakdown: RankingBreakdown, evidence_floor: int
) -> bool:
    """The six conjuncts of Founder decision D1 (ADR 0004), verbatim, with
    ``evidence_level >= evidence_floor`` as the only parameter -- used both
    for the real waiver (``evidence_floor=4``) and the
    ``d1_would_admit_at_evidence_3`` diagnostic (``evidence_floor=3``). The
    "claim is directly verifiable in the artifact" conjunct reads the
    anchor's ``claim_directly_verifiable_in_artifact`` field (added to
    ``SourceItem`` for this decision); ``evidence_type`` and
    ``first_party_authoritative`` are read off the anchor item and
    ``inputs.has_first_party_authoritative`` respectively, mirroring the
    existing precedent (``cluster.to_ranking_inputs`` already reads
    ``change_class``/``action_required``/``experiment_affordance`` off the
    anchor)."""
    return (
        anchor.evidence_type in _D1_EVIDENCE_TYPES
        and inputs.evidence_level >= evidence_floor
        and _consequence_effective(breakdown) >= 4
        and not inputs.marketing_risk
        and anchor.claim_directly_verifiable_in_artifact
        and inputs.has_first_party_authoritative
    )


def d1_exception_fires(
    anchor: SourceItem, inputs: RankingInputs, breakdown: RankingBreakdown
) -> bool:
    """The real D1 waiver predicate (``evidence_level >= 4``). Per the ADR's
    measured constraint, this is expected to NEVER return True on real data:
    reaching evidence_level >= 4 already implies
    ``inputs.has_independent_evidence`` in every current rubric branch, so a
    waiver for an UNCORROBORATED first-party-authoritative source can never
    actually need to fire."""
    return _d1_predicate(anchor, inputs, breakdown, evidence_floor=4)


def d1_would_admit_at_evidence_3(
    anchor: SourceItem, inputs: RankingInputs, breakdown: RankingBreakdown
) -> bool:
    """DIAGNOSTIC ONLY (ADR 0004, decision-support for the Founder): the same
    six conjuncts with the evidence floor lowered to 3. NEVER consulted by
    :func:`_tier1_admission` -- must never affect ``tier1_admitted``."""
    return _d1_predicate(anchor, inputs, breakdown, evidence_floor=3)


def _d1_reason_text(
    anchor: SourceItem, inputs: RankingInputs, breakdown: RankingBreakdown, fired: bool
) -> str:
    consequence_effective = _consequence_effective(breakdown)
    evidence_type_ok = anchor.evidence_type in _D1_EVIDENCE_TYPES
    return (
        "D1 (ADR 0004) exception predicate: evidence_type "
        f"'{anchor.evidence_type}' in {sorted(_D1_EVIDENCE_TYPES)}: {evidence_type_ok}; "
        f"evidence_level {inputs.evidence_level} >= 4: {inputs.evidence_level >= 4}; "
        f"consequence effective_value {consequence_effective} >= 4: "
        f"{consequence_effective >= 4}; not marketing_risk: {not inputs.marketing_risk}; "
        f"claim_directly_verifiable_in_artifact: {anchor.claim_directly_verifiable_in_artifact}; "
        f"has_first_party_authoritative: {inputs.has_first_party_authoritative} -- "
        f"{'FIRES' if fired else 'does not fire'} (measured, per the ADR, to be unreachable: "
        "evidence_level >= 4 already implies independent evidence in every current rubric "
        "branch, so this waiver for an uncorroborated source can never actually need to "
        "trigger)."
    )


def _tier_bucket_for_rank(rank: int, tier1_admitted: bool) -> tuple[TierName, str]:
    """Rank-to-tier bucketing (Founder decision, M4 ticket). Ranks 1-3 are
    Tier 1 CANDIDATES only -- an admission failure there falls straight
    through to Tier 2 at its own true rank (never demoted further, never
    reshuffled): NO ARTIFICIAL BACKFILL. Ranks 4-7 are always Tier 2; ranks
    8-10 are always Tier 3, unconditionally."""
    if rank <= 3:
        if tier1_admitted:
            return "tier_1", f"rank {rank} <= 3 and Tier 1 admission passed."
        return (
            "tier_2",
            f"rank {rank} <= 3 but Tier 1 admission failed -- falls through to Tier 2 at "
            "its own rank per the no-backfill rule (never promoted, never demoted past "
            "Tier 2).",
        )
    if rank <= 7:
        return "tier_2", f"rank {rank} is in the fixed Tier 2 window (4-7)."
    return "tier_3", f"rank {rank} is in the fixed Tier 3 window (8-10)."


def _recommend_action(
    tier: TierName, claim_class: ClaimClass, confidence: ConfidenceLevel
) -> tuple[RecommendedAction, str]:
    """Deterministic recommended action from (tier, claim_class, confidence)
    only. The rule chain is exhaustive by construction: Tier 1 is handled
    first (unconditionally 'study'); marketing is handled next (regardless of
    tier, since marketing_risk blocks Tier-1 admission -- a Tier-1 item is
    never marketing); a 'fact' claim's confidence is always 'high' or
    'medium' (never 'low' -- see ``_assess_confidence``), covering the
    remaining fact cases; 'hypothesis' is covered by the two remaining tiers.
    No combination of (tier in {tier_2, tier_3}, claim_class, confidence) is
    left uncovered, so the trailing raise should be provably unreachable --
    kept explicit rather than silently falling through (mirrors
    ``ranking.DimensionOrderError``)."""
    if tier == "tier_1":
        return (
            "study",
            "Tier 1 (Must Understand): admitted to the highest-priority tier -- study in "
            "full.",
        )
    if claim_class == "marketing":
        return (
            "monitor",
            "claim_class=marketing: a self-serving or unresolved promotional claim -- "
            "monitor for independent corroboration before acting on it.",
        )
    if claim_class == "fact" and confidence == "high":
        return (
            "read",
            "claim_class=fact, confidence=high: well-attested and independently "
            "corroborated -- read in full.",
        )
    if claim_class == "fact" and confidence == "medium":
        return (
            "save",
            "claim_class=fact, confidence=medium: attested by an artifact but not "
            "independently corroborated -- save for later review.",
        )
    if claim_class == "hypothesis" and tier == "tier_2":
        return (
            "monitor",
            "claim_class=hypothesis in Tier 2 (Should Know): unconfirmed -- monitor for "
            "corroboration.",
        )
    if claim_class == "hypothesis" and tier == "tier_3":
        return (
            "ignore",
            "claim_class=hypothesis in Tier 3 (Radar): rumor or isolated uncorroborated "
            "secondary news -- safe to ignore until corroborated.",
        )
    raise TierAssignmentError(
        f"recommended_action rule chain fell through for tier={tier}, "
        f"claim_class={claim_class}, confidence={confidence} -- this combination should be "
        "unreachable by construction; update the rule chain rather than adding a silent "
        "default."
    )


def _build_tier_assignment(
    rank: int,
    anchor: SourceItem,
    inputs: RankingInputs,
    breakdown: RankingBreakdown,
    claim: ClaimAssessment,
) -> TierAssignment:
    if not 1 <= rank <= TOP_N:
        raise TierAssignmentError(f"rank {rank} is outside the Top-{TOP_N} window (1-{TOP_N}).")

    base_admitted = breakdown.tier1_eligible
    d1_fires = d1_exception_fires(anchor, inputs, breakdown)
    d1_would_at_3 = d1_would_admit_at_evidence_3(anchor, inputs, breakdown)
    tier1_admitted = base_admitted or d1_fires

    # Every one of ranking.py's four base conditions (pass or fail) lands in
    # exactly one bucket -- nothing is dropped, so both lists are populated
    # regardless of the overall outcome.
    admission_reasons = [r for r in breakdown.eligibility_reasons if r.endswith(": pass")]
    exclusion_reasons = [r for r in breakdown.eligibility_reasons if not r.endswith(": pass")]

    d1_reason = _d1_reason_text(anchor, inputs, breakdown, d1_fires)
    (admission_reasons if d1_fires else exclusion_reasons).append(d1_reason)

    warnings: list[str] = []
    if d1_fires and not base_admitted:
        warnings.append(
            "Tier 1 admission relies solely on the D1 exception (ADR 0004): this path is "
            "measured, per the ADR, to be unreachable on real data -- this occurrence is "
            "unexpected and should be reviewed."
        )
    if d1_would_at_3 and not d1_fires:
        warnings.append(
            "d1_would_admit_at_evidence_3 diagnostic is True for this topic: it would "
            "satisfy the D1 exception if its threshold were evidence_level >= 3 instead of "
            ">= 4. Decision-support only (ADR 0004) -- does not affect admission."
        )

    tier, tier_reason = _tier_bucket_for_rank(rank, tier1_admitted)
    if rank <= 3 and not tier1_admitted:
        warnings.append(f"No-backfill rule applied: {tier_reason}")

    action, action_reason = _recommend_action(tier, claim.claim_class, claim.confidence)

    return TierAssignment(
        tier=tier,
        tier_label=_TIER_LABELS[tier],
        rank=rank,
        tier1_admitted=tier1_admitted,
        admission_reasons=admission_reasons,
        exclusion_reasons=exclusion_reasons,
        warnings=warnings,
        d1_exception_fired=d1_fires,
        d1_would_admit_at_evidence_3=d1_would_at_3,
        recommended_action=action,
        recommended_action_reason=action_reason,
    )


def build_tiered_topic(
    rank: int,
    cluster: TopicCluster,
    inputs: RankingInputs,
    breakdown: RankingBreakdown,
    anchor: SourceItem,
) -> TieredTopic:
    """Build the full M4 view of one already-ranked topic. ``rank`` must be
    the topic's position (1-indexed) in the final, already tie-broken order
    produced by ``ranking.rank_topics`` -- this function never re-sorts."""
    claim = build_claim_assessment(inputs)
    tier_assignment = _build_tier_assignment(rank, anchor, inputs, breakdown, claim)
    return TieredTopic(
        rank=rank,
        topic_id=cluster.topic_id,
        canonical_title=cluster.canonical_title,
        score=breakdown.score,
        claim=claim,
        tier_assignment=tier_assignment,
    )


def assign_tiers(
    ranked: list[tuple[RankingInputs, RankingBreakdown]],
    clusters_by_topic_id: dict[str, TopicCluster],
    items_by_id: dict[str, SourceItem],
) -> list[TieredTopic]:
    """Take ``ranked`` -- the EXACT output of ``ranking.rank_topics`` (final
    rank order, ties already broken) -- and annotate its Top 10 with tier
    assignments. Never re-sorts and never invents a second ordering: rank is
    assigned purely by position in ``ranked``.

    ``clusters_by_topic_id`` and ``items_by_id`` let this function reach the
    ``TopicCluster`` (for ``canonical_title`` and the anchor's id) and the
    anchor ``SourceItem`` (for ``evidence_type`` and
    ``claim_directly_verifiable_in_artifact``, per the D1 predicate) for each
    ranked topic, mirroring how ``cluster.to_ranking_inputs`` already reaches
    the anchor for ``change_class``/``action_required``/
    ``experiment_affordance``.
    """
    top_n = ranked[:TOP_N]
    tiered: list[TieredTopic] = []
    for position, (inputs, breakdown) in enumerate(top_n, start=1):
        cluster = clusters_by_topic_id[inputs.topic_id]
        anchor = items_by_id[cluster.anchor_item_id]
        tiered.append(build_tiered_topic(position, cluster, inputs, breakdown, anchor))
    return tiered
