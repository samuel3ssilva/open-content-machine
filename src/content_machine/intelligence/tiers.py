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
the Founder decision this module implements. D1 was issued at
``evidence_level >= 4``, which measurement showed was unreachable: reaching
evidence >= 4 already implies ``has_independent_evidence`` in every current
rubric branch (``cluster._evidence_level_and_marketing_risk``), so a waiver
meant to admit an UNCORROBORATED first-party-authoritative source could never
actually fire at that floor. The Founder's final ruling (Gate C) resolves
this by restating the threshold as ``evidence_level >= 3`` -- this is now the
REAL, live admission rule: :func:`d1_exception_fires` uses
``evidence_floor=3`` and, when it fires without the base rule also passing,
admits the topic to Tier 1 as an uncorroborated first-party-authoritative
source with no independent analysis. That absence of independent
corroboration is recorded explicitly in the admission reason and surfaced in
the brief (``brief.py``'s Tier-1 rendering) -- it must never be silently
implied.

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

# R2 (2026-08-01, post-merge-gate round): a version marker OWNED BY THIS
# MODULE'S CONFIDENCE SEMANTICS -- distinct from ranking.RUBRIC_VERSION (the
# six-dimension scoring rubric, which this module never touches) and from
# brief.BRIEF_VERSION (the rendered-document schema). Bumped whenever
# ``_assess_confidence``'s corroboration semantics change -- as they did
# across the Part B / Part C / round-3 Fable rulings folded into that
# function (see its docstring) -- even when no ``WeeklyBrief`` field or
# rendered-Markdown template changes as a result. Recorded ADDITIVELY on
# both ``brief.WeeklyBrief.confidence_rubric_version`` and
# ``weekly.RunManifest.confidence_rubric_version``; deliberately EXCLUDED
# from ``weekly.compute_run_id``/``compute_input_fingerprint`` (Fable
# ruling 2026-08-01, R1/R2: semantics must never enter run identity, since
# that would silently un-guard every historical output directory and make
# past run_ids non-recomputable). Its job is purely diagnostic: when
# ``write_weekly_run_outputs``'s idempotency-skip byte-compare (R1) finds a
# stale on-disk run, the manifest delta on this field is the human-readable
# "why" that complements the byte-level "that".
CONFIDENCE_RUBRIC_VERSION = "gate-b-tiers-2"

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
    artifact, a self-published rigorous artifact, or evidence from at least
    one publisher structurally independent of the subject.

    Fable ruling 2026-08-01 (reversing an earlier "it's just a disjunction"
    classification): the fourth disjunct of ``has_direct_artifact_or_
    independent_source`` is ``independent_publisher_count > 0`` --
    ``has_genuine_independent_evidence`` in ``cluster.py`` -- and that is
    independent EVIDENCE, not independent CORROBORATION: a single source
    suffices, no second source is required. For the anchor
    ``evid_4_independent_rigorous_alone``, the first three disjuncts
    (first-party authoritative, non-subject authoritative, first-party
    artifact) are all false by construction, so this fourth disjunct is the
    ONLY one that holds -- and only under the "evidence" reading. Calling it
    "corroboration" here would be false for exactly that anchor.

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
                "rigorous artifact, or evidence from at least one publisher structurally "
                "independent of the subject (Founder decision D3)."
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


# Fable ruling 2026-08-01 (Part B, "independence must stop being rendered as
# corroboration"): the evidence anchors that, on their own, already establish
# TWO distinct sources -- a first-party artifact plus independent evidence, or
# an authoritative source plus independent analysis plus independent rigor.
# ``evid_4_independent_rigorous_alone`` is deliberately NOT a member: it is
# reached by a SINGLE independent source, zero corroboration.
_CORROBORATED_ANCHORS = frozenset(
    {
        "evid_5_authoritative_plus_analysis_plus_independent_rigor",
        "evid_4_first_party_plus_independent",
    }
)


def has_cross_source_corroboration(
    evidence_anchor_id: str, independent_publisher_count: int
) -> bool:
    """Whether a topic has genuine cross-source corroboration -- the exact
    predicate ``_assess_confidence`` uses internally to gate ``high``
    confidence for fact claims (Fable ruling 2026-08-01, Part B / round-3
    final recheck). Public (not a private helper) so callers OUTSIDE this
    module -- specifically ``brief.py``'s executive-summary and
    content-opportunity single-source disclosures (G1/G3, product review
    round 4) -- can ask the SAME question this module already answers
    internally, instead of re-deriving or approximating it from
    ``independent_publisher_count`` alone. That would be wrong: a topic
    reached via ``evid_4_first_party_plus_independent`` with
    ``independent_publisher_count == 1`` (a first-party leg plus one
    independent leg) IS genuinely cross-source corroborated even though its
    independent-publisher count alone is only 1 -- counting it as
    "single-sourced" would contradict its own ``high``-confidence
    classification a few lines away in the same rendered document."""
    return independent_publisher_count >= 2 or (
        evidence_anchor_id in _CORROBORATED_ANCHORS and independent_publisher_count >= 1
    )


def _assess_confidence(
    claim_class: ClaimClass, inputs: RankingInputs, independent_publisher_count: int
) -> tuple[ConfidenceLevel, str]:
    """Deterministic confidence, built only from ``evidence_level``,
    ``has_independent_evidence``, ``independent_publisher_count``, and
    whether ``claim_class == "fact"`` -- no new inputs invented. A non-
    ``fact`` claim (hypothesis or marketing) is always ``low`` confidence,
    regardless of evidence_level: an unestablished or self-serving claim does
    not become more trustworthy just because its evidence_level happens to be
    high (e.g. a marketing_risk topic can still reach evidence_level 3+, see
    D4/hardening).

    Fable ruling 2026-08-01 (Part B): ``evidence_level >= 4`` alone does NOT
    imply cross-source corroboration -- the anchor
    ``evid_4_independent_rigorous_alone`` reaches evidence_level 4 with a
    SINGLE source. ``high`` confidence now additionally requires
    ``has_cross_source_corroboration`` (either a corroborating evidence
    anchor, or two-or-more distinct independent publishers). A single-source
    independent topic is capped at ``medium`` and says so explicitly, rather
    than being rendered as "genuine independent corroboration is present".

    Fable ruling 2026-08-01 (round 3, final recheck defect): a
    ``_CORROBORATED_ANCHORS`` member alone is NOT sufficient -- both anchors
    in that set are reached by a FIRST-PARTY leg plus a SECOND, independent
    leg, and it is the independent leg that must actually be countable.
    ``cluster._evidence_level_and_marketing_risk`` sets
    ``has_independent_rigorous``/``has_independent_analysis`` off the raw
    evidence_type/publisher check alone -- it does not consult
    ``_is_independent``/``may_supply_independence`` -- so a Gate E0.3
    registry-denied member (``may_supply_independence=False``) can still
    drive the cluster to ``evid_4_first_party_plus_independent`` while
    contributing ZERO to ``independent_publisher_count``. Requiring
    ``independent_publisher_count >= 1`` alongside the anchor closes that
    gap: the anchor alone no longer manufactures a second source out of a
    leg the registry has explicitly forbidden from supplying independence."""
    cross_source_corroborated = has_cross_source_corroboration(
        inputs.evidence_anchor_id, independent_publisher_count
    )
    if claim_class != "fact":
        return (
            "low",
            f"claim_class={claim_class} (not 'fact'): confidence is low regardless of "
            "evidence_level.",
        )
    if inputs.evidence_level >= 4 and cross_source_corroborated:
        return (
            "high",
            (
                f"claim_class=fact, evidence_level={inputs.evidence_level} >= 4 AND "
                f"cross-source corroboration is present (evidence_anchor_id="
                f"{inputs.evidence_anchor_id}, independent_publisher_count="
                f"{independent_publisher_count}): at least two distinct sources "
                "support the claim."
            ),
        )
    if inputs.has_independent_evidence and independent_publisher_count == 1:
        return (
            "medium",
            (
                f"claim_class=fact, evidence_level={inputs.evidence_level}, "
                f"independent_publisher_count=1: single-source independent evidence "
                "-- one publisher structurally independent of the subject, on its "
                "own. No second, distinct source supports the claim; confidence is "
                "capped at medium."
            ),
        )
    # Fable ruling 2026-08-01 (follow-up, Part C): this catch-all used to say
    # "no second, distinct source supports the claim" unconditionally -- FALSE
    # whenever independent_publisher_count >= 2 (e.g. two independent_analysis
    # items from different publishers, no first-party member --
    # evid_3_independent_only, evidence_level 3). This branch is reached
    # ONLY because evidence_level is below the high-confidence bar (the
    # independent_publisher_count >= 1 single-source case is branch (c)
    # above, and count >= 2 already reaches 'high' whenever evidence_level
    # also clears >= 4 -- see the corroboration check above). The text below
    # is therefore true whatever independent_publisher_count is, and asserts
    # nothing about single-sourcing.
    #
    # Fable ruling 2026-08-01 (round 3, final recheck defect): the text
    # ALSO used to say "evidence_level is below the >= 4 bar that high
    # confidence requires" unconditionally -- FALSE once the anchor +
    # independent_publisher_count >= 1 corroboration check above was
    # introduced, because this branch is now also reached at evidence_level
    # == 4 whenever cross-source corroboration is absent (e.g. a Gate E0.3
    # registry-denied member drove the anchor to
    # evid_4_independent_rigorous_alone or evid_4_first_party_plus_independent
    # without supplying a countable independent publisher). The wording below
    # names the real, two-part bar (level AND corroboration) instead of
    # asserting which part failed, so it stays true at level 4 without
    # corroboration and at any level below 4.
    return (
        "medium",
        (
            f"claim_class=fact, evidence_level={inputs.evidence_level}, "
            f"has_independent_evidence={inputs.has_independent_evidence}, "
            f"independent_publisher_count={independent_publisher_count}: "
            "does not meet the high-confidence bar (evidence_level >= 4 together with "
            "cross-source corroboration)."
        ),
    )


def build_claim_assessment(
    inputs: RankingInputs, *, independent_publisher_count: int
) -> ClaimAssessment:
    """Build the full M4 claim assessment (classification + confidence) for
    one topic's ``RankingInputs``. Pure and deterministic.

    ``independent_publisher_count`` is threaded in directly from the topic's
    ``TopicCluster`` (never added to ``RankingInputs`` itself -- see
    ``build_tiered_topic``, the only caller within this module)."""
    claim_class, claim_reason = _classify_claim_class(inputs)
    confidence, confidence_reason = _assess_confidence(
        claim_class, inputs, independent_publisher_count
    )
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
    """The six conjuncts of Founder decision D1 (ADR 0004). ``evidence_floor``
    is the only parameter; the real waiver (:func:`d1_exception_fires`) calls
    this with ``evidence_floor=3``, the Founder's final Gate C ruling. The
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
    """The real D1 waiver predicate, at ``evidence_level >= 3`` -- the
    Founder's final ruling (Gate C) on the threshold ADR 0004 left open. This
    is now the live admission rule: when it fires and the base Tier-1 rule
    does not, the topic is admitted to Tier 1 as an uncorroborated
    first-party-authoritative source with no independent analysis (see
    :func:`_build_tier_assignment`, which records that fact explicitly in
    ``admission_reasons``)."""
    return _d1_predicate(anchor, inputs, breakdown, evidence_floor=3)


def _d1_reason_text(
    anchor: SourceItem, inputs: RankingInputs, breakdown: RankingBreakdown, fired: bool
) -> str:
    consequence_effective = _consequence_effective(breakdown)
    evidence_type_ok = anchor.evidence_type in _D1_EVIDENCE_TYPES
    return (
        "D1 (ADR 0004, Founder final decision, Gate C) exception predicate: evidence_type "
        f"'{anchor.evidence_type}' in {sorted(_D1_EVIDENCE_TYPES)}: {evidence_type_ok}; "
        f"evidence_level {inputs.evidence_level} >= 3: {inputs.evidence_level >= 3}; "
        f"consequence effective_value {consequence_effective} >= 4: "
        f"{consequence_effective >= 4}; not marketing_risk: {not inputs.marketing_risk}; "
        f"claim_directly_verifiable_in_artifact: {anchor.claim_directly_verifiable_in_artifact}; "
        f"has_first_party_authoritative: {inputs.has_first_party_authoritative} -- "
        f"{'FIRES' if fired else 'does not fire'}."
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
        # F7 (round 7, product review -- "structurally the same construction
        # P1 spent a round removing from the line directly above"): the old
        # text, "claim_class=fact, confidence=medium: attested by an
        # artifact but lacking a second, distinct supporting source -- save
        # for later review.", said the action word twice (the recommended
        # action itself, plus "save for later review" inside its own
        # reason), embedded two raw key=value diagnostic tokens in
        # reader-facing prose, and buried the operative caveat behind a
        # colon that opens an explanatory scope with the limitation
        # (`"but lacking..."`) sitting inside it -- the exact P1 defect
        # (see brief._human_evidence_sentence's docstring). Rewritten as two
        # plain clauses, no key=value tokens, no colon-opened scope, and the
        # deferral instruction stated once (not restated with the action
        # word again) -- see ADR 0007's "Study Queue / Tier 2 action,
        # revisited (round 7)" decision for how this reason now also
        # coexists with the Study Queue's own light-study line for the SAME
        # topic.
        #
        # F9 (round 8, this branch, Fable + product review): round 7's
        # rewrite was STILL a constant -- this function takes no
        # corroboration input, so the text asserted "no second, independent
        # source corroborates it yet" for every fact/medium topic
        # regardless of the real state. That is false whenever
        # independent_publisher_count >= 2 (two independent, corroborating
        # sources DO exist and DO corroborate -- has_cross_source_
        # corroboration is True) -- and even then, further corroboration
        # alone could never lift the topic to 'high', because in that state
        # it is the evidence_level >= 4 bar that failed, not the
        # corroboration bar. "Attested by an artifact" is additionally
        # wrong for authoritative-source-only fact states.
        #
        # Fable's two options (see ADR 0007 Round 8): (a) cause-neutral
        # two-part-bar wording that presumes neither cause, or (b) branch on
        # the corroboration predicate threaded at the tiers join (the P4
        # single_sourced plumbing pattern, never through RankingInputs).
        # Chosen: (a) -- this function is deliberately a pure function of
        # (tier, claim_class, confidence) only (see its own docstring); (b)
        # would thread independent_publisher_count/
        # has_cross_source_corroboration through _build_tier_assignment and
        # every brief.py call site that renders a Tier 2/light-study reason,
        # a materially larger edit for a wording-truthfulness ticket to make
        # unreviewed, mirroring round 5's own reasoning for rejecting the
        # gate-on-action alternative. The two-part bar
        # ("evidence_level >= 4 together with cross-source corroboration")
        # mirrors the exact phrase this module's own catch-all
        # confidence_reason already uses (see _assess_confidence, Part C) --
        # true regardless of WHICH conjunct failed, so it never presumes
        # missing corroboration. "Save the deeper follow-up" (not "hold it")
        # also closes the scope defect product review flagged: the referent
        # is now explicit, so brief._light_study_reason's paraphrase of this
        # same deferral is accurate rather than corrective.
        return (
            "save",
            "Attested by an artifact, but confidence has not reached the high bar "
            "(evidence_level >= 4 together with cross-source corroboration) -- save the "
            "deeper follow-up for later review as the evidence picture develops.",
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
    tier1_admitted = base_admitted or d1_fires

    # Every one of ranking.py's four base conditions (pass or fail) lands in
    # exactly one bucket -- nothing is dropped, so both lists are populated
    # regardless of the overall outcome.
    admission_reasons = [r for r in breakdown.eligibility_reasons if r.endswith(": pass")]
    exclusion_reasons = [r for r in breakdown.eligibility_reasons if not r.endswith(": pass")]

    d1_reason = _d1_reason_text(anchor, inputs, breakdown, d1_fires)
    (admission_reasons if d1_fires else exclusion_reasons).append(d1_reason)

    d1_only_admission = d1_fires and not base_admitted
    if d1_only_admission:
        admission_reasons.append(
            "admitted via D1 first-party-authoritative exception (evidence>=3, uncorroborated "
            "first-party source -- no independent analysis)."
        )

    warnings: list[str] = []

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
    claim = build_claim_assessment(
        inputs, independent_publisher_count=cluster.independent_publisher_count
    )
    tier_assignment = _build_tier_assignment(rank, anchor, inputs, breakdown, claim)
    return TieredTopic(
        rank=rank,
        topic_id=cluster.topic_id,
        canonical_title=cluster.canonical_title,
        score=breakdown.score,
        claim=claim,
        tier_assignment=tier_assignment,
        # Gate E0 (E0.1, R7): copied verbatim from the cluster at exactly the
        # point this function joins a cluster with its ranked position --
        # "where clusters join ranked topics", never through ranking.py.
        security_flags=cluster.security_flags,
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
