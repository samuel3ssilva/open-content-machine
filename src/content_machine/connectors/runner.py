"""Batch discovery runner: per-source isolation, deterministic ordering, and
the per-run coverage report (Gate D §8, commit 2).

:func:`run_discovery` is the ONLY place in ``connectors`` that orchestrates
more than one source in a single call. Every adapter is isolated: an adapter
that raises ANY exception while discovering never aborts the batch, and its
failure is recorded as exactly one :class:`~content_machine.connectors.failures.SourceFailure`
for that source while every other source's results are unaffected (§8's
literal invariant, pinned in ``tests/test_connectors_runner.py``).

**IMPLEMENTER ASSUMPTIONS (spec §8 names the function signature
``run_discovery(adapters, permission_registry, source_registry, request)`` but
leaves the exact shape of ``adapters``/``request`` to the implementer -- the
spec's own §5.1 ``DiscoveryRequest`` is bound to exactly ONE ``source_id``,
which cannot itself be the ``request`` a multi-source batch call receives):

1. ``request`` here is :class:`BatchDiscoveryRequest` -- the window and
   per-source ceilings shared by every source in one run, WITHOUT a
   ``source_id``. For each adapter, :func:`run_discovery` builds the
   per-source ``models.DiscoveryRequest`` that spec §5.1 actually describes,
   copying that adapter's own ``source_id`` in.
2. ``adapters`` is any ``Sequence[ConnectorAdapter]`` -- a ``Protocol`` with a
   ``source_id`` attribute and a ``discover(request) -> AdapterDiscoveryOutcome``
   method, defined in THIS module (not in ``synthetic/``) because it is the
   contract future REAL adapters must also satisfy, not a synthetic-only
   concept. ``synthetic/adapters.py`` is one implementer of this protocol;
   nothing here imports ``synthetic``.
3. ``consecutive_failure_count`` (spec §8's ``SourceCoverageReport`` field)
   requires cross-run memory a single ``run_discovery`` call cannot have on
   its own, so the caller may pass ``prior_consecutive_failures`` (a
   ``source_id -> count`` mapping, default empty) computed from ITS OWN prior
   coverage reports; this run adds 1 on a fresh failure or resets to 0 on
   success/skip. Wiring that persistence is out of scope for this gate (see
   the module docstring's scope boundary below).
4. A source whose adapter raises mid-discovery is treated as a TOTAL failure
   for this run: whatever partial results the adapter may have produced
   in-process are discarded entirely (never surfaced), because a partial,
   unbounded-provenance result set cannot be trusted to represent "what
   happened" -- this is the deliberate distinction between
   ``FailureKind.partial_batch`` (an adapter that broke partway through and
   is recorded purely as a failure, zero results) and the ordinary, NON-error
   ``truncated``/``dropped_count`` reporting on a SUCCESSFUL
   :class:`AdapterDiscoveryOutcome` that simply hit its configured ceiling
   (spec §5.1's "overflow is never silent").

**Scope boundary (spec §8, restated so it is not lost in this module):**
:class:`SourceCoverageReport` is its own object. Nothing in this module (or
anywhere in ``connectors``) imports or writes to ``brief.py``, ``weekly.py``,
or the CLI -- wiring coverage into the published brief is explicitly deferred
to the gate that ships the first real adapter.

Deterministic ordering (rule F): results, failures, audit events, and
coverage rows are always returned SORTED by explicit keys (never by adapter
list order or dict/set iteration), and every timestamp is a caller-supplied
input (``occurred_at``) -- this module contains no ``datetime.now()``/
``date.today()``/``random``/``uuid4``/``hash()``-on-strings call anywhere.

**Gate D round-1 security correction (B1/C4).** A Fable security review
proved a permission-bypass path: nothing checked that a
``DiscoveryResult.source_id`` an adapter returned matched the adapter's OWN
``source_id`` -- an adapter registered (and approved) as ``src_approved``
could return a result self-labeled ``source_id="src_REVOKED"``, and
``enforce_discovery_fields`` would look up (and apply) THAT source's
permission instead of the one actually authorized for this adapter. Three
changes close this:

1. Every source must now be present in ``source_registry`` (previously
   accepted and never used -- the root cause the review named) before its
   adapter is even invoked; an unregistered source is skipped exactly like
   an unapproved one.
2. Immediately after ``adapter.discover()`` returns, ANY result whose
   ``source_id`` differs from ``adapter.source_id`` fails the WHOLE source
   closed: zero results admitted, one ``SourceFailure``
   (``FailureKind.source_id_mismatch``) recorded, never a
   retry-eligible/transient condition -- this is adapter compromise, not an
   ordinary field violation.
3. ``DiscoveryResult.permission_ref`` is no longer trusted from the
   adapter's self-report at all: this function reconstructs it from the
   ``PermissionRegistry`` entry that actually authorized this run, via
   ``PermissionRegistry.get()``, before any further processing.

See ``permissions.py``'s ``FieldEnforcementReasonCode.status_not_approved``
for the companion fix inside ``enforce_discovery_fields`` itself, and
ADR 0005's round-1 findings section for the full writeup.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from content_machine.connectors.failures import FailureKind, SourceFailure
from content_machine.connectors.models import (
    DEFAULT_MAX_ITEMS_PER_SOURCE,
    DEFAULT_MAX_REQUESTS_PER_RUN,
    AuditEventKind,
    ConnectorAuditEvent,
    DiscoveryRequest,
    DiscoveryResult,
    PermissionRef,
)
from content_machine.connectors.permissions import PermissionRegistry, SourceMode
from content_machine.connectors.registry import SourceRegistry
from content_machine.connectors.sanitize import sanitize_error
from content_machine.intelligence.models import SecurityFlag

# --- batch request (see IMPLEMENTER ASSUMPTION 1 above) ---------------------


class BatchDiscoveryRequest(BaseModel):
    """The window + per-source ceilings shared by every source in one
    :func:`run_discovery` call. Deliberately has no ``source_id`` -- see the
    module docstring's assumption 1."""

    model_config = ConfigDict(extra="forbid")

    window_start: date
    window_end: date
    max_items_per_source: int = Field(default=DEFAULT_MAX_ITEMS_PER_SOURCE, gt=0)
    max_requests_per_source: int = Field(default=DEFAULT_MAX_REQUESTS_PER_RUN, gt=0)

    @field_validator("window_end")
    @classmethod
    def _window_end_after_start(cls, window_end: date, info: ValidationInfo) -> date:
        window_start = info.data.get("window_start")
        if window_start is not None and window_end <= window_start:
            raise ValueError(
                "window_end must be strictly after window_start (inclusive start, exclusive end)"
            )
        return window_end


# --- adapter contract ---------------------------------------------------


class AdapterDiscoveryOutcome(BaseModel):
    """What one adapter's ``discover()`` returns on SUCCESS (a raised
    exception is how an adapter signals failure -- see
    :class:`ConnectorAdapterError`). ``dropped_count``/``truncated`` report a
    NON-error ceiling hit (spec §5.1); they are never used to represent a
    partial failure (see the module docstring's assumption 4)."""

    model_config = ConfigDict(extra="forbid")

    results: tuple[DiscoveryResult, ...] = Field(default_factory=tuple)
    dropped_count: int = Field(default=0, ge=0)
    truncated: bool = False


class ConnectorAdapterError(Exception):
    """Raised by an adapter's ``discover()`` to signal that ITS source
    failed. Carries the closed :class:`FailureKind` and an advisory
    ``retry_eligible`` flag (never acted on automatically -- see
    ``failures.py``'s "no automatic retry anywhere" rule). Any OTHER
    exception an adapter raises is still caught by :func:`run_discovery`
    (per-source isolation must hold for every exception, not only this one)
    and is recorded as ``FailureKind.unavailable`` with ``retry_eligible=False``.
    """

    def __init__(self, kind: FailureKind, reason: str, *, retry_eligible: bool) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.retry_eligible = retry_eligible


@runtime_checkable
class ConnectorAdapter(Protocol):
    """The contract every discovery adapter -- synthetic today, real in a
    future gate -- must satisfy. Defined here, not in ``synthetic/``, because
    ``run_discovery`` depends on this shape regardless of which package
    implements it."""

    source_id: str

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome: ...


# --- coverage report ------------------------------------------------------


class SourceCoverage(BaseModel):
    """One source's outcome within a single :func:`run_discovery` call (spec
    §8's ``SourceCoverageReport`` per-source fields). Frozen (Gate D
    round-1 correction, B3): a result value, never mutated after
    construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    attempted: bool
    discovered: int = Field(default=0, ge=0)
    dropped_count: int = Field(default=0, ge=0)
    truncated: bool = False
    failed: bool = False
    failure_kind: FailureKind | None = None
    sanitized_reason: str = Field(default="", max_length=200)
    skipped_not_approved: bool = False
    consecutive_failure_count: int = Field(default=0, ge=0)
    review_overdue: bool = False


class SourceCoverageReport(BaseModel):
    """Per-source coverage for one batch, sorted by ``source_id`` -- its own
    object (see the module docstring's scope boundary: never wired into
    ``brief.py``/``weekly.py``/the CLI in this gate). Frozen (Gate D
    round-1 correction, B3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[SourceCoverage, ...] = Field(default_factory=tuple)


class BatchStatus(StrEnum):
    """The batch's overall outcome. Never all-or-nothing by construction: a
    batch with at least one success AND at least one failure is always
    ``partial`` -- see the §8 literal invariant test."""

    all_succeeded = "all_succeeded"
    partial = "partial"
    all_failed = "all_failed"


class BatchDiscoveryResult(BaseModel):
    """The full, deterministic outcome of one :func:`run_discovery` call.
    Frozen (Gate D round-1 correction, B3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BatchStatus
    results: tuple[DiscoveryResult, ...] = Field(default_factory=tuple)
    failures: tuple[SourceFailure, ...] = Field(default_factory=tuple)
    audit_events: tuple[ConnectorAuditEvent, ...] = Field(default_factory=tuple)
    coverage: SourceCoverageReport


# --- cross-item hostile checks (duplicate reference / conflicting dates) ----


def _with_security_flags(
    result: DiscoveryResult, security_flags: tuple[SecurityFlag, ...]
) -> DiscoveryResult:
    """Return a NEW, fully re-validated ``DiscoveryResult`` with
    ``security_flags`` replaced.

    Gate D round-1 correction (B3): this module previously used
    ``result.model_copy(update={...})`` here, which -- like every
    ``model_copy(update=...)`` call -- writes directly to internal state and
    skips every field validator, including on a ``frozen=True`` model (see
    ``test_connectors_contracts.py::test_discovery_result_model_copy_update_still_bypasses_frozen``).
    That made this module's own code the in-tree idiom a future engineer
    would copy to bypass validation. Re-validating construction (dump, then
    ``model_validate``) closes that: every bound on every field runs again.
    """
    return DiscoveryResult.model_validate(
        {**result.model_dump(), "security_flags": security_flags}
    )


def _with_permission_ref(result: DiscoveryResult, permission_ref: PermissionRef) -> DiscoveryResult:
    """Return a NEW, fully re-validated ``DiscoveryResult`` with
    ``permission_ref`` replaced by the RUNNER-CONSTRUCTED, authorized ref
    (Gate D round-1 correction, B1) -- see :func:`run_discovery`."""
    return DiscoveryResult.model_validate(
        {**result.model_dump(), "permission_ref": permission_ref.model_dump()}
    )


def _flag_batch_level_duplicates(
    results: Sequence[DiscoveryResult],
) -> tuple[DiscoveryResult, ...]:
    """Add ``duplicate_canonical_reference`` (and, when the dates disagree,
    ``conflicting_publication_date``) to every result that shares its
    ``canonical_reference`` with at least one other result IN THIS BATCH.

    This is necessarily a batch-level (not per-item) check -- a single
    ``DiscoveryResult`` cannot know about a sibling it was never compared
    against. Existing flags on each result are preserved and the flag set is
    re-sorted by value for determinism (rule F: never leave the output order
    dependent on set iteration).
    """
    by_reference: dict[str, list[DiscoveryResult]] = {}
    for result in results:
        by_reference.setdefault(result.canonical_reference, []).append(result)

    updated: list[DiscoveryResult] = []
    for result in results:
        siblings = by_reference[result.canonical_reference]
        if len(siblings) < 2:
            updated.append(result)
            continue

        new_flags = set(result.security_flags)
        new_flags.add(SecurityFlag.duplicate_canonical_reference)
        dates = {sibling.publication_date for sibling in siblings}
        if len(dates) > 1:
            new_flags.add(SecurityFlag.conflicting_publication_date)

        updated.append(
            _with_security_flags(result, tuple(sorted(new_flags, key=lambda f: f.value)))
        )
    return tuple(updated)


# --- the runner -------------------------------------------------------------


def run_discovery(
    adapters: Sequence[ConnectorAdapter],
    permission_registry: PermissionRegistry,
    source_registry: SourceRegistry,
    request: BatchDiscoveryRequest,
    *,
    occurred_at: datetime,
    prior_consecutive_failures: Mapping[str, int] | None = None,
    review_as_of: date | None = None,
) -> BatchDiscoveryResult:
    """Run discovery over every adapter in ``adapters``, isolating failures
    per source (see the module docstring's literal invariant).

    ``source_registry`` is now an ACTIVE fail-closed gate (Gate D round-1
    correction, B1/C4), not merely accepted-and-unused: a source must be a
    known, curated entry in ``source_registry`` -- in addition to being
    ``approved`` in ``permission_registry`` -- before its adapter is ever
    invoked, and this function independently re-derives
    ``DiscoveryResult.permission_ref`` from ``permission_registry`` rather
    than trusting whatever an adapter self-reports. See the module
    docstring's round-1 correction section for the full rationale.

    ``occurred_at`` is the one timestamp every audit/failure row in this
    batch uses -- a caller-supplied input, never read from the wall clock
    here (rule F).
    """
    prior_failures = dict(prior_consecutive_failures or {})

    all_results: list[DiscoveryResult] = []
    all_failures: list[SourceFailure] = []
    audit_events: list[ConnectorAuditEvent] = []
    coverage_rows: list[SourceCoverage] = []

    for adapter in adapters:
        source_id = adapter.source_id

        # --- C4/B1: a source must be curated (source_registry) before its
        # adapter is even considered, independent of permission approval. ---
        if source_id not in source_registry:
            audit_events.append(
                ConnectorAuditEvent(
                    source_id=source_id,
                    event_kind=AuditEventKind.security,
                    reason_code="source_not_in_registry",
                    occurred_at=occurred_at,
                    detail=(
                        "adapter's source_id has no curated SourceRegistryEntry; skipped, "
                        "never invoked"
                    ),
                )
            )
            coverage_rows.append(
                SourceCoverage(
                    source_id=source_id,
                    attempted=False,
                    skipped_not_approved=True,
                    consecutive_failure_count=prior_failures.get(source_id, 0),
                    review_overdue=False,
                )
            )
            continue

        decision = permission_registry.authorize(
            source_id, SourceMode.discovery, as_of=review_as_of
        )
        audit_events.append(
            ConnectorAuditEvent(
                source_id=source_id,
                event_kind=AuditEventKind.permission,
                reason_code=decision.reason_code.value,
                occurred_at=occurred_at,
                detail="discovery authorization checked",
            )
        )

        if not decision.allowed:
            coverage_rows.append(
                SourceCoverage(
                    source_id=source_id,
                    attempted=False,
                    skipped_not_approved=True,
                    consecutive_failure_count=prior_failures.get(source_id, 0),
                    review_overdue=decision.review_overdue,
                )
            )
            continue

        if decision.review_overdue:
            audit_events.append(
                ConnectorAuditEvent(
                    source_id=source_id,
                    event_kind=AuditEventKind.permission,
                    reason_code="review_overdue",
                    occurred_at=occurred_at,
                    detail="permission review_due date has passed; run proceeded",
                )
            )

        per_source_request = DiscoveryRequest(
            source_id=source_id,
            window_start=request.window_start,
            window_end=request.window_end,
            max_items_per_source=request.max_items_per_source,
            max_requests=request.max_requests_per_source,
        )

        try:
            outcome = adapter.discover(per_source_request)
        except Exception as exc:  # noqa: BLE001 -- per-source isolation requires this
            if isinstance(exc, ConnectorAdapterError):
                kind = exc.kind
                retry_eligible = exc.retry_eligible
                # C6 (Gate D round-1 correction): sanitize_error(exc) on a
                # BaseException returns ONLY the type name ("ConnectorAdapterError"
                # for every single adapter failure, discarding exc.reason entirely).
                # exc.reason is the adapter's OWN structured, non-secret reason
                # string (e.g. "synthetic adapter simulated a discovery timeout"),
                # not an arbitrary exception message -- route it through
                # sanitize_error's STRING branch instead, which still redacts and
                # caps it, rather than the exception branch, which discards it.
                reason = sanitize_error(exc.reason)
            else:
                # A foreign/unexpected exception's own message is untrusted and
                # discarded entirely -- only its type name is safe to record.
                kind = FailureKind.unavailable
                retry_eligible = False
                reason = sanitize_error(exc)
            all_failures.append(
                SourceFailure(
                    source_id=source_id,
                    kind=kind,
                    sanitized_reason=reason,
                    retry_eligible=retry_eligible,
                    occurred_at=occurred_at,
                )
            )
            audit_events.append(
                ConnectorAuditEvent(
                    source_id=source_id,
                    event_kind=AuditEventKind.failure,
                    reason_code=kind.value,
                    occurred_at=occurred_at,
                    detail=reason,
                )
            )
            coverage_rows.append(
                SourceCoverage(
                    source_id=source_id,
                    attempted=True,
                    failed=True,
                    failure_kind=kind,
                    sanitized_reason=reason,
                    consecutive_failure_count=prior_failures.get(source_id, 0) + 1,
                    review_overdue=decision.review_overdue,
                )
            )
            continue

        # --- B1: reject a source outright if ANY result it returned claims a
        # source_id other than this adapter's own. Treated as adapter
        # compromise, never a retry-eligible transient condition -- the whole
        # source contributes zero results for this run. ---------------------
        mismatched_ids = sorted({r.source_id for r in outcome.results if r.source_id != source_id})
        if mismatched_ids:
            reason = sanitize_error(
                "adapter discover() returned at least one result whose source_id did not "
                "match the adapter's own registered source_id"
            )
            all_failures.append(
                SourceFailure(
                    source_id=source_id,
                    kind=FailureKind.source_id_mismatch,
                    sanitized_reason=reason,
                    retry_eligible=False,
                    occurred_at=occurred_at,
                )
            )
            audit_events.append(
                ConnectorAuditEvent(
                    source_id=source_id,
                    event_kind=AuditEventKind.security,
                    reason_code=FailureKind.source_id_mismatch.value,
                    occurred_at=occurred_at,
                    detail=(
                        "adapter identity mismatch: treated as adapter compromise; zero "
                        "results admitted for this source this run"
                    ),
                )
            )
            coverage_rows.append(
                SourceCoverage(
                    source_id=source_id,
                    attempted=True,
                    failed=True,
                    failure_kind=FailureKind.source_id_mismatch,
                    sanitized_reason=reason,
                    consecutive_failure_count=prior_failures.get(source_id, 0) + 1,
                    review_overdue=decision.review_overdue,
                )
            )
            continue

        # --- C3 (Gate D round-2 correction): enforce max_items_per_source as
        # a HARD ceiling here, not merely a value passed into the per-source
        # DiscoveryRequest and trusted to well-behaved adapters. An adapter
        # returning more than the ceiling (buggy or malicious) previously had
        # every item admitted in full. Truncation keeps the first N results
        # in the adapter's own returned order (deterministic; never resorted
        # for this purpose) -- the excess is added to dropped_count and
        # truncated is set True, exactly like the ordinary, non-error
        # ceiling-hit reporting an adapter is expected to do itself; this is
        # never reported as a failure. ------------------------------------
        ceiling = request.max_items_per_source
        if len(outcome.results) > ceiling:
            ceiling_kept_results = outcome.results[:ceiling]
            ceiling_excess_count = len(outcome.results) - ceiling
            ceiling_truncated = True
        else:
            ceiling_kept_results = outcome.results
            ceiling_excess_count = 0
            ceiling_truncated = False

        # --- B1: never trust the adapter's self-reported permission_ref --
        # reconstruct it from the PermissionRegistry entry that actually
        # authorized this run. `permission` cannot be None here: decision.allowed
        # is True only for a registered, approved SourcePermission.
        permission = permission_registry.get(source_id)
        if permission is None:  # pragma: no cover - see the guarantee above
            raise RuntimeError(
                "permission registry inconsistency: an authorized source's permission "
                "vanished mid-run"
            )
        authorized_permission_ref = PermissionRef(
            source_id=source_id,
            approved_mode=permission.approved_mode.value,
            status_at_discovery=permission.status.value,
        )
        authorized_results = tuple(
            _with_permission_ref(result, authorized_permission_ref)
            for result in ceiling_kept_results
        )

        accepted: list[DiscoveryResult] = []
        rejected_count = 0
        for result in authorized_results:
            enforcement = permission_registry.enforce_discovery_fields(result)
            if not enforcement.allowed:
                rejected_count += 1
                audit_events.append(
                    ConnectorAuditEvent(
                        source_id=source_id,
                        event_kind=AuditEventKind.permission,
                        reason_code=enforcement.reason_code.value,
                        occurred_at=occurred_at,
                        detail=(
                            "rejected DiscoveryResult, violating fields: "
                            f"{','.join(enforcement.violating_fields)}"
                        )[:280],
                    )
                )
                continue
            accepted.append(result)

        all_results.extend(accepted)
        coverage_rows.append(
            SourceCoverage(
                source_id=source_id,
                attempted=True,
                discovered=len(accepted),
                dropped_count=outcome.dropped_count + rejected_count + ceiling_excess_count,
                truncated=outcome.truncated or ceiling_truncated,
                consecutive_failure_count=0,
                review_overdue=decision.review_overdue,
            )
        )

    flagged_results = _flag_batch_level_duplicates(all_results)
    for result in flagged_results:
        if SecurityFlag.duplicate_canonical_reference in result.security_flags:
            audit_events.append(
                ConnectorAuditEvent(
                    source_id=result.source_id,
                    event_kind=AuditEventKind.security,
                    reason_code=SecurityFlag.duplicate_canonical_reference.value,
                    occurred_at=occurred_at,
                    detail="canonical_reference shared by more than one result in this batch",
                )
            )

    sorted_results = tuple(
        sorted(flagged_results, key=lambda r: (r.source_id, r.canonical_reference))
    )
    sorted_failures = tuple(sorted(all_failures, key=lambda f: f.source_id))
    sorted_audit_events = tuple(
        sorted(
            audit_events,
            key=lambda e: (e.source_id, e.event_kind.value, e.reason_code, e.detail),
        )
    )
    sorted_coverage = tuple(sorted(coverage_rows, key=lambda c: c.source_id))

    attempted_count = sum(1 for row in sorted_coverage if row.attempted)
    failed_count = len(sorted_failures)
    if failed_count == 0:
        status = BatchStatus.all_succeeded
    elif attempted_count > 0 and failed_count == attempted_count:
        status = BatchStatus.all_failed
    else:
        status = BatchStatus.partial

    return BatchDiscoveryResult(
        status=status,
        results=sorted_results,
        failures=sorted_failures,
        audit_events=sorted_audit_events,
        coverage=SourceCoverageReport(sources=sorted_coverage),
    )


# --- C5 (Gate D round-2 correction): human-readable coverage rendering ------

_COVERAGE_REPORT_TITLE = "Source Coverage Report"
_COVERAGE_COLUMNS = (
    ("source_id", 30),
    ("attempted", 9),
    ("discovered", 10),
    ("dropped", 7),
    ("truncated", 9),
    ("failure_kind", 20),
    ("skipped", 7),
    ("review_overdue", 14),
)


def format_coverage_report(report: SourceCoverageReport) -> str:
    """Render a :class:`SourceCoverageReport` as a deterministic,
    human-readable text table (Gate D round-2 correction, C5).

    ``docs/connector-security.md``'s pre-activation checklist makes human
    review of the coverage report, its ``SecurityFlag``s, and the audit
    events MANDATORY before any real adapter's output reaches the bridge --
    but the only way to read a ``SourceCoverageReport`` was
    ``model_dump_json()`` in a REPL. This is a pure formatting function:
    no clock read, no file/network I/O, no randomness -- the same report
    always renders to the exact same string, which the C5 tests pin.

    Every column comes from :class:`SourceCoverage` fields that are already
    bounded, sanitized, or plain counts (``sanitized_reason`` is produced by
    ``sanitize.sanitize_error``/``sanitize_text`` before it ever reaches this
    report) -- ``SourceCoverage`` has no title/summary/raw-content field at
    all, so there is structurally nothing here for raw retrieved content to
    leak through, in addition to this function never touching any such
    field.

    Rows are always rendered sorted by ``source_id`` (never the caller's
    original tuple order, in case a caller assembled ``sources`` by hand)
    -- rule F: deterministic ordering.
    """
    lines = [_COVERAGE_REPORT_TITLE, "=" * len(_COVERAGE_REPORT_TITLE)]

    header = " ".join(f"{name:<{width}}" for name, width in _COVERAGE_COLUMNS)
    header += " sanitized_reason"
    lines.append(header)
    lines.append("-" * len(header))

    if not report.sources:
        lines.append("(no sources attempted or skipped)")
        return "\n".join(lines)

    for row in sorted(report.sources, key=lambda r: r.source_id):
        failure_kind = row.failure_kind.value if row.failure_kind is not None else "-"
        values = (
            row.source_id,
            str(row.attempted),
            str(row.discovered),
            str(row.dropped_count),
            str(row.truncated),
            failure_kind,
            str(row.skipped_not_approved),
            str(row.review_overdue),
        )
        line = " ".join(
            f"{value:<{width}}"
            for value, (_name, width) in zip(values, _COVERAGE_COLUMNS, strict=True)
        )
        line += f" {row.sanitized_reason}"
        lines.append(line)

    return "\n".join(lines)
