"""Deterministic audience statistics and report rendering.

``analyze`` consumes only the anonymized (safe-zone) connections plus structural
metadata from the load/normalization steps. By construction it never sees names,
emails, or URLs, so the Markdown and JSON renders cannot contain them. Every
inferred aggregate (seniority, role family) is labeled with its confidence, and
a mandatory caveat notes that a connection's existence is not evidence of
interest in the creator's content.

The report is the *private* artifact: it may contain small-group aggregates and
so is treated as sensitive-by-default (docs/privacy.md). Producing a shareable
artifact is a separate, explicit sanitization step (see
``content_machine.audience.public_report``).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, Field

from content_machine.audience.normalize import NormalizationResult
from content_machine.ingestion.csv_loader import LoadResult
from content_machine.privacy.anonymizer import AnonymizationResult, AnonymizedConnection

MANDATORY_CAVEAT = (
    "Connection presence does not imply interest in your content. "
    "Seniority buckets are heuristic inferences."
)

# The three limitations that MUST always appear (docs/privacy.md rule 5).
_REQUIRED_LIMITATIONS: tuple[str, ...] = (
    "Connections are not evidence of interest in your content.",
    "Role and family classifications are keyword heuristics with a stated "
    "confidence level; treat them as directional, not authoritative.",
    "Company and title data reflect the export snapshot and may be stale.",
)

_TOP_N = 10

# Human-readable labels for segment names / rationales. Fall back to the raw
# value if a key is missing so output never crashes on an unexpected bucket.
_FAMILY_LABELS: dict[str, str] = {
    "founder_executive": "Founders & Executives",
    "engineering_data_ai": "Engineering, Data & AI",
    "product": "Product",
    "marketing_growth_content": "Marketing, Growth & Content",
    "sales_bd_partnerships": "Sales, BD & Partnerships",
    "design_ux": "Design & UX",
    "operations_people_finance_legal": "Operations, People, Finance & Legal",
    "education_research": "Education & Research",
    "other": "Other professions",
    "unknown": "Unclassified",
}

_SENIORITY_LABELS: dict[str, str] = {
    "founder_owner": "founders & owners",
    "c_level": "C-level",
    "vp_head_director": "VPs, heads & directors",
    "manager_lead": "managers & leads",
    "individual_contributor": "individual contributors",
    "entry_student": "entry-level & students",
    "unknown": "unspecified seniority",
}


class CountItem(BaseModel):
    """A label with its count, for top-N tables and distributions."""

    label: str
    count: int


class ReportTotals(BaseModel):
    total_rows: int
    unique_connections: int
    duplicates: int


class Segment(BaseModel):
    """A candidate audience cluster (role family x seniority).

    ``evidence`` is deterministic aggregate data only (contributing counts and
    top normalized titles with their frequencies) -- never a person's data.
    """

    name: str
    size: int
    share: float
    evidence: list[str] = Field(default_factory=list)
    rationale: str


class AudienceReport(BaseModel):
    """Machine-readable *private* audience report. Aggregates only; no
    identifiers. May contain small-group counts -- sanitize before sharing."""

    totals: ReportTotals
    valid_rows: int
    invalid_rows: int
    completeness_pct: dict[str, float] = Field(default_factory=dict)
    top_companies: list[CountItem] = Field(default_factory=list)
    top_roles: list[CountItem] = Field(default_factory=list)
    seniority_distribution: list[CountItem] = Field(default_factory=list)
    seniority_inferred: bool = True
    role_family_distribution: list[CountItem] = Field(default_factory=list)
    confidence_distribution: list[CountItem] = Field(default_factory=list)
    unknown_share: float = 0.0
    connections_per_year: list[CountItem] = Field(default_factory=list)
    candidate_segments: list[Segment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
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

    # Usable rows are those that became records; empty source rows were dropped
    # at load time and recorded as empty_row issues.
    valid_rows = total_rows
    invalid_rows = sum(1 for i in load_result.issues if i.kind == "empty_row")

    completeness = _completeness(load_result, normalization_result)

    top_companies = _top_counts((r.company for r in rows if r.company), _TOP_N)
    top_roles = _top_counts(
        (r.position.casefold() for r in rows if r.position), _TOP_N
    )

    seniority = _top_counts((r.seniority_bucket for r in rows), None)
    role_families = _top_counts((r.role_family for r in rows), None)
    confidences = _top_counts((r.role_confidence for r in rows), None)

    unknown_count = sum(1 for r in rows if r.role_family == "unknown")
    unknown_share = round(unknown_count / total_rows, 4) if total_rows else 0.0

    years = _year_counts(r.connected_year for r in rows)

    segments = _candidate_segments(rows, valid_rows)

    notes = _build_notes(anonymization_result, normalization_result, years)

    return AudienceReport(
        totals=ReportTotals(
            total_rows=total_rows,
            unique_connections=unique,
            duplicates=duplicates,
        ),
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        completeness_pct=completeness,
        top_companies=top_companies,
        top_roles=top_roles,
        seniority_distribution=seniority,
        seniority_inferred=True,
        role_family_distribution=role_families,
        confidence_distribution=confidences,
        unknown_share=unknown_share,
        connections_per_year=years,
        candidate_segments=segments,
        limitations=list(_REQUIRED_LIMITATIONS),
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


def _candidate_segments(
    rows: list[AnonymizedConnection], valid_rows: int
) -> list[Segment]:
    """Derive 3-5 candidate segments by crossing role family x seniority.

    Deterministic: cluster all rows by (role_family, seniority_bucket), rank by
    size (ties broken by family then seniority name), and take clusters with
    ``size >= max(5, 2% of valid rows)``, capped at 5. If fewer than 3 qualify,
    fall back to the top 3 non-empty clusters, excluding the unknown family
    unless nothing else exists.
    """
    if not rows:
        return []

    cluster_counts: Counter[tuple[str, str]] = Counter()
    cluster_titles: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for r in rows:
        key = (r.role_family, r.seniority_bucket)
        cluster_counts[key] += 1
        if r.position:
            cluster_titles[key][r.position.casefold()] += 1

    ranked = sorted(
        cluster_counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
    )

    threshold = max(5, math.ceil(0.02 * valid_rows)) if valid_rows else 5
    qualifying = [kv for kv in ranked if kv[1] >= threshold][:5]

    if len(qualifying) >= 3:
        chosen = qualifying
    else:
        non_unknown = [kv for kv in ranked if kv[0][0] != "unknown"]
        chosen = non_unknown[:3]
        if len(chosen) < 3:
            for kv in ranked:
                if kv[0][0] == "unknown" and kv not in chosen and len(chosen) < 3:
                    chosen.append(kv)

    segments: list[Segment] = []
    for (family, seniority), size in chosen:
        share = round(size / valid_rows, 4) if valid_rows else 0.0
        family_label = _FAMILY_LABELS.get(family, family)
        seniority_label = _SENIORITY_LABELS.get(seniority, seniority)
        evidence: list[str] = [
            f"role_family={family} x seniority={seniority}: {size} connections"
        ]
        top_titles = sorted(
            cluster_titles[(family, seniority)].items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:3]
        for title, count in top_titles:
            evidence.append(f"title '{title}' ({count})")
        segments.append(
            Segment(
                name=f"{family_label} — {seniority_label}",
                size=size,
                share=share,
                evidence=evidence,
                rationale=(
                    f"{size} connections ({share:.0%}) cluster in {family_label} "
                    f"at the {seniority_label} level."
                ),
            )
        )
    return segments


def _build_notes(
    anonymization_result: AnonymizationResult,
    normalization_result: NormalizationResult,
    years: list[CountItem],
) -> list[str]:
    notes: list[str] = [MANDATORY_CAVEAT]
    if anonymization_result.ephemeral_salt:
        notes.append(
            "An ephemeral salt was used: pseudonym IDs are NOT stable across "
            "runs. Set CONTENT_MACHINE_SALT in .env for stable IDs."
        )
    unparseable_dates = sum(
        1
        for i in normalization_result.issues
        if i.column == "connected_on" and i.kind == "parse_error"
    )
    if unparseable_dates:
        notes.append(
            f"{unparseable_dates} connection date(s) could not be parsed and are "
            "excluded from the per-year timeline."
        )
    if years:
        # The most recent year on the timeline is very likely a partial,
        # year-to-date count (the export was taken part-way through it), so it
        # should not be read as a full-year total. Wording is data-driven -- the
        # year comes from the data, never hardcoded.
        latest_year = years[-1].label
        notes.append(
            f"The most recent year on the timeline ({latest_year}) may be a "
            "partial, year-to-date count and is likely lower than a full year; "
            "do not read it as a completed-year total."
        )
    else:
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
    lines.append(f"- Valid rows: {report.valid_rows}")
    lines.append(f"- Invalid rows (empty): {report.invalid_rows}")
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

    lines.append("## Top roles")
    lines.append("")
    _append_count_table(lines, report.top_roles, "Role (normalized)")

    lines.append("## Role family distribution (inferred)")
    lines.append("")
    lines.append("_Heuristic keyword classification; see confidence below._")
    lines.append("")
    _append_count_table(lines, report.role_family_distribution, "Family")

    lines.append("## Seniority distribution (inferred)")
    lines.append("")
    _append_count_table(lines, report.seniority_distribution, "Bucket")

    lines.append("## Classification confidence")
    lines.append("")
    lines.append(f"- Unclassified (unknown family) share: {report.unknown_share:.1%}")
    lines.append("")
    _append_count_table(lines, report.confidence_distribution, "Confidence")

    lines.append("## Candidate segments")
    lines.append("")
    if report.candidate_segments:
        for seg in report.candidate_segments:
            lines.append(f"### {seg.name}")
            lines.append("")
            lines.append(f"- Size: {seg.size} ({seg.share:.1%})")
            lines.append(f"- Rationale: {seg.rationale}")
            lines.append("- Evidence:")
            for ev in seg.evidence:
                lines.append(f"  - {ev}")
            lines.append("")
    else:
        lines.append("_No segments met the minimum-size threshold._")
        lines.append("")

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

    lines.append("## Limitations")
    lines.append("")
    for limitation in report.limitations:
        lines.append(f"- {limitation}")
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
