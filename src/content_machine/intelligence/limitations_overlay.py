"""Fable ruling 2026-08-01 (Part C, rekeyed under Part A of the 2026-08-01
follow-up ruling): the Founder-approved, per-item limitations overlay.

Five per-item limitations the Founder approved never reached the brief
because ``SourceItem`` has no ``limitations`` field and never will (this is
deliberate -- see ``docs/privacy.md``: the field would have to be either
authored into every fixture/connector item, or invented after the fact, and
both routes risk it silently becoming a pipeline input). Fable APPROVED a
private, run-specific, human-authored overlay instead: a small JSON sidecar
file, ``<output_dir>/limitations-overlay.json``, that a human (the Founder)
places alongside ``brief.md`` in the weekly run directory BEFORE the CLI
writes that run's outputs.

Part A ruling (rejecting an earlier topic_id-keyed implementation): a
limitation is a fact about an ARTIFACT, authored per paper -- the overlay
file's ``limitations`` object is keyed by ``item_id`` (``SourceItem.item_id``)
and MEANS it, not ``topic_id``. Keying by topic would silently rescope a
per-artifact statement to whatever a merge-driven cluster happens to contain,
and ``topic_id`` is run-scoped and does not exist until a run has executed --
which would make it impossible to author the overlay file BEFORE the run,
breaking the fail-closed ordering below. Each ``item_id`` key is resolved to
its CONTAINING topic (via the caller-supplied ``item_topic_map`` -- see
``weekly.WeeklyRunResult.item_topic_map``, built from every
``TopicCluster.member_ids``) so the validated result can be composed into
that topic's rendered block. Two distinct ``item_id``s resolving to the SAME
topic is legitimate (not a duplicate): both limitations are rendered, one
line each, in the file's own authored order.

This module owns parsing and validating that file. It is loaded and invoked
from ``cli/main.py``'s ``intelligence_weekly_run`` command, which is the only
place with both the file's expected location (``--output-dir``) and the
already-computed ``WeeklyRunResult`` (``run_id``, ``item_topic_map``, and the
brief's own rendered topic ids) needed to validate it.

Composition is FAIL-CLOSED and ALL-OR-NOTHING: any validation failure aborts
the entire render (the caller must write nothing) rather than silently
dropping the bad entry or falling back to "no limitations". A validated
overlay is composed ONLY into the rendered ``brief.md`` string (see
``brief.render_markdown``'s ``limitations_overlay`` parameter, which stays
TOPIC-keyed -- ``dict[str, list[str]]`` -- since that is what a rendered
Tier 1/2/3 block is addressed by) -- it is never added to the structured
``WeeklyBrief`` object, so it can never reach ``brief.json``,
``topics.jsonl``, the pipeline (``cluster``/``ranking``/``tiers``),
``privacy.strip_for_model``, or ``providers/``. Every error message this
module raises names an ``item_id``/``run_id`` only -- never the limitation
text itself, which may be human-authored prose about a real, sensitive
private run.

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
    """What a successfully validated overlay file yields.

    ``limitations`` is ALREADY RESOLVED from the file's own item_id-keyed
    object to the ``{topic_id: [limitation_text, ...]}`` shape
    ``brief.render_markdown``'s ``limitations_overlay`` argument expects --
    each entry is one item_id's limitation text, appended to its containing
    topic's list in the overlay file's own authored (JSON object) order. Two
    distinct item_ids resolving to the same topic_id therefore yield a
    two-element list for that topic (both rendered, one line each) -- this is
    legitimate, not a duplicate. ``item_count`` is the number of item_id
    entries in the SOURCE file (not the number of topics after resolution),
    matching what ``run-manifest.json``'s additive ``limitations_overlay``
    field records (see ``weekly.LimitationsOverlayManifest``). ``None`` from
    :func:`load_limitations_overlay` -- not this type -- is how "file
    absent, nothing to compose" is represented; this type only ever
    describes a present, fully validated overlay."""

    limitations: dict[str, list[str]]
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


def rendered_topic_ids(brief: WeeklyBrief) -> set[str]:
    """Every topic_id that actually receives its own per-item block in the
    rendered Tier 1/2/3 sections of ``brief.md`` -- the only place a
    limitation line can be attached (see ``brief._render_tier1_section``/
    ``_render_tier2_section``/``_render_tier3_section``). A topic that was
    only discarded (ranked below the Top N) gets one summary line with no
    per-item block, so it is never "rendered" for this purpose: an overlay
    item_id whose CONTAINING TOPIC is only in ``brief.discarded`` fails
    closed as "unrendered" (see :func:`load_limitations_overlay`)."""
    ids = {item.topic_id for item in brief.tier1}
    ids.update(item.topic_id for item in brief.tier2)
    ids.update(item.topic_id for item in brief.tier3)
    return ids


def load_limitations_overlay(
    path: Path,
    *,
    run_id: str,
    item_topic_map: dict[str, str],
    rendered_topic_ids: set[str],
) -> LimitationsOverlayResult | None:
    """Load, parse, fully validate, and RESOLVE the limitations overlay at
    ``path``.

    ``item_topic_map`` (``weekly.WeeklyRunResult.item_topic_map``) is this
    run's full addressable corpus IN ITEM TERMS: every ``item_id`` that
    appears anywhere in the run's output, mapped to the topic_id of the
    cluster that contains it -- the union of every cluster's ``member_ids``,
    covering discarded topics too. ``rendered_topic_ids`` (see that function)
    is the subset of topic_ids that actually received a Tier 1/2/3 block.

    Returns ``None`` if ``path`` does not exist -- an absent file is
    documented as NOT a failure: the render proceeds with no limitations
    composed. Every other failure mode raises :class:`LimitationsOverlayError`
    and the caller must abort the ENTIRE render (write nothing):

    1. an overlay item_id not in ``item_topic_map`` (outside this run's
       corpus -- names an item this run never produced at all);
    2. an overlay item_id in the corpus whose CONTAINING TOPIC is not in
       ``rendered_topic_ids`` (e.g. a topic only in ``brief.discarded`` --
       there is no block to attach the limitation to);
    3. a duplicate item_id (or any other) key in the overlay file;
    4. an empty or whitespace-only limitation string (absence of a
       limitation means the key is omitted entirely, never an empty value);
    5. the file is present but unparseable or schema-invalid;
    6. the overlay's own ``run_id`` does not match ``run_id`` (the run
       actually being rendered).

    On success, ``LimitationsOverlayResult.limitations`` is already resolved
    to the topic-keyed ``{topic_id: [limitation_text, ...]}`` shape
    ``brief.render_markdown`` expects -- each item_id's text is appended to
    its containing topic's list in the overlay file's own authored order, so
    two distinct item_ids naming the same topic both render (legitimate, not
    a duplicate).
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

    resolved: dict[str, list[str]] = {}
    for item_id, text in overlay.limitations.items():
        if not text.strip():
            raise LimitationsOverlayError(
                f"{path} has an empty or whitespace-only limitation for item_id "
                f"{item_id!r}; aborting render (omit the key entirely to mean 'no "
                "limitation for this item')."
            )
        topic_id = item_topic_map.get(item_id)
        if topic_id is None:
            raise LimitationsOverlayError(
                f"{path} references item_id {item_id!r}, which is not part of this run's "
                f"corpus (run_id={run_id!r}); aborting render."
            )
        if topic_id not in rendered_topic_ids:
            raise LimitationsOverlayError(
                f"{path} references item_id {item_id!r}, whose containing topic is in this "
                f"run's corpus but was not rendered in the brief (run_id={run_id!r}); "
                "aborting render."
            )
        resolved.setdefault(topic_id, []).append(text)

    return LimitationsOverlayResult(
        limitations=resolved,
        overlay_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        item_count=len(overlay.limitations),
        provenance=overlay.provenance,
    )
