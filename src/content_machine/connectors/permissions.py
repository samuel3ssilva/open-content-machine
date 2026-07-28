"""Permission model: who may discover/verify what, and enforcement (Gate D §4).

:class:`PermissionRegistry.authorize` is the single fail-closed gate every
source must pass before discovery or verification may proceed: unknown,
proposed, suspended, revoked, and mode-mismatched sources are each rejected
with a DISTINCT reason code (never collapsed into one generic "denied"),
because a human auditing a denial needs to know WHICH invariant fired.

**Implementer decision on ``permitted_fields`` (spec left this "decide +
test").** ``permitted_fields`` names the subset of
:class:`~content_machine.connectors.models.DiscoveryResult`'s CONTENT-BEARING
field names -- ``title``, ``publication_date``, ``canonical_reference``,
``summary_normalized``, ``content_type`` -- that this source's permission
authorizes to carry actual content. Structural/provenance fields
(``source_id``, ``source_group``, ``retrieved_at``, ``provenance``,
``permission_ref``, ``security_flags``, ``summary_provenance``) are exempt:
they carry no source-supplied content, only the contract's own bookkeeping,
so no permission is needed to populate them. A content-bearing field is
"outside ``permitted_fields``" when it is both (a) not named in
``permitted_fields`` and (b) not at its empty default (``None`` for
``publication_date``, ``""`` for ``summary_normalized``; ``title``,
``canonical_reference``, and ``content_type`` are always non-empty by
``DiscoveryResult``'s own validation, so a permission that omits them from
``permitted_fields`` can never be satisfied -- which is intentional: a source
without permission to supply a title or a canonical reference cannot
meaningfully produce a ``DiscoveryResult`` at all). Enforcement is REJECT +
audit, never a silent strip -- a silently stripped ``publication_date`` would
mis-window an item with no visible symptom.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from content_machine.connectors.models import DiscoveryResult

#: Content-bearing DiscoveryResult field names that permitted_fields governs.
#: See the module docstring for the full rationale.
_CONTENT_BEARING_FIELDS: frozenset[str] = frozenset(
    {"title", "publication_date", "canonical_reference", "summary_normalized", "content_type"}
)


class SourceMode(StrEnum):
    """What a source is permitted to do."""

    discovery = "discovery"
    verification = "verification"
    both = "both"


class PermissionStatus(StrEnum):
    """A permission's lifecycle state. Only ``approved`` ever executes."""

    proposed = "proposed"
    approved = "approved"
    suspended = "suspended"
    revoked = "revoked"


class SourcePermission(BaseModel):
    """One source's standing permission: what mode, what fields, what
    retention policy, under whose authorization, and its current status.

    Frozen (Gate D round-1 correction, B3): a status/lifecycle change is
    always a NEW registry entry (the caller rebuilds the ``PermissionRegistry``
    with an updated ``SourcePermission``), never an in-place mutation of an
    existing one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    approved_mode: SourceMode
    permitted_fields: frozenset[str]
    retention_policy_id: str
    review_due: date | None = None
    #: NEW field added in Gate E0 (E0.2; ruling R6/F4) -- never a repurposing
    #: of ``review_due``. ``review_due``'s own semantics are completely
    #: unchanged by this addition: an overdue review still returns
    #: ``allowed=True`` with ``review_overdue=True`` ("never a silent
    #: expiry"), enforced only by the caller-clocked :meth:`authorize`.
    #: ``expires_at``, by contrast, is a hard cutoff enforced by the NEW,
    #: separately-clocked :meth:`PermissionRegistry.authorize_retrieval`
    #: (see that method's docstring for the fail-closed boundary rule and
    #: why it deliberately cannot accept a caller-supplied date). ``None``
    #: means no expiry constraint on this permission.
    expires_at: date | None = None
    authorization_owner: str
    status: PermissionStatus


class AuthorizationReasonCode(StrEnum):
    """Every possible outcome of :meth:`PermissionRegistry.authorize`, one
    distinct code per invariant -- never collapsed."""

    approved = "approved"
    not_registered = "not_registered"
    status_proposed = "status_proposed"
    status_suspended = "status_suspended"
    status_revoked = "status_revoked"
    mode_mismatch = "mode_mismatch"
    #: NEW in Gate E0 (E0.2). Its own distinct code, never collapsed into
    #: ``status_revoked`` or any other -- an expired permission is a
    #: different invariant (a clock comparison) from a lifecycle status
    #: change, and a human auditing a denial needs to know which one fired.
    status_expired = "status_expired"


class AuthorizationDecision(BaseModel):
    """The explicit, closed outcome of an authorization check.

    ``review_overdue`` is carried here (in addition to the core fields the
    spec names) so an overdue review is never silently dropped even though
    wiring it into a ``SourceCoverageReport`` warning happens in ``runner.py``
    (a later commit) -- see the ``review_due`` rule in the module docstring
    of :meth:`PermissionRegistry.authorize`. Frozen (Gate D round-1
    correction, B3): a decision is a point-in-time result value, never
    mutated after it is returned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: AuthorizationReasonCode
    source_id: str
    mode: SourceMode
    review_overdue: bool = False


class FieldEnforcementReasonCode(StrEnum):
    """Outcome codes for :meth:`PermissionRegistry.enforce_discovery_fields`."""

    ok = "ok"
    not_registered = "not_registered"
    #: Gate D round-1 correction (B1): added so enforce_discovery_fields
    #: independently re-checks status == approved rather than relying solely
    #: on the runner's own authorize() call having already gated it -- the
    #: two enforcement points must never be able to disagree.
    status_not_approved = "status_not_approved"
    field_not_permitted = "field_not_permitted"


class FieldEnforcementDecision(BaseModel):
    """Whether a :class:`~content_machine.connectors.models.DiscoveryResult`
    stayed within its source's ``permitted_fields``. Frozen (Gate D round-1
    correction, B3): a point-in-time result value, never mutated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    source_id: str
    reason_code: FieldEnforcementReasonCode
    violating_fields: tuple[str, ...] = Field(default_factory=tuple)


def _today_utc() -> date:
    """The ONE clock read behind :meth:`PermissionRegistry.authorize_retrieval`
    (Gate E0, E0.2; Fable ruling F5, "the clock is an identity too").

    Isolated in its own module-level function so the live gate's public
    signature never needs a date/clock parameter at all: a test that wants a
    fixed "today" monkeypatches THIS function (e.g.
    ``monkeypatch.setattr(permissions, "_today_utc", lambda: date(...))``)
    rather than passing an ``as_of``/``now`` argument into
    ``authorize_retrieval`` itself. See that method's docstring for why a
    caller-suppliable enforcement clock would let a stale date dodge
    ``expires_at`` entirely -- the time-based analogue of the B1
    identity-spoofing bug Gate D closed by having ``runner.py`` build
    ``PermissionRef`` from the registry's own record rather than trusting an
    adapter's self-report.
    """
    return datetime.now(UTC).date()


class PermissionRegistry:
    """In-memory registry of :class:`SourcePermission`, keyed by ``source_id``.

    Built from an explicit, caller-supplied list -- no filesystem or
    environment I/O in this gate.
    """

    def __init__(self, permissions: list[SourcePermission]) -> None:
        by_id: dict[str, SourcePermission] = {}
        for permission in permissions:
            if permission.source_id in by_id:
                raise ValueError(
                    f"duplicate source_id in permission registry construction: "
                    f"{permission.source_id!r}"
                )
            by_id[permission.source_id] = permission
        self._by_id = by_id

    def authorize(
        self, source_id: str, mode: SourceMode, *, as_of: date | None = None
    ) -> AuthorizationDecision:
        """Fail-closed authorization check.

        Only ``status == approved`` AND a matching mode ever return
        ``allowed=True``. ``proposed``/``suspended``/``revoked`` are each
        rejected with their own reason code; an unknown ``source_id`` is
        rejected as ``not_registered``; a mode mismatch (e.g. a
        ``discovery``-only permission asked to authorize ``verification``) is
        rejected as ``mode_mismatch``.

        If ``as_of`` is given and the permission's ``review_due`` date is in
        the past, the result is still allowed (never a silent expiry) but
        ``review_overdue=True`` is set so the caller can raise a warning --
        wiring that into a per-run coverage report happens in ``runner.py``
        (a later Gate D commit).
        """
        permission = self._by_id.get(source_id)
        if permission is None:
            return AuthorizationDecision(
                allowed=False,
                reason_code=AuthorizationReasonCode.not_registered,
                source_id=source_id,
                mode=mode,
            )

        if permission.status != PermissionStatus.approved:
            status_reason = {
                PermissionStatus.proposed: AuthorizationReasonCode.status_proposed,
                PermissionStatus.suspended: AuthorizationReasonCode.status_suspended,
                PermissionStatus.revoked: AuthorizationReasonCode.status_revoked,
            }[permission.status]
            return AuthorizationDecision(
                allowed=False, reason_code=status_reason, source_id=source_id, mode=mode
            )

        if permission.approved_mode != SourceMode.both and permission.approved_mode != mode:
            return AuthorizationDecision(
                allowed=False,
                reason_code=AuthorizationReasonCode.mode_mismatch,
                source_id=source_id,
                mode=mode,
            )

        review_overdue = (
            as_of is not None
            and permission.review_due is not None
            and permission.review_due < as_of
        )
        return AuthorizationDecision(
            allowed=True,
            reason_code=AuthorizationReasonCode.approved,
            source_id=source_id,
            mode=mode,
            review_overdue=review_overdue,
        )

    def authorize_retrieval(self, source_id: str, mode: SourceMode) -> AuthorizationDecision:
        """The LIVE, pre-retrieval permission gate (Gate E0, E0.2; Fable
        ruling F5, "the clock is an identity too").

        Deliberately a NEW, SEPARATE entry point from :meth:`authorize`, not
        an extension of it. ``authorize()`` accepts a caller-supplied
        ``as_of`` so a caller can ask "was ``review_due`` overdue as of some
        date" for reporting purposes -- exactly the kind of caller-suppliable
        clock that would let a stale ``as_of`` dodge ``expires_at`` entirely
        if expiry enforcement were bolted onto that same method. This
        method's public signature therefore takes NO date, NO ``as_of``, NO
        ``now``, and no clock parameter of any kind -- there is nothing here
        for any caller to supply. It reads the enforcement clock itself, at
        the call site, via the module-level :func:`_today_utc`; a test that
        needs a fixed date monkeypatches that function, never a parameter on
        this method. (Do not add a ``_clock=`` keyword "for testability": a
        parameter that exists can be passed, and this method's whole point
        is that it cannot be.)

        It fetches the permission from THIS registry by ``source_id`` --
        it never accepts a permission record, status, or identity from a
        caller or adapter (the same B1 lesson Gate D applied to ``get()``).

        Blocks fail-closed, each with its OWN distinct
        :class:`AuthorizationReasonCode` (never collapsed): unregistered
        (``not_registered``), ``proposed`` (``status_proposed``),
        ``suspended`` (``status_suspended``), ``revoked``
        (``status_revoked``), a mode mismatch (``mode_mismatch``), and an
        expired permission (``status_expired``).

        **Expiry.** ``expires_at is None`` places no expiry constraint on
        the permission -- an unset field must never itself block. Otherwise
        expiry is fail-closed: ``expires_at`` strictly BEFORE today blocks.
        The boundary case, ``expires_at == today``, is decided as ALLOWED:
        ``expires_at`` is read as "valid through the end of this calendar
        date" (the ordinary reading of an expiry date on a document -- a
        permission that "expires 2026-07-27" is still valid to use during
        2026-07-27 itself; it only blocks starting the day after). This
        choice is pinned by a dedicated test in
        ``tests/test_connectors_permissions.py``.

        **Not yet wired to a fetch path.** The frozen design places this
        check immediately before retrieval, inside the fetch path -- but the
        fetch path does not exist in this codebase yet; it lands in Gate E0
        Part 2b (E0.4). This method exists, is fully tested, and is NOT
        invoked by ``runner.py`` or any adapter in this ticket.
        """
        permission = self._by_id.get(source_id)
        if permission is None:
            return AuthorizationDecision(
                allowed=False,
                reason_code=AuthorizationReasonCode.not_registered,
                source_id=source_id,
                mode=mode,
            )

        if permission.status != PermissionStatus.approved:
            status_reason = {
                PermissionStatus.proposed: AuthorizationReasonCode.status_proposed,
                PermissionStatus.suspended: AuthorizationReasonCode.status_suspended,
                PermissionStatus.revoked: AuthorizationReasonCode.status_revoked,
            }[permission.status]
            return AuthorizationDecision(
                allowed=False, reason_code=status_reason, source_id=source_id, mode=mode
            )

        if permission.approved_mode != SourceMode.both and permission.approved_mode != mode:
            return AuthorizationDecision(
                allowed=False,
                reason_code=AuthorizationReasonCode.mode_mismatch,
                source_id=source_id,
                mode=mode,
            )

        if permission.expires_at is not None and permission.expires_at < _today_utc():
            return AuthorizationDecision(
                allowed=False,
                reason_code=AuthorizationReasonCode.status_expired,
                source_id=source_id,
                mode=mode,
            )

        return AuthorizationDecision(
            allowed=True,
            reason_code=AuthorizationReasonCode.approved,
            source_id=source_id,
            mode=mode,
        )

    def get(self, source_id: str) -> SourcePermission | None:
        """Return the raw, authoritative :class:`SourcePermission` for
        ``source_id``, or ``None`` if unregistered.

        Gate D round-1 correction (B1): added so a caller (``runner.py``) can
        construct a ``DiscoveryResult.permission_ref`` FROM this registry's
        own record rather than trusting an adapter's self-reported
        ``approved_mode``/``status_at_discovery`` -- the proven spoofing
        probe relied on exactly that self-report being taken at face value.
        """
        return self._by_id.get(source_id)

    def enforce_discovery_fields(self, result: DiscoveryResult) -> FieldEnforcementDecision:
        """REJECT (never silently strip) a ``DiscoveryResult`` that carries
        content in a field its source's permission does not name in
        ``permitted_fields``. See the module docstring for exactly which
        fields are content-bearing and how "populated" is judged.

        Gate D round-1 correction (B1): also independently re-checks
        ``status == approved`` before looking at ``permitted_fields`` at
        all, so this enforcement point can never disagree with
        ``authorize()`` having already gated the same source upstream --
        even if a future caller invokes this method directly, without going
        through ``run_discovery`` first.
        """
        permission = self._by_id.get(result.source_id)
        if permission is None:
            return FieldEnforcementDecision(
                allowed=False,
                source_id=result.source_id,
                reason_code=FieldEnforcementReasonCode.not_registered,
            )
        if permission.status != PermissionStatus.approved:
            return FieldEnforcementDecision(
                allowed=False,
                source_id=result.source_id,
                reason_code=FieldEnforcementReasonCode.status_not_approved,
            )

        populated: dict[str, bool] = {
            "title": True,  # always non-empty by DiscoveryResult's own validation
            "canonical_reference": True,  # ditto
            "content_type": True,  # ditto
            "publication_date": result.publication_date is not None,
            "summary_normalized": result.summary_normalized != "",
        }
        violations = tuple(
            sorted(
                field_name
                for field_name in _CONTENT_BEARING_FIELDS
                if populated[field_name] and field_name not in permission.permitted_fields
            )
        )
        if violations:
            return FieldEnforcementDecision(
                allowed=False,
                source_id=result.source_id,
                reason_code=FieldEnforcementReasonCode.field_not_permitted,
                violating_fields=violations,
            )
        return FieldEnforcementDecision(
            allowed=True,
            source_id=result.source_id,
            reason_code=FieldEnforcementReasonCode.ok,
        )
