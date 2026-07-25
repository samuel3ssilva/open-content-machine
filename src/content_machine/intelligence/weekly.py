"""M7: the weekly Intelligence Run engine -- pure orchestration of the
existing M4/M5/M6 pipeline into one deterministic, offline, idempotent
weekly run, plus its auditable :class:`RunManifest`.

This module does NOT change ranking weights, the Moonshot rubric, privacy
policy, or the ranking/tiers/brief/library logic -- it only COMPOSES the
already-frozen pipeline (``loader`` -> ``cluster`` -> ``ranking`` ->
``tiers`` -> ``brief`` -> ``library``) into a single ``run_weekly`` call plus
an atomic, idempotent output writer.

=== 1. Seven-day window + timezone (documented boundary convention) ========

``reference_date`` is a caller-supplied ISO date or datetime string (only
its CALENDAR DATE component is used -- see :func:`resolve_window`). The
7-day analysis window is::

    [reference_date 00:00 minus 7 days,  reference_date 00:00)

i.e. INCLUSIVE of ``window_start``, EXCLUSIVE of ``window_end``, both
anchored at LOCAL MIDNIGHT of ``reference_date``'s calendar date in the
caller-supplied IANA ``timezone`` (via the stdlib ``zoneinfo`` -- never the
system local zone, never a network lookup). A signal is IN the window iff
its ``publication_date`` (falling back to ``detection_date`` when absent) --
compared as local midnight of that date in ``timezone`` -- satisfies
``window_start <= signal_midnight < window_end``. See
``test_intelligence_weekly.py`` for the exact-boundary tests (one second
before ``window_start`` excluded; ``window_start`` itself included;
``window_end`` excluded).

Default cadence (a DOCUMENTED DEFAULT only -- this module and its CLI
command never wait for Saturday, never install a scheduler, never run
themselves): **Saturday 18:00 America/Sao_Paulo** -- see
:data:`DEFAULT_CADENCE_DESCRIPTION` / :data:`DEFAULT_TIMEZONE`.

=== 2. Run manifest (auditable, §RunManifest) ==============================

:data:`RunManifest.run_id` is a STABLE, deterministic sha256 of
``(week_label, input_fingerprint, code_version, profile_version)`` -- same
inputs, same ``run_id``, always; never random, never wall-clock-derived.
``input_fingerprint`` is a sha256 over the CANONICALIZED, SORTED signal set
-- see :func:`compute_input_fingerprint` -- computed over every signal
passed to :func:`run_weekly` (before window filtering), so the run's
identity reflects its whole input corpus, not just the days that happened to
fall in this week's window. ``execution_timestamp`` is the ONE place a real
timestamp is allowed: it is an INPUT parameter the caller passes in (e.g.
the CLI layer reads the wall clock once and hands it in) -- this module
itself contains no ``datetime.now()``/``date.today()`` call anywhere, so
``run_weekly`` stays pure and deterministic given fixed inputs.

=== 3. Idempotency + atomicity (§4) =========================================

Re-running the SAME week (same signals, profile, prior library, and code
version) reproduces the SAME ``run_id`` and the SAME logical brief/library
state. :func:`write_weekly_run_outputs` detects an already-completed run by
comparing ``result.manifest.run_id`` against any ``run-manifest.json``
already in ``output_dir``: if they match and ``regenerate`` is False (the
default), nothing is read or written and no library/score-history/audit row
is ever duplicated. ``regenerate=True`` redoes the write, but this week's
append-only score-history/audit rows REPLACE (never duplicate) any existing
rows already recorded for the SAME ``week_label`` -- see
:func:`_jsonl_lines_excluding_week`. Every output is written via
:func:`_atomic_write_all`: every file is staged to a temp file in the same
directory first, and only once EVERY temp file has been written
successfully are any of them renamed into place with ``os.replace`` -- a
failure while staging any one output leaves every destination path in
``output_dir`` completely untouched, with no partial or temp files left
behind.

No raw private artifact bodies are persisted here -- this module writes only
what ``library.py``/``brief.py`` already produce, both of which uphold that
guarantee themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from content_machine import __version__ as _PACKAGE_VERSION
from content_machine.intelligence.brief import (
    LibraryMovementsSection,
    WeeklyBrief,
    build_weekly_brief,
    render_json,
    render_markdown,
)
from content_machine.intelligence.cluster import cluster_items, to_ranking_inputs
from content_machine.intelligence.library import (
    AuditRow,
    LibraryUpdateResult,
    ScoreHistoryRow,
    TopicLibraryEntry,
    library_movements_for_brief,
    update_library,
)
from content_machine.intelligence.models import RelevanceProfile, SourceItem
from content_machine.intelligence.ranking import rank_topics
from content_machine.intelligence.tiers import assign_tiers

#: The one place review_status is spelled out; mirrors brief.py's own
#: REVIEW_STATUS -- nothing here (or anywhere upstream) may set this to
#: anything else, and no code path here publishes, sends, or schedules
#: anything.
REVIEW_STATUS = "awaiting_founder_review"

#: Documented default cadence -- NOT an active scheduler. Neither
#: ``run_weekly`` nor the ``intelligence weekly-run`` CLI command waits for
#: Saturday or installs anything; a caller (or an OS cron entry -- see the
#: CLI command's ``--help`` epilog) decides when to invoke this.
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_CADENCE_DESCRIPTION = "Saturday 18:00 America/Sao_Paulo"

WINDOW_DAYS = 7

#: The six core outputs this task writes (per output-dir). ``movements.md``
#: and ``discarded.jsonl`` are library v0.2 (topic merge/decay/deltas) --
#: out of scope for this task; see the module docstring.
OUTPUT_FILENAMES: tuple[str, ...] = (
    "brief.md",
    "brief.json",
    "topics.jsonl",
    "score-history.jsonl",
    "audit.jsonl",
    "run-manifest.json",
)


class WindowBounds(NamedTuple):
    """The resolved, timezone-aware bounds of one week's analysis window
    (``start`` inclusive, ``end`` exclusive -- see the module docstring)."""

    start: datetime
    end: datetime


def _parse_reference_date(reference_date: str) -> date:
    """Extract the CALENDAR DATE component of ``reference_date``, accepting
    either a bare ISO date (``"2026-07-12"``) or an ISO datetime
    (``"2026-07-12T18:00:00"``, with or without an offset) -- the
    time-of-day, if any, is deliberately ignored: the documented cadence
    (Saturday 18:00) is when a run is EXPECTED to be triggered, not a window
    boundary, which always sits at local midnight (see ``resolve_window``).
    """
    try:
        return date.fromisoformat(reference_date)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(reference_date)
    except ValueError as exc:
        raise ValueError(
            "reference_date must be a valid ISO-8601 date or datetime string "
            f"(got {reference_date!r})"
        ) from exc
    return parsed.date()


def resolve_window(reference_date: str, timezone: str) -> WindowBounds:
    """Resolve the 7-day analysis window for ``reference_date`` in
    ``timezone`` -- see the module docstring's boundary convention. Uses
    ``zoneinfo`` (stdlib) only: never the system local zone, never a network
    call. Raises ``ValueError`` if ``reference_date`` cannot be parsed or
    ``timezone`` is not a known IANA zone.
    """
    tz = ZoneInfo(timezone)
    ref = _parse_reference_date(reference_date)
    window_end = datetime(ref.year, ref.month, ref.day, tzinfo=tz)
    window_start = window_end - timedelta(days=WINDOW_DAYS)
    return WindowBounds(start=window_start, end=window_end)


def is_within_window(moment: datetime, window: WindowBounds) -> bool:
    """The ONE inclusive/exclusive comparison the module docstring's window
    convention is built on: ``window.start <= moment < window.end``. Public
    (not a private helper) specifically so ``test_intelligence_weekly.py``
    can exercise the exact boundary semantics directly with hand-built
    datetimes (one second before ``window.start`` excluded, ``window.start``
    itself included, ``window.end`` excluded) -- independent of
    ``SourceItem``'s day-only granularity, which can never construct a
    sub-day offset on its own."""
    return window.start <= moment < window.end


def _signal_effective_datetime(item: SourceItem, timezone: str) -> datetime:
    """A signal's effective date -- ``publication_date``, falling back to
    the always-present ``detection_date`` -- as local midnight in
    ``timezone``. ``SourceItem`` carries no time-of-day, so every signal
    dated on a given calendar day is either wholly in or wholly out of the
    window."""
    effective_date = item.publication_date or item.detection_date
    return datetime(
        effective_date.year, effective_date.month, effective_date.day, tzinfo=ZoneInfo(timezone)
    )


def filter_signals_to_window(
    signals: list[SourceItem], window: WindowBounds, timezone: str
) -> list[SourceItem]:
    """Keep only the signals whose effective date falls in ``window`` (see
    the module docstring). Order-preserving; never mutates ``signals``."""
    return [
        item
        for item in signals
        if is_within_window(_signal_effective_datetime(item, timezone), window)
    ]


def derive_week_label(reference_date: str, timezone: str) -> str:
    """Derive an ISO week label (e.g. ``"2026-W29"``, the format
    ``library.py`` already expects -- see its ``week_label`` docstrings)
    from ``reference_date``'s calendar date in ``timezone``, using the
    standard ISO-8601 week-numbering calendar (``date.isocalendar``).

    ASSUMPTION (documented, not in the ticket's explicit CLI option list):
    the ``intelligence weekly-run`` CLI command takes no separate
    ``--week-label`` option, so it derives one from ``--reference-date`` /
    ``--timezone`` via this function rather than asking the caller to keep
    two dates in sync by hand. ``run_weekly`` itself still takes
    ``week_label`` as an explicit, independent parameter for callers who
    want to supply their own.
    """
    ref = _parse_reference_date(reference_date)
    iso_year, iso_week, _iso_weekday = ref.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _canonical_signal_repr(item: SourceItem) -> str:
    """A canonical (sorted-keys, separator-fixed) JSON representation of one
    signal, used only as the unit fingerprinted by
    :func:`compute_input_fingerprint` -- never written to disk, never
    logged."""
    return json.dumps(item.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def compute_input_fingerprint(signals: list[SourceItem]) -> str:
    """Order-independent sha256 fingerprint over the full ``signals`` set
    (BEFORE window filtering): every signal is canonicalized, the resulting
    strings are SORTED (so shuffling ``signals`` produces the identical
    fingerprint), then joined and hashed. Computed over every signal handed
    to :func:`run_weekly`, not just the ones inside this week's window, so
    the run's identity reflects the whole input corpus it was given."""
    canonical = sorted(_canonical_signal_repr(item) for item in signals)
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def compute_run_id(
    week_label: str, input_fingerprint: str, code_version: str, profile_version: str
) -> str:
    """Deterministic ``run_id``: sha256 over ``(week_label,
    input_fingerprint, code_version, profile_version)`` in that fixed order.
    Never random, never derived from a timestamp -- the same four inputs
    always produce the same ``run_id``, and changing any ONE of them changes
    it."""
    payload = "\x1f".join([week_label, input_fingerprint, code_version, profile_version])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_code_version(code_version: str | None) -> str:
    """The code/version marker for the manifest: the caller-supplied
    override if given, else the installed package version
    (``content_machine.__version__``)."""
    return code_version if code_version is not None else _PACKAGE_VERSION


class RunManifest(BaseModel):
    """Auditable record of one weekly run (see the module docstring's #2).
    Every field is deterministic given the same inputs EXCEPT
    ``execution_timestamp``, which records when the run was actually
    executed -- separate from ``reference_date``/the analysis window, and
    the one field this module lets a caller pass a real timestamp into."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    input_fingerprint: str
    code_version: str
    profile_version: str
    week_label: str
    reference_date: str
    timezone: str
    #: Resolved window bounds (ISO 8601, timezone-aware): start inclusive,
    #: end exclusive -- see the module docstring's boundary convention.
    window_start: str
    window_end: str
    #: When this run was actually executed -- an INPUT the caller supplies
    #: (e.g. the CLI reads the wall clock once), never computed here.
    execution_timestamp: str
    signal_count: int
    topic_count: int
    tier1_count: int
    library_created_count: int
    library_lifecycle_transition_count: int
    library_score_change_count: int
    library_rejected_reappearance_count: int
    review_status: str = REVIEW_STATUS


class WeeklyRunResult(BaseModel):
    """The full result of one :func:`run_weekly` call: the brief (both
    structured and rendered forms), the updated library state, this week's
    new append-only rows, and the auditable :class:`RunManifest`."""

    model_config = ConfigDict(extra="forbid")

    manifest: RunManifest
    brief: WeeklyBrief
    brief_markdown: str
    brief_json: str
    library_entries: list[TopicLibraryEntry]
    new_score_history_rows: list[ScoreHistoryRow]
    new_audit_rows: list[AuditRow]


def run_weekly(
    signals: list[SourceItem],
    profile: RelevanceProfile,
    prior_library: list[TopicLibraryEntry],
    week_label: str,
    reference_date: str,
    timezone: str,
    execution_timestamp: str,
    *,
    code_version: str | None = None,
) -> WeeklyRunResult:
    """Run the full weekly pipeline deterministically and offline.

    Composes, in order: window filtering -> ``cluster.cluster_items`` ->
    ``cluster.to_ranking_inputs`` -> ``ranking.rank_topics`` ->
    ``tiers.assign_tiers`` -> ``library.update_library`` ->
    ``brief.build_weekly_brief``. Never re-derives, never re-weights, and
    never changes any decision already frozen in those modules -- this
    function only wires their existing contracts together and adds the
    7-day window + the auditable :class:`RunManifest`.

    ``signals`` is the FULL candidate set (e.g. every ``SourceItem`` loaded
    from ``--signals``); this function filters it to the 7-day window
    resolved from ``reference_date``/``timezone`` (see
    :func:`resolve_window`) before handing it to the pipeline.
    ``prior_library`` is the previously persisted library state (``[]`` for
    the very first run). ``execution_timestamp`` is a caller-supplied input
    (see :class:`RunManifest`); this function never reads the wall clock.

    Pure and deterministic given fixed inputs: the same (signals, profile,
    prior_library, week_label, reference_date, timezone, code_version)
    always yields a byte-identical brief/library/manifest, aside from the
    caller-supplied ``execution_timestamp`` field itself.
    """
    window = resolve_window(reference_date, timezone)
    windowed_signals = filter_signals_to_window(signals, window, timezone)

    items_by_id = {item.item_id: item for item in windowed_signals}
    clusters = cluster_items(windowed_signals)
    clusters_by_topic_id = {cluster.topic_id: cluster for cluster in clusters}
    ranking_inputs = [to_ranking_inputs(cluster, items_by_id) for cluster in clusters]
    ranked = rank_topics(ranking_inputs, profile)
    tiered = assign_tiers(ranked, clusters_by_topic_id, items_by_id)

    library_result: LibraryUpdateResult = update_library(
        tiered, ranked, clusters_by_topic_id, items_by_id, week_label, prior_library
    )
    library_movements: LibraryMovementsSection = library_movements_for_brief(library_result)

    brief = build_weekly_brief(
        tiered, ranked, clusters_by_topic_id, items_by_id, week_label, library_movements
    )

    resolved_code_version = _resolve_code_version(code_version)
    input_fingerprint = compute_input_fingerprint(signals)
    run_id = compute_run_id(
        week_label, input_fingerprint, resolved_code_version, profile.profile_version
    )

    movement_counts: Counter[str] = Counter(
        row.event_type for row in library_result.new_audit_rows
    )

    manifest = RunManifest(
        run_id=run_id,
        input_fingerprint=input_fingerprint,
        code_version=resolved_code_version,
        profile_version=profile.profile_version,
        week_label=week_label,
        reference_date=reference_date,
        timezone=timezone,
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        execution_timestamp=execution_timestamp,
        signal_count=len(windowed_signals),
        topic_count=len(clusters),
        tier1_count=sum(1 for topic in tiered if topic.tier_assignment.tier == "tier_1"),
        library_created_count=movement_counts.get("created", 0),
        library_lifecycle_transition_count=movement_counts.get("lifecycle_transition", 0),
        library_score_change_count=movement_counts.get("score_change", 0),
        library_rejected_reappearance_count=movement_counts.get("rejected_reappearance", 0),
    )

    return WeeklyRunResult(
        manifest=manifest,
        brief=brief,
        brief_markdown=render_markdown(brief),
        brief_json=render_json(brief),
        library_entries=library_result.entries,
        new_score_history_rows=library_result.new_score_history_rows,
        new_audit_rows=library_result.new_audit_rows,
    )


class WeeklyWriteOutcome(BaseModel):
    """What :func:`write_weekly_run_outputs` actually did -- reported by the
    CLI command so a re-run's idempotent skip is visible, not silent."""

    model_config = ConfigDict(extra="forbid")

    wrote: bool
    skipped_reason: str | None = None
    files_written: list[str] = Field(default_factory=list)
    run_id: str


def _existing_manifest_run_id(output_dir: Path) -> str | None:
    """The ``run_id`` recorded in ``output_dir/run-manifest.json``, or
    ``None`` if the file is absent or unreadable/invalid (treated the same
    as "no completed run yet" -- never a fatal error)."""
    manifest_path = output_dir / "run-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    run_id = data.get("run_id")
    return run_id if isinstance(run_id, str) else None


def _jsonl_lines_excluding_week(path: Path, week_label: str) -> list[str]:
    """Existing JSONL lines at ``path`` (``[]`` if it doesn't exist yet)
    with any row for ``week_label`` dropped. Used only when
    ``regenerate=True``: redoing THIS week's run REPLACES its own rows
    instead of duplicating them, while every other week's rows are left
    untouched."""
    if not path.exists():
        return []
    kept: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("week_label") != week_label:
            kept.append(line)
    return kept


def _atomic_write_all(staged: dict[Path, str]) -> None:
    """Write every path in ``staged`` all-or-nothing (see the module
    docstring's #3). Every file is first written to a temp file in the SAME
    directory as its destination (so the final rename is atomic on any
    single POSIX filesystem, never a cross-device copy) and fsync'd before
    closing. Only once EVERY temp file has been staged successfully are any
    of them renamed into place with ``os.replace``. On any failure -- while
    staging OR while renaming -- every temp file created so far is removed
    before the exception propagates, so a partial failure never leaves a
    stray ``.tmp`` file or a half-written destination behind."""
    staged_temp: list[tuple[Path, Path]] = []
    try:
        for final_path, content in staged.items():
            fd, tmp_name = tempfile.mkstemp(
                dir=str(final_path.parent), prefix=f".{final_path.name}.", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            staged_temp.append((tmp_path, final_path))

        for tmp_path, final_path in staged_temp:
            os.replace(tmp_path, final_path)
    except BaseException:
        for tmp_path, _final_path in staged_temp:
            tmp_path.unlink(missing_ok=True)
        raise


def write_weekly_run_outputs(
    result: WeeklyRunResult, output_dir: str | Path, *, regenerate: bool = False
) -> WeeklyWriteOutcome:
    """Write ``result``'s six outputs to ``output_dir`` (see
    :data:`OUTPUT_FILENAMES`), atomically and idempotently.

    IDEMPOTENCY: if ``output_dir`` already contains a ``run-manifest.json``
    whose ``run_id`` matches ``result.manifest.run_id`` and ``regenerate``
    is False (the default), this is a no-op: nothing is read or written
    beyond the existing manifest, and ``outcome.wrote`` is False -- no
    library/score-history/audit row is ever duplicated by re-running the
    same week. Pass ``regenerate=True`` to redo it anyway; even then, this
    week's score-history/audit rows REPLACE (never duplicate) any rows
    already recorded for the SAME ``week_label`` (see
    :func:`_jsonl_lines_excluding_week`), and ``topics.jsonl``/``brief.*``
    are always the full current state, so they are naturally idempotent on
    re-write.

    ATOMICITY: delegated to :func:`_atomic_write_all` -- either all six
    files land, or none of them change.
    """
    output_dir = Path(output_dir)
    run_id = result.manifest.run_id
    week_label = result.manifest.week_label

    if not regenerate and _existing_manifest_run_id(output_dir) == run_id:
        return WeeklyWriteOutcome(
            wrote=False,
            skipped_reason=(
                f"a completed run with run_id={run_id} already exists in {output_dir}; "
                "pass --regenerate to redo it"
            ),
            files_written=[],
            run_id=run_id,
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    score_history_path = output_dir / "score-history.jsonl"
    audit_path = output_dir / "audit.jsonl"

    prior_score_lines = _jsonl_lines_excluding_week(score_history_path, week_label)
    prior_audit_lines = _jsonl_lines_excluding_week(audit_path, week_label)

    new_score_lines = [row.model_dump_json() for row in result.new_score_history_rows]
    new_audit_lines = [row.model_dump_json() for row in result.new_audit_rows]

    def _jsonl_text(lines: list[str]) -> str:
        return "".join(f"{line}\n" for line in lines)

    topics_lines = [
        entry.model_dump_json()
        for entry in sorted(result.library_entries, key=lambda entry: entry.topic_id)
    ]

    staged: dict[Path, str] = {
        output_dir / "brief.md": result.brief_markdown,
        output_dir / "brief.json": result.brief_json,
        output_dir / "topics.jsonl": _jsonl_text(topics_lines),
        score_history_path: _jsonl_text([*prior_score_lines, *new_score_lines]),
        audit_path: _jsonl_text([*prior_audit_lines, *new_audit_lines]),
        output_dir / "run-manifest.json": result.manifest.model_dump_json(indent=2) + "\n",
    }

    _atomic_write_all(staged)

    return WeeklyWriteOutcome(
        wrote=True,
        skipped_reason=None,
        files_written=sorted(path.name for path in staged),
        run_id=run_id,
    )
