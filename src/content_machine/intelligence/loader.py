"""Safe loading of intelligence signal items (mirrors ``ingestion.csv_loader``).

Policy for a malformed item (this IS the "extra fields handled per contract"
contract):

* an item with unknown/extra fields is SKIPPED and reported as a
  :class:`LoadIssue` listing the offending FIELD NAMES ONLY (never values),
  plus the item's index in the file;
* an item missing a required field is skipped with an issue naming the
  missing field(s);
* an unparseable ``publication_date`` does NOT skip the item -- it yields the
  item with ``publication_date=None`` plus an issue, since ``publication_date``
  is optional by design;
* an unparseable/missing ``detection_date`` DOES skip the item (it is
  required and has no ``None`` state);
* unknown ``topic_tags`` (outside :data:`TOPIC_TAXONOMY`) are an issue and the
  item is skipped.

The run never crashes on bad input: only a file-level failure (missing file,
invalid JSON, wrong top-level shape) raises :class:`SignalLoadError`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from content_machine.intelligence.models import TOPIC_TAXONOMY, RelevanceProfile, SourceItem
from content_machine.intelligence.normalize import parse_iso_date

# Repo-root-relative default: the synthetic stand-in profile. The real
# Founder profile is private and must never enter this repo (see
# examples/intelligence-profile-synthetic.json).
DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[3] / "examples" / "intelligence-profile-synthetic.json"
)

IssueKind = Literal[
    "unknown_fields",
    "missing_field",
    "invalid_date",
    "unknown_topic_tag",
    "invalid_value",
]

# A tag outside the closed TOPIC_TAXONOMY is only echoed verbatim into a
# LoadIssue when it is itself taxonomy-SHAPED (lowercase ascii letters,
# digits, hyphens, <=32 chars) -- i.e. plausibly just a new/misspelled tag,
# not arbitrary attacker- or user-supplied text. Anything else (e.g. an
# email-shaped string, mixed case, unicode, or an over-long value) is
# reported only as the literal "<non-conforming>" placeholder, once per
# offending tag, so the count is still visible without leaking the value.
_TAG_SHAPE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_NON_CONFORMING_TAG_PLACEHOLDER = "<non-conforming>"


class LoadIssue(BaseModel):
    """A single non-fatal problem found while loading a signal item.

    ``fields`` names the offending field(s), or, for ``unknown_topic_tag``,
    the offending tag string(s) -- but ONLY when a tag is itself
    taxonomy-shaped (see ``_TAG_SHAPE_RE``); a tag that is not taxonomy-shaped
    could be arbitrary text (an email address, an injection attempt, etc.),
    so it is replaced by the literal ``"<non-conforming>"`` placeholder
    instead of being echoed verbatim. ``message`` never contains free-text
    field values (titles, summaries, publisher ids, etc.).
    """

    model_config = ConfigDict(extra="forbid")

    item_index: int
    kind: IssueKind
    fields: list[str] = Field(default_factory=list)
    message: str


class LoadResult(BaseModel):
    """Outcome of loading a signals file."""

    model_config = ConfigDict(extra="forbid")

    items: list[SourceItem] = Field(default_factory=list)
    issues: list[LoadIssue] = Field(default_factory=list)


class SignalLoadError(Exception):
    """Raised for file-level failures: missing file, invalid JSON, or a
    top-level shape that is not a JSON array. References the path only --
    never file content."""


class ProfileLoadError(Exception):
    """Raised for file-level failures loading a :class:`RelevanceProfile`."""


def load_signals(path: str | Path) -> LoadResult:
    """Load a JSON array of intelligence signal items.

    Raises :class:`SignalLoadError` for file-level failures. Row-level
    problems are collected into ``result.issues`` and never abort the load.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SignalLoadError(f"File not found: {path}") from exc
    except OSError as exc:
        raise SignalLoadError(f"Could not read file: {path}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SignalLoadError(f"File is not valid JSON: {path}") from exc

    if not isinstance(raw, list):
        raise SignalLoadError(f"Expected a JSON array of signal items: {path}")

    allowed_fields = set(SourceItem.model_fields.keys())
    required_fields = {
        name for name, field in SourceItem.model_fields.items() if field.is_required()
    }

    items: list[SourceItem] = []
    issues: list[LoadIssue] = []

    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="invalid_value",
                    fields=[],
                    message="Item is not a JSON object; skipped.",
                )
            )
            continue

        present = set(entry.keys())

        extra = sorted(present - allowed_fields)
        if extra:
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="unknown_fields",
                    fields=extra,
                    message=f"Item has {len(extra)} unknown field(s); skipped.",
                )
            )
            continue

        missing = sorted(required_fields - present)
        if missing:
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="missing_field",
                    fields=missing,
                    message=f"Item is missing {len(missing)} required field(s); skipped.",
                )
            )
            continue

        raw_tags = entry.get("topic_tags")
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="invalid_value",
                    fields=["topic_tags"],
                    message="Field 'topic_tags' must be a list of strings; item skipped.",
                )
            )
            continue

        unknown_tags = sorted(set(raw_tags) - TOPIC_TAXONOMY)
        if unknown_tags:
            reported_tags = [
                tag if _TAG_SHAPE_RE.match(tag) else _NON_CONFORMING_TAG_PLACEHOLDER
                for tag in unknown_tags
            ]
            non_conforming_count = sum(1 for tag in unknown_tags if not _TAG_SHAPE_RE.match(tag))
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="unknown_topic_tag",
                    fields=reported_tags,
                    message=(
                        f"Item references {len(unknown_tags)} topic tag(s) outside the "
                        f"closed taxonomy ({non_conforming_count} non-conforming); skipped."
                    ),
                )
            )
            continue

        working = dict(entry)

        detection_raw = working.get("detection_date")
        detection_parsed = parse_iso_date(detection_raw) if isinstance(detection_raw, str) else None
        if detection_parsed is None:
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="invalid_date",
                    fields=["detection_date"],
                    message=(
                        "Field 'detection_date' is missing or not a valid ISO-8601 "
                        "date; item skipped."
                    ),
                )
            )
            continue
        working["detection_date"] = detection_parsed

        pub_raw = working.get("publication_date")
        if pub_raw is None:
            working["publication_date"] = None
        else:
            pub_parsed = parse_iso_date(pub_raw) if isinstance(pub_raw, str) else None
            if pub_parsed is None:
                issues.append(
                    LoadIssue(
                        item_index=index,
                        kind="invalid_date",
                        fields=["publication_date"],
                        message=(
                            "Field 'publication_date' is not a valid ISO-8601 date; "
                            "value discarded (item kept)."
                        ),
                    )
                )
            working["publication_date"] = pub_parsed

        try:
            item = SourceItem.model_validate(working)
        except ValidationError as exc:
            bad_fields = sorted({str(err["loc"][0]) for err in exc.errors() if err.get("loc")})
            issues.append(
                LoadIssue(
                    item_index=index,
                    kind="invalid_value",
                    fields=bad_fields,
                    message=(
                        f"Item failed schema validation on {len(bad_fields)} field(s); skipped."
                    ),
                )
            )
            continue

        items.append(item)

    return LoadResult(items=items, issues=issues)


def load_profile(path: str | Path = DEFAULT_PROFILE_PATH) -> RelevanceProfile:
    """Load a :class:`RelevanceProfile` from JSON.

    Defaults to the synthetic example profile
    (``examples/intelligence-profile-synthetic.json``) -- the real Founder
    profile is private and must never enter this repo. Raises
    :class:`ProfileLoadError` (referencing only the path) on any file-level or
    schema failure.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileLoadError(f"Could not read profile file: {path}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileLoadError(f"Profile file is not valid JSON: {path}") from exc

    try:
        return RelevanceProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileLoadError(
            f"Profile file does not match the RelevanceProfile schema: {path}"
        ) from exc
