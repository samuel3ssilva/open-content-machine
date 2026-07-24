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
on the *set* of edges, not on the order operations are applied. Within each
cluster, a member whose summary token signature has Jaccard >= 0.85 against
an *earlier* member (members ordered deterministically by ``item_id``) is a
syndication collapse: it gets ``member_role="syndicated"`` and contributes
ZERO to ``independent_publisher_count``, ``has_independent_evidence``, and
``has_first_party_authoritative``. Evidence types ``roundup``/``relay`` never
count toward independence at any similarity (they are excluded from the
independent evidence-type set entirely, not just deduplicated).

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
where the anchor is the earliest member by
``(publication_date or detection_date, stable_reference)``.
``cluster_fingerprint`` is a full sha256 over the sorted canonical member
references; it changes as members accrete and is used ONLY for change
detection, NEVER as identity. Persistent cross-run identity (stable ``topic_id``
across re-runs as new items append over time) is out of scope for Gate A and
lands at M6 -- this module's ``topic_id`` is only guaranteed stable across
multiple *identical* invocations of :func:`cluster_items`, not across a corpus
that has grown.
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
# non-subject publisher (evidence rubric, anchors 4/5).
_INDEPENDENT_EVIDENCE_TYPES = frozenset(
    {
        "independent_analysis",
        "benchmark_with_methodology",
        "independent_implementation",
        "research_paper",
    }
)
# "official_doc|spec_change|deprecation_notice|security_advisory published BY
# the subject" -- evidence anchor 3 ("first-party authoritative").
_FIRST_PARTY_AUTHORITATIVE_TYPES = frozenset(
    {"deprecation_notice", "security_advisory", "official_doc", "spec_change"}
)
# "announcement|release_note published BY the subject" -- evidence anchor 2
# (marketing_risk is set here, and ONLY here).
_FIRST_PARTY_PROMOTIONAL_TYPES = frozenset({"announcement", "release_note"})


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


def _anchor_sort_key(item: SourceItem) -> tuple[date, str]:
    return (_effective_date(item), item.stable_reference)


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


def _syndicated_ids(members_sorted: list[SourceItem]) -> set[str]:
    """Members whose summary collapses (Jaccard >= 0.85) against an earlier
    member, in deterministic ``item_id`` order. The first member can never be
    syndicated (there is no earlier member to collapse against)."""
    syndicated: set[str] = set()
    for i, member in enumerate(members_sorted):
        member_sig = token_signature(member.summary_normalized)
        for earlier in members_sorted[:i]:
            earlier_sig = token_signature(earlier.summary_normalized)
            if jaccard(member_sig, earlier_sig) >= _JACCARD_SYNDICATION_THRESHOLD:
                syndicated.add(member.item_id)
                break
    return syndicated


def _duplicate_ids(members_sorted: list[SourceItem], anchor_id: str) -> set[str]:
    """Non-anchor members that share an identical canonical reference with at
    least one other member in the cluster."""
    ref_groups: dict[str, list[str]] = defaultdict(list)
    for member in members_sorted:
        ref_groups[normalize_canonical_reference(member.stable_reference)].append(member.item_id)
    return {
        item_id
        for ids in ref_groups.values()
        if len(ids) > 1
        for item_id in ids
        if item_id != anchor_id
    }


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
        elif member.evidence_type in {"roundup", "relay"}:
            roles[member.item_id] = "relay"
        else:
            roles[member.item_id] = "independent"
    return roles


def _evidence_level_and_marketing_risk(
    members_sorted: list[SourceItem],
    member_roles: dict[str, str],
    subject_entity_ids: frozenset[str],
) -> tuple[int, bool, bool, int]:
    """Compute the evidence rubric (0-5), ``marketing_risk``, whether a
    first-party-authoritative source is present, and the distinct independent
    publisher count -- all excluding syndicated members, which contribute
    zero to every evidentiary signal."""
    independent_publishers: set[str] = set()
    has_first_party_authoritative = False
    has_first_party_promotional = False
    has_benchmark_present = False
    has_benchmark_non_subject = False
    has_independent_implementation_present = False
    has_rumor = False

    for member in members_sorted:
        if member_roles[member.item_id] == "syndicated":
            continue
        if _is_independent(member, subject_entity_ids):
            independent_publishers.add(member.publisher_id)
        by_subject = member.publisher_id in subject_entity_ids
        if member.evidence_type in _FIRST_PARTY_AUTHORITATIVE_TYPES and by_subject:
            has_first_party_authoritative = True
        if member.evidence_type in _FIRST_PARTY_PROMOTIONAL_TYPES and by_subject:
            has_first_party_promotional = True
        if member.evidence_type == "benchmark_with_methodology":
            has_benchmark_present = True
            if not by_subject:
                has_benchmark_non_subject = True
        if member.evidence_type == "independent_implementation":
            has_independent_implementation_present = True
        if member.evidence_type == "rumor":
            has_rumor = True

    has_independent_evidence = bool(independent_publishers)
    # Any first-party source at all, authoritative OR merely promotional --
    # used only for the level-4/5 "corroborated" branches. Level 3 vs level 2
    # still distinguish authoritative from promotional (see below): the
    # broader flag here resolves an ambiguity in the rubric text ("official/
    # primary present" in anchors 4/5 vs the narrower "official_doc|spec_change|
    # ..." list that defines anchor 3 specifically) -- independent corroboration
    # of even a promotional announcement should lift a topic out of the
    # marketing_risk bucket, which a strictly-authoritative-only reading of
    # anchor 4 would not allow.
    has_first_party_present = has_first_party_authoritative or has_first_party_promotional

    # First hit wins, most-evidenced first (mirrors the categorize() pattern
    # in sources.inventory: an explainable, ordered rule chain).
    marketing_risk = False
    if (
        has_first_party_authoritative
        and has_independent_evidence
        and (has_benchmark_present or has_independent_implementation_present)
    ):
        evidence_level = 5
    elif (has_first_party_present and has_independent_evidence) or has_benchmark_non_subject:
        evidence_level = 4
    elif has_first_party_authoritative:
        evidence_level = 3
    elif has_first_party_promotional:
        evidence_level = 2
        marketing_risk = True
    elif has_rumor:
        evidence_level = 1
    else:
        evidence_level = 0

    return (
        evidence_level,
        marketing_risk,
        has_first_party_authoritative,
        len(independent_publishers),
    )


def _build_cluster(members: list[SourceItem], duplication_reasons: set[str]) -> TopicCluster:
    members_sorted = sorted(members, key=lambda m: m.item_id)
    subject_entity_ids = sorted({sid for m in members_sorted for sid in m.subject_entity_ids})
    subject_set = frozenset(subject_entity_ids)

    anchor = min(members_sorted, key=_anchor_sort_key)

    syndicated_ids = _syndicated_ids(members_sorted)
    duplicate_ids = _duplicate_ids(members_sorted, anchor.item_id)
    member_roles = _assign_roles(members_sorted, anchor.item_id, duplicate_ids, syndicated_ids)

    (
        evidence_level,
        marketing_risk,
        has_first_party_authoritative,
        independent_publisher_count,
    ) = _evidence_level_and_marketing_risk(members_sorted, member_roles, subject_set)

    topic_tags = sorted(
        {
            tag
            for m in members_sorted
            if member_roles[m.item_id] != "syndicated"
            for tag in m.topic_tags
        }
    )
    evidence_types: list[str] = sorted({str(m.evidence_type) for m in members_sorted})

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
        evidence_level=evidence_level,
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
    ``action_required``, and ``experiment_affordance`` -- item-level facts not
    carried on ``TopicCluster`` -- are taken from the cluster's anchor item
    (the earliest, primary source), since that is the item whose classification
    the topic is understood to represent.
    """
    anchor = items_by_id[cluster.anchor_item_id]
    return RankingInputs(
        topic_id=cluster.topic_id,
        topic_tags=cluster.topic_tags,
        change_class=anchor.change_class,
        action_required=anchor.action_required,
        evidence_level=cluster.evidence_level,
        has_independent_evidence=cluster.has_independent_evidence,
        marketing_risk=cluster.marketing_risk,
        experiment_affordance=anchor.experiment_affordance,
        evidence_types=cluster.evidence_types,
        first_seen=cluster.first_seen,
    )
