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
    assert (
        hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        == "50b221d238e6bc29107798ca34104ed91c8c7ac2c4adb63924d1a56b915da151"
    )
    assert (
        hashlib.sha256(brief.model_dump_json().encode("utf-8")).hexdigest()
        == "2bae3f0a4910a2e1b8196b60288d932eb853b4a34ec6c86e9aefe5eed817e8c7"
    )
