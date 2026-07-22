"""Deterministic audience statistics and report rendering.

``analyze`` consumes only the anonymized (safe-zone) connections plus structural
metadata from the load/normalization steps. By construction it never sees names,
emails, or URLs, so the Markdown and JSON renders cannot contain them. Every
inferred aggregate (seniority) is labeled as such, and a mandatory caveat notes
that a connection's existence is not evidence of interest in the creator's
content.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from pydantic import BaseModel, Field

from content_machine.audience.normalize import NormalizationResult
from content_machine.ingestion.csv_loader import LoadResult
from content_machine.privacy.anonymizer import AnonymizationResult

MANDATORY_CAVEAT = (
    "Connection presence does not imply interest in your content. "
    "Seniority buckets are heuristic inferences."
)

_TOP_N = 10


class CountItem(BaseModel):
    """A label with its count, for top-N tables and distributions."""

    label: str
    count: int


class ReportTotals(BaseModel):
    total_rows: int
    unique_connections: int
    duplicates: int


class AudienceReport(BaseModel):
    """Machine-readable audience report. Aggregates only; no identifiers."""

    totals: ReportTotals
    completeness_pct: dict[str, float] = Field(default_factory=dict)
    top_companies: list[CountItem] = Field(default_factory=list)
    top_positions: list[CountItem] = Field(default_factory=list)
    seniority_distribution: list[CountItem] = Field(default_factory=list)
    seniority_inferred: bool = True
    connections_per_year: list[CountItem] = Field(default_factory=list)
    generated_notes: list[str] = Field(default_factory=list)


def analyze(
    anonymization_result: AnonymizationResult,
    load_result: LoadResult,
    normalization_result: NormalizationResult,
) -> AudienceReport:
    """Compute deterministic aggregates over anonymized connections."""
    rows = anonymization_result.connections
    total_rows = len(rows)
    duplicates = sum(1 for r in rows if r.is_duplicate)
    unique = total_rows - duplicates

    completeness = _completeness(load_result, normalization_result)

    top_companies = _top_counts((r.company for r in rows if r.company), _TOP_N)
    top_positions = _top_counts((r.position for r in rows if r.position), _TOP_N)

    seniority = _top_counts((r.seniority_bucket for r in rows), None)

    years = _year_counts(r.connected_year for r in rows)

    notes = _build_notes(anonymization_result, years)

    return AudienceReport(
        totals=ReportTotals(
            total_rows=total_rows,
            unique_connections=unique,
            duplicates=duplicates,
        ),
        completeness_pct=completeness,
        top_companies=top_companies,
        top_positions=top_positions,
        seniority_distribution=seniority,
        seniority_inferred=True,
        connections_per_year=years,
        generated_notes=notes,
    )


def _completeness(
    load_result: LoadResult, normalization_result: NormalizationResult
) -> dict[str, float]:
    """Percentage of rows with a non-empty value, per present column.

    Uses the normalized working records (which know each field's emptiness) but
    reports only columns that were actually present in the source file, so an
    absent column is not shown as 0% complete.
    """
    conns = normalization_result.connections
    total = len(conns)
    if total == 0:
        return {}

    present = set(load_result.columns_present)
    # Map canonical column names to an accessor on the normalized record.
    accessors = {
        "first_name": lambda c: c.first_name,
        "last_name": lambda c: c.last_name,
        "url": lambda c: c.url,
        "email": lambda c: c.email,
        "company": lambda c: c.company_raw,
        "position": lambda c: c.position,
        "connected_on": lambda c: c.connected_on,
    }

    result: dict[str, float] = {}
    for column in sorted(present):
        accessor = accessors.get(column)
        if accessor is None:
            continue
        filled = sum(1 for c in conns if accessor(c))
        result[column] = round(100.0 * filled / total, 1)
    return result


def _top_counts(values: Iterable[str], limit: int | None) -> list[CountItem]:
    """Count string values and return the top ``limit`` (all if ``None``).

    Ties are broken alphabetically for stable, deterministic output.
    """
    counter: Counter[str] = Counter(values)
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit is not None:
        items = items[:limit]
    return [CountItem(label=label, count=count) for label, count in items]


def _year_counts(years: Iterable[int | None]) -> list[CountItem]:
    """Connections per year, chronologically ordered. Empty when no dates."""
    counter: Counter[int] = Counter(y for y in years if y is not None)
    return [
        CountItem(label=str(year), count=counter[year]) for year in sorted(counter)
    ]


def _build_notes(
    anonymization_result: AnonymizationResult,
    years: list[CountItem],
) -> list[str]:
    notes: list[str] = [MANDATORY_CAVEAT]
    if anonymization_result.ephemeral_salt:
        notes.append(
            "An ephemeral salt was used: pseudonym IDs are NOT stable across "
            "runs. Set CONTENT_MACHINE_SALT in .env for stable IDs."
        )
    if not years:
        notes.append("No connection dates were available, so growth-by-year is omitted.")
    return notes


def to_json(report: AudienceReport) -> str:
    """Render the report as pretty-printed JSON."""
    return report.model_dump_json(indent=2)


def to_markdown(report: AudienceReport) -> str:
    """Render the report as Markdown.

    Contains only anonymized aggregates by construction (no names/emails/URLs).
    """
    t = report.totals
    lines: list[str] = []
    lines.append("# Audience Report")
    lines.append("")
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Total rows: {t.total_rows}")
    lines.append(f"- Unique connections: {t.unique_connections}")
    lines.append(f"- Duplicates: {t.duplicates}")
    lines.append("")

    lines.append("## Data completeness")
    lines.append("")
    if report.completeness_pct:
        lines.append("| Column | Complete |")
        lines.append("| --- | --- |")
        for column, pct in report.completeness_pct.items():
            lines.append(f"| {column} | {pct:.1f}% |")
    else:
        lines.append("_No columns available._")
    lines.append("")

    lines.append("## Top companies")
    lines.append("")
    _append_count_table(lines, report.top_companies, "Company")

    lines.append("## Top positions")
    lines.append("")
    _append_count_table(lines, report.top_positions, "Position")

    lines.append("## Seniority distribution (inferred)")
    lines.append("")
    _append_count_table(lines, report.seniority_distribution, "Bucket")

    lines.append("## Connections per year")
    lines.append("")
    if report.connections_per_year:
        lines.append("| Year | Count |")
        lines.append("| --- | --- |")
        for item in report.connections_per_year:
            lines.append(f"| {item.label} | {item.count} |")
    else:
        lines.append("_No connection dates available._")
    lines.append("")

    lines.append("## Notes")
    lines.append("")
    for note in report.generated_notes:
        lines.append(f"- {note}")
    lines.append("")

    return "\n".join(lines)


def _append_count_table(lines: list[str], items: list[CountItem], label: str) -> None:
    if items:
        lines.append(f"| {label} | Count |")
        lines.append("| --- | --- |")
        for item in items:
            lines.append(f"| {item.label} | {item.count} |")
    else:
        lines.append("_None available._")
    lines.append("")
