"""Before/after comparison of two full audience runs (ticket OPUS-1.1b §3).

Pure local functions, no CLI. This is the CONTRACT the CTO runs locally against
the real second export (before = a run under the previous classifier, after = a
run under the current one). We never run it against real data here; the code
ships with synthetic-only tests.

Two granularities:

* :func:`build_before_after` compares two :class:`AudienceReport` aggregates
  (the private report model). It needs no per-record data -- it diffs the
  already-aggregated distributions and segments. Output is aggregate labels,
  counts, shares, and deltas only.
* :func:`count_reclassified` needs per-record data and so takes two lists of
  :class:`AnonymizedConnection` matched by ``id``. Because the pseudonym id is
  stable for a fixed salt, the SAME person lands on the SAME id across two runs,
  so we can count how many records actually moved. The OUTPUT
  (:class:`ReclassificationStats`) is aggregate only -- no ids, no titles.

Privacy: both output models carry only counts / labels / rates. Names, emails,
URLs, ids, and titles never appear (docs/privacy.md rules 3 & 6). Inputs may be
the working ``AnonymizedConnection`` records, but nothing identifying is copied
into the returned models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.report import AudienceReport, CountItem
from content_machine.privacy.anonymizer import AnonymizedConnection

_UNKNOWN = "unknown"


class DistributionDelta(BaseModel):
    """A single label's before/after counts and their signed delta."""

    model_config = ConfigDict(extra="forbid")

    label: str
    before: int
    after: int
    delta: int


class SegmentRef(BaseModel):
    """A segment reference: name, size, share -- no per-person evidence."""

    model_config = ConfigDict(extra="forbid")

    name: str
    size: int
    share: float


class RunComparison(BaseModel):
    """Aggregate before/after diff of two :class:`AudienceReport` runs."""

    model_config = ConfigDict(extra="forbid")

    before_unknown_rate: float
    after_unknown_rate: float
    unknown_reduction: float
    family_before: list[CountItem] = Field(default_factory=list)
    family_after: list[CountItem] = Field(default_factory=list)
    seniority_before: list[CountItem] = Field(default_factory=list)
    seniority_after: list[CountItem] = Field(default_factory=list)
    confidence_before: list[CountItem] = Field(default_factory=list)
    confidence_after: list[CountItem] = Field(default_factory=list)
    family_deltas: list[DistributionDelta] = Field(default_factory=list)
    top_segments_before: list[SegmentRef] = Field(default_factory=list)
    top_segments_after: list[SegmentRef] = Field(default_factory=list)
    segments_added: list[str] = Field(default_factory=list)
    segments_removed: list[str] = Field(default_factory=list)


class ReclassificationStats(BaseModel):
    """Per-record reclassification counts across two runs, matched by id.

    Aggregate only: the number of matched records and how many changed family /
    confidence / seniority, plus the two directional unknown transitions. No id
    or title is ever stored.
    """

    model_config = ConfigDict(extra="forbid")

    n_matched: int
    n_family_changed: int
    n_confidence_changed: int
    n_seniority_changed: int
    unknown_to_classified: int
    classified_to_unknown: int


def _as_counter(items: list[CountItem]) -> dict[str, int]:
    """Index a distribution list by label for delta computation."""
    return {item.label: item.count for item in items}


def _family_deltas(
    before: list[CountItem], after: list[CountItem]
) -> list[DistributionDelta]:
    """Per-family before/after deltas over the union of labels, largest |delta|
    first (ties broken by label) for stable, readable output."""
    b = _as_counter(before)
    a = _as_counter(after)
    labels = set(b) | set(a)
    deltas = [
        DistributionDelta(
            label=label,
            before=b.get(label, 0),
            after=a.get(label, 0),
            delta=a.get(label, 0) - b.get(label, 0),
        )
        for label in labels
    ]
    deltas.sort(key=lambda d: (-abs(d.delta), d.label))
    return deltas


def _segments(report: AudienceReport) -> list[SegmentRef]:
    return [
        SegmentRef(name=s.name, size=s.size, share=s.share)
        for s in report.candidate_segments
    ]


def build_before_after(
    before_report: AudienceReport, after_report: AudienceReport
) -> RunComparison:
    """Compare two audience reports into an aggregate before/after diff.

    Deterministic and pure. ``unknown_reduction`` is ``before - after`` on the
    unknown-family share (positive = the after run left fewer rows unclassified).
    Segment membership changes are reported by NAME only.
    """
    before_names = {s.name for s in before_report.candidate_segments}
    after_names = {s.name for s in after_report.candidate_segments}

    return RunComparison(
        before_unknown_rate=before_report.unknown_share,
        after_unknown_rate=after_report.unknown_share,
        unknown_reduction=round(
            before_report.unknown_share - after_report.unknown_share, 4
        ),
        family_before=before_report.role_family_distribution,
        family_after=after_report.role_family_distribution,
        seniority_before=before_report.seniority_distribution,
        seniority_after=after_report.seniority_distribution,
        confidence_before=before_report.confidence_distribution,
        confidence_after=after_report.confidence_distribution,
        family_deltas=_family_deltas(
            before_report.role_family_distribution,
            after_report.role_family_distribution,
        ),
        top_segments_before=_segments(before_report),
        top_segments_after=_segments(after_report),
        segments_added=sorted(after_names - before_names),
        segments_removed=sorted(before_names - after_names),
    )


def count_reclassified(
    old_rows: list[AnonymizedConnection], new_rows: list[AnonymizedConnection]
) -> ReclassificationStats:
    """Count per-record changes between two runs, matched by pseudonym id.

    Only ids present in BOTH runs are compared (``n_matched``); rows unique to
    one run are ignored for the change counts. Deterministic and pure. The
    output holds counts only -- never an id or title.
    """
    old_by_id: dict[str, AnonymizedConnection] = {}
    for row in old_rows:
        # A stable id can repeat (an exact duplicate person); the first wins so
        # the match is deterministic and mirrors dedup's canonical-first rule.
        old_by_id.setdefault(row.id, row)

    seen_new: set[str] = set()
    n_matched = 0
    n_family_changed = 0
    n_confidence_changed = 0
    n_seniority_changed = 0
    unknown_to_classified = 0
    classified_to_unknown = 0

    for new in new_rows:
        if new.id in seen_new:
            continue
        seen_new.add(new.id)
        old = old_by_id.get(new.id)
        if old is None:
            continue
        n_matched += 1
        if old.role_family != new.role_family:
            n_family_changed += 1
            if old.role_family == _UNKNOWN and new.role_family != _UNKNOWN:
                unknown_to_classified += 1
            elif old.role_family != _UNKNOWN and new.role_family == _UNKNOWN:
                classified_to_unknown += 1
        if old.role_confidence != new.role_confidence:
            n_confidence_changed += 1
        if old.seniority_bucket != new.seniority_bucket:
            n_seniority_changed += 1

    return ReclassificationStats(
        n_matched=n_matched,
        n_family_changed=n_family_changed,
        n_confidence_changed=n_confidence_changed,
        n_seniority_changed=n_seniority_changed,
        unknown_to_classified=unknown_to_classified,
        classified_to_unknown=classified_to_unknown,
    )
