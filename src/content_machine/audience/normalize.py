"""Deterministic normalization of raw connections.

This is plain local code (docs/privacy.md rule 3: deterministic before
generative). It trims and collapses whitespace, normalizes company names by
stripping common legal suffixes, derives a heuristic seniority bucket from the
job title, parses the LinkedIn connection date into a year, and flags exact
duplicate people. Every derived/inferred field is explicitly marked.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, Field

from content_machine.ingestion.csv_loader import LoadResult, RawConnection, RowIssue

# Legal-entity suffixes stripped from company names (case-insensitive). Order
# does not matter; matching is done on tokenized, punctuation-light forms.
_LEGAL_SUFFIXES: tuple[str, ...] = (
    "incorporated",
    "inc",
    "corporation",
    "corp",
    "company",
    "co",
    "limited",
    "ltd",
    "ltda",
    "llc",
    "llp",
    "lp",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "s a",
    "s p a",
    "spa",
    "srl",
    "bv",
    "nv",
    "oy",
    "ab",
    "pty",
    "pte",
)

# Seniority keyword rules, evaluated top to bottom; first match wins. Each rule
# is (bucket, tuple of lowercase substrings matched against the title). These
# are heuristics only and always carry seniority_inferred=True.
#
# Buckets (exactly seven, plus "unknown"): founder_owner, c_level,
# vp_head_director, manager_lead, individual_contributor, entry_student,
# unknown. The former "senior_ic" and "ic" buckets are merged into
# individual_contributor.
_SENIORITY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("founder_owner", ("founder", "co-founder", "cofounder", "owner")),
    (
        "c_level",
        ("ceo", "cto", "cfo", "coo", "cmo", "ciso", "cio", "chief", "president"),
    ),
    ("vp_head_director", ("vp", "vice president", "director", "head of", "partner")),
    ("manager_lead", ("manager", "lead", "supervisor", "principal")),
    ("entry_student", ("student", "intern", "trainee", "apprentice", "graduate")),
    (
        "individual_contributor",
        (
            "senior",
            "sr",
            "staff",
            "specialist",
            "engineer",
            "developer",
            "analyst",
            "designer",
            "consultant",
            "associate",
            "coordinator",
            "assistant",
            "representative",
            "accountant",
        ),
    ),
)

# LinkedIn's default connection-date format, e.g. "18 Apr 2024".
_LINKEDIN_DATE = "%d %b %Y"

# Localized month-abbreviation -> month number, for tolerant parsing of dates
# like "12 jan 2023" (Portuguese) or "14 mai 2019". Covers English, Portuguese
# and Spanish abbreviations. Only the year is used downstream; the map exists to
# recognize a date shape without a full locale dependency.
_MONTH_ABBREVS: dict[str, int] = {
    # English
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # Portuguese
    "fev": 2, "abr": 4, "mai": 5, "ago": 8, "set": 9, "out": 10, "dez": 12,
    # Spanish
    "ene": 1, "dic": 12,
}

# "12 jan 2023", "14 mai. 2019" -> day, month token, 4-digit year.
_LOCALIZED_DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-zçÇ.]+)\s+(\d{4})\s*$")


class NormalizedConnection(BaseModel):
    """A cleaned connection record. Direct identifiers are still present here —
    this is the "working" zone (docs/architecture.md §3) and never leaves the
    machine. Anonymization happens downstream."""

    row_index: int
    first_name: str | None = None
    last_name: str | None = None
    url: str | None = None
    email: str | None = None
    company: str | None = None
    company_raw: str | None = None
    position: str | None = None
    seniority_bucket: str = "unknown"
    seniority_inferred: bool = True
    connected_on: str | None = None
    connected_year: int | None = None
    is_duplicate: bool = False


class NormalizationResult(BaseModel):
    """Normalized rows plus detected duplicate relationships.

    ``issues`` collects non-fatal normalization problems (currently: a present
    connection date that could not be parsed). Messages reference the row/column
    only, never values.
    """

    connections: list[NormalizedConnection] = Field(default_factory=list)
    duplicate_pairs: list[tuple[int, int]] = Field(default_factory=list)
    issues: list[RowIssue] = Field(default_factory=list)


def _collapse_ws(value: str | None) -> str | None:
    """Trim and collapse internal whitespace; empty becomes ``None``."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def normalize_company(value: str | None) -> str | None:
    """Return a company name with common legal suffixes removed.

    Matching is case-insensitive and punctuation-tolerant: trailing tokens like
    "Inc", "Ltd", "LLC", "S.A.", "Ltda" are dropped, possibly several in a row
    (e.g. "Foo Holdings, LLC" -> "Foo Holdings"). Returns ``None`` if nothing
    meaningful remains.
    """
    cleaned = _collapse_ws(value)
    if cleaned is None:
        return None

    tokens = cleaned.split()
    while tokens:
        # Compare the last token stripped of surrounding punctuation and dots.
        candidate = re.sub(r"[.,]", "", tokens[-1]).lower()
        if candidate in _LEGAL_SUFFIXES:
            tokens = tokens[:-1]
            continue
        break

    # Drop a dangling trailing comma left after removing a suffix.
    result = " ".join(tokens).strip().strip(",").strip()
    return result or None


def infer_seniority(position: str | None) -> str:
    """Derive a seniority bucket from a job title using keyword rules.

    Buckets: founder_owner, c_level, vp_head_director, manager_lead,
    individual_contributor, entry_student, unknown. This is a heuristic; callers
    must treat the result as an inference (``seniority_inferred=True``), never as
    ground truth. Returns "unknown" when no rule matches or the title is absent.
    """
    if not position:
        return "unknown"
    title = position.lower()
    # Use word-ish boundaries for short tokens to avoid false hits inside words.
    for bucket, keywords in _SENIORITY_RULES:
        for kw in keywords:
            if " " in kw:
                if kw in title:
                    return bucket
            elif re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", title):
                return bucket
    return "unknown"


def parse_connected_year(value: str | None) -> int | None:
    """Parse a LinkedIn connection date into a year.

    Accepts the LinkedIn default ("18 Apr 2024"), localized variants with
    Portuguese/Spanish month abbreviations ("12 jan 2023", "14 mai 2019"), and
    an ISO fallback ("2024-04-18" / "2024"). Returns ``None`` when the value is
    absent or unparseable; unparseable dates are not treated as fatal here (the
    caller records a normalization issue instead).
    """
    text = _collapse_ws(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, _LINKEDIN_DATE).year
    except ValueError:
        pass
    try:
        return date.fromisoformat(text).year
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}", text):
        return int(text)
    match = _LOCALIZED_DATE_RE.match(text)
    if match:
        token = match.group(2).casefold().rstrip(".")
        if token in _MONTH_ABBREVS:
            return int(match.group(3))
    return None


def _dedup_key(conn: NormalizedConnection) -> tuple[str, str, str] | None:
    """Identity key for duplicate detection: casefolded (first, last, company).

    Returns ``None`` when there is not enough identity to compare (no name at
    all), so near-empty rows are never collapsed together.
    """
    first = (conn.first_name or "").casefold().strip()
    last = (conn.last_name or "").casefold().strip()
    company = (conn.company or "").casefold().strip()
    if not first and not last:
        return None
    return first, last, company


def normalize(load_result: LoadResult) -> NormalizationResult:
    """Normalize every raw row and detect exact duplicate people.

    Duplicates are rows sharing an identical casefolded (first_name, last_name,
    company). The first occurrence is kept as canonical; every later occurrence
    is flagged ``is_duplicate=True`` and recorded as a
    ``(first_row_index, duplicate_row_index)`` pair.
    """
    connections: list[NormalizedConnection] = []
    issues: list[RowIssue] = []
    for raw in load_result.rows:
        conn = _normalize_one(raw)
        connections.append(conn)
        # A present but unparseable connection date -> connected_year is None
        # plus a non-fatal issue (references the column, never the value).
        if conn.connected_on is not None and conn.connected_year is None:
            issues.append(
                RowIssue(
                    row_index=raw.row_index,
                    column="connected_on",
                    kind="parse_error",
                    message="Present column 'connected_on' held an unparseable date.",
                )
            )

    duplicate_pairs: list[tuple[int, int]] = []
    seen: dict[tuple[str, str, str], int] = {}
    for conn in connections:
        key = _dedup_key(conn)
        if key is None:
            continue
        if key in seen:
            conn.is_duplicate = True
            duplicate_pairs.append((seen[key], conn.row_index))
        else:
            seen[key] = conn.row_index

    return NormalizationResult(
        connections=connections, duplicate_pairs=duplicate_pairs, issues=issues
    )


def _normalize_one(raw: RawConnection) -> NormalizedConnection:
    company_raw = _collapse_ws(raw.company)
    position = _collapse_ws(raw.position)
    return NormalizedConnection(
        row_index=raw.row_index,
        first_name=_collapse_ws(raw.first_name),
        last_name=_collapse_ws(raw.last_name),
        url=_collapse_ws(raw.url),
        email=_collapse_ws(raw.email),
        company=normalize_company(company_raw),
        company_raw=company_raw,
        position=position,
        seniority_bucket=infer_seniority(position),
        seniority_inferred=True,
        connected_on=_collapse_ws(raw.connected_on),
        connected_year=parse_connected_year(raw.connected_on),
        is_duplicate=False,
    )
