"""Sanitized, shareable public report (TB-3: report -> publication).

A private :class:`~content_machine.audience.report.AudienceReport` may contain
small-group aggregates that could re-identify people in a small population
(docs/privacy.md: derived data is sensitive-by-default). This module produces a
*public* artifact from an existing private report by suppressing any group whose
count is below :data:`SUPPRESSION_THRESHOLD`:

- top-lists (companies, roles): groups under the threshold are dropped;
- distributions (family, seniority, confidence, year): groups under the
  threshold are merged into a single ``(suppressed, <10)`` bucket;
- segments under the threshold are dropped entirely.

The result carries ``privacy_label="sanitized-aggregate"`` and a Markdown banner.
Sanitization is never automatic -- it is an explicit, human-invoked step.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.report import AudienceReport, CountItem

SUPPRESSION_THRESHOLD = 10
SUPPRESSED_LABEL = "(suppressed, <10)"

BANNER = (
    "Sanitized aggregate export — groups under 10 suppressed. "
    "Generated from private data; review before sharing."
)


class PublicSegment(BaseModel):
    """A segment safe to share: name, size, and share only (no evidence)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    size: int
    share: float


class PublicReport(BaseModel):
    """Minimal, sanitized aggregate contract intended for sharing."""

    model_config = ConfigDict(extra="forbid")

    privacy_label: Literal["sanitized-aggregate"] = "sanitized-aggregate"
    total_connections: int
    unique_connections: int
    top_companies: list[CountItem] = Field(default_factory=list)
    top_roles: list[CountItem] = Field(default_factory=list)
    role_family_distribution: list[CountItem] = Field(default_factory=list)
    seniority_distribution: list[CountItem] = Field(default_factory=list)
    confidence_distribution: list[CountItem] = Field(default_factory=list)
    connections_per_year: list[CountItem] = Field(default_factory=list)
    segments: list[PublicSegment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def _drop_small(items: list[CountItem]) -> list[CountItem]:
    """Top-list policy: drop any group under the threshold entirely."""
    return [i for i in items if i.count >= SUPPRESSION_THRESHOLD]


def _bucket_small(items: list[CountItem]) -> list[CountItem]:
    """Distribution policy: merge under-threshold groups into one bucket.

    Kept groups retain their (count-descending) order; the suppressed bucket, if
    any, is appended last.
    """
    kept = [i for i in items if i.count >= SUPPRESSION_THRESHOLD]
    suppressed = sum(i.count for i in items if i.count < SUPPRESSION_THRESHOLD)
    if suppressed > 0:
        kept.append(CountItem(label=SUPPRESSED_LABEL, count=suppressed))
    return kept


def sanitize(report: AudienceReport) -> PublicReport:
    """Produce a sanitized :class:`PublicReport` from a private report."""
    segments = [
        PublicSegment(name=s.name, size=s.size, share=s.share)
        for s in report.candidate_segments
        if s.size >= SUPPRESSION_THRESHOLD
    ]
    return PublicReport(
        total_connections=report.totals.total_rows,
        unique_connections=report.totals.unique_connections,
        top_companies=_drop_small(report.top_companies),
        top_roles=_drop_small(report.top_roles),
        role_family_distribution=_bucket_small(report.role_family_distribution),
        seniority_distribution=_bucket_small(report.seniority_distribution),
        confidence_distribution=_bucket_small(report.confidence_distribution),
        connections_per_year=_bucket_small(report.connections_per_year),
        segments=segments,
        limitations=list(report.limitations),
    )


def to_json(public: PublicReport) -> str:
    """Render the public report as pretty-printed JSON."""
    return public.model_dump_json(indent=2)


def to_markdown(public: PublicReport) -> str:
    """Render the public report as Markdown, led by the sanitization banner."""
    lines: list[str] = []
    lines.append("# Audience Report (sanitized aggregate)")
    lines.append("")
    lines.append(f"> {BANNER}")
    lines.append("")
    lines.append(f"- Privacy label: {public.privacy_label}")
    lines.append(f"- Total connections: {public.total_connections}")
    lines.append(f"- Unique connections: {public.unique_connections}")
    lines.append("")

    lines.append("## Top companies")
    lines.append("")
    _table(lines, public.top_companies, "Company")

    lines.append("## Top roles")
    lines.append("")
    _table(lines, public.top_roles, "Role (normalized)")

    lines.append("## Role family distribution")
    lines.append("")
    _table(lines, public.role_family_distribution, "Family")

    lines.append("## Seniority distribution")
    lines.append("")
    _table(lines, public.seniority_distribution, "Bucket")

    lines.append("## Classification confidence")
    lines.append("")
    _table(lines, public.confidence_distribution, "Confidence")

    lines.append("## Connections per year")
    lines.append("")
    _table(lines, public.connections_per_year, "Year")

    lines.append("## Segments")
    lines.append("")
    if public.segments:
        lines.append("| Segment | Size | Share |")
        lines.append("| --- | --- | --- |")
        for seg in public.segments:
            lines.append(f"| {seg.name} | {seg.size} | {seg.share:.1%} |")
    else:
        lines.append("_No segments of shareable size (>=10)._")
    lines.append("")

    lines.append("## Limitations")
    lines.append("")
    for limitation in public.limitations:
        lines.append(f"- {limitation}")
    lines.append("")

    return "\n".join(lines)


def _table(lines: list[str], items: list[CountItem], label: str) -> None:
    if items:
        lines.append(f"| {label} | Count |")
        lines.append("| --- | --- |")
        for item in items:
            lines.append(f"| {item.label} | {item.count} |")
    else:
        lines.append("_None above the suppression threshold._")
    lines.append("")
