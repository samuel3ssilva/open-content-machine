"""Fable ruling 2026-08-01 (Part C): the Founder-approved, per-item
limitations overlay.

Five per-item limitations the Founder approved never reached the brief
because ``SourceItem`` has no ``limitations`` field and never will (this is
deliberate -- see ``docs/privacy.md``: the field would have to be either
authored into every fixture/connector item, or invented after the fact, and
both routes risk it silently becoming a pipeline input). Fable APPROVED a
private, run-specific, human-authored overlay instead: a small JSON sidecar
file, ``<output_dir>/limitations-overlay.json``, that a human (the Founder)
places alongside ``brief.md`` in the weekly run directory BEFORE the CLI
writes that run's outputs.

This module owns parsing and validating that file. It is loaded and invoked
from ``cli/main.py``'s ``intelligence_weekly_run`` command, which is the only
place with both the file's expected location (``--output-dir``) and the
already-computed ``WeeklyRunResult`` (``run_id``, and the brief's own topic
ids) needed to validate it.

Composition is FAIL-CLOSED and ALL-OR-NOTHING: any validation failure aborts
the entire render (the caller must write nothing) rather than silently
dropping the bad entry or falling back to "no limitations". A validated
overlay is composed ONLY into the rendered ``brief.md`` string (see
``brief.render_markdown``'s ``limitations_overlay`` parameter) -- it is never
added to the structured ``WeeklyBrief`` object, so it can never reach
``brief.json``, ``topics.jsonl``, the pipeline (``cluster``/``ranking``/
``tiers``), ``privacy.strip_for_model``, or ``providers/``. Every error
message this module raises names an ``item_id``/``run_id`` only -- never the
limitation text itself, which may be human-authored prose about a real,
sensitive private run.

This module performs no network I/O (offline, like every other module in
``intelligence/`` -- see ``tests/test_connectors_no_network.py``'s full-tree
scan of this package) and reads only the one file path it is given -- never
``data/private/``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from content_machine.intelligence.brief import WeeklyBrief

#: The only schema version this module currently accepts. A future,
#: incompatible overlay shape would need a new accepted value here -- see
#: ``_OverlaySchema.schema_version``.
SCHEMA_VERSION = 1

#: The filename this overlay is ALWAYS looked for under ``--output-dir``,
#: alongside ``brief.md`` -- never configurable, per the ruling.
OVERLAY_FILENAME = "limitations-overlay.json"


class LimitationsOverlayError(RuntimeError):
    """Raised for every fail-closed condition this module enforces: an
    overlay item_id outside the run's corpus, an item_id in the corpus but
    never rendered, a duplicate item_id key, an empty/whitespace-only
    limitation string, an unparseable or schema-invalid file, or an overlay
    ``run_id`` that does not match the run being rendered. The message never
    contains limitation text -- only item_id/run_id/path, per the ruling."""


class _OverlaySchema(BaseModel):
    """The overlay file's own JSON shape, exactly as specified by the
    ruling. ``extra="forbid"`` so an unexpected top-level key is itself a
    schema-invalid failure rather than being silently ignored."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    run_id: str
    provenance: Literal["human_authored"]
    limitations: dict[str, str]


@dataclass(frozen=True)
class LimitationsOverlayResult:
    """What a successfully validated overlay file yields: the
    ``{item_id: limitation_text}`` mapping to compose into ``brief.md``
    (``brief.render_markdown``'s ``limitations_overlay`` argument), plus the
    three facts ``run-manifest.json``'s additive ``limitations_overlay``
    field needs (see ``weekly.LimitationsOverlayManifest``). ``None`` from
    :func:`load_limitations_overlay` -- not this type -- is how "file
    absent, nothing to compose" is represented; this type only ever
    describes a present, fully validated overlay."""

    limitations: dict[str, str]
    overlay_sha256: str
    item_count: int
    provenance: str


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` for ``json.loads``: a duplicate key anywhere in
    the document raises immediately, rather than the default ``json.loads``
    behaviour of silently keeping the LAST occurrence. Applies to every JSON
    object in the document (including the top level and the nested
    ``limitations`` object) -- a duplicate anywhere is a failure."""
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise LimitationsOverlayError(
                f"duplicate key {key!r} in {OVERLAY_FILENAME}; aborting render (the entire "
                "render aborts on any overlay validation failure -- see "
                "LimitationsOverlayError)."
            )
        seen[key] = value
    return seen


def _parse_overlay_bytes(raw_bytes: bytes, path: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except LimitationsOverlayError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError (a ValueError subclass).
        # The underlying exception's own message is deliberately NOT
        # included: it can quote arbitrary bytes from the file (which may
        # carry human-authored limitation prose), and the ruling requires
        # errors to name item_id/run_id/path only, never file content.
        raise LimitationsOverlayError(
            f"{path} is unparseable ({type(exc).__name__}); aborting render."
        ) from exc
    if not isinstance(data, dict):
        raise LimitationsOverlayError(
            f"{path} is not a JSON object at its top level; aborting render."
        )
    return data


def _validate_overlay_schema(data: dict[str, Any], path: Path) -> _OverlaySchema:
    try:
        return _OverlaySchema.model_validate(data)
    except ValidationError as exc:
        # Only the error COUNT is named, never pydantic's own rendered
        # error detail, which can echo back offending field values
        # (including limitation text) in its "input_value=..." context.
        raise LimitationsOverlayError(
            f"{path} is schema-invalid ({exc.error_count()} error(s)); aborting render."
        ) from exc


def corpus_topic_ids(brief: WeeklyBrief) -> set[str]:
    """This run's full addressable corpus: every topic_id that appears
    ANYWHERE in the run's output -- Tier 1, Tier 2, Tier 3 (the Top N), plus
    every topic discarded below the Top-N window. An overlay item_id outside
    this set names a topic this run never produced at all."""
    ids = {item.topic_id for item in brief.tier1}
    ids.update(item.topic_id for item in brief.tier2)
    ids.update(item.topic_id for item in brief.tier3)
    ids.update(item.topic_id for item in brief.discarded)
    return ids


def rendered_topic_ids(brief: WeeklyBrief) -> set[str]:
    """The subset of :func:`corpus_topic_ids` that actually receives its own
    per-item block in the rendered Tier 1/2/3 sections of ``brief.md`` --
    the only place a limitation line can be attached (see
    ``brief._render_tier1_section``/``_render_tier2_section``/
    ``_render_tier3_section``). Discarded topics get one summary line with
    no per-item block, so they are in the corpus but never "rendered" for
    this purpose."""
    ids = {item.topic_id for item in brief.tier1}
    ids.update(item.topic_id for item in brief.tier2)
    ids.update(item.topic_id for item in brief.tier3)
    return ids


def load_limitations_overlay(
    path: Path,
    *,
    run_id: str,
    corpus_topic_ids: set[str],
    rendered_topic_ids: set[str],
) -> LimitationsOverlayResult | None:
    """Load, parse, and fully validate the limitations overlay at ``path``.

    Returns ``None`` if ``path`` does not exist -- an absent file is
    documented as NOT a failure: the render proceeds with no limitations
    composed. Every other failure mode raises :class:`LimitationsOverlayError`
    and the caller must abort the ENTIRE render (write nothing):

    1. an overlay item_id not in ``corpus_topic_ids``;
    2. an overlay item_id in the corpus but not in ``rendered_topic_ids``;
    3. a duplicate item_id (or any other) key in the overlay file;
    4. an empty or whitespace-only limitation string (absence of a
       limitation means the key is omitted entirely, never an empty value);
    5. the file is present but unparseable or schema-invalid;
    6. the overlay's own ``run_id`` does not match ``run_id`` (the run
       actually being rendered).
    """
    if not path.exists():
        return None

    raw_bytes = path.read_bytes()
    data = _parse_overlay_bytes(raw_bytes, path)
    overlay = _validate_overlay_schema(data, path)

    if overlay.run_id != run_id:
        raise LimitationsOverlayError(
            f"{path} has run_id={overlay.run_id!r}, which does not match the run being "
            f"rendered (run_id={run_id!r}); aborting render."
        )

    for item_id, text in overlay.limitations.items():
        if not text.strip():
            raise LimitationsOverlayError(
                f"{path} has an empty or whitespace-only limitation for item_id "
                f"{item_id!r}; aborting render (omit the key entirely to mean 'no "
                "limitation for this item')."
            )
        if item_id not in corpus_topic_ids:
            raise LimitationsOverlayError(
                f"{path} references item_id {item_id!r}, which is not part of this run's "
                f"corpus (run_id={run_id!r}); aborting render."
            )
        if item_id not in rendered_topic_ids:
            raise LimitationsOverlayError(
                f"{path} references item_id {item_id!r}, which is in this run's corpus but "
                f"was not rendered in the brief (run_id={run_id!r}); aborting render."
            )

    return LimitationsOverlayResult(
        limitations=dict(overlay.limitations),
        overlay_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        item_count=len(overlay.limitations),
        provenance=overlay.provenance,
    )
