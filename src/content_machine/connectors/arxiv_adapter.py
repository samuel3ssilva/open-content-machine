"""The arXiv shadow-window RSS discovery adapter (Gate E1 pre-network
approval; Fable rulings RC-2/RC-4/RC-5).

**This module builds the first real-adapter SHAPE in this codebase's
history, but performs no real network call in this ticket.** Nothing here is
wired into a scheduler, nothing is registered against the real arXiv feed
host, and ``arxiv.org`` is deliberately NOT allowlisted anywhere in ``src/``
or ``tests/`` -- that omission is load-bearing, not an oversight (see
:func:`build_arxiv_rss_adapter`'s docstring). A separate Fable security audit
(RC-6) must pass, and a real, Founder-curated
:class:`~content_machine.connectors.registry.SourceRegistryEntry` /
:class:`~content_machine.connectors.permissions.SourcePermission` /
:class:`~content_machine.connectors.private_config.PrivateSourceEndpoint`
must exist in the Founder's private workspace, before this adapter's
``discover()`` is ever invoked against a real host.

**RC-4 endpoint hygiene.** The real feed hostname and URL are never
hardcoded here: :class:`ArxivRssAdapter` takes ``feed_url`` as a REQUIRED
keyword argument with no default, and :func:`build_arxiv_rss_adapter` reads
it from a :class:`~content_machine.connectors.private_config.PrivateSourceConfig`
(``endpoint.endpoint.get_secret_value()``) -- the only place a real value may
ever live. Every test in this package's test suite uses an
``example.com``-shaped synthetic config; no real hostname or URL appears in
``src/`` or ``tests/`` anywhere.

**The approved configuration (Fable, RC-2/RC-4/RC-5).** The module-level
constants below are pinned literals, each covered by a dedicated test
asserting its literal value in ``tests/test_connectors_arxiv_adapter.py``
(the same "no tautological test" discipline ``models.py``'s own pinned
constants use):

- allowlist: exactly one hostname, sourced from private config (never
  ``arxiv.org`` itself, anywhere in this repository).
- exactly one :meth:`~content_machine.connectors.network.NetworkFetcher.fetch`
  call per :meth:`ArxivRssAdapter.discover` -- no retry; a failed fetch is one
  :class:`~content_machine.connectors.runner.ConnectorAdapterError` and the
  run ends (`run_discovery`'s per-source isolation records exactly one
  :class:`~content_machine.connectors.failures.SourceFailure` for it).
- ``max_redirects=0``, ``allowed_ports={443}``,
  ``allowed_mime_types={application/rss+xml, text/xml, application/xml}``,
  rate limit 1 call / 3.0s, ``max_response_bytes=2_000_000``.
- retention: metadata only -- no PDF, no source file, no full article text.
  The fetched feed BODY itself is never persisted anywhere: it is held only
  in a :class:`~content_machine.connectors.models.TemporaryContentHandle`,
  decoded, parsed, and disposed of before :meth:`discover` returns (see that
  class's disposal-honesty docstring for exactly what "disposed" does and
  does not guarantee). ``AdapterDiscoveryOutcome``/``DiscoveryResult`` have
  no body field to smuggle it into.

**Body handling.** The fetched bytes are decoded as UTF-8 with
``errors="replace"`` (never ``strict`` -- a hostile/malformed byte sequence
must never crash discovery); any resulting U+FFFD replacement character that
lands inside a retained ``title``/``summary_normalized`` field is caught and
flagged ``malformed_encoding`` by
:func:`~content_machine.connectors.sanitize.sanitize_text`, exactly the
signal the ticket calls for. The decoded text is then parsed with the
**stdlib** :mod:`xml.etree.ElementTree` parser -- no ``defusedxml`` or other
third-party dependency (a stdlib-only precedent this codebase already holds
for its network and sanitization layers). CPython's bundled ``expat`` (the
C parser ``ElementTree`` uses) enforces a built-in entity-amplification
ceiling: a classic "billion laughs" DOCTYPE with nested internal entities
was measured, directly, against this exact adapter's parse path to raise
``xml.etree.ElementTree.ParseError`` in well under a second (never hangs,
never a large in-memory expansion) -- this is PINNED, not assumed; see
``test_billion_laughs_feed_fails_closed_in_bounded_time`` for the
wall-clock proof. Every other DOCTYPE/entity-shaped hostile input this
module was tested against (an undefined entity, a ``SYSTEM`` external
entity reference, malformed/truncated XML) also raises ``ParseError`` --
external entities are never fetched or resolved (``ElementTree`` does not
support external entity resolution at all), so there is no XXE path here
either. Any ``ParseError``, and a document whose root element is not
``<rss>``, both raise :class:`~content_machine.connectors.runner.ConnectorAdapterError`
with :data:`~content_machine.connectors.failures.FailureKind.invalid_response`
-- a clean, fail-closed :class:`~content_machine.connectors.failures.SourceFailure`
for the whole run, never a partial/best-effort parse.

**RC-3 non-retention.** ``dc:creator``, ``managingEditor``, and ``dc:rights``
are never looked up on an ``<item>`` element at all -- not merely "not
copied forward." Only ``title``, ``link``, ``description``, and ``pubDate``
are ever read. :class:`~content_machine.connectors.models.DiscoveryResult`
has no author field to smuggle a name into, and this module never
concatenates anything into ``title``/``summary_normalized`` beyond those
four elements' own (sanitized) text.

**Item handling.** ``publication_date`` parses fail-soft to ``None`` (a
malformed or missing ``pubDate`` never fails the whole item, let alone the
whole run). Items are capped at
:data:`MAX_ITEMS` (20) with non-silent truncation
(``AdapterDiscoveryOutcome.truncated``/``dropped_count``) -- an item that
cannot even produce a valid ``canonical_reference`` (missing/invalid
``<link>``) is also counted in ``dropped_count``, distinctly from the
ceiling truncation, in this adapter's own accounting (see :meth:`_parse_item`).

**Audit.** Every :meth:`discover` call appends exactly one
:class:`~content_machine.connectors.models.ConnectorAuditEvent` (readable via
:attr:`ArxivRssAdapter.audit_events` after the call returns) carrying the
underlying :class:`~content_machine.connectors.network.FetchResult.reason_code`
verbatim (even when it is ``"ok"`` but the body then failed to parse -- the
network-level and adapter-level outcomes are reported as what they actually
were, never collapsed into one), the HTTP status code, the observed byte
count, and items-parsed/dropped -- packed into ``detail`` (``reason_code``
itself is ``ConnectorAuditEvent``'s own top-level field).
**Implementer assumption, stated explicitly:** the
:class:`~content_machine.connectors.runner.ConnectorAdapter` protocol
(``source_id`` + ``discover()`` only) has no return channel for an
adapter's own audit trail back into ``run_discovery``'s
``BatchDiscoveryResult.audit_events`` in this gate -- extending that
protocol or ``run_discovery``'s signature is an architecture decision this
ticket was explicitly told not to make unilaterally, so this adapter exposes
its own audit trail as a plain, adapter-local attribute instead, for a
future gate's wiring to consume. **Also an explicit implementer judgment
call:** this module maps each
:class:`~content_machine.connectors.network.FetchReasonCode` onto a
:class:`~content_machine.connectors.failures.FailureKind` for the
``ConnectorAdapterError`` it raises on a failed fetch (see
:data:`_FETCH_REASON_TO_FAILURE_KIND`) -- the two enums are not 1:1, and
this mapping is this adapter's own choice, not a spec-mandated table.
**Known limitation, stated honestly rather than assumed away:**
:class:`~content_machine.connectors.network.FetchResult` only ever
populates ``body`` on the success path; a rejected fetch (oversized,
wrong-MIME, too-many-redirects, ...) always reports an observed byte count
of 0 in this adapter's audit row, because ``network.py`` itself does not
expose a partial bytes-read count on those paths. Fixing that is a
``network.py``-level change out of this ticket's scope -- flagged here for a
future RC-5 cap-tightening ticket, not silently worked around.

**What this module deliberately does NOT do (ticket scope).** No scheduler
wiring, no real run, no ``arxiv.org`` entry in any allowlist, no
:class:`~content_machine.connectors.models.VerificationUpgrade` wiring (that
stays deferred, per ``models.py``'s own honest scope statement). The real
:class:`~content_machine.connectors.registry.SourceRegistryEntry` /
:class:`~content_machine.connectors.permissions.SourcePermission` for this
source, with real dates, are curated in the Founder's private workspace --
exactly the same precedent ``registry.py``'s own module docstring already
states for every other real source. This module's test suite constructs
ONLY synthetic, ``example.com``-shaped registry/permission fixtures to prove
the wiring (including the ``expires_at`` = end-of-7-day-shadow-window kill
switch) works, never a real entry.
"""

from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Protocol

from pydantic import ValidationError

from content_machine.connectors.failures import FailureKind
from content_machine.connectors.models import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    AuditEventKind,
    ConnectorAuditEvent,
    DiscoveryRequest,
    DiscoveryResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
    TemporaryContentHandle,
)
from content_machine.connectors.network import FetchReasonCode, FetchResult, NetworkFetcher
from content_machine.connectors.permissions import PermissionRegistry, SourceMode
from content_machine.connectors.private_config import (
    PrivateSourceConfig,
    source_allowed_hosts_from_config,
)
from content_machine.connectors.runner import AdapterDiscoveryOutcome, ConnectorAdapterError
from content_machine.connectors.sanitize import sanitize_text

__all__ = [
    "ALLOWED_MIME_TYPES",
    "ALLOWED_PORTS",
    "MAX_ITEMS",
    "MAX_REDIRECTS",
    "MAX_REQUESTS_PER_DISCOVER",
    "MAX_RESPONSE_BYTES",
    "RATE_LIMIT_MAX_CALLS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "ArxivRssAdapter",
    "UnknownPrivateSourceEndpointError",
    "build_arxiv_rss_adapter",
]

# --- RC-2/RC-4/RC-5 approved configuration, pinned literals ------------------

#: Exactly one `NetworkFetcher.fetch()` call per `discover()` -- no retry.
MAX_REQUESTS_PER_DISCOVER = 1
#: A redirect of any kind ends the fetch as `too_many_redirects` -- deliberate.
MAX_REDIRECTS = 0
ALLOWED_PORTS: frozenset[int] = frozenset({443})
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {"application/rss+xml", "text/xml", "application/xml"}
)
RATE_LIMIT_MAX_CALLS = 1
RATE_LIMIT_WINDOW_SECONDS = 3.0
MAX_RESPONSE_BYTES = 2_000_000
#: Discovery-time item cap, independent of (and typically tighter than)
#: `DiscoveryRequest.max_items_per_source` / `runner.py`'s own ceiling
#: enforcement -- see the module docstring's "Item handling" section.
MAX_ITEMS = 20


class UnknownPrivateSourceEndpointError(KeyError):
    """Raised by :func:`build_arxiv_rss_adapter` when ``source_id`` has no
    matching entry in the supplied :class:`PrivateSourceConfig`."""


class _Fetcher(Protocol):
    """The minimal shape :class:`ArxivRssAdapter` needs from a fetcher --
    exactly :class:`~content_machine.connectors.network.NetworkFetcher.fetch`'s
    signature, as a structural ``Protocol`` rather than a concrete import
    dependency. A real ``NetworkFetcher`` instance satisfies this
    structurally; so does a test-only stub with no network capability at
    all -- see the test suite's ``_ScriptedFetcher``."""

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult: ...


#: This adapter's own judgment call mapping each network-level outcome onto
#: the closed `FailureKind` taxonomy for the `ConnectorAdapterError` it
#: raises -- see the module docstring's "Audit" section for why this is
#: called out explicitly as an implementer decision, not a spec table.
#: Every `FetchReasonCode` member except `ok` (the success case, handled
#: separately) MUST appear here exactly once --
#: `test_fetch_reason_to_failure_kind_mapping_is_complete` pins that.
_FETCH_REASON_TO_FAILURE_KIND: dict[FetchReasonCode, FailureKind] = {
    FetchReasonCode.scheme_not_https: FailureKind.unavailable,
    FetchReasonCode.credential_in_url: FailureKind.unavailable,
    FetchReasonCode.invalid_url: FailureKind.unavailable,
    FetchReasonCode.ip_literal_host: FailureKind.unavailable,
    FetchReasonCode.host_not_allowed: FailureKind.unavailable,
    FetchReasonCode.port_not_allowed: FailureKind.unavailable,
    FetchReasonCode.address_blocked: FailureKind.unavailable,
    FetchReasonCode.dns_resolution_failed: FailureKind.unavailable,
    FetchReasonCode.permission_denied: FailureKind.permission_revoked,
    FetchReasonCode.rate_limited: FailureKind.rate_limited,
    FetchReasonCode.connect_failed: FailureKind.unavailable,
    FetchReasonCode.connect_timeout: FailureKind.timeout,
    FetchReasonCode.read_timeout: FailureKind.timeout,
    FetchReasonCode.too_many_redirects: FailureKind.unavailable,
    FetchReasonCode.invalid_response: FailureKind.invalid_response,
    FetchReasonCode.mime_not_allowed: FailureKind.unsupported_content,
    FetchReasonCode.response_too_large: FailureKind.invalid_response,
    FetchReasonCode.fetch_failed: FailureKind.unavailable,
}


def _parse_pub_date(raw: str | None) -> date | None:
    """Fail-soft RFC 2822 (RSS ``pubDate``) parsing: ``None`` on anything
    missing or malformed, never an exception -- see the module docstring's
    "Item handling" section."""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed.date()


class ArxivRssAdapter:
    """Satisfies :class:`~content_machine.connectors.runner.ConnectorAdapter`
    (a ``source_id`` attribute + ``discover(request) -> AdapterDiscoveryOutcome``)
    for one RSS feed source. Intended to be invoked ONLY through
    :func:`~content_machine.connectors.runner.run_discovery` -- see that
    module's docstring for the per-source isolation and identity-binding
    guarantees this relies on. Every constructor argument that could ever
    carry a real endpoint value (``feed_url``) is REQUIRED, with no default
    -- see the module docstring's RC-4 section.
    """

    def __init__(
        self,
        source_id: str,
        *,
        feed_url: str,
        fetcher: _Fetcher,
        source_group: str,
        discovery_run_id: str,
        retrieved_at: datetime,
        approved_mode: str = "discovery",
        status_at_discovery: str = "approved",
    ) -> None:
        self.source_id = source_id
        self._feed_url = feed_url
        self._fetcher = fetcher
        self._source_group = source_group
        self._discovery_run_id = discovery_run_id
        self._retrieved_at = retrieved_at
        self._approved_mode = approved_mode
        self._status_at_discovery = status_at_discovery
        self._audit_events: tuple[ConnectorAuditEvent, ...] = ()

    @property
    def audit_events(self) -> tuple[ConnectorAuditEvent, ...]:
        """The audit trail produced by the most recent :meth:`discover` call
        (empty before ``discover`` has ever been called). See the module
        docstring's "Audit" section for why this is a plain adapter
        attribute rather than a ``run_discovery`` return value in this
        gate."""
        return self._audit_events

    def _provenance(self) -> ProvenanceMetadata:
        return ProvenanceMetadata(
            adapter_name=type(self).__name__,
            discovery_run_id=self._discovery_run_id,
            discovered_at=self._retrieved_at,
        )

    def _permission_ref(self) -> PermissionRef:
        # Never trusted downstream anyway -- runner.run_discovery
        # reconstructs the authoritative PermissionRef from the live
        # PermissionRegistry (Gate D round-1 correction, B1). This is only
        # ever a self-reported placeholder.
        return PermissionRef(
            source_id=self.source_id,
            approved_mode=self._approved_mode,
            status_at_discovery=self._status_at_discovery,
        )

    def _record_audit(
        self,
        fetch_result: FetchResult,
        *,
        byte_count: int,
        items_parsed: int,
        dropped_count: int,
        extra_detail: str | None = None,
    ) -> None:
        detail = (
            f"status_code={fetch_result.status_code} byte_count={byte_count} "
            f"items_parsed={items_parsed} dropped_count={dropped_count}"
        )
        if extra_detail:
            detail = f"{detail} {extra_detail}"
        event_kind = AuditEventKind.retention if fetch_result.ok else AuditEventKind.failure
        self._audit_events = (
            ConnectorAuditEvent(
                source_id=self.source_id,
                event_kind=event_kind,
                reason_code=fetch_result.reason_code.value,
                occurred_at=self._retrieved_at,
                detail=detail[:280],
            ),
        )

    def _parse_item(self, item_el: ET.Element, *, content_type: str) -> DiscoveryResult | None:
        """Build one ``DiscoveryResult`` from an ``<item>`` element, or
        ``None`` if it cannot become one (no usable ``<link>``).

        RC-3: only ``title``/``link``/``description``/``pubDate`` are ever
        looked up on ``item_el`` -- ``dc:creator``, ``managingEditor``, and
        ``dc:rights`` are never read, not merely "not copied forward".
        """
        raw_title = (item_el.findtext("title") or "").strip()
        raw_link = (item_el.findtext("link") or "").strip()
        raw_description = (item_el.findtext("description") or "").strip()
        raw_pub_date = item_el.findtext("pubDate")

        if not raw_link:
            return None

        title_clean = sanitize_text(raw_title, max_chars=TITLE_MAX_CHARS)
        summary_clean = sanitize_text(raw_description, max_chars=SUMMARY_MAX_CHARS)
        flags = tuple(
            sorted(
                set(title_clean.flags) | set(summary_clean.flags),
                key=lambda flag: flag.value,
            )
        )
        title_text = title_clean.text or "(untitled feed item)"

        try:
            return DiscoveryResult(
                source_id=self.source_id,
                source_group=self._source_group,
                title=title_text,
                publication_date=_parse_pub_date(raw_pub_date),
                canonical_reference=raw_link,
                summary_normalized=summary_clean.text,
                summary_provenance=SummaryProvenance.source_supplied,
                content_type=content_type,
                retrieved_at=self._retrieved_at,
                provenance=self._provenance(),
                permission_ref=self._permission_ref(),
                security_flags=flags,
            )
        except ValidationError:
            # canonical_reference failed DiscoveryResult's own shape checks
            # (e.g. a non-http(s) scheme, or an over-length value) -- drop
            # this one item rather than failing the whole source.
            return None

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        """Fetch the feed exactly once, parse it, and return a bounded,
        sanitized ``AdapterDiscoveryOutcome`` -- or raise
        ``ConnectorAdapterError`` for the whole source. See the module
        docstring for the full behavior this method relies on
        (``NetworkFetcher``'s enforcement, stdlib-only XML parsing,
        RC-3 non-retention, the 20-item cap).
        """
        fetch_result = self._fetcher.fetch(
            source_id=self.source_id, mode=SourceMode.discovery, url=self._feed_url
        )

        if not fetch_result.ok:
            self._record_audit(
                fetch_result,
                byte_count=len(fetch_result.body),
                items_parsed=0,
                dropped_count=0,
            )
            failure_kind = _FETCH_REASON_TO_FAILURE_KIND.get(
                fetch_result.reason_code, FailureKind.unavailable
            )
            raise ConnectorAdapterError(
                failure_kind,
                f"feed fetch did not succeed: {fetch_result.reason_code.value}",
                retry_eligible=False,
            )

        content_type = fetch_result.content_type or "application/xml"
        handle = TemporaryContentHandle(
            canonical_reference=self._feed_url,
            content_type=content_type,
            content=fetch_result.body,
        )
        with handle:
            raw_text = bytes(handle.content).decode("utf-8", errors="replace")
            disposal = handle.dispose()

        try:
            root = ET.fromstring(raw_text)
        except ET.ParseError as exc:
            self._record_audit(
                fetch_result,
                byte_count=disposal.byte_count,
                items_parsed=0,
                dropped_count=0,
                extra_detail=f"xml_parse_error={type(exc).__name__}",
            )
            raise ConnectorAdapterError(
                FailureKind.invalid_response,
                "feed body failed to parse as XML (malformed, hostile entity "
                "expansion, or truncated)",
                retry_eligible=False,
            ) from None

        if root.tag != "rss":
            self._record_audit(
                fetch_result,
                byte_count=disposal.byte_count,
                items_parsed=0,
                dropped_count=0,
                extra_detail="unexpected_root_element",
            )
            raise ConnectorAdapterError(
                FailureKind.invalid_response,
                "feed root element was not <rss>",
                retry_eligible=False,
            )

        channel = root.find("channel")
        raw_items = channel.findall("item") if channel is not None else []

        parsed_results: list[DiscoveryResult] = []
        invalid_item_count = 0
        for item_el in raw_items:
            result = self._parse_item(item_el, content_type=content_type)
            if result is None:
                invalid_item_count += 1
            else:
                parsed_results.append(result)

        ceiling_excess = max(len(parsed_results) - MAX_ITEMS, 0)
        kept_results = tuple(parsed_results[:MAX_ITEMS])
        truncated = ceiling_excess > 0
        dropped_count = invalid_item_count + ceiling_excess

        self._record_audit(
            fetch_result,
            byte_count=disposal.byte_count,
            items_parsed=len(kept_results),
            dropped_count=dropped_count,
        )

        return AdapterDiscoveryOutcome(
            results=kept_results, dropped_count=dropped_count, truncated=truncated
        )


def build_arxiv_rss_adapter(
    source_id: str,
    private_config: PrivateSourceConfig,
    permission_registry: PermissionRegistry,
    *,
    source_group: str,
    discovery_run_id: str,
    retrieved_at: datetime,
) -> ArxivRssAdapter:
    """Wire an :class:`ArxivRssAdapter` using the EXISTING private-config
    mechanism (``source_allowed_hosts_from_config``) -- the ticket's
    required entry point for turning a loaded
    :class:`~content_machine.connectors.private_config.PrivateSourceConfig`
    into a fetcher and adapter, with the approved RC-2/RC-4/RC-5
    configuration applied.

    **This function only CONSTRUCTS objects -- it never calls ``fetch()``
    or ``discover()``, opens no socket, and is never invoked against a real
    private config anywhere in this repository's test suite** (every test
    exercising this function passes an ``example.com``-shaped synthetic
    :class:`PrivateSourceConfig`). Wiring this into a scheduler or an
    actual run is explicitly out of this ticket's scope.

    Raises :class:`UnknownPrivateSourceEndpointError` if ``source_id`` has
    no matching entry in ``private_config.endpoints`` -- fail closed rather
    than silently falling back to a default endpoint (matching
    ``private_config.py``'s own "no default endpoint" posture).
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

    fetcher = NetworkFetcher(
        permission_registry=permission_registry,
        source_allowed_hosts=source_allowed_hosts_from_config(private_config),
        allowed_ports=ALLOWED_PORTS,
        allowed_mime_types=ALLOWED_MIME_TYPES,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_redirects=MAX_REDIRECTS,
        rate_limit_max_calls=RATE_LIMIT_MAX_CALLS,
        rate_limit_window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    )
    return ArxivRssAdapter(
        source_id,
        feed_url=endpoint.endpoint.get_secret_value(),
        fetcher=fetcher,
        source_group=source_group,
        discovery_run_id=discovery_run_id,
        retrieved_at=retrieved_at,
    )
