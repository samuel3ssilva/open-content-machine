"""Deterministic normalization of raw connections.

This is plain local code (docs/privacy.md rule 3: deterministic before
generative). It trims and collapses whitespace, normalizes company names by
stripping common legal suffixes, derives a heuristic seniority bucket from the
job title, parses the LinkedIn connection date into a year, and flags exact
duplicate people. Every derived/inferred field is explicitly marked.
"""

from __future__ import annotations

import re
import unicodedata
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

# --- Title normalization pre-pass (shared with audience.classify) -----------
#
# ``normalize_title`` produces the single canonical string that BOTH role-family
# classification (audience.classify) and seniority extraction (below) match
# against, so the two never disagree about what the title says (ticket OPUS-1.1
# §3/§6). It is used only for *matching*; the raw ``position`` is still stored
# verbatim on the working record for private display. Steps, in order:
#
#   1. casefold;
#   2. strip accents (Unicode NFD, drop combining marks) so PT/ES variants like
#      "operações"/"diretor" match their accent-free rule tokens;
#   3. strip a trailing "@ Company" or " at Company" employer suffix (family is
#      a function, not an employer). We deliberately do NOT strip PT " na " / ES
#      " en " here: "en" is also the Spanish preposition "in" ("Ingeniero en
#      Sistemas") and stripping it would truncate real functional titles. Sonnet
#      may extend this with a company-name-aware rule later.
#   4. collapse separators (/ , | · – —) and hyphens to spaces;
#   5. fold known ambiguous abbreviations via *phrase* rules BEFORE token
#      expansion (esp. "biz dev"/"business dev" -> "business development", so the
#      "dev" -> "developer" expansion below cannot mis-route BD titles to
#      engineering);
#   6. expand a small, safe set of single-token abbreviations.
_COMPANY_SUFFIX_RE = re.compile(r"\s*(?:@\s*|\bat\s+)\S.*$")

# Enthusiasm/self-description clauses carry no functional evidence: being
# "passionate about technology" is not employment in technology. The clause and
# everything after it is discarded BEFORE matching, so a bare enthusiasm title
# classifies as unknown while "engenheira de software apaixonada por dados"
# still classifies through its real function. Fable ruling, Sprint 1.1
# (anti-forced-classification review); applied on accent-stripped text.
_ENTHUSIASM_CLAUSE_RE = re.compile(
    r"\s*\b(?:"
    r"apaixonad[oa]s?\s+(?:por|pel[oa])"
    r"|passionate\s+about"
    r"|entusiasta\s+(?:de|da|do|em|por)"
    r"|enthusiast\s+(?:of|about|in)"
    r"|amante\s+(?:de|da|do)"
    r"|fa\s+(?:de|da|do)"
    r"|aficionad[oa]\s+(?:por|em)"
    r"|lover\s+of"
    r")\b.*$"
)
_SEPARATORS_RE = re.compile(r"[\/,|·–—\-]+")

# Phrase-level normalizations applied before token expansion. Each maps a
# compiled pattern to its replacement. Order does not matter (disjoint inputs).
_PHRASE_NORMALIZATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:biz|business)\s+dev\b"), "business development"),
    (re.compile(r"\bbizdev\b"), "business development"),
)

# Single-token abbreviation expansions (token compared with trailing dots
# stripped). Values may be multi-word; they are re-joined afterwards. "bd" is
# intentionally absent -- it is handled by the phrase rules above, never blindly
# expanded (BD = business development only in the right context).
_ABBREV_EXPANSIONS: dict[str, str] = {
    "eng": "engineering",
    "dev": "developer",
    "mkt": "marketing",
    "rh": "recursos humanos",
    "ti": "tecnologia da informacao",
    "ml": "machine learning",
    "ia": "ai",
    "sr": "senior",
    "jr": "junior",
}


def strip_accents(value: str) -> str:
    """Return ``value`` with combining accent marks removed (Unicode NFD)."""
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_title(raw: str | None) -> str:
    """Canonicalize a job title for family/seniority matching.

    Pure and deterministic. Returns ``""`` for an empty/whitespace title. See the
    module-level comment above for the exact, ordered transformation. The output
    is accent-free, employer-suffix-free, separator-collapsed and abbreviation-
    expanded, so downstream keyword tables can stay explicit and explainable.
    """
    if not raw or not raw.strip():
        return ""
    text = strip_accents(raw.casefold())
    text = _ENTHUSIASM_CLAUSE_RE.sub("", text)
    text = _COMPANY_SUFFIX_RE.sub("", text)
    text = _SEPARATORS_RE.sub(" ", text)
    text = " ".join(text.split())
    for pattern, replacement in _PHRASE_NORMALIZATIONS:
        text = pattern.sub(replacement, text)
    tokens = [_ABBREV_EXPANSIONS.get(tok.rstrip("."), tok) for tok in text.split()]
    return " ".join(" ".join(tokens).split())


# Seniority keyword rules, evaluated top to bottom; first match wins. Each rule
# is (bucket, tuple of accent-free tokens matched against ``normalize_title``).
# These are heuristics only and always carry seniority_inferred=True.
#
# Buckets (exactly seven, plus "unknown"): founder_owner, c_level,
# vp_head_director, manager_lead, individual_contributor, entry_student,
# unknown. The former "senior_ic" and "ic" buckets are merged into
# individual_contributor.
#
# LEVEL is independent of FAMILY (ticket OPUS-1.1 §1): this table only decides
# seniority; it never assigns a role family. Documented tie-breaks:
#   * founder_owner is checked first, so ownership wins the level even in a
#     compound like "Founder & CTO" (-> founder_owner), matching the family
#     rule that ownership dominates (see audience.classify §6).
#   * vp_head_director is checked before c_level so "Vice President" resolves to
#     vp_head_director rather than matching "president" at the c_level tier;
#     a bare "President" (no "vice") still falls through to c_level.
#   * a bare "Partner"/"Sócio" matches NO rule (only "managing partner" ->
#     founder_owner), so an ownership-less partner has unknown seniority rather
#     than being over-promoted.
_SENIORITY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "founder_owner",
        (
            "founder",
            "co founder",
            "cofounder",
            "fundador",
            "fundadora",
            "cofundador",
            "cofundadora",
            "co fundador",
            "owner",
            "proprietor",
            "proprietario",
            "proprietaria",
            "managing partner",
            "socio gerente",
            "socio fundador",
        ),
    ),
    (
        "vp_head_director",
        (
            "vp",
            "vice president",
            "vice presidente",
            "head",
            "director",
            "diretor",
            "diretora",
        ),
    ),
    (
        "c_level",
        (
            "ceo",
            "cto",
            "cfo",
            "coo",
            "cmo",
            "ciso",
            "cio",
            "chro",
            "cro",
            "cpo",
            "cxo",
            "chief",
            "c level",
            "president",
            "presidente",
        ),
    ),
    (
        "manager_lead",
        (
            "manager",
            "gerente",
            "lead",
            "leader",
            "lider",
            "supervisor",
            "supervisora",
            "coordinator",
            "coordenador",
            "coordenadora",
            "principal",
        ),
    ),
    (
        "entry_student",
        (
            "intern",
            "estagiario",
            "estagiaria",
            "trainee",
            "junior",
            "student",
            "estudante",
            "graduate",
            "apprentice",
            "aprendiz",
            "bolsista",
        ),
    ),
    (
        "individual_contributor",
        (
            "senior",
            "staff",
            "pleno",
            "specialist",
            "especialista",
            "engineer",
            "engenheiro",
            "engenheira",
            "developer",
            "desenvolvedor",
            "desenvolvedora",
            "analyst",
            "analista",
            "designer",
            "consultant",
            "consultor",
            "consultora",
            "associate",
            "assistant",
            "assistente",
            "representative",
            "representante",
            "accountant",
            "contador",
            "contadora",
        ),
    ),
)

# LinkedIn's default connection-date format, e.g. "18 Apr 2024".
# Functional "X owner" titles where "owner" denotes role stewardship, not company
# ownership -- the founder_owner seniority (and the classifier's ownership tier)
# must NOT fire on these. Kept in sync with audience.classify's T0 guard.
_OWNER_FALSE_FRIENDS: tuple[str, ...] = (
    "product owner",
    "process owner",
    "service owner",
    "scrum owner",
)

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
    title = normalize_title(position)
    if not title:
        return "unknown"
    # Match against the shared normalized title so seniority is parsed from the
    # SAME string the family classifier sees (ticket OPUS-1.1 §6). Multi-word
    # tokens match as substrings; single tokens match on word-ish boundaries so
    # "lead" does not fire inside "leadership".
    for bucket, keywords in _SENIORITY_RULES:
        for kw in keywords:
            if kw == "owner" and any(ff in title for ff in _OWNER_FALSE_FRIENDS):
                continue
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
