"""Private endpoint configuration loader (Gate E0 §5, Fable ruling F6).

**What lives where.** This module ships the CONTRACT (a pydantic model),
the LOADER, and its validation. It ships **synthetic examples only** --
``tests/test_connectors_private_config.py`` is the one place a fixture value
appears, and every fixture value there is ``example.com``/``example.org``
shaped, never a real endpoint. The actual source id, hostname, endpoint,
limits, permission record, and authorization state for any real connector
source live in a file OUTSIDE this repository, in the Founder's private
workspace. This module does not create or read that file's content ahead of
time -- it only knows how to find one at runtime, via
:class:`~content_machine.config.settings.Settings` (never ``os.environ``
directly; see the module docstring's "Path resolution" section below), and
how to fail closed if what it finds is missing, unreadable, invalid, or
expired.

**Path resolution goes through ``config/`` only.** ``docs/architecture.md``
§2 and ``config/settings.py``'s own module docstring make
``content_machine.config`` the ONLY module allowed to read environment
variables, and ``tests/test_architecture_env_boundary.py`` enforces that
with a static AST scan. This module never imports ``os`` and never touches
``os.environ``/``os.getenv`` -- it accepts an already-constructed
:class:`~content_machine.config.settings.Settings` instance (built by
whatever entry point owns that responsibility) or an explicit ``path``
argument, exactly as the ticket requires.

**F6 -- pydantic's ``ValidationError`` echoes input values.** By default,
a pydantic v2 ``ValidationError`` embeds the OFFENDING INPUT VALUE in its
own ``str()``/``repr()`` (verified directly against this repo's pinned
pydantic version before this module was written -- see the commit that
introduced this module for the reproduction). For this module's contract,
the offending input value could be the real hostname or endpoint itself,
which would then land in a traceback, a log line, or CI output the instant
a private config fails to validate. **This module never lets a raw
``ValidationError`` (or any other exception raised while loading) escape
unmodified.** Every fail-closed exception raised by :func:`load_private_source_config`
carries ONLY the output of :func:`content_machine.connectors.sanitize.sanitize_error`
-- the offending exception's TYPE NAME, and nothing else; never its
message, never a field value, never a fragment of the file's content. This
reuses the existing ``sanitize_error`` contract rather than inventing a
second sanitizer, per the ticket.

**``SecretStr`` everywhere a real value could land.** :class:`PrivateSourceEndpoint`
holds ``hostname`` and ``endpoint`` as ``pydantic.SecretStr``, which pydantic
itself masks in ``repr()``, ``str()``, ``model_dump()``, and
``model_dump_json()`` (all verified directly, not merely asserted, in this
module's test file) -- so even a caller who accidentally logs a whole
:class:`PrivateSourceEndpoint` or :class:`PrivateSourceConfig` object never
leaks the real value that way either.

**Fail CLOSED, always, with a distinct exception per invariant** (mirrors
the "one distinct code per invariant, never collapsed" philosophy already
used by ``permissions.AuthorizationReasonCode`` and
``network.FetchReasonCode``): missing file, unreadable file, invalid
content, and an expired/unauthorized source are each their own exception
type under :class:`PrivateConfigError`. There is no default endpoint to
fall back to and no partial/silent success path -- an expired source causes
the WHOLE load to fail (never a silent per-source drop that lets the rest
of the file's sources through unnoticed), matching this codebase's existing
"reject, never silently strip" posture (see
``permissions.PermissionRegistry.enforce_discovery_fields``'s docstring for
the same principle applied to a different invariant).

**CI never requires a private config to exist.** ``Settings.private_source_config_path``
defaults to ``None``, and :func:`load_private_source_config` treats "no path
configured" identically to "configured path does not exist" --
:class:`MissingPrivateConfigError`, the ONE fail-closed path CI's test suite
exercises. Nothing in this repository's test suite points
``CONTENT_MACHINE_PRIVATE_SOURCE_CONFIG_PATH`` at a real file.

**Wiring to the fetcher is minimal, by design.** :func:`source_allowed_hosts_from_config`
produces exactly the ``Mapping[str, Iterable[str]]`` shape
:class:`~content_machine.connectors.network.NetworkFetcher`'s constructor
already accepts as ``source_allowed_hosts`` -- nothing in
``network.py`` changes, and this module does not attempt to also drive
``NetworkFetcher``'s rate-limit or other constructor arguments from a
private config in this ticket; ``rate_limit_max_calls``/
``rate_limit_window_seconds`` on :class:`PrivateSourceEndpoint` are captured
as data (the ticket's "limits") for a later, separately-scoped wiring
ticket.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from content_machine.config.settings import Settings
from content_machine.connectors.sanitize import sanitize_error

__all__ = [
    "ExpiredPrivateConfigError",
    "InvalidPrivateConfigError",
    "MissingPrivateConfigError",
    "PrivateConfigError",
    "PrivateSourceConfig",
    "PrivateSourceEndpoint",
    "UnreadablePrivateConfigError",
    "load_private_source_config",
    "source_allowed_hosts_from_config",
]


def _today_utc() -> date:
    """The ONE clock read behind :func:`load_private_source_config`'s
    expiry check -- isolated exactly the way
    ``permissions._today_utc`` is, so a test that wants a fixed "today"
    monkeypatches THIS function rather than threading a date parameter
    through the loader's public signature (which would let a stale caller-
    supplied date dodge an expiry, the same reasoning
    ``permissions.PermissionRegistry.authorize_retrieval``'s docstring
    gives for its own clock)."""
    return datetime.now(UTC).date()


class PrivateSourceEndpoint(BaseModel):
    """One source's private retrieval endpoint and its authorization state.

    Never populated with a real value anywhere in this repository. Frozen,
    ``extra="forbid"`` -- matching every other point-in-time contract record
    in ``connectors`` (``SourcePermission``, ``FetchResult``,
    ``AuthorizationDecision``).

    ``hostname``/``endpoint`` are ``SecretStr`` so that this object's own
    ``repr()``/``str()``/``model_dump()``/``model_dump_json()`` can never
    accidentally surface the real value -- see the module docstring.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    #: The DNS hostname a ``NetworkFetcher`` should be allowed to connect
    #: to on this source's behalf. Not the full endpoint -- see ``endpoint``.
    hostname: SecretStr
    #: The full private endpoint (scheme, host, path). Stored for a future,
    #: separately-scoped adapter to consume; not read by this module's own
    #: ``source_allowed_hosts_from_config`` helper, which only needs
    #: ``hostname``.
    endpoint: SecretStr
    #: Whether this source's private authorization currently permits
    #: retrieval at all. ``False`` fails the WHOLE load closed -- see the
    #: module docstring.
    authorized: bool = True
    #: The date this source's authorization is valid THROUGH (inclusive --
    #: mirrors ``permissions.PermissionRegistry.authorize_retrieval``'s own
    #: "valid through the end of this calendar date" reading of an expiry
    #: boundary). ``None`` places no expiry constraint.
    authorized_until: date | None = None
    #: Per-source retrieval limits ("limits" in the ticket's field list).
    #: Captured as data only in this ticket -- not yet wired into
    #: ``NetworkFetcher``'s constructor. See the module docstring.
    rate_limit_max_calls: int = Field(default=5, gt=0)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)

    @field_validator("hostname", "endpoint")
    @classmethod
    def _secret_must_be_non_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value


class PrivateSourceConfig(BaseModel):
    """A validated, loaded private endpoint configuration: zero or more
    :class:`PrivateSourceEndpoint` records, one per source, with no
    duplicate ``source_id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoints: tuple[PrivateSourceEndpoint, ...] = Field(default_factory=tuple)

    @field_validator("endpoints")
    @classmethod
    def _no_duplicate_source_ids(
        cls, value: tuple[PrivateSourceEndpoint, ...]
    ) -> tuple[PrivateSourceEndpoint, ...]:
        seen: set[str] = set()
        for endpoint in value:
            if endpoint.source_id in seen:
                raise ValueError("duplicate source_id in private source config")
            seen.add(endpoint.source_id)
        return value


class PrivateConfigError(Exception):
    """Base for every fail-closed outcome of :func:`load_private_source_config`.

    Every subclass's ``str()`` carries ONLY a generic, static description or
    ``sanitize_error()``'s output -- never a path, a field value, or a raw
    exception message. See the module docstring's F6 section.
    """


class MissingPrivateConfigError(PrivateConfigError):
    """No private source config is configured, or the configured path does
    not exist / is not a file.

    **This is the ONE fail-closed path CI exercises.** ``Settings.private_source_config_path``
    defaults to ``None`` and nothing in this repository's test suite points
    it at a real file, so CI always takes this path and never requires a
    private config to exist.
    """

    def __init__(self) -> None:
        super().__init__("private source config: not configured or file not found")


class UnreadablePrivateConfigError(PrivateConfigError):
    """The configured path exists but could not be read (permissions, not a
    regular file, an I/O error, ...). Carries only ``sanitize_error(exc)`` --
    never the underlying ``OSError``'s own message, which can include the
    path itself."""

    def __init__(self, sanitized_error: str) -> None:
        self.sanitized_error = sanitized_error
        super().__init__(f"private source config: unreadable file ({sanitized_error})")


class InvalidPrivateConfigError(PrivateConfigError):
    """The file's content failed to parse as JSON, or failed
    :class:`PrivateSourceConfig`'s pydantic validation.

    F6: carries only ``sanitize_error(exc)`` -- a raw ``json.JSONDecodeError``
    or ``pydantic.ValidationError`` is NEVER attached to or embedded in this
    exception, because pydantic's ``ValidationError`` embeds the offending
    input value (which could be the real hostname or endpoint) in its own
    ``str()``/``repr()``. See the module docstring.
    """

    def __init__(self, sanitized_error: str) -> None:
        self.sanitized_error = sanitized_error
        super().__init__(f"private source config: invalid content ({sanitized_error})")


class ExpiredPrivateConfigError(PrivateConfigError):
    """At least one configured source's authorization was expired or
    withdrawn as of load time. Fails the WHOLE load closed -- never a
    silent per-source drop (see the module docstring). Carries the affected
    ``source_id``s (curated identifiers, never PII or endpoint data) so a
    human can act."""

    def __init__(self, expired_source_ids: tuple[str, ...]) -> None:
        self.expired_source_ids = expired_source_ids
        joined = ", ".join(expired_source_ids)
        super().__init__(
            f"private source config: {len(expired_source_ids)} source(s) "
            f"expired or unauthorized ({joined})"
        )


def load_private_source_config(
    settings: Settings | None = None,
    *,
    path: Path | None = None,
) -> PrivateSourceConfig:
    """Load, validate, and return a :class:`PrivateSourceConfig`.

    Exactly one of ``settings`` or ``path`` should be meaningful:
    ``path``, if given, is used as-is; otherwise ``settings.private_source_config_path``
    is used. Passing neither is a caller contract error (``ValueError``, not
    a fail-closed data outcome -- this module never guesses at a default
    endpoint or reads the environment itself to find one; see the module
    docstring).

    Never falls back to a default endpoint and never silently continues on
    a data problem. Raises one of :class:`MissingPrivateConfigError`,
    :class:`UnreadablePrivateConfigError`, :class:`InvalidPrivateConfigError`,
    or :class:`ExpiredPrivateConfigError` -- each a DISTINCT, closed outcome
    -- for every fail-closed case; returns a validated, non-expired
    :class:`PrivateSourceConfig` otherwise.
    """
    if path is None:
        if settings is None:
            raise ValueError(
                "load_private_source_config requires either `settings` or an "
                "explicit `path` -- it never reads the environment itself"
            )
        path = settings.private_source_config_path

    if path is None or not path.is_file():
        raise MissingPrivateConfigError()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnreadablePrivateConfigError(sanitize_error(exc)) from None

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidPrivateConfigError(sanitize_error(exc)) from None

    try:
        config = PrivateSourceConfig.model_validate(raw_data)
    except ValidationError as exc:
        # F6: exc's own str()/repr() may embed the offending input value
        # (verified directly -- see the module docstring). sanitize_error()
        # reduces it to the exception's type name only, and that reduced
        # form is the ONLY thing attached to the exception this function
        # raises.
        raise InvalidPrivateConfigError(sanitize_error(exc)) from None

    today = _today_utc()
    expired_source_ids = tuple(
        sorted(
            endpoint.source_id
            for endpoint in config.endpoints
            if not endpoint.authorized
            or (endpoint.authorized_until is not None and endpoint.authorized_until < today)
        )
    )
    if expired_source_ids:
        raise ExpiredPrivateConfigError(expired_source_ids)

    return config


def source_allowed_hosts_from_config(
    config: PrivateSourceConfig,
) -> dict[str, frozenset[str]]:
    """Build the ``source_allowed_hosts`` mapping that
    :class:`~content_machine.connectors.network.NetworkFetcher`'s
    constructor expects (``source_id -> {hostnames}``) from an already
    loaded, already non-expired :class:`PrivateSourceConfig`.

    Only ever meaningful on a :class:`PrivateSourceConfig` returned by
    :func:`load_private_source_config`, which refuses to return one at all
    unless every entry's authorization was still valid at load time -- so
    there is nothing further to filter here. Does not import or otherwise
    touch ``network.py`` -- this module produces a plain mapping in the
    shape that constructor already accepts; ``NetworkFetcher``'s own
    enforcement behaviour is unchanged.
    """
    return {
        endpoint.source_id: frozenset({endpoint.hostname.get_secret_value()})
        for endpoint in config.endpoints
    }
