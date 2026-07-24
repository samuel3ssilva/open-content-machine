"""Deterministic topic clustering over :class:`SourceItem` records.

Depends only on ``content_machine.intelligence.models`` and
``content_machine.intelligence.normalize`` -- never on ``ranking.py``. Every
collection is sorted before iteration so output never depends on input order
(shuffling the input list produces byte-identical clusters, just possibly
discovered via different intermediate edges).

Merge rules (an edge is drawn between two items when either fires):

1. Identical ``canonical_reference`` (see
   :func:`content_machine.intelligence.normalize.normalize_canonical_reference`)
   -- reason ``"same_canonical_reference"``.
2. Jaccard similarity of normalized title tokens >= 0.6 AND the two items'
   ``subject_entity_ids`` intersect -- reason
   ``"title_similarity_and_shared_subject"``. The shared-subject requirement
   is the OVER-MERGE GUARD: two items with near-identical titles but
   different subjects (e.g. "VendorA Sandbox 1.0" vs "VendorB Sandbox 1.0")
   must never merge on title similarity alone.

Clusters are the connected components of the graph these edges define, which
makes the partition itself independent of processing order: it depends only
on the *set* of edges, not on the order operations are applied.

Within each cluster, two collapse mechanisms mark non-origin copies so they
contribute nothing to the evidence rubric:

* ``member_role="duplicate"`` -- members that share an identical canonical
  reference with another member (a same-URL mirror).
* ``member_role="syndicated"`` -- members whose summary token signature has
  Jaccard >= 0.85 against another member (a near-identical restatement),
  computed as the connected components of that similarity relation, not a
  single earlier-vs-later pairwise check.

For BOTH mechanisms, the ORIGIN of a collapse group -- the one member that is
NOT marked duplicate/syndicated -- is CONTENT-determined, never
date-determined (Gate A correction round 2, R2/R3): the member published BY
a cluster subject if one exists in the group (a vendor is the origin of its
own announcement, regardless of publication date), otherwise the earliest
member by the shared canonical order (see ``_canonical_order_key``). Picking
the origin by date alone let two byte-identical artifacts -- one first-party,
one third-party -- flip which one counts as independent evidence purely
because of which was dated earlier; picking by content (subject-published
wins) makes that flip impossible: a republished/paraphrased copy of a
vendor's own announcement is correctly excluded as syndicated no matter which
one happens to carry the earlier date. ``_select_anchor``, ``_duplicate_ids``,
and ``_syndicated_ids`` all iterate members in that SAME canonical order --
never in ``item_id`` order, which was the earlier source of inconsistency.

Both ``syndicated`` and ``duplicate`` members contribute ZERO to
``independent_publisher_count``, ``has_independent_evidence``,
``has_first_party_authoritative``, and the evidence rubric generally --
appending a syndicated copy or a duplicate-URL mirror must never change what
a cluster's evidence supports. Evidence types ``roundup``/``relay`` never
count toward independence at any similarity (they are excluded from the
independent evidence-type set entirely, not just deduplicated); they also
never become the cluster ANCHOR when any other (non-``roundup``/``relay``)
member is present in the cluster -- see ``_select_anchor`` -- so
``change_class``, ``action_required``, and ``experiment_affordance`` (which
are read off the anchor) cannot be authored by a relay/roundup copy unless
every member in the cluster is one.

NOTE: relay/syndicated/duplicate members still contribute their own
``publication_date``/``detection_date`` to ``first_seen``/``last_seen`` --
that feeds only the ranking tie-break key (ordering), never any dimension's
points.

Independence is per (item, cluster-subject) pair::

    is_independent = (
        publisher_id not in cluster.subject_entity_ids
        and evidence_type in {
            "independent_analysis", "benchmark_with_methodology",
            "independent_implementation", "research_paper",
        }
    )

IDENTITY (documented per the frozen design): ``topic_id`` is
``"t_" + sha256(anchor_canonical_reference + "|" + sorted_subject_ids)[:12]``,
where the anchor is chosen by ``_select_anchor`` (see above). ``cluster_fingerprint``
is a full sha256 over the sorted canonical member references; it changes as
members accrete and is used ONLY for change detection, NEVER as identity.
Persistent cross-run identity (stable ``topic_id`` across re-runs as new items
append over time) is out of scope for Gate A and lands at M6 -- this module's
``topic_id`` is only guaranteed stable across multiple *identical* invocations
of :func:`cluster_items`, not across a corpus that has grown.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date

from content_machine.intelligence.models import RankingInputs, SourceItem, TopicCluster
from content_machine.intelligence.normalize import (
    jaccard,
    normalize_canonical_reference,
    token_signature,
)

_JACCARD_TITLE_THRESHOLD = 0.6
_JACCARD_SYNDICATION_THRESHOLD = 0.85

# Evidence types that count as independent corroboration when published by a
# non-subject publisher (evidence rubric, levels 4/5). CRITICAL (Gate A
# correction round 2, R1): non-subject "authoritative" types and self-published
# "artifact" types (below) are deliberately NOT in this set -- they raise the
# evidence LEVEL but must never flip has_independent_evidence/Tier-1 admission
# on their own. See
# test_non_subject_authoritative_and_first_party_artifact_never_flip_independence.
_INDEPENDENT_EVIDENCE_TYPES = frozenset(
    {
        "independent_analysis",
        "benchmark_with_methodology",
        "independent_implementation",
        "research_paper",
    }
)
# "official_doc|spec_change|deprecation_notice|security_advisory|
# official_api_behavior_change" -- the AUTHORITATIVE evidence types.
# Published BY a cluster subject: evidence anchor 3 ("first-party
# authoritative"). Published by a THIRD PARTY about a subject (e.g. a
# standards body's spec change, or a security advisory about a vendor): also
# evidence anchor 3 ("non-subject authoritative") -- real, uncorroborated
# third-party evidence, distinct from independent analysis or rigorous
# corroboration, but never level 0 (Gate A correction round 2, R1).
# ``official_api_behavior_change`` was added for Founder decision D1 (see
# ranking.py's Tier-1 eligibility docstring for the M4 contract that names
# it); it is authoritative here for the same reason official_doc/spec_change
# are, and is exercised by the fixture item using it.
_AUTHORITATIVE_TYPES = frozenset(
    {
        "deprecation_notice",
        "security_advisory",
        "official_doc",
        "spec_change",
        "official_api_behavior_change",
    }
)
# "announcement|release_note" published BY the subject -- evidence anchor 2
# (marketing_risk is set here, and ONLY here).
_FIRST_PARTY_PROMOTIONAL_TYPES = frozenset({"announcement", "release_note"})
# "announcement|release_note" published by a NON-subject, with no other
# qualifying evidence in the cluster -- Founder decision D2: isolated,
# uncorroborated secondary news about someone else is weak, single-source
# evidence, evidence anchor 1 (``evid_1_secondary_news_uncorroborated``,
# distinct from ``evid_1_rumor``). If the SAME item is clustered with a
# first-party authoritative/artifact member or genuine independent evidence,
# those higher branches fire instead -- this flag only decides the anchor
# when it is the cluster's BEST signal.
_SECONDARY_NEWS_TYPES = frozenset({"announcement", "release_note"})
# "independent_implementation|benchmark_with_methodology|research_paper" --
# the RIGOROUS/reproducible types. Published BY the subject: a self-published
# runnable/rigorous artifact (evidence anchor 3, the item015 class -- still
# real evidence, but self-published, so it never satisfies level 5's
# independent-methodology leg -- R6). Published by a non-subject: independent
# rigorous corroboration (evidence anchor 4, or the methodology leg of 5).
_RIGOROUS_TYPES = frozenset(
    {"independent_implementation", "benchmark_with_methodology", "research_paper"}
)
_RELAY_EVIDENCE_TYPES = frozenset({"roundup", "relay"})


# ---------------------------------------------------------------------------
# Union-find over item_id, used only to compute connected components. The
# specific root chosen for a component is an implementation detail discarded
# once members are grouped -- it does not affect the resulting partition.
# ---------------------------------------------------------------------------


def _find(parent: dict[str, str], item_id: str) -> str:
    root = item_id
    while parent[root] != root:
        root = parent[root]
    while parent[item_id] != root:
        parent[item_id], item_id = root, parent[item_id]
    return root


def _union(parent: dict[str, str], a: str, b: str) -> None:
    root_a, root_b = _find(parent, a), _find(parent, b)
    if root_a != root_b:
        parent[root_b] = root_a


def _effective_date(item: SourceItem) -> date:
    return item.publication_date or item.detection_date


def _is_relay(item: SourceItem) -> bool:
    return item.evidence_type in _RELAY_EVIDENCE_TYPES


def _canonical_order_key(item: SourceItem) -> tuple[int, date, str]:
    """The ONE deterministic ordering shared by anchor selection and both
    collapse mechanisms (duplicate-by-URL, syndicated-by-text): non-roundup/
    relay members first, then ``(effective_date, stable_reference)``. Never
    ``item_id`` order -- see the module docstring (R2)."""
    return (1 if _is_relay(item) else 0, _effective_date(item), item.stable_reference)


def _earliest_by_canonical_order(members: list[SourceItem]) -> SourceItem:
    return sorted(members, key=_canonical_order_key)[0]


def _group_origin(group: list[SourceItem], subject_entity_ids: frozenset[str]) -> SourceItem:
    """Content-determined origin of a collapse group (duplicate-by-URL or
    syndicated-by-text): the member published BY a cluster subject if one
    exists in the group (a vendor is the origin of its own content,
    regardless of date); otherwise the earliest member by the shared
    canonical order. Deliberately NOT date-first: date-first origin selection
    is exactly what let a byte-identical (vendor announcement, third-party
    analysis) pair flip evidence_level/marketing_risk/tier1_eligible purely
    on which one happened to be dated earlier (Gate A correction round 2,
    R2/R3)."""
    subject_published = [m for m in group if m.publisher_id in subject_entity_ids]
    pool = subject_published if subject_published else group
    return _earliest_by_canonical_order(pool)


def _is_independent(item: SourceItem, subject_entity_ids: frozenset[str]) -> bool:
    return (
        item.publisher_id not in subject_entity_ids
        and item.evidence_type in _INDEPENDENT_EVIDENCE_TYPES
    )


def _topic_id(anchor: SourceItem, subject_entity_ids: list[str]) -> str:
    canonical_anchor_ref = normalize_canonical_reference(anchor.stable_reference)
    payload = f"{canonical_anchor_ref}|{','.join(sorted(subject_entity_ids))}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"t_{digest[:12]}"


def _cluster_fingerprint(members: list[SourceItem]) -> str:
    refs = sorted(normalize_canonical_reference(m.stable_reference) for m in members)
    return hashlib.sha256(",".join(refs).encode("utf-8")).hexdigest()


def _duplicate_ids(
    members_sorted: list[SourceItem], subject_entity_ids: frozenset[str]
) -> set[str]:
    """Non-origin members that share an identical canonical reference with at
    least one other member in the cluster. The origin of each same-reference
    group is content-determined (see ``_group_origin``), never the overall
    cluster anchor -- so an appended earlier-dated same-URL duplicate can
    never "take over" and rewrite which member the evidence rubric sees
    (Gate A correction round 2, R3)."""
    ref_groups: dict[str, list[SourceItem]] = defaultdict(list)
    for member in members_sorted:
        ref_groups[normalize_canonical_reference(member.stable_reference)].append(member)
    duplicate_ids: set[str] = set()
    for group in ref_groups.values():
        if len(group) <= 1:
            continue
        origin = _group_origin(group, subject_entity_ids)
        duplicate_ids |= {m.item_id for m in group if m.item_id != origin.item_id}
    return duplicate_ids


def _syndication_groups(members_sorted: list[SourceItem]) -> list[list[SourceItem]]:
    """Connected components of the "near-identical summary" relation (Jaccard
    >= 0.85 over token signatures) -- every member reachable from another via
    a chain of pairwise-similar summaries collapses into one group, rather
    than only checking each member against members earlier in some arbitrary
    order."""
    parent = {m.item_id: m.item_id for m in members_sorted}
    signatures = {m.item_id: token_signature(m.summary_normalized) for m in members_sorted}
    for i in range(len(members_sorted)):
        for j in range(i + 1, len(members_sorted)):
            item_a, item_b = members_sorted[i], members_sorted[j]
            if (
                jaccard(signatures[item_a.item_id], signatures[item_b.item_id])
                >= _JACCARD_SYNDICATION_THRESHOLD
            ):
                _union(parent, item_a.item_id, item_b.item_id)
    groups: dict[str, list[SourceItem]] = defaultdict(list)
    for member in members_sorted:
        groups[_find(parent, member.item_id)].append(member)
    return [group for group in groups.values() if len(group) > 1]


def _syndicated_ids(
    members_sorted: list[SourceItem], subject_entity_ids: frozenset[str]
) -> set[str]:
    """Non-origin members of a near-identical-summary group (see
    ``_syndication_groups``). The origin is content-determined (see
    ``_group_origin``): a non-subject member with an "independent" evidence
    type is still syndicated -- and therefore excluded from evidence -- when
    its text is a near-identical restatement of the group's origin, because
    republishing is not analysing (Gate A correction round 2, R2)."""
    syndicated: set[str] = set()
    for group in _syndication_groups(members_sorted):
        origin = _group_origin(group, subject_entity_ids)
        syndicated |= {m.item_id for m in group if m.item_id != origin.item_id}
    return syndicated


def _assign_roles(
    members_sorted: list[SourceItem],
    anchor_id: str,
    duplicate_ids: set[str],
    syndicated_ids: set[str],
) -> dict[str, str]:
    roles: dict[str, str] = {}
    for member in members_sorted:
        if member.item_id == anchor_id:
            roles[member.item_id] = "primary"
        elif member.item_id in duplicate_ids:
            roles[member.item_id] = "duplicate"
        elif member.item_id in syndicated_ids:
            roles[member.item_id] = "syndicated"
        elif member.evidence_type in _RELAY_EVIDENCE_TYPES:
            roles[member.item_id] = "relay"
        else:
            roles[member.item_id] = "independent"
    return roles


# Roles excluded from every evidentiary signal: a syndicated copy (near-
# identical restatement) or a duplicate-URL mirror must never change what a
# cluster's evidence supports -- only "primary"/"independent"/"relay" members
# (real, distinct sources) count.
_EVIDENCE_EXCLUDED_ROLES = frozenset({"syndicated", "duplicate"})


def _evidence_level_and_marketing_risk(
    members_sorted: list[SourceItem],
    member_roles: dict[str, str],
    subject_entity_ids: frozenset[str],
) -> tuple[int, bool, bool, int, str, bool]:
    """Compute the evidence rubric (0-5) plus its anchor id, ``marketing_risk``,
    whether a first-party-authoritative source is present, the distinct
    independent publisher count, and (Founder decision D3)
    ``has_direct_artifact_or_independent_source`` -- all excluding syndicated
    and duplicate members, which contribute zero to every evidentiary signal.

    TOTAL over evidence_type x publisher-polarity (Gate A correction round 2,
    R1): every one of the 14 evidence types, in either polarity, lands above
    0 for a single-member cluster EXCEPT ``roundup``/``relay`` (never
    evidentiary) -- see ``test_evidence_rubric_totality_matrix``. Presence
    flags, computed only over evidence-counting members (role not in
    ``_EVIDENCE_EXCLUDED_ROLES``):

    * ``first_party_authoritative``  -- publisher IS a cluster subject, type
      in ``_AUTHORITATIVE_TYPES``.
    * ``non_subject_authoritative``  -- SAME types, published by a THIRD
      PARTY (e.g. a standards body's spec change about a vendor, or a
      security advisory about one). Raises the evidence level but is
      deliberately NOT added to ``_INDEPENDENT_EVIDENCE_TYPES`` -- it does
      not flip ``has_independent_evidence``/Tier-1 admission on its own.
    * ``first_party_promotional``    -- publisher IS a subject, type in
      ``_FIRST_PARTY_PROMOTIONAL_TYPES``. ``marketing_risk`` is set ONLY
      here (and, conditionally, at ``first_party_commentary`` below).
    * ``first_party_artifact``       -- publisher IS a subject, type in
      ``_RIGOROUS_TYPES`` -- a self-published runnable/rigorous artifact
      (the item015 class). Real evidence, but self-published: raises the
      level, never counts as independent (same reasoning as
      ``non_subject_authoritative``).
    * ``independent_rigorous``       -- SAME three types, published by a
      non-subject. This is the ONLY methodology leg level 5 accepts (R6): a
      subject's own benchmark must never satisfy level 5's rigor
      requirement.
    * ``independent_analysis``       -- type == ``independent_analysis``,
      published by a non-subject.
    * ``first_party_commentary``     -- (Founder decision D4) type ==
      ``independent_analysis``, published BY a cluster subject -- i.e. the
      subject analysing itself. This is NOT independent (``_is_independent``
      already excludes it, since publisher is a subject) and is capped at
      evidence level 2, never 3+ -- it replaces the old
      ``evid_3_other_uncorroborated`` branch's self-analysis half.
      ``marketing_risk`` is set for it ONLY when the authoring item's
      ``contains_benefit_or_performance_claim`` flag is True.
    * ``secondary_news_uncorroborated`` -- (Founder decision D2) type in
      ``_SECONDARY_NEWS_TYPES`` (announcement/release_note), published by a
      NON-subject, with no first-party or independent signal anywhere else
      in the cluster. An isolated, uncorroborated news item about someone
      else is weak single-source evidence -- level 1, distinct from
      ``rumor``. If ANY higher-branch signal is also present in the cluster,
      that branch fires instead (see the rubric below): this flag only ever
      decides the outcome when it is the cluster's best evidence.
    * ``rumor``                      -- type == ``rumor`` (no publisher
      condition; unchanged from before this fix).

    Rubric (first hit wins, most-evidenced first -- mirrors the
    ``categorize()`` pattern in ``sources.inventory``: an explainable,
    ordered rule chain):

    5. (first_party_authoritative OR non_subject_authoritative) AND
       independent_analysis AND independent_rigorous -- an authoritative
       source, PLUS independent analysis, PLUS an independent (never
       self-published -- R6) benchmark/implementation/paper.
    4. (any first-party evidentiary member present AND (independent_analysis
       OR independent_rigorous)) OR independent_rigorous on its own --
       independent_rigorous alone is rigorous/reproducible enough to earn
       level 4 without first-party corroboration.
    3. first_party_authoritative OR non_subject_authoritative OR
       first_party_artifact OR independent_analysis -- any single-source
       evidentiary signal that is not literal first-party promotion, not
       self-authored commentary, not secondary news, not rumor, and does not
       clear level 4/5 on its own.
    2. first_party_promotional -- ``marketing_risk = True`` (unconditional).
       Otherwise first_party_commentary -- capped here (D4), never 3+;
       ``marketing_risk`` set only when the authoring item's
       ``contains_benefit_or_performance_claim`` is True.
    1. secondary_news_uncorroborated (D2) or rumor -- isolated, uncorroborated
       single-source coverage of someone else, or an unconfirmed rumor.
    0. ONLY roundup/relay members, or no evidence-counting members left after
       role exclusion.

    ``has_direct_artifact_or_independent_source`` (D3) is the derived FACT
    the breaking-change consequence floor in ``ranking.py`` is gated on: True
    when the cluster has a first-party authoritative or first-party-artifact
    member, OR genuine independent evidence (``independent_publisher_count >
    0``). It is a fact, never a count, and never roundup/relay/duplicate/
    syndicated -- those never contribute to it.
    """
    independent_publishers: set[str] = set()
    has_first_party_authoritative = False
    has_non_subject_authoritative = False
    has_first_party_promotional = False
    has_first_party_artifact = False
    has_independent_rigorous = False
    has_independent_analysis = False
    has_first_party_commentary = False
    has_first_party_commentary_claim = False
    has_secondary_news_uncorroborated = False
    has_rumor = False

    for member in members_sorted:
        if member_roles[member.item_id] in _EVIDENCE_EXCLUDED_ROLES:
            continue
        if _is_independent(member, subject_entity_ids):
            independent_publishers.add(member.publisher_id)

        by_subject = member.publisher_id in subject_entity_ids
        evidence_type = member.evidence_type

        if evidence_type in _AUTHORITATIVE_TYPES:
            if by_subject:
                has_first_party_authoritative = True
            else:
                has_non_subject_authoritative = True
        elif evidence_type in _FIRST_PARTY_PROMOTIONAL_TYPES:
            if by_subject:
                has_first_party_promotional = True
            else:
                has_secondary_news_uncorroborated = True
        elif evidence_type in _RIGOROUS_TYPES:
            if by_subject:
                has_first_party_artifact = True
            else:
                has_independent_rigorous = True
        elif evidence_type == "independent_analysis":
            if by_subject:
                has_first_party_commentary = True
                if member.contains_benefit_or_performance_claim:
                    has_first_party_commentary_claim = True
            else:
                has_independent_analysis = True
        elif evidence_type == "rumor":
            has_rumor = True
        # roundup/relay: never evidentiary, no flag set.

    has_any_first_party = (
        has_first_party_authoritative or has_first_party_promotional or has_first_party_artifact
    )
    independent_publisher_count = len(independent_publishers)
    has_direct_artifact_or_independent_source = (
        has_first_party_authoritative or has_first_party_artifact or independent_publisher_count > 0
    )

    marketing_risk = False
    if (
        (has_first_party_authoritative or has_non_subject_authoritative)
        and has_independent_analysis
        and has_independent_rigorous
    ):
        evidence_level = 5
        anchor_id = "evid_5_authoritative_plus_analysis_plus_independent_rigor"
    elif (
        has_any_first_party and (has_independent_analysis or has_independent_rigorous)
    ) or has_independent_rigorous:
        evidence_level = 4
        if has_any_first_party and (has_independent_analysis or has_independent_rigorous):
            anchor_id = "evid_4_first_party_plus_independent"
        else:
            anchor_id = "evid_4_independent_rigorous_alone"
    elif (
        has_first_party_authoritative
        or has_non_subject_authoritative
        or has_first_party_artifact
        or has_independent_analysis
    ):
        evidence_level = 3
        if has_first_party_authoritative:
            anchor_id = "evid_3_first_party_authoritative"
        elif has_non_subject_authoritative:
            anchor_id = "evid_3_non_subject_authoritative"
        elif has_first_party_artifact:
            anchor_id = "evid_3_first_party_artifact"
        else:
            anchor_id = "evid_3_independent_only"
    elif has_first_party_promotional:
        evidence_level = 2
        marketing_risk = True
        anchor_id = "evid_2_first_party_promotional"
    elif has_first_party_commentary:
        evidence_level = 2
        marketing_risk = has_first_party_commentary_claim
        anchor_id = "evid_2_first_party_commentary"
    elif has_secondary_news_uncorroborated:
        evidence_level = 1
        anchor_id = "evid_1_secondary_news_uncorroborated"
    elif has_rumor:
        evidence_level = 1
        anchor_id = "evid_1_rumor"
    else:
        evidence_level = 0
        anchor_id = "evid_0_no_qualifying_evidence"

    return (
        evidence_level,
        marketing_risk,
        has_first_party_authoritative,
        independent_publisher_count,
        anchor_id,
        has_direct_artifact_or_independent_source,
    )


def _select_anchor(
    members_sorted: list[SourceItem], duplicate_ids: set[str], syndicated_ids: set[str]
) -> SourceItem:
    """Pick the cluster anchor: the earliest (by ``_canonical_order_key``)
    member that is neither a duplicate nor a syndicated copy of another
    member, AND is not a roundup/relay item when a better candidate exists --
    so that ``change_class``, ``action_required``, and
    ``experiment_affordance`` (read off the anchor by
    :func:`to_ranking_inputs`) are never authored by a relay/roundup copy, nor
    by a duplicate/syndicated non-origin member. Falls back to the earliest
    member overall only when every member in the cluster is excluded (there
    is no better candidate)."""
    excluded = duplicate_ids | syndicated_ids
    candidates = [m for m in members_sorted if m.item_id not in excluded]
    if not candidates:
        candidates = members_sorted
    non_relay_candidates = [m for m in candidates if not _is_relay(m)]
    pool = non_relay_candidates if non_relay_candidates else candidates
    return _earliest_by_canonical_order(pool)


def _build_cluster(members: list[SourceItem], duplication_reasons: set[str]) -> TopicCluster:
    members_sorted = sorted(members, key=lambda m: m.item_id)
    subject_entity_ids = sorted({sid for m in members_sorted for sid in m.subject_entity_ids})
    subject_set = frozenset(subject_entity_ids)

    duplicate_ids = _duplicate_ids(members_sorted, subject_set)
    syndicated_ids = _syndicated_ids(members_sorted, subject_set)
    anchor = _select_anchor(members_sorted, duplicate_ids, syndicated_ids)
    member_roles = _assign_roles(members_sorted, anchor.item_id, duplicate_ids, syndicated_ids)

    (
        evidence_level,
        marketing_risk,
        has_first_party_authoritative,
        independent_publisher_count,
        evidence_anchor_id,
        has_direct_artifact_or_independent_source,
    ) = _evidence_level_and_marketing_risk(members_sorted, member_roles, subject_set)

    # topic_tags and evidence_types are built from "real" sources only:
    # members whose role is primary or independent. The anchor is always
    # labelled "primary" (see _assign_roles), so this set is never empty --
    # even an all-roundup/relay cluster's sole non-excluded signal is then
    # its anchor's own tags/evidence_type.
    tag_source_ids = {
        m.item_id for m in members_sorted if member_roles[m.item_id] in {"primary", "independent"}
    }

    topic_tags = sorted(
        {tag for m in members_sorted if m.item_id in tag_source_ids for tag in m.topic_tags}
    )
    evidence_types: list[str] = sorted(
        {str(m.evidence_type) for m in members_sorted if m.item_id in tag_source_ids}
    )

    # first_seen/last_seen span EVERY member's date, including relay/
    # syndicated/duplicate copies -- that only feeds the ranking tie-break
    # key (ordering), never any dimension's points (see module docstring).
    dates = [_effective_date(m) for m in members_sorted]

    return TopicCluster(
        topic_id=_topic_id(anchor, subject_entity_ids),
        cluster_fingerprint=_cluster_fingerprint(members_sorted),
        canonical_title=anchor.title,
        anchor_item_id=anchor.item_id,
        member_ids=sorted(m.item_id for m in members_sorted),
        member_roles=member_roles,
        duplication_reasons=sorted(duplication_reasons),
        subject_entity_ids=subject_entity_ids,
        independent_publisher_count=independent_publisher_count,
        has_independent_evidence=independent_publisher_count > 0,
        has_first_party_authoritative=has_first_party_authoritative,
        has_direct_artifact_or_independent_source=has_direct_artifact_or_independent_source,
        evidence_level=evidence_level,
        evidence_anchor_id=evidence_anchor_id,
        marketing_risk=marketing_risk,
        first_seen=min(dates),
        last_seen=max(dates),
        cluster_size=len(members_sorted),
        topic_tags=topic_tags,
        evidence_types=evidence_types,
    )


def cluster_items(items: list[SourceItem]) -> list[TopicCluster]:
    """Deterministically merge ``items`` into :class:`TopicCluster` records.

    Order-independent: shuffling ``items`` before calling this produces the
    same clusters (same members, same computed fields) in the same final
    order, because the connected components depend only on the edge set, and
    the returned list is sorted by ``topic_id``.
    """
    items_sorted = sorted(items, key=lambda it: it.item_id)
    parent: dict[str, str] = {it.item_id: it.item_id for it in items_sorted}
    edges: list[tuple[str, str, str]] = []

    for i in range(len(items_sorted)):
        for j in range(i + 1, len(items_sorted)):
            item_a, item_b = items_sorted[i], items_sorted[j]
            ref_a = normalize_canonical_reference(item_a.stable_reference)
            ref_b = normalize_canonical_reference(item_b.stable_reference)
            if ref_a == ref_b:
                edges.append((item_a.item_id, item_b.item_id, "same_canonical_reference"))
            shared_subject = bool(
                set(item_a.subject_entity_ids) & set(item_b.subject_entity_ids)
            )
            if (
                shared_subject
                and jaccard(token_signature(item_a.title), token_signature(item_b.title))
                >= _JACCARD_TITLE_THRESHOLD
            ):
                edges.append(
                    (item_a.item_id, item_b.item_id, "title_similarity_and_shared_subject")
                )

    for id_a, id_b, _reason in edges:
        _union(parent, id_a, id_b)

    groups: dict[str, list[SourceItem]] = defaultdict(list)
    for item in items_sorted:
        groups[_find(parent, item.item_id)].append(item)

    reasons_by_root: dict[str, set[str]] = defaultdict(set)
    for id_a, _id_b, reason in edges:
        reasons_by_root[_find(parent, id_a)].add(reason)

    clusters = [
        _build_cluster(members, reasons_by_root.get(root, set()))
        for root, members in groups.items()
    ]
    clusters.sort(key=lambda c: c.topic_id)
    return clusters


def to_ranking_inputs(cluster: TopicCluster, items_by_id: dict[str, SourceItem]) -> RankingInputs:
    """Build the ``RankingInputs`` for one cluster.

    This is the ONLY bridge between clustering and ranking: ``ranking.py``
    never imports or receives a ``TopicCluster``. ``change_class``,
    ``action_required``, ``change_class_rationale``, and
    ``experiment_affordance`` -- item-level facts not carried on
    ``TopicCluster`` -- are taken from the cluster's anchor item (the
    non-excluded, non-roundup/relay primary source whenever one exists; see
    ``cluster._select_anchor``), since that is the item whose classification
    the topic is understood to represent.
    """
    anchor = items_by_id[cluster.anchor_item_id]
    return RankingInputs(
        topic_id=cluster.topic_id,
        topic_tags=cluster.topic_tags,
        change_class=anchor.change_class,
        change_class_rationale=anchor.change_class_rationale,
        action_required=anchor.action_required,
        evidence_level=cluster.evidence_level,
        evidence_anchor_id=cluster.evidence_anchor_id,
        has_independent_evidence=cluster.has_independent_evidence,
        has_first_party_authoritative=cluster.has_first_party_authoritative,
        has_direct_artifact_or_independent_source=cluster.has_direct_artifact_or_independent_source,
        marketing_risk=cluster.marketing_risk,
        experiment_affordance=anchor.experiment_affordance,
        evidence_types=cluster.evidence_types,
        first_seen=cluster.first_seen,
    )
