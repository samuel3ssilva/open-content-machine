"""Deterministic snapshot-based comparison between classifier versions.

Plain local code (no model calls). It answers a single question: *when the
keyword tables in :mod:`content_machine.audience.classify` change, what moved?*

Why snapshots (ticket OPUS-1.1b §2)
-----------------------------------
The pre-hardening classifier code is gone, so there is nothing to diff against
by importing two implementations side by side. Instead we compare *outputs*: a
classifier version, run over a fixed PUBLIC fixture, produces an index-aligned
list of ``(family, seniority, confidence)`` snapshots. Two such snapshots (an
old baseline captured earlier and the current run) are compared position by
position.

Baselines are captured GOING FORWARD. No baseline could be produced for the
pre-hardening code; the first committed reference is
``tests/fixtures/classifier_snapshot_sprint11.json`` (this post-hardening
version, captured from the synthetic labeled fixture). Every future rule change
can then be diffed against it via ``compare-classifiers``.

Privacy
-------
A snapshot holds ONLY family/seniority/confidence labels, index-aligned to the
input titles -- never a title itself (docs/privacy.md rule 6). The comparison
report is likewise aggregate: from→to labels and counts only. Both are safe to
commit and to print.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.classify import classify_role
from content_machine.audience.normalize import infer_seniority

_UNKNOWN = "unknown"


class ClassificationSnapshot(BaseModel):
    """One title's classification outcome, WITHOUT the title (index-aligned)."""

    model_config = ConfigDict(extra="forbid")

    family: str
    seniority: str
    confidence: str


class LabeledChange(BaseModel):
    """An aggregated ``from → to`` transition and how often it occurred."""

    model_config = ConfigDict(extra="forbid")

    from_label: str
    to_label: str
    count: int


class RegressionFlag(BaseModel):
    """A heuristic possible-regression bucket -- counts + labels only.

    ``kind`` is ``high_conf_family_flip`` (a high-confidence classification that
    became a DIFFERENT high-confidence family) or ``classified_to_unknown`` (a
    previously-placed row that became ``unknown``). No titles are referenced.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    from_family: str
    to_family: str
    count: int


class ComparisonReport(BaseModel):
    """Aggregate diff between two index-aligned classification snapshots."""

    model_config = ConfigDict(extra="forbid")

    n: int
    total_changed: int
    unknown_to_classified: int
    classified_to_unknown: int
    family_changes: list[LabeledChange] = Field(default_factory=list)
    confidence_changes: list[LabeledChange] = Field(default_factory=list)
    seniority_changes: list[LabeledChange] = Field(default_factory=list)
    possible_regressions: list[RegressionFlag] = Field(default_factory=list)


def snapshot_classifications(titles: list[str]) -> list[ClassificationSnapshot]:
    """Classify each title with the CURRENT code, index-aligned to ``titles``.

    Pure and deterministic. The returned list is the same length and order as
    ``titles`` but contains no title text -- only the derived labels.
    """
    snapshots: list[ClassificationSnapshot] = []
    for title in titles:
        role = classify_role(title)
        snapshots.append(
            ClassificationSnapshot(
                family=role.family.value,
                seniority=infer_seniority(title),
                confidence=role.confidence.value,
            )
        )
    return snapshots


def snapshot_to_json(snapshots: list[ClassificationSnapshot]) -> str:
    """Serialize snapshots to a compact, deterministic JSON array (no titles)."""
    payload = [s.model_dump() for s in snapshots]
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_snapshot(path: str | Path) -> list[ClassificationSnapshot]:
    """Load a snapshot JSON array previously written by :func:`snapshot_to_json`.

    Raises ``ValueError`` (never a traceback for the caller to swallow) when the
    file is not a JSON array of snapshot objects.
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Snapshot file is not valid JSON: {exc}") from None
    if not isinstance(data, list):
        raise ValueError("Snapshot file must contain a JSON array of snapshots.")
    return [ClassificationSnapshot.model_validate(item) for item in data]


def _aggregate(pairs: dict[tuple[str, str], int]) -> list[LabeledChange]:
    """Order transitions by count desc, then labels, for stable output."""
    ordered = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return [
        LabeledChange(from_label=frm, to_label=to, count=count)
        for (frm, to), count in ordered
    ]


def compare(
    snapshot_a: list[ClassificationSnapshot],
    snapshot_b: list[ClassificationSnapshot],
) -> ComparisonReport:
    """Compare two index-aligned snapshots (``a`` = baseline, ``b`` = current).

    Deterministic and pure. Requires equal length -- the snapshots must be over
    the SAME ordered inputs, so position ``i`` in each refers to the same title.
    """
    if len(snapshot_a) != len(snapshot_b):
        raise ValueError(
            "Snapshots are not index-aligned: baseline has "
            f"{len(snapshot_a)} rows, current has {len(snapshot_b)}. Both must be "
            "captured over the same ordered fixture."
        )

    n = len(snapshot_a)
    total_changed = 0
    unknown_to_classified = 0
    classified_to_unknown = 0

    family_pairs: Counter[tuple[str, str]] = Counter()
    confidence_pairs: Counter[tuple[str, str]] = Counter()
    seniority_pairs: Counter[tuple[str, str]] = Counter()
    flip_pairs: Counter[tuple[str, str]] = Counter()
    to_unknown_pairs: Counter[tuple[str, str]] = Counter()

    for a, b in zip(snapshot_a, snapshot_b, strict=True):
        changed = False
        if a.family != b.family:
            family_pairs[(a.family, b.family)] += 1
            changed = True
            if a.family == _UNKNOWN and b.family != _UNKNOWN:
                unknown_to_classified += 1
            elif a.family != _UNKNOWN and b.family == _UNKNOWN:
                classified_to_unknown += 1
                to_unknown_pairs[(a.family, _UNKNOWN)] += 1
        if a.confidence != b.confidence:
            confidence_pairs[(a.confidence, b.confidence)] += 1
            changed = True
        if a.seniority != b.seniority:
            seniority_pairs[(a.seniority, b.seniority)] += 1
            changed = True
        if changed:
            total_changed += 1

        # Regression heuristic: a high-confidence call that stayed high but
        # switched family is the riskiest silent flip.
        if (
            a.confidence == "high"
            and b.confidence == "high"
            and a.family != b.family
        ):
            flip_pairs[(a.family, b.family)] += 1

    regressions: list[RegressionFlag] = []
    for (frm, to), count in sorted(
        flip_pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
    ):
        regressions.append(
            RegressionFlag(
                kind="high_conf_family_flip",
                from_family=frm,
                to_family=to,
                count=count,
            )
        )
    for (frm, to), count in sorted(
        to_unknown_pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
    ):
        regressions.append(
            RegressionFlag(
                kind="classified_to_unknown",
                from_family=frm,
                to_family=to,
                count=count,
            )
        )

    return ComparisonReport(
        n=n,
        total_changed=total_changed,
        unknown_to_classified=unknown_to_classified,
        classified_to_unknown=classified_to_unknown,
        family_changes=_aggregate(family_pairs),
        confidence_changes=_aggregate(confidence_pairs),
        seniority_changes=_aggregate(seniority_pairs),
        possible_regressions=regressions,
    )


def render_comparison(report: ComparisonReport) -> str:
    """Render a comparison report as plain text (aggregate labels + counts)."""
    lines: list[str] = []
    lines.append("Classifier comparison — baseline → current")
    lines.append("")
    lines.append(f"Rows compared:          {report.n}")
    lines.append(f"Rows changed:           {report.total_changed}")
    lines.append(f"unknown → classified:   {report.unknown_to_classified}")
    lines.append(f"classified → unknown:   {report.classified_to_unknown}")
    lines.append("")
    _append_changes(lines, "Family changes", report.family_changes)
    _append_changes(lines, "Confidence changes", report.confidence_changes)
    _append_changes(lines, "Seniority changes", report.seniority_changes)
    lines.append("Possible regressions (heuristic):")
    if report.possible_regressions:
        for flag in report.possible_regressions:
            lines.append(
                f"  [{flag.kind}] {flag.from_family} → {flag.to_family}: {flag.count}"
            )
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)


def _append_changes(
    lines: list[str], title: str, changes: list[LabeledChange]
) -> None:
    lines.append(f"{title}:")
    if changes:
        for change in changes:
            lines.append(f"  {change.from_label} → {change.to_label}: {change.count}")
    else:
        lines.append("  (none)")
    lines.append("")
