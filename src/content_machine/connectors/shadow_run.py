"""The shadow-run harness (Fable RC-6: operational conditions C2, C3, C4).

**Orchestrator's design decision, implemented here.** Fable classified C2,
C3, and C4 as "harness-side, not code changes to the adapter diff" -- meaning
:mod:`content_machine.connectors.arxiv_adapter` is NOT touched by this
module, at all, in any way. The orchestrator's ruling this module carries
out is to **enforce these three conditions in code, not in a runbook**: a
kill criterion that depends on a human remembering to check something is not
a kill criterion. Every check below is mechanical, runs unconditionally
after every single invocation, and never depends on an operator's memory or
discipline.

**What this module does NOT do (ticket scope).** It performs no real network
call in its own tests (every test injects a fetcher stub), adds
``arxiv.org`` to no allowlist, wires no scheduler, and changes nothing in
:mod:`~content_machine.connectors.runner`,
:mod:`~content_machine.connectors.arxiv_adapter`, or
:mod:`~content_machine.connectors.network`. It does not extend the
:class:`~content_machine.connectors.runner.ConnectorAdapter` protocol
(Fable's C5, a separate architecture ticket) -- the extra attribute this
harness needs (``audit_events``) is expressed here as a small, additional,
structurally-typed :class:`_AuditingAdapter` Protocol, exactly the same
technique :mod:`arxiv_adapter` itself uses for its own ``_Fetcher`` Protocol,
not a change to ``runner.ConnectorAdapter`` itself.

**C2 -- evidence survives the run.** ``ArxivRssAdapter.audit_events`` is
OVERWRITTEN on every ``discover()`` call (Fable verified this by execution:
two calls leave one row) -- it is a plain adapter attribute, not an
append-only log (see that class's own docstring for why: the
``ConnectorAdapter`` protocol has no audit-trail return channel in this
gate). :func:`run_shadow_discovery` reads ``adapter.audit_events``
IMMEDIATELY after :func:`~content_machine.connectors.runner.run_discovery`
returns -- before anything else in this process could invoke ``discover()``
again -- and persists it, together with the full
:class:`~content_machine.connectors.runner.BatchDiscoveryResult`, via
:func:`_atomic_append_jsonl_line` before doing anything else with the
result. Persistence is APPEND-ONLY (one JSON line per invocation, to
``output_dir/shadow-run-history.jsonl``): running twice leaves two distinct,
independently-readable records, never one record clobbering the other the
way ``ArxivRssAdapter.audit_events`` itself would.

**Atomic-write discipline, reimplemented, not imported.**
``docs/architecture.md``'s dependency rules let ``connectors`` import only
``config`` and, narrowly, ``intelligence.models``/``intelligence.normalize``
-- nothing else in ``intelligence``. That means
``intelligence.weekly._atomic_write_all`` (which already implements exactly
the stage-in-same-directory / fsync / backup-then-replace / roll-back-on-any-
failure discipline this ticket asks the harness to follow) cannot be
imported from here without violating that boundary. :func:`_atomic_append_jsonl_line`
below reproduces the IDENTICAL discipline -- never a second, different
pattern -- applied to one file (a shadow-run history has exactly one output
path, unlike ``weekly.py``'s eight-file batch, so there is nothing to
coordinate across multiple destinations here).

**C3 -- status codes are read, not awaited.**
:mod:`~content_machine.connectors.network` has no 4xx/5xx branch (see
``network.py``'s ``_connect_and_fetch_one_hop``: only ``_REDIRECT_STATUSES``
is inspected) -- a live 403, 429, or 451 carrying an allowed XML content type
parses as an ordinary successful fetch, with ``FetchResult.ok=True``. A
future ``reason_code`` for this case does not exist and this harness does
not wait for one: :func:`_evaluate_run` independently parses
``status_code=NNN`` out of the adapter's own audit-event ``detail`` string
(the only place a fetch's HTTP status reaches a persisted record --
:class:`~content_machine.connectors.models.ConnectorAuditEvent` has no
dedicated ``status_code`` field) on EVERY run, and treats 403/429/451 as a
kill trigger regardless of what ``reason_code`` the fetch produced.

**C4 -- the byte cap tightens from real data only.** The observed
``byte_count`` is recorded on every run (parsed from the same audit-event
detail string), but a cap RECOMMENDATION is computed only when the run was a
genuine, un-halted success (see :func:`_measure_bytes`): a rejected fetch
always audits ``byte_count=0`` (``FetchResult.body`` is populated only on
the success path -- see ``arxiv_adapter``'s own "known limitation" note),
and feeding that into a cap calculation would produce a nonsense cap.
:class:`ByteCapMeasurement.measured` is ``False`` and
:class:`ByteCapMeasurement.recommended_cap_bytes` is ``None`` for any run
that did not succeed cleanly -- the harness reports "no measurement
available" as a literal, non-numeric fact, never a fabricated number.
**Fable's ruling caps a cap recommendation at no more than 5x the observed
byte count; this module's own :data:`_CAP_SAFETY_MARGIN` is 2x -- an
implementer choice made WITHIN that ceiling, more conservative than the
ceiling requires, not the ceiling itself.** A future reader must not read
``2x`` as Fable's number.

**Kill criteria, evaluated mechanically after the run (see
:class:`KillReason` and :func:`_evaluate_run`).** Every criterion below is
independent and all are checked on every run; more than one may fire at
once. None of them retries and none of them loops -- see "Invocation
discipline" below.

- a :class:`~content_machine.connectors.network.FetchReasonCode` naming a
  fetch-boundary security violation (``address_blocked``,
  ``ip_literal_host``, ``host_not_allowed``, ``scheme_not_https``,
  ``credential_in_url``, ``port_not_allowed``, ``too_many_redirects``,
  ``response_too_large``, ``mime_not_allowed``);
- ``status_code`` in ``{403, 429, 451}`` (C3, above);
- more than one outbound request recorded for this run (see
  :class:`RequestCountingFetcher` -- this harness counts requests itself
  rather than trusting ``ArxivRssAdapter``'s own
  ``MAX_REQUESTS_PER_DISCOVER`` constant to have been honored, the same
  "never trust the adapter's self-report" posture
  ``runner.run_discovery``'s own B1 correction already applies elsewhere in
  this package);
- any RETAINED :class:`~content_machine.connectors.models.DiscoveryResult`
  (i.e. one that survived permission-field enforcement and made it into
  ``BatchDiscoveryResult.results``) carrying a flag in
  :data:`~content_machine.connectors.bridge.BLOCKING_SECURITY_FLAGS` --
  Fable widened this from ``instruction_shaped_text``-only. This one is
  reported distinctly (``founder_review_required``) rather than a flat
  halt: it PAUSES for Founder review rather than terminating the shadow
  window outright;
- permission denied or expired -- the automatic kill switch, since
  ``expires_at`` is set to the end of the 7-day shadow window (see
  ``arxiv_adapter``'s own module docstring). Detected two ways, together
  covering both the coverage-report layer (``run_discovery``'s own
  ``authorize()`` call, before ``discover()`` is even invoked) and the
  retrieval-time layer (``NetworkFetcher.fetch()``'s live
  ``authorize_retrieval()`` call, which is where ``expires_at`` itself is
  actually enforced): ``SourceCoverage.skipped_not_approved``
  (:mod:`~content_machine.connectors.runner`) for the former, and the
  adapter's own audit-event ``reason_code == "permission_denied"`` for the
  latter;
- :data:`KillReason.request_counter_not_wired` -- see "Wiring-integrity
  check" below. A DIFFERENT invariant from "more than one outbound request":
  that one detects an adapter that made too many requests through the
  counter it was actually given; this one detects that the counter this
  function was HANDED is not the one the adapter actually used at all, which
  would otherwise make the "more than one outbound request" check trivially,
  silently pass on ``request_count == 0`` regardless of what really
  happened.

**Wiring-integrity check (mismatched/unused request counter must be
detected, not assumed correct).** ``fetcher.call_count`` is read exactly
once, after the run, and nothing about that read alone proves ``fetcher``
is the SAME :class:`RequestCountingFetcher` instance the adapter's own
internal fetcher calls actually went through -- a caller who constructs a
second, fresh wrapper and passes THAT one here by mistake would silently
get ``call_count == 0`` forever, and "more than one outbound request" can
never fire no matter how many requests the adapter really made. Detecting
this without touching ``arxiv_adapter.py`` (out of scope) requires a signal
that a fetch attempt definitely happened, sourced from evidence THIS module
already has: ``batch_result.coverage``'s ``SourceCoverage.attempted`` field
is set to ``True`` by ``run_discovery`` itself (:mod:`~content_machine.connectors.runner`)
if and only if ``adapter.discover()`` was actually called (both the success
and the per-source-isolation exception branches set it, immediately after
that call -- see ``run_discovery``'s own source), independent of anything
the adapter itself claims. Separately, ``adapter_audit_events`` being
non-empty is corroborating evidence of the same fact for
``ArxivRssAdapter`` specifically: its ``discover()`` calls
``self._fetcher.fetch(...)`` as the unconditional FIRST action on every
code path, before any audit event can be recorded (see that class's own
"Audit" docstring section: "Every discover() call appends exactly one
ConnectorAuditEvent"). :func:`_evaluate_run` therefore treats "the run
reached the fetch stage" as ``coverage_row.attempted or
bool(adapter_audit_events)`` -- if that is true but ``request_count == 0``,
the passed counter cannot be the one the adapter used, and
:data:`KillReason.request_counter_not_wired` fires. This makes ``halted``
true (so no clean run is reported) and, via the SAME gating
:func:`_measure_bytes` already applies to every other kill reason, also
guarantees no byte-cap measurement is produced for that run. **Stated
honestly, not overclaimed:** this soundness argument leans on
``ArxivRssAdapter``'s own documented "always audits, even on the failure
path, fetch-call-first" contract for the corroborating (non-coverage)
half -- a hypothetical FUTURE adapter satisfying the structural
:class:`_AuditingAdapter` Protocol but NOT upholding that contract could, in
principle, defeat the ``adapter_audit_events`` half of this check; the
``coverage_row.attempted`` half does not depend on any adapter's internal
behavior at all (it is ``run_discovery``'s OWN bookkeeping) and holds for
any :class:`~content_machine.connectors.runner.ConnectorAdapter`
whatsoever. No reflection into adapter internals (e.g. reading a private
``adapter._fetcher`` attribute) was used: that would coincidentally work
for ``ArxivRssAdapter`` today but silently break, or worse silently pass,
the instant that attribute's name changed or a different adapter shape was
used -- exactly the kind of brittle, adapter-shape-coupled check this
module otherwise avoids everywhere else.

**Override frequency is a monitored quantity.** Fable: reviewer overrides of
a blocking flag are the alarm-fatigue leading indicator. Every
:func:`run_shadow_discovery` call recomputes ``override_count_in_window``
(:func:`_count_reviewer_overrides`) from the FULL persisted history in
``output_dir`` (this run's record plus every prior one) -- the report always
surfaces a COUNT, never merely whether an override happened at least once.

**Invocation discipline.** :func:`run_shadow_discovery` performs exactly one
``run_discovery`` call over exactly one adapter -- there is no loop, no
retry, and nothing in this module reads a scheduler, a cron table, or waits
on a clock.

**CLI command vs. plain callable -- judgment call, stated explicitly.** This
ticket offered either. This module exposes a **plain callable**
(:func:`run_shadow_discovery`), deliberately NOT a new ``content-machine``
CLI command, for three reasons: (1) a Typer subcommand is, by construction,
listed in ``--help``, shell completion, and this repo's own CLI-contract
docs -- exactly the kind of ambient discoverability the ticket's "must not
be reachable by accident" instruction is warning against for an operation
this security-sensitive (it can perform a REAL network call against a
Founder-curated private endpoint once wired to a real ``PrivateSourceConfig``
and ``PermissionRegistry``); (2) real invocation of this harness requires
several already-curated, real (non-committable) objects --
a real ``PrivateSourceConfig``, a real ``PermissionRegistry`` entry with a
7-day ``expires_at``, a real ``SourceRegistryEntry`` -- that only the
Founder's private workspace holds, so a generic CLI flag surface would
either have to accept raw paths to those (widening the private-data attack
surface of the CLI itself) or would not actually be simpler than a short,
explicit Python invocation the Founder writes once; (3) nothing about "one
bounded run, mechanically checked" benefits from shell-level ergonomics
(argument parsing, ``--help`` text) the way the existing ``intelligence
weekly-run``/``source inspect`` commands do -- those are meant to be run
routinely, this is meant to be run deliberately, rarely, and reviewed by a
human every time. A future gate that wires a real scheduler or a routine
Founder workflow around this harness (out of scope here, and explicitly
listed as a non-goal) is the natural place to reconsider a CLI wrapper.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from content_machine.config.settings import Settings
from content_machine.connectors.arxiv_adapter import (
    ALLOWED_MIME_TYPES,
    ALLOWED_PORTS,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    RATE_LIMIT_MAX_CALLS,
    RATE_LIMIT_WINDOW_SECONDS,
    ArxivRssAdapter,
    UnknownPrivateSourceEndpointError,
)
from content_machine.connectors.bridge import BLOCKING_SECURITY_FLAGS
from content_machine.connectors.models import ConnectorAuditEvent, DiscoveryRequest
from content_machine.connectors.network import FetchReasonCode, FetchResult, NetworkFetcher
from content_machine.connectors.permissions import PermissionRegistry, SourceMode
from content_machine.connectors.private_config import (
    PrivateSourceConfig,
    source_allowed_hosts_from_config,
)
from content_machine.connectors.registry import SourceRegistry
from content_machine.connectors.runner import (
    AdapterDiscoveryOutcome,
    BatchDiscoveryRequest,
    BatchDiscoveryResult,
    BatchStatus,
    run_discovery,
)

__all__ = [
    "SHADOW_RUN_HISTORY_FILENAME",
    "ByteCapMeasurement",
    "KillReason",
    "MissingShadowRunOutputDirError",
    "RequestCountingFetcher",
    "ShadowRunEvaluation",
    "ShadowRunRecord",
    "ShadowRunReport",
    "build_shadow_run_arxiv_adapter",
    "read_shadow_run_history",
    "render_shadow_run_report",
    "resolve_shadow_run_output_dir",
    "run_shadow_discovery",
]

# --- persisted output ---------------------------------------------------

#: One file, append-only (JSON Lines): one record per invocation. Never
#: overwritten wholesale -- see the module docstring's C2 section.
SHADOW_RUN_HISTORY_FILENAME = "shadow-run-history.jsonl"

#: The one reason_code the bridge (not this harness) stamps on an override
#: audit event -- see content_machine.connectors.bridge.to_source_item_with_audit.
#: Duplicated here as a plain string constant (not imported) because it is a
#: string literal on bridge.py's own ConnectorAuditEvent construction, not an
#: exported symbol.
_OVERRIDE_REASON_CODE = "human_reviewed_security_flags_override"


class MissingShadowRunOutputDirError(RuntimeError):
    """Raised by :func:`resolve_shadow_run_output_dir` when no shadow-run
    output directory is configured. Mirrors
    :class:`~content_machine.connectors.private_config.MissingPrivateConfigError`'s
    fail-closed posture: this harness never guesses a path, and never falls
    back to anywhere inside ``src/``."""

    def __init__(self) -> None:
        super().__init__(
            "no shadow-run output directory is configured; set "
            "CONTENT_MACHINE_SHADOW_RUN_OUTPUT_DIR (Settings.shadow_run_output_dir) "
            "or pass an explicit path -- this harness never persists to a path "
            "hardcoded in src/"
        )


def resolve_shadow_run_output_dir(
    settings: Settings | None = None, *, path: Path | None = None
) -> Path:
    """Resolve the private directory :func:`run_shadow_discovery` persists
    to. Exactly one of ``settings``/``path`` should be meaningful -- mirrors
    :func:`~content_machine.connectors.private_config.load_private_source_config`'s
    settings-or-path duality. Never returns a default embedded in this
    module: raises :class:`MissingShadowRunOutputDirError` if neither
    resolves to a configured path.
    """
    if path is None:
        if settings is None:
            raise ValueError(
                "resolve_shadow_run_output_dir requires either `settings` or an "
                "explicit `path` -- it never reads the environment itself"
            )
        path = settings.shadow_run_output_dir
    if path is None:
        raise MissingShadowRunOutputDirError()
    return path


def _atomic_append_jsonl_line(path: Path, line: str) -> None:
    """Append ``line`` (one already-serialized JSON object, no trailing
    newline) to ``path`` atomically -- see the module docstring's
    "Atomic-write discipline" section for why this reimplements, rather than
    imports, ``intelligence.weekly._atomic_write_all``'s pattern.

    Steps, exactly mirroring that function's per-file algorithm: stage the
    FULL new file content (existing content + the new line) into a temp
    file in the SAME directory (so the final rename is a same-filesystem,
    atomic ``os.replace``) and ``fsync`` it; if a file already exists at
    ``path``, move it aside into a ``.bak.tmp`` temp file (also an atomic
    ``os.replace`` -- this is what makes rollback possible); then
    ``os.replace`` the new temp file into place. If ANY step raises, any
    backup already taken is restored and every temp/backup file is cleaned
    up before the exception propagates -- ``path`` is left byte-for-byte
    exactly as it was before this call, never partially written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = existing + line + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    backup_path: Path | None = None
    if path.exists():
        backup_fd, backup_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".bak.tmp"
        )
        os.close(backup_fd)
        backup_path = Path(backup_name)
        try:
            os.replace(path, backup_path)
        except BaseException:
            backup_path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            raise

    try:
        os.replace(tmp_path, path)
    except BaseException:
        if backup_path is not None:
            os.replace(backup_path, path)
        tmp_path.unlink(missing_ok=True)
        raise

    if backup_path is not None:
        backup_path.unlink(missing_ok=True)


# --- request counting (defense in depth for the multi-request kill check) --


class _FetchLike(Protocol):
    """Structurally identical to ``arxiv_adapter._Fetcher`` -- the minimal
    shape a fetcher needs for :class:`RequestCountingFetcher` to wrap it.
    A separate Protocol (not imported from ``arxiv_adapter``, which does not
    export its private ``_Fetcher``) so this module never depends on that
    module's private surface."""

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult: ...


class RequestCountingFetcher:
    """Wraps any fetcher satisfying :class:`_FetchLike` and counts every
    call to :meth:`fetch` -- the harness's OWN, independently-observed count
    of outbound requests made during one run. Never trusts
    ``ArxivRssAdapter.MAX_REQUESTS_PER_DISCOVER`` to have been honored (or
    any future adapter's analogous constant): this is defense in depth, the
    same "never trust the callee's self-report" posture
    ``runner.run_discovery``'s own B1 correction already applies elsewhere
    in this package. Performs no network I/O of its own -- it only delegates
    to the wrapped fetcher and increments a counter; nothing here blocks a
    second call (the "more than one outbound request" kill criterion is
    evaluated mechanically AFTER the run, per the module docstring, never
    enforced preemptively mid-run).
    """

    def __init__(self, wrapped: _FetchLike) -> None:
        self._wrapped = wrapped
        self.call_count = 0

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult:
        self.call_count += 1
        return self._wrapped.fetch(source_id=source_id, mode=mode, url=url)


# --- the adapter shape this harness needs (audit_events, beyond the base
# ConnectorAdapter protocol -- see the module docstring) --------------------


class _AuditingAdapter(Protocol):
    """``runner.ConnectorAdapter`` (``source_id`` + ``discover()``) has no
    audit-trail return channel in this gate (see ``arxiv_adapter``'s own
    module docstring's "Implementer assumption" section) -- this harness
    needs one more thing, ``audit_events``, so it is expressed as an
    additional, purely structural Protocol here rather than a change to
    ``runner.ConnectorAdapter`` itself (extending that protocol is Fable's
    C5, a separate architecture ticket). ``ArxivRssAdapter`` is the only
    implementer today."""

    source_id: str

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome: ...

    @property
    def audit_events(self) -> tuple[ConnectorAuditEvent, ...]: ...


# --- kill criteria (C3's status-code read; the reason-code/request-count/
# security-flag/permission checks) ------------------------------------------


class KillReason(StrEnum):
    """One distinct code per RC-6 kill/pause invariant -- never collapsed,
    mirroring ``network.FetchReasonCode``/``permissions.AuthorizationReasonCode``'s
    own "one distinct code per invariant" philosophy. See the module
    docstring's "Kill criteria" section for what each one means."""

    security_reason_code = "security_reason_code"
    blocked_status_code = "blocked_status_code"
    multiple_outbound_requests = "multiple_outbound_requests"
    blocking_security_flag = "blocking_security_flag"
    permission_denied_or_expired = "permission_denied_or_expired"
    #: The passed `RequestCountingFetcher` recorded zero calls despite the
    #: run demonstrably reaching the fetch stage -- it is not the instance
    #: the adapter actually used. See the module docstring's
    #: "Wiring-integrity check" section. Distinct from
    #: `multiple_outbound_requests`: that one detects too many requests
    #: through a counter that WAS wired correctly; this one detects that the
    #: counter was never wired to begin with.
    request_counter_not_wired = "request_counter_not_wired"


#: FetchReasonCode members that represent a fetch-boundary SECURITY
#: violation (module docstring's "Kill criteria" list). Deliberately
#: EXCLUDES `permission_denied` (its own separate criterion below -- the
#: automatic, expiry-driven kill switch) and every ordinary transient/infra
#: code (`rate_limited`, `connect_failed`, `connect_timeout`,
#: `read_timeout`, `dns_resolution_failed`, `invalid_response`,
#: `fetch_failed`, `invalid_url`), which are expected failure conditions,
#: not RC-6 kill triggers.
_SECURITY_REASON_CODES: frozenset[str] = frozenset(
    {
        FetchReasonCode.address_blocked.value,
        FetchReasonCode.ip_literal_host.value,
        FetchReasonCode.host_not_allowed.value,
        FetchReasonCode.scheme_not_https.value,
        FetchReasonCode.credential_in_url.value,
        FetchReasonCode.port_not_allowed.value,
        FetchReasonCode.too_many_redirects.value,
        FetchReasonCode.response_too_large.value,
        FetchReasonCode.mime_not_allowed.value,
    }
)

#: C3: network.py has no 4xx/5xx branch, so a live 403/429/451 carrying an
#: allowed content type parses as an ordinary successful fetch. The harness
#: reads status_code independently rather than waiting for a reason_code
#: that network.py will never produce for this case (see the module
#: docstring).
_BLOCKED_STATUS_CODES: frozenset[int] = frozenset({403, 429, 451})

#: ArxivRssAdapter._record_audit's own detail format is
#: "status_code=... byte_count=... items_parsed=... dropped_count=...",
#: optionally followed by extra_detail. These are the only place a fetch's
#: status_code/byte_count reach a persisted ConnectorAuditEvent (neither has
#: its own dedicated field on that model) -- see the module docstring's C3
#: section.
_STATUS_CODE_RE = re.compile(r"status_code=(\d+)")
_BYTE_COUNT_RE = re.compile(r"byte_count=(\d+)")


def _parse_int_field(detail: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(detail)
    if match is None:
        return None
    return int(match.group(1))


class ShadowRunEvaluation(BaseModel):
    """The mechanical, post-run verdict (see the module docstring's "Kill
    criteria" section). ``halted`` is true whenever ``kill_reasons`` is
    non-empty -- including :data:`KillReason.blocking_security_flag`, which
    ALSO sets ``founder_review_required`` to route that one case to a human
    review rather than a flat stop. Frozen: a point-in-time evaluation,
    never mutated after construction (matches every other decision record in
    ``connectors``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kill_reasons: tuple[KillReason, ...] = Field(default_factory=tuple)
    halted: bool
    founder_review_required: bool
    status_code_observed: int | None = None
    request_count: int = Field(ge=0)


def _evaluate_run(
    *,
    source_id: str,
    adapter_audit_events: tuple[ConnectorAuditEvent, ...],
    batch_result: BatchDiscoveryResult,
    request_count: int,
) -> ShadowRunEvaluation:
    reasons: set[KillReason] = set()
    status_code: int | None = None

    for event in adapter_audit_events:
        if event.reason_code in _SECURITY_REASON_CODES:
            reasons.add(KillReason.security_reason_code)
        if event.reason_code == FetchReasonCode.permission_denied.value:
            reasons.add(KillReason.permission_denied_or_expired)
        parsed_status = _parse_int_field(event.detail, _STATUS_CODE_RE)
        if parsed_status is not None:
            status_code = parsed_status
            if parsed_status in _BLOCKED_STATUS_CODES:
                reasons.add(KillReason.blocked_status_code)

    coverage_row = next(
        (row for row in batch_result.coverage.sources if row.source_id == source_id), None
    )
    if coverage_row is not None and coverage_row.skipped_not_approved:
        reasons.add(KillReason.permission_denied_or_expired)

    # Wiring-integrity check (see the module docstring's dedicated section):
    # a fetch attempt demonstrably happened -- either run_discovery's OWN
    # bookkeeping says this source was attempted, or the adapter's own
    # audit trail is non-empty (which, for ArxivRssAdapter specifically,
    # only happens after its unconditional, first-action fetch() call) --
    # yet the counter we were handed saw zero calls. That contradiction
    # means `fetcher` is not the instance the adapter actually used, never
    # merely "the adapter made zero requests" (a source that made zero
    # requests could never have been attempted or produced an audit event
    # in the first place).
    reached_fetch_stage = bool(adapter_audit_events) or (
        coverage_row is not None and coverage_row.attempted
    )
    if reached_fetch_stage and request_count == 0:
        reasons.add(KillReason.request_counter_not_wired)
    elif request_count > 1:
        reasons.add(KillReason.multiple_outbound_requests)

    if any(set(result.security_flags) & BLOCKING_SECURITY_FLAGS for result in batch_result.results):
        reasons.add(KillReason.blocking_security_flag)

    ordered_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
    return ShadowRunEvaluation(
        kill_reasons=ordered_reasons,
        halted=bool(ordered_reasons),
        founder_review_required=KillReason.blocking_security_flag in ordered_reasons,
        status_code_observed=status_code,
        request_count=request_count,
    )


# --- C4: byte-cap measurement, successful runs only -------------------------


class ByteCapMeasurement(BaseModel):
    """C4: ``observed_byte_count``/``recommended_cap_bytes`` are populated
    ONLY when the run was a genuine, un-halted success (see
    :func:`_measure_bytes`) -- otherwise both are ``None`` and ``note``
    reads literally "no measurement available", never a number. A rejected
    fetch always audits ``byte_count=0``; feeding that into a cap
    calculation would produce a nonsense cap (see the module docstring).
    Frozen: a point-in-time measurement, never mutated after construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    measured: bool
    observed_byte_count: int | None = None
    recommended_cap_bytes: int | None = None
    note: str = Field(max_length=200)


#: C4 judgment call, stated explicitly (this codebase's own style for an
#: implementer decision the spec leaves open -- see e.g. arxiv_adapter.py's
#: `_FETCH_REASON_TO_FAILURE_KIND` docstring for the same kind of callout):
#: a recommended cap is the observed byte count with a fixed safety margin,
#: so a legitimate feed that grows moderately between shadow-run
#: observations does not immediately start tripping `response_too_large`.
#: This is ADVISORY data for a human to review -- never applied
#: automatically to `ArxivRssAdapter.MAX_RESPONSE_BYTES` by this module.
_CAP_SAFETY_MARGIN = 2

_NO_MEASUREMENT_NOTE = "no measurement available"


def _measure_bytes(
    *,
    adapter_audit_events: tuple[ConnectorAuditEvent, ...],
    batch_result: BatchDiscoveryResult,
    evaluation: ShadowRunEvaluation,
) -> ByteCapMeasurement:
    succeeded = not evaluation.halted and batch_result.status == BatchStatus.all_succeeded

    byte_count: int | None = None
    for event in adapter_audit_events:
        parsed = _parse_int_field(event.detail, _BYTE_COUNT_RE)
        if parsed is not None:
            byte_count = parsed

    if not succeeded or byte_count is None:
        return ByteCapMeasurement(measured=False, note=_NO_MEASUREMENT_NOTE)

    return ByteCapMeasurement(
        measured=True,
        observed_byte_count=byte_count,
        recommended_cap_bytes=byte_count * _CAP_SAFETY_MARGIN,
        note=f"recommended cap = {_CAP_SAFETY_MARGIN}x observed byte_count from a successful run",
    )


# --- persisted record + report ----------------------------------------------


class ShadowRunRecord(BaseModel):
    """One shadow-run invocation's full, persisted evidence (C2): the
    adapter's own audit trail -- otherwise overwritten on the adapter's NEXT
    ``discover()`` call, so this is the only durable copy -- the full
    :class:`~content_machine.connectors.runner.BatchDiscoveryResult`, and
    this run's kill-criteria evaluation and byte measurement. One record is
    appended per invocation; see :data:`SHADOW_RUN_HISTORY_FILENAME`.
    Frozen: a persisted record of a past run, never mutated after
    construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    occurred_at: datetime
    adapter_audit_events: tuple[ConnectorAuditEvent, ...]
    batch_result: BatchDiscoveryResult
    evaluation: ShadowRunEvaluation
    byte_measurement: ByteCapMeasurement


class ShadowRunReport(BaseModel):
    """The full, returned outcome of one :func:`run_shadow_discovery`
    invocation: this run's own record, plus the reviewer-override count
    computed over the FULL persisted history (see the module docstring's
    "Override frequency is a monitored quantity" section)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: ShadowRunRecord
    override_count_in_window: int = Field(ge=0)
    total_runs_persisted: int = Field(ge=0)


def read_shadow_run_history(path: Path) -> tuple[ShadowRunRecord, ...]:
    """Every :class:`ShadowRunRecord` persisted so far at ``path`` (``()``
    if the file does not exist yet), in the order they were appended."""
    if not path.exists():
        return ()
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(ShadowRunRecord.model_validate_json(line))
    return tuple(records)


def _count_reviewer_overrides(records: Iterable[ShadowRunRecord]) -> int:
    """Count of ``human_reviewed_security_flags_override`` audit events
    across every persisted record's ``adapter_audit_events`` +
    ``batch_result.audit_events`` -- Fable: reviewer overrides of a blocking
    flag are the alarm-fatigue leading indicator, and the report must
    surface the COUNT, not just whether one ever happened."""
    count = 0
    for record in records:
        all_events = (*record.adapter_audit_events, *record.batch_result.audit_events)
        count += sum(1 for event in all_events if event.reason_code == _OVERRIDE_REASON_CODE)
    return count


# --- the entry point ---------------------------------------------------


def run_shadow_discovery(
    adapter: _AuditingAdapter,
    fetcher: RequestCountingFetcher,
    *,
    permission_registry: PermissionRegistry,
    source_registry: SourceRegistry,
    request: BatchDiscoveryRequest,
    occurred_at: datetime,
    output_dir: Path,
    review_as_of: date | None = None,
) -> ShadowRunReport:
    """Perform ONE bounded shadow-discovery run and mechanically enforce
    Fable's RC-6 operational conditions C2, C3, and C4, plus the kill
    criteria -- see the module docstring for the full rationale.

    ``fetcher`` MUST be the same :class:`RequestCountingFetcher` instance
    that was wired into ``adapter``'s own constructor (see
    :func:`build_shadow_run_arxiv_adapter` for the recommended wiring) --
    this function reads ``fetcher.call_count`` to evaluate the
    "more than one outbound request" kill criterion. Passing a fresh, unused
    wrapper here is NOT a silent failure mode: :func:`_evaluate_run`'s
    wiring-integrity check (see the module docstring) detects the
    contradiction -- a run that demonstrably reached the fetch stage
    (``coverage_row.attempted`` or a non-empty ``adapter_audit_events``) yet
    reports ``request_count == 0`` -- and fails closed with
    :data:`KillReason.request_counter_not_wired`, never a clean report.

    ``output_dir`` is caller-resolved (typically via
    :func:`resolve_shadow_run_output_dir`) -- this function never guesses a
    path or falls back to anywhere inside ``src/``.

    Performs exactly one ``run_discovery`` call over exactly one adapter:
    no loop, no retry, no scheduler. See the module docstring's "Invocation
    discipline" section.
    """
    batch_result = run_discovery(
        [adapter],
        permission_registry,
        source_registry,
        request,
        occurred_at=occurred_at,
        review_as_of=review_as_of,
    )
    # C2: capture the adapter's own audit trail IMMEDIATELY -- it is
    # overwritten on the adapter's NEXT discover() call, so nothing may run
    # between this line and persistence below.
    adapter_audit_events = adapter.audit_events
    request_count = fetcher.call_count

    evaluation = _evaluate_run(
        source_id=adapter.source_id,
        adapter_audit_events=adapter_audit_events,
        batch_result=batch_result,
        request_count=request_count,
    )
    byte_measurement = _measure_bytes(
        adapter_audit_events=adapter_audit_events,
        batch_result=batch_result,
        evaluation=evaluation,
    )

    record = ShadowRunRecord(
        source_id=adapter.source_id,
        occurred_at=occurred_at,
        adapter_audit_events=adapter_audit_events,
        batch_result=batch_result,
        evaluation=evaluation,
        byte_measurement=byte_measurement,
    )

    history_path = output_dir / SHADOW_RUN_HISTORY_FILENAME
    _atomic_append_jsonl_line(history_path, record.model_dump_json())

    persisted = read_shadow_run_history(history_path)
    override_count = _count_reviewer_overrides(persisted)

    return ShadowRunReport(
        record=record,
        override_count_in_window=override_count,
        total_runs_persisted=len(persisted),
    )


def render_shadow_run_report(report: ShadowRunReport) -> str:
    """Human-readable rendering of one :class:`ShadowRunReport`, in the same
    spirit as ``runner.format_coverage_report`` (a pure formatting function:
    no clock read, no file/network I/O, no randomness). Every field
    rendered here is already bounded/sanitized upstream -- there is nothing
    here for raw retrieved content to leak through.
    """
    evaluation = report.record.evaluation
    byte_measurement = report.record.byte_measurement
    lines = [
        "Shadow-Run Report",
        "=================",
        f"source_id: {report.record.source_id}",
        f"occurred_at: {report.record.occurred_at.isoformat()}",
        f"batch_status: {report.record.batch_result.status.value}",
        f"request_count: {evaluation.request_count}",
        f"status_code_observed: {evaluation.status_code_observed}",
        f"halted: {evaluation.halted}",
        f"founder_review_required: {evaluation.founder_review_required}",
        "kill_reasons: "
        + (", ".join(reason.value for reason in evaluation.kill_reasons) or "(none)"),
        f"byte_measurement.measured: {byte_measurement.measured}",
        f"byte_measurement.observed_byte_count: {byte_measurement.observed_byte_count}",
        f"byte_measurement.recommended_cap_bytes: {byte_measurement.recommended_cap_bytes}",
        f"byte_measurement.note: {byte_measurement.note}",
        f"override_count_in_window: {report.override_count_in_window}",
        f"total_runs_persisted: {report.total_runs_persisted}",
    ]
    return "\n".join(lines)


# --- convenience wiring for the one adapter this ticket targets ------------


def build_shadow_run_arxiv_adapter(
    source_id: str,
    private_config: PrivateSourceConfig,
    permission_registry: PermissionRegistry,
    *,
    source_group: str,
    discovery_run_id: str,
    retrieved_at: datetime,
) -> tuple[ArxivRssAdapter, RequestCountingFetcher]:
    """Wire an :class:`~content_machine.connectors.arxiv_adapter.ArxivRssAdapter`
    for use with :func:`run_shadow_discovery`.

    Identical wiring to
    :func:`~content_machine.connectors.arxiv_adapter.build_arxiv_rss_adapter`
    -- same endpoint lookup, same ``NetworkFetcher`` construction, using
    ``arxiv_adapter``'s own EXPORTED pinned constants (``ALLOWED_PORTS``,
    ``ALLOWED_MIME_TYPES``, ``MAX_RESPONSE_BYTES``, ``MAX_REDIRECTS``,
    ``RATE_LIMIT_MAX_CALLS``, ``RATE_LIMIT_WINDOW_SECONDS``), so nothing
    here re-decides any part of RC-2/RC-4/RC-5's approved configuration --
    except the constructed ``NetworkFetcher`` is wrapped in a
    :class:`RequestCountingFetcher` before being handed to
    ``ArxivRssAdapter``. That wrapping is necessary because
    ``ArxivRssAdapter`` itself is explicitly OUT OF SCOPE for this ticket's
    changes (Fable classified C2/C3/C4 as harness-side, not adapter-diff
    changes) and so exposes no request-count attribute of its own; this
    function cannot reuse ``build_arxiv_rss_adapter`` directly because that
    function does not accept a caller-supplied fetcher.

    Raises :class:`~content_machine.connectors.arxiv_adapter.UnknownPrivateSourceEndpointError`
    -- exactly like ``build_arxiv_rss_adapter``, from the identical lookup
    -- if ``source_id`` has no matching entry in ``private_config.endpoints``.
    """
    endpoint = next(
        (candidate for candidate in private_config.endpoints if candidate.source_id == source_id),
        None,
    )
    if endpoint is None:
        raise UnknownPrivateSourceEndpointError(
            "source_id has no matching PrivateSourceEndpoint in the supplied "
            "PrivateSourceConfig"
        )

    raw_fetcher = NetworkFetcher(
        permission_registry=permission_registry,
        source_allowed_hosts=source_allowed_hosts_from_config(private_config),
        allowed_ports=ALLOWED_PORTS,
        allowed_mime_types=ALLOWED_MIME_TYPES,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_redirects=MAX_REDIRECTS,
        rate_limit_max_calls=RATE_LIMIT_MAX_CALLS,
        rate_limit_window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    )
    counting_fetcher = RequestCountingFetcher(raw_fetcher)
    adapter = ArxivRssAdapter(
        source_id,
        feed_url=endpoint.endpoint.get_secret_value(),
        fetcher=counting_fetcher,
        source_group=source_group,
        discovery_run_id=discovery_run_id,
        retrieved_at=retrieved_at,
    )
    return adapter, counting_fetcher
