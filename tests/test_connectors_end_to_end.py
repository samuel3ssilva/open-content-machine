"""Gate D §9.2 demonstration + §10 regression (commit 2): prove the
synthetic connector pipeline's output enters the EXISTING Intelligence Brief
pipeline with ZERO changes to ranking or rendering.

Sequence exercised, exactly as spec §9.2 names it: discovery -> triage ->
authored assessment (fixture-provided, human_authored) -> to_source_item ->
existing cluster -> rank -> tier -> brief. Nothing in this test (or anywhere
in ``connectors/``) imports or modifies ``brief.py``'s/``weekly.py``'s
behavior -- every intelligence-side call below is the exact, unmodified
public function a caller outside ``connectors`` would use.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from content_machine.connectors.bridge import (
    AssessmentProvenance,
    AuthoredAssessment,
    to_source_item,
)
from content_machine.connectors.models import triage
from content_machine.connectors.permissions import (
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)
from content_machine.connectors.registry import (
    PublisherClassification,
    SourceRegistry,
    SourceRegistryEntry,
)
from content_machine.connectors.runner import BatchDiscoveryRequest, BatchStatus, run_discovery
from content_machine.connectors.synthetic.adapters import SuccessfulSyntheticAdapter
from content_machine.intelligence.brief import build_weekly_brief, render_markdown
from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.loader import load_profile, load_signals
from content_machine.intelligence.models import RelevanceProfile
from content_machine.intelligence.ranking import rank_topics
from content_machine.intelligence.tiers import assign_tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"

_ALL_PERMITTED_FIELDS = frozenset(
    {"title", "canonical_reference", "content_type", "publication_date", "summary_normalized"}
)


def test_synthetic_discovery_reaches_the_existing_pipeline_with_zero_changes() -> None:
    """discovery -> triage -> authored assessment -> to_source_item ->
    cluster -> rank -> tier -> brief, using ONLY the connectors package's own
    public API for the first four steps and the intelligence package's
    UNMODIFIED public API for the rest."""
    source_id = "src_demo_vendor"
    registry_entry = SourceRegistryEntry(
        source_id=source_id,
        source_group="synthetic",
        publisher_id="vendor-demo",
        source_category="vendor_blog",
        source_type="feed",
        publisher_classification=PublisherClassification.independent,
        endpoint_label="demo endpoint",
    )
    source_registry = SourceRegistry([registry_entry])
    permission_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=_ALL_PERMITTED_FIELDS,
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
            )
        ]
    )

    # 1. discovery
    batch_request = BatchDiscoveryRequest(
        window_start=date(2026, 7, 11), window_end=date(2026, 7, 18)
    )
    batch_result = run_discovery(
        [SuccessfulSyntheticAdapter(source_id)],
        permission_registry,
        source_registry,
        batch_request,
        occurred_at=datetime(2026, 7, 18, 10, 0, tzinfo=UTC),
    )
    assert batch_result.status == BatchStatus.all_succeeded
    assert len(batch_result.results) == 1

    # 2. triage
    candidates = triage(
        batch_result.results, profile_tags=["agents", "harnesses"], max_candidates=5
    )
    selected = [c for c in candidates if c.selected]
    assert len(selected) == 1
    assert (
        selected[0].discovery_result.canonical_reference
        == batch_result.results[0].canonical_reference
    )

    # 3. authored assessment (human_authored, fixture-provided)
    assessment = AuthoredAssessment(
        evidence_type="announcement",
        change_class="material_change",
        change_class_rationale="VendorAlpha's own blog post announcing the packaging format",
        action_required="new_option_available",
        experiment_affordance="local_reproducible",
        contains_benefit_or_performance_claim=False,
        claim_directly_verifiable_in_artifact=True,
        topic_tags=("agents", "skills"),
        subject_entity_ids=(registry_entry.publisher_id,),
        provenance=AssessmentProvenance.human_authored,
    )

    # 4. to_source_item -- the ONLY connector-output -> SourceItem path
    item = to_source_item(
        selected[0].discovery_result,
        assessment,
        registry_entry,
        detection_date=date(2026, 7, 18),
        permission_registry=permission_registry,
    )
    assert item.publisher_id == registry_entry.publisher_id
    assert item.topic_tags == ["agents", "skills"]

    # 5-8. existing, UNMODIFIED M1-M7 pipeline: cluster -> rank -> tier -> brief
    items_by_id = {item.item_id: item}
    clusters = cluster_items([item])
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    inputs = [to_ranking_inputs(c, items_by_id) for c in clusters]
    profile = RelevanceProfile(
        profile_version="v1",
        territories=[{"tag": "agents", "priority": 5}, {"tag": "skills", "priority": 3}],
        live_questions=[],
        current_tooling=[],
        experiment_budget="medium",
    )
    ranked = rank_topics(inputs, profile)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, "2026-W29")

    assert brief.week_label == "2026-W29"
    assert len(clusters) == 1
    assert clusters[0].anchor_item_id == item.item_id
    assert ranked[0][0].topic_id == clusters[0].topic_id
    # Renders without error, through the completely unmodified renderer.
    markdown = render_markdown(brief)
    assert "2026-W29" in markdown


# --- §10 regression: a fixed fixture's existing ranking/tier/brief output --
# --- is BYTE-IDENTICAL after Gate D (proves connectors/ changed nothing) ---


def test_golden_existing_synthetic_fixture_output_is_unchanged_by_gate_d() -> None:
    """Pins the CURRENT, pre-Gate-D output of the existing
    examples/intelligence-signals-synthetic.json pipeline (loader -> cluster
    -> rank -> tier -> brief -- exactly test_intelligence_guarantees.py's own
    sequence). Gate D adds new files under connectors/ only and touches
    nothing under intelligence/, so this must remain byte-identical for as
    long as that additive-only guarantee holds; a change here means
    something in ``intelligence/`` moved, which Gate D must never do."""
    result = load_signals(VALID_FIXTURE)
    profile = load_profile(PROFILE_FIXTURE)
    clusters = cluster_items(result.items)
    items_by_id = {item.item_id: item for item in result.items}
    clusters_by_topic_id = {c.topic_id: c for c in clusters}
    inputs = [to_ranking_inputs(c, items_by_id) for c in clusters]
    ranked = rank_topics(inputs, profile)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)
    brief = build_weekly_brief(tiered, ranked, clusters_by_topic_id, items_by_id, "2026-W29")
    markdown = render_markdown(brief)

    # Structural counts, re-verified unchanged after Gate E0 (R4): 22
    # clusters / 22 ranked / 10 tiered / 3 tier_1 / same top topic_id and
    # score. Nothing here moved, which is the proof the security-flag
    # propagation (E0.1) and may_supply_independence narrowing (E0.3) are
    # purely additive on this fixture (it has no security-flagged items and
    # no registry-denied-independence items, so neither change alters any
    # existing computation).
    assert len(clusters) == 22
    assert len(ranked) == 22
    assert len(tiered) == 10
    assert sum(1 for t in tiered if t.tier_assignment.tier == "tier_1") == 3
    assert ranked[0][0].topic_id == "t_d19f893857e2"
    assert ranked[0][1].score == 80

    # Gate E0 re-pin (R4): the hashes below MUST change, and did, because
    # WeeklyBrief/Tier1LeanItem/Tier2Item/Tier1AppendixRecord all gained new
    # fields (security_flag_summary, adversarial_security_flags) and
    # BRIEF_VERSION was bumped (product ruling P4) -- both additive schema
    # changes, not a change in any ranking/tiering/evidence computation, as
    # the unchanged structural counts above prove. Old hashes (pre-Gate-E0):
    # markdown "695a5f6854654e8ef4b09a77258083efe43daee21c54e60cd64eb3b4c46f4d7b",
    # json "860ac9d6c85896fba281925fbc1afd66cdab5b8cd0312766bfbb6bcb6c070bfe".
    #
    # Fable ruling 2026-08-01 (Part B, "independence must stop being rendered
    # as corroboration") re-pin: the hashes below changed again, and MUST
    # have, because this is exactly the defect the ruling fixes -- on this
    # real fixture, the rank-3 topic ("Independent Benchmark Study Measures
    # VendorA Harness Reliability", evidence_anchor_id
    # evid_4_independent_rigorous_alone, a SINGLE independent source) used to
    # be scored confidence=high with the reason text "genuine independent
    # corroboration is present", even though it has zero cross-source
    # corroboration. It now correctly scores confidence=medium. Score, tier
    # admission, claim_class, and the evidence rubric are unchanged (see the
    # unchanged structural counts above and unchanged
    # ranked[0][1].score == 80): only the confidence/independence PROSE and
    # the one topic's confidence LEVEL changed. Old hashes (pre-Part-B):
    # markdown "dced152158d2c669e7dd0cf624b2c5c4df5a46106ae3effe466b635d7f4ce30d",
    # json "6ffa84f2440007140a02a5c6b8731792feb85541952277b4a587dee481e65cbf".
    #
    # Fable ruling 2026-08-01 (follow-up, Part C, "the mirror-image prose
    # defect") re-pin: the hashes below changed again -- this is item C's
    # authorized exception to the "do not touch the golden-hash re-pin"
    # instruction, since Part C's tiers.py catch-all fix changes rendered
    # confidence_reason text. On this real fixture, two topics reach the
    # catch-all branch: rank 2 ("Core Project Spec: Breaking Change To Tool
    # Call Framing", Tier 1) and rank 5 ("VendorC Deprecates Legacy Harness
    # Plugin API", Tier 2). Both used to say "attested by an artifact, but no
    # second, distinct source supports the claim at the high-confidence bar"
    # -- both topics have independent_publisher_count 0 or 1 on this fixture,
    # so that old text was not actually false here, but the fix removes the
    # single-sourcing claim from the catch-all branch UNCONDITIONALLY (it is
    # false whenever independent_publisher_count >= 2 -- see
    # test_confidence_two_independent_publishers_below_level_4_does_not_claim_single_source
    # in test_intelligence_tiers.py for that exact case). Confidence LEVEL
    # (medium), claim_class, score, and tier admission are all unchanged for
    # both topics -- see the unchanged structural counts above and unchanged
    # ranked[0][1].score == 80: only the confidence-reason PROSE changed.
    # Old hashes (pre-Part-C):
    # markdown "a83aa65ea1c38b987bca8b64403e9a2c478540ab3686799b7250e567857c89be",
    # json "348db9d34fed658ee92bdc96e9e85899b9ad1a102ce412bfdca98f5f367cd5e0".
    #
    # Round 3 (Fable final-recheck defect, F1+F2) re-pin: the hashes below
    # changed again. F1 (tiers._assess_confidence's has_cross_source_
    # corroboration predicate now also requires independent_publisher_count
    # >= 1 alongside a _CORROBORATED_ANCHORS membership) changes NOTHING on
    # this fixture -- verified directly against the per-topic dump below --
    # because this fixture has no may_supply_independence=False items (see
    # the R4 comment above), and every non-denied member whose evidence_type
    # drives a _CORROBORATED_ANCHORS anchor also, by construction, satisfies
    # _is_independent and therefore independent_publisher_count >= 1. Only
    # F2 (the branch-(d) catch-all text, changed unconditionally from
    # "evidence_level is below the >= 4 bar that high confidence requires"
    # -- false at evidence_level == 4, the exact round-3 defect -- to "does
    # not meet the high-confidence bar (evidence_level >= 4 together with
    # cross-source corroboration)") moved anything here, and it moved
    # exactly the two topics the Part-C re-pin comment above already names
    # as reaching that catch-all branch on this fixture: rank 2 ("Core
    # Project Spec: Breaking Change To Tool Call Framing", Tier 1,
    # evidence_level=3) and rank 5 ("VendorC Deprecates Legacy Harness
    # Plugin API", Tier 2, evidence_level=3). For both, the OLD text
    # ("evidence_level is below the >= 4 bar...") was not actually false on
    # this fixture (both are below 4), so this re-pin is prose-only, not a
    # bugfix on this fixture. Confidence LEVEL (medium for both), claim_class
    # (fact), score, and tier admission are unchanged for every topic -- see
    # the unchanged structural counts above, unchanged ranked[0][1].score ==
    # 80, and the full per-topic (claim_class, confidence) dump used to
    # derive this re-pin, which is identical to the pre-round-3 dump except
    # for the two confidence_reason strings named above. Old hashes
    # (pre-round-3):
    # markdown "76f856ea357fc931a029b05ba110f3477ed5e00a4918e701f565d868df9eea45",
    # json "3e782c404ca69aab5ced740d2667c0b4bca565d992f002a57c28a20249c20b34".
    #
    # Post-merge-gate round (2026-08-01, ADR 0007/0009) re-pin: the hashes
    # below changed again, and MUST have -- this round bumped both version
    # markers that a prior round left unbumped despite changing rendering
    # semantics (brief.BRIEF_VERSION "gate-e0-m5-2" -> "gate-e0-m5-3"; the new
    # additive tiers.CONFIDENCE_RUBRIC_VERSION field), and fixed rendering
    # itself: P1 (Tier 2's principal-evidence line was ungrammatical --
    # _human_principal_evidence renamed to the shared _human_evidence_sentence
    # with a verb-clause independence phrase instead of a comma-spliced noun
    # phrase), P2 (Tier 1's evidence_and_confidence now routes through that
    # same builder instead of piping tiers.py's raw confidence_reason audit
    # string into reader-facing prose), G1 (the executive summary's marketing
    # sentence dropped its corroboration-scoping trailing clause and gained a
    # new Top-N single-source sentence), and G3 (single-sourced content
    # opportunities now disclose it in their own reason line) all change
    # brief.md text on this fixture, and the new
    # confidence_rubric_version/corroboration_methodology_note fields (G2)
    # change brief.json. Re-verified directly, same method as every prior
    # re-pin comment above: clusters/ranked/tiered/tier1 counts and the top
    # topic's id+score are UNCHANGED (see the asserts above this one) --
    # nothing in cluster.py/ranking.py/tiers.py's admission or scoring logic
    # moved; only rendered prose and additive schema fields did. Old hashes
    # (pre-this-round):
    # markdown "50b221d238e6bc29107798ca34104ed91c8c7ac2c4adb63924d1a56b915da151",
    # json "2bae3f0a4910a2e1b8196b60288d932eb853b4a34ec6c86e9aefe5eed817e8c7".
    #
    # Same-round correction: the FIRST version of G1/G3's single-source
    # predicate above approximated "single-sourced" as
    # independent_publisher_count <= 1, which wrongly counted this
    # fixture's own top-ranked, high-confidence topic (corroborated via the
    # evid_4_first_party_plus_independent anchor with
    # independent_publisher_count == 1 -- a first-party leg plus one
    # independent leg) as single-sourced, contradicting the "high
    # confidence" it is given one sentence earlier in the same executive
    # summary. Fixed by delegating to the new
    # tiers.has_cross_source_corroboration (the exact predicate
    # tiers._assess_confidence already uses to gate 'high' confidence)
    # instead of re-deriving an approximation -- on this fixture the
    # Top-10 single-source count changed from 10/10 to 8/10, correctly
    # excluding the two anchor-corroborated topics. Old hashes (pre-fix,
    # same round):
    # markdown "b58d377fd9d6c4fb7947f2f895e58251c97bd0d79d87bfdd6104ba5811d40e48",
    # json "31d4c1822d1b41191cd37f95290af6297f35e4095dde2122c5757e4f72bc900f".
    #
    # Round 5 re-pin (2026-08-01, #5 -- same structural-count-proof standard
    # as the previous four): the hashes below changed again, and MUST have --
    # this round bumped BRIEF_VERSION ("gate-e0-m5-3" -> "gate-e0-m5-4",
    # document wording only, CONFIDENCE_RUBRIC_VERSION unchanged) and fixed
    # five more rendering/wording defects, verified per-topic against this
    # exact fixture (the per-topic (rank, evidence_anchor_id, claim_class,
    # confidence) dump behind this re-pin is unchanged from the pre-round-5
    # dump except for the rendered text named below -- ranks/tiers/scores did
    # not move):
    #   - F1: tiers._classify_claim_class's fact-branch reason text changed
    #     for every fact-classified topic's claim_class_reason (ranks 1, 2,
    #     3, 4, 5, 10 on this fixture are claim_class='fact'; only ranks 1-3
    #     render it, in the Tier 1 appendix -- ranks 4/5/10 carry the same
    #     structured field but Tier2Item/RadarItem never expose it).
    #   - P1: the shared evidence sentence is now two sentences instead of
    #     one comma/"and"-joined sentence, for every Tier 1/2 topic (ranks
    #     1-7); AND evidence_level 4's phrase is now resolved per anchor
    #     instead of an unresolved disjunction, affecting ranks 1 and 4
    #     (evid_4_first_party_plus_independent -> "a first-party source
    #     corroborated by independent evidence") and rank 3
    #     (evid_4_independent_rigorous_alone -> "rigorous independent
    #     evidence"). Ranks 2, 5, 6, 7, 8, 9, 10 are not at evidence_level 4,
    #     so only the sentence-split applies to them (ranks 6-9's evidence
    #     phrase text itself is unchanged; rank 10 is Tier 3, a different
    #     paragraph builder untouched by this line).
    #   - P2: Tier2Item gains recommended_action_reason, rendered next to
    #     Tier 2's "Recommended action" line -- ranks 4-7.
    #   - P3: the executive summary's Top-N single-source sentence gains a
    #     "(see appendix for method)" pointer -- one sentence, run-level.
    #   - P4: ContentOpportunity's single-sourced disclosure moved from a
    #     `reason`-suffix to a structured field rendered on its own Markdown
    #     line -- this fixture's 3 content opportunities are ranks 1, 2, 3;
    #     rank 1 is cross-source corroborated (no disclosure either before or
    #     after), ranks 2 and 3 are single-sourced (disclosure text
    #     unchanged, only its line position moved).
    #   - P5: Tier 1's why_it_matters no longer restates
    #     "{claim_class} at {confidence} confidence" (ranks 1-3); Tier 2's
    #     bare "- **Confidence:** {level}" bullet is removed (ranks 4-7).
    # Confidence LEVEL, claim_class, score, and tier admission are unchanged
    # for every topic -- see the unchanged structural counts above and
    # unchanged ranked[0][1].score == 80. Old hashes (pre-round-5):
    # markdown "232f204e03c74cbb49f5e134c786671a2688e9b9f9355919f38575af219978ac",
    # json "8a2b830f28e5065198e6f076caab9bdc3f4e2fbda61e56c9793687c33f1d8442".
    #
    # Round 6 re-pin (2026-08-01, F4 -- gate ``_evidence_level_phrase`` on
    # ``inputs.has_independent_evidence`` for ``evid_4_first_party_plus_
    # independent``): the JSON hash below changed, the markdown hash did
    # NOT. Verified directly (not merely asserted): (1) the markdown hash is
    # BYTE-IDENTICAL to its pre-round-6 value, which is the mechanical proof
    # F4's gate is a genuine no-op on this fixture's rendered Markdown --
    # both of this fixture's level-4 anchor topics (ranks 1 and 4,
    # ``evid_4_first_party_plus_independent``) have
    # ``independent_publisher_count == 1`` (registry-permitted, not
    # denied), so ``inputs.has_independent_evidence`` is True for both and
    # they take the same affirmative phrase branch as before F4 -- F4 only
    # changes behavior for a registry-DENIED sole independent leg, which
    # this fixture does not contain. (2) The JSON hash moved SOLELY because
    # ``WeeklyBrief.brief_version`` embeds ``BRIEF_VERSION``, bumped
    # ``"gate-e0-m5-4"`` -> ``"gate-e0-m5-5"`` (F5, document-version-marker
    # bookkeeping only) -- confirmed by substituting the single token
    # ``"gate-e0-m5-5"`` back to ``"gate-e0-m5-4"`` in the current
    # ``brief.model_dump_json()`` output (the token occurs exactly once) and
    # reproducing the prior pinned JSON hash below byte-for-byte, with ZERO
    # other differing bytes. Structural counts, top topic id/score, and the
    # markdown hash are all unchanged (see the asserts above and this
    # comment) -- nothing in cluster.py/ranking.py/tiers.py's admission or
    # scoring logic moved, and no rendered Markdown prose moved either; only
    # the JSON-embedded version marker did. Old JSON hash (pre-round-6):
    # "36433ca24801bce184e842e13ed851531d0e7d8961e4f22f01e1a5482290060e"
    # (markdown hash unchanged, still "98b9609314cf...846493" below).
    #
    # Round 7 re-pin (2026-08-01, this branch): BOTH hashes changed, and
    # MUST have -- this round bumped BRIEF_VERSION ("gate-e0-m5-5" ->
    # "gate-e0-m5-6", document wording/structure only,
    # CONFIDENCE_RUBRIC_VERSION unchanged) and fixed real rendering defects,
    # verified per-topic against this exact fixture the same way as every
    # prior re-pin (structural counts/top-topic id+score unchanged, see the
    # asserts above -- nothing in cluster.py/ranking.py/tiers.py's admission
    # or scoring logic moved):
    #   - F6: CORROBORATION_METHODOLOGY_NOTE's final sentence corrected (run
    #     -level, prints once in the appendix and once in brief.json's
    #     corroboration_methodology_note).
    #   - F7: tiers._recommend_action's fact/medium reason text reshaped
    #     (this fixture's only fact/medium Tier 2 topic is rank 5, "VendorC
    #     Deprecates Legacy Harness Plugin API" -- its Tier 2 body
    #     "Recommended action" line and its tldr/appendix-adjacent uses
    #     change); _build_study_queue's light-study reason now states WHEN
    #     as well as WHY, and branches correctly on the topic's OWN
    #     recommended_action instead of assuming every light-study pick is
    #     a 'save' topic (this fixture's two light-study picks are rank 4,
    #     fact/high/'read', and rank 5, fact/medium/'save' -- both lines
    #     change, each with the text appropriate to its own action).
    #   - S1/S2: _build_tldr's three Tier 1 lines (ranks 1-3) no longer
    #     embed a literal "{rank}." inside the bullet or repeat the one
    #     constant Tier-1 reason verbatim -- each now states its own rank
    #     and score.
    #   - S3: Tier 2/3 headings changed from "## Should Know"/"## Radar" to
    #     "## Tier 2 -- Should Know"/"## Tier 3 -- Radar".
    #   - S4: the appendix heading changed from "## Appendix -- Full Tier 1
    #     Records" to "## Appendix -- Tier 1 Records & Methodology Notes".
    #   - S5: the appendix "evidence" dimension line for rank 3
    #     ("Independent Benchmark Study Measures VendorA Harness
    #     Reliability", evidence_anchor_id evid_4_independent_rigorous_
    #     alone -- this fixture's only topic reaching that anchor) now
    #     resolves its disjunction to the one type this topic's own
    #     experiment-dimension line already names (evidence_types=
    #     ['benchmark_with_methodology']) instead of listing all three
    #     possible types.
    #   - S6: STUDY_SELECTION_RULE and CONTENT_OPPORTUNITY_SELECTION_RULE
    #     (both run-level, printed once each as the Study Queue/Content
    #     Opportunities "Selection rule" lines) no longer cite "tiers.py"/
    #     "the Founder spec" by name.
    # Confidence LEVEL, claim_class, score, and tier admission are unchanged
    # for every topic -- see the unchanged structural counts above and
    # unchanged ranked[0][1].score == 80. Old hashes (pre-round-7):
    # markdown "98b9609314cf60e2f6b9d0df6e332d66e09eded987a235c5098b0faafc846493",
    # json "2eafaa91a695a100cbb8768e0848cfe40b6dfe487d774e829c6064a7e0ed0feb".
    assert (
        hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        == "c595500f06cbf45aa37ff2ec00831fc6f7f1a11ca5fc0aee3ad9b9d76677f25d"
    )
    assert (
        hashlib.sha256(brief.model_dump_json().encode("utf-8")).hexdigest()
        == "49cfdaa1cd4066bc03d49021e9283ebf553f5cbcacf6507ccf9f0039e22d0172"
    )
