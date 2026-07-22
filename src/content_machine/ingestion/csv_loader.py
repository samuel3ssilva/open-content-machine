"""Safe loading of LinkedIn-style connection exports.

Responsibilities (see docs/architecture.md §4):

- Detect the file encoding via a small fallback chain and reject binary /
  non-CSV input gracefully.
- Skip the preamble lines LinkedIn sometimes prepends before the real header
  ("Notes:" lines, blank lines).
- Map a tolerant set of header variants onto stable field names, while
  tracking which columns were actually present (an absent column is not the
  same as an empty value).
- Collect row-level issues instead of silently dropping rows. Issue messages
  never contain field values (privacy rule; docs/privacy.md §6).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# Encodings tried in order. utf-8-sig strips a BOM if present; latin-1 never
# fails to decode, so it is the guaranteed-terminating fallback.
_ENCODING_CHAIN: tuple[str, ...] = ("utf-8-sig", "utf-8", "latin-1")

# Canonical field name -> set of accepted header spellings (already normalized
# to lowercase with spaces/underscores collapsed; see _normalize_header).
_HEADER_ALIASES: dict[str, set[str]] = {
    "first_name": {"first name", "firstname", "first"},
    "last_name": {"last name", "lastname", "last"},
    "url": {"url", "profile url", "linkedin url", "public profile url"},
    "email": {"email address", "email", "e mail", "e-mail", "emailaddress"},
    "company": {"company", "organization", "organisation"},
    "position": {"position", "title", "job title", "role"},
    "connected_on": {"connected on", "connection date", "connected", "date connected"},
}

# A candidate header row must map at least this many recognized columns to be
# considered "the header" (guards against matching a stray preamble line).
_MIN_HEADER_MATCHES = 2

IssueKind = Literal["missing_value", "empty_row", "parse_error"]


class RowIssue(BaseModel):
    """A single non-fatal problem found while reading a row.

    ``message`` never contains field values, only structural information.
    """

    row_index: int
    column: str | None
    kind: IssueKind
    message: str


class RawConnection(BaseModel):
    """One raw connection record. Every field is optional by design: a missing
    column and an empty cell are both represented as ``None``, and the presence
    of a column is tracked separately in :class:`LoadResult.columns_present`."""

    first_name: str | None = None
    last_name: str | None = None
    url: str | None = None
    email: str | None = None
    company: str | None = None
    position: str | None = None
    connected_on: str | None = None
    row_index: int


class LoadResult(BaseModel):
    """Outcome of loading a CSV export."""

    rows: list[RawConnection] = Field(default_factory=list)
    columns_present: list[str] = Field(default_factory=list)
    issues: list[RowIssue] = Field(default_factory=list)
    encoding_used: str
    skipped_preamble_lines: int = 0


class CsvLoadError(Exception):
    """Raised when a file cannot be read as a plausible CSV at all.

    This is a user-facing error (bad path, unreadable bytes, no header). The
    message references the file and structure, never row content.
    """


def _normalize_header(raw: str) -> str:
    """Lowercase a header cell and collapse separators so variants compare
    equal (e.g. ``"E-mail Address"`` and ``"email_address"``)."""
    lowered = raw.strip().lower()
    for ch in ("_", "-"):
        lowered = lowered.replace(ch, " ")
    return " ".join(lowered.split())


def _decode(path: Path) -> tuple[str, str]:
    """Read ``path`` and return ``(text, encoding_used)`` using the fallback
    chain. Raises :class:`CsvLoadError` if the file is missing or looks binary.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise CsvLoadError(f"File not found: {path}") from exc
    except OSError as exc:
        raise CsvLoadError(f"Could not read file: {path}") from exc

    if b"\x00" in data:
        # NUL bytes indicate a binary file (or UTF-16); we do not support these.
        raise CsvLoadError(f"File does not look like text CSV: {path}")

    for encoding in _ENCODING_CHAIN:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # latin-1 cannot raise, so this is unreachable in practice.
    raise CsvLoadError(f"Could not decode file with known encodings: {path}")


def _find_header(
    lines: list[str],
) -> tuple[int, dict[int, str]] | None:
    """Locate the header row among leading lines.

    Returns ``(line_index, {column_index: canonical_name})`` for the first line
    that maps at least ``_MIN_HEADER_MATCHES`` recognized columns, or ``None``
    if no plausible header exists. Scans a bounded number of leading lines so a
    body-only file does not get misread as headerful.
    """
    scan_limit = min(len(lines), 25)
    for idx in range(scan_limit):
        line = lines[idx]
        if not line.strip():
            continue
        try:
            cells = next(csv.reader([line]))
        except csv.Error:
            continue
        mapping: dict[int, str] = {}
        for col_idx, cell in enumerate(cells):
            norm = _normalize_header(cell)
            for canonical, aliases in _HEADER_ALIASES.items():
                if norm in aliases and canonical not in mapping.values():
                    mapping[col_idx] = canonical
                    break
        if len(mapping) >= _MIN_HEADER_MATCHES:
            return idx, mapping
    return None


def load_csv(path: str | Path) -> LoadResult:
    """Load a LinkedIn-style connections CSV into a :class:`LoadResult`.

    Raises :class:`CsvLoadError` for user-level failures (missing file, binary
    content, no recognizable header). Row-level problems are collected into
    ``result.issues`` and never abort the load.
    """
    path = Path(path)
    text, encoding = _decode(path)

    # splitlines handles \n, \r\n and \r uniformly.
    all_lines = text.splitlines()
    if not all_lines:
        raise CsvLoadError(f"File is empty: {path}")

    found = _find_header(all_lines)
    if found is None:
        raise CsvLoadError(
            f"No recognizable header row found in {path}. Expected columns like "
            "'First Name', 'Last Name', 'Company', 'Position'."
        )
    header_line_index, column_map = found
    skipped = header_line_index

    columns_present = sorted(set(column_map.values()))

    # Re-parse the body (everything after the header line) as CSV so quoted
    # fields spanning commas are handled correctly.
    body_text = "\n".join(all_lines[header_line_index + 1 :])
    reader = csv.reader(io.StringIO(body_text))

    rows: list[RawConnection] = []
    issues: list[RowIssue] = []
    max_col = max(column_map) if column_map else -1

    row_index = 0
    for cells in reader:
        # Fully blank line -> empty row issue, skip.
        if not any(cell.strip() for cell in cells):
            issues.append(
                RowIssue(
                    row_index=row_index,
                    column=None,
                    kind="empty_row",
                    message="Row is empty and was skipped for record building.",
                )
            )
            row_index += 1
            continue

        values: dict[str, str | None] = {name: None for name in _HEADER_ALIASES}
        for col_idx, canonical in column_map.items():
            if col_idx < len(cells):
                cell = cells[col_idx].strip()
                values[canonical] = cell if cell else None

        # Record a missing_value issue for present columns that are empty here.
        for canonical in columns_present:
            if values[canonical] is None:
                issues.append(
                    RowIssue(
                        row_index=row_index,
                        column=canonical,
                        kind="missing_value",
                        message=f"Present column '{canonical}' had no value in this row.",
                    )
                )

        # Rows with more cells than the header declared are a mild parse concern.
        if len(cells) > max_col + 1 and any(
            cells[i].strip() for i in range(max_col + 1, len(cells))
        ):
            issues.append(
                RowIssue(
                    row_index=row_index,
                    column=None,
                    kind="parse_error",
                    message=(
                        f"Row had {len(cells)} columns; only {max_col + 1} were "
                        "mapped. Extra columns ignored."
                    ),
                )
            )

        rows.append(RawConnection(row_index=row_index, **values))
        row_index += 1

    return LoadResult(
        rows=rows,
        columns_present=columns_present,
        issues=issues,
        encoding_used=encoding,
        skipped_preamble_lines=skipped,
    )
