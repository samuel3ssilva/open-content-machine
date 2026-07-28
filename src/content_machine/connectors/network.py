"""The enforced network-fetch boundary (Gate E0, E0.4; Fable rulings F3.1/F3.2/F3.3).

**This is the second, and only other, module in the whole codebase permitted
to perform network I/O** -- see ``SECURITY.md`` and ``CLAUDE.md`` privacy
rule 4, which both name this module as the sole retrieval-boundary
exception (both were amended to say so in the Gate E0 commit that
introduced this file). Everywhere else, "no network calls in core" still
holds absolutely; this module exists precisely
because a *retrieval* boundary (as opposed to the *model* boundary
``providers/`` guards) has to live somewhere, and Fable's ruling was to
confine it to exactly one, heavily-enforced module rather than let every
future adapter grow its own ad hoc fetch code.

**Design decision: stdlib only.** ``urllib.request``/``http.client``/
``socket``/``ssl`` -- no ``requests``/``httpx``/``aiohttp``. This keeps the
one module that is allowed to open a socket free of any third-party
networking stack whose redirect/retry/connection-pool behavior we would not
fully control. Every redirect hop and every connection is handled by hand
(see :class:`_PinnedHTTPSConnection` and :meth:`NetworkFetcher.fetch`) so
nothing here can silently do more than this module intends.

**What is enforced, all of it, on every single call to** :meth:`NetworkFetcher.fetch`:

1. **HTTPS only.** Any other scheme is rejected before a socket is ever opened.
2. **Credential-bearing URLs are rejected** (``user:pass@host`` in the URL
   itself -- see ``_validate_url``).
3. **A per-source hostname allowlist, never a single global pool** (Fable
   F3.3): ``source_allowed_hosts`` maps ``source_id -> {hostnames}``, so a
   hostile discovery result from source A can never point retrieval at
   source B's allowed host -- there is no shared pool to point at.
4. **Loopback, private, link-local, unspecified, and multicast addresses are
   blocked**, and so is any URL whose *hostname* is itself an IP literal
   (see :func:`_default_address_is_blocked` and :func:`_is_ip_literal`).
5. **Only an explicit allowed-ports set** may be connected to (default:
   ``{443}``).
6. **Redirects are handled entirely by hand** (never by a library's
   auto-follow): each hop's target URL is re-parsed and re-validated through
   the *exact same* :meth:`_validate_url` call used for the original
   request -- same per-source allowlist, same scheme/credential/port/IP-literal
   checks -- and the hop count is bounded by ``max_redirects``.
7. **DNS rebinding is defeated by resolve-once-and-pin** (Fable F3.1): the
   hostname is resolved via :func:`_default_resolve_host` (or an injected
   resolver) exactly ONCE per hop, every candidate address returned is
   checked against the address predicate, and the connection is opened
   directly to the one vetted, literal IP address chosen -- there is no
   second DNS lookup between the check and the connect that a hostile
   resolver could answer differently. **The TLS certificate is still
   verified against the ORIGINAL hostname, never the pinned IP** (see
   :class:`_PinnedHTTPSConnection.connect`, which passes
   ``server_hostname=self.host`` -- the human-configured allowlisted
   hostname -- to ``wrap_socket``, even though the raw socket itself
   connects to the pinned IP address). Pinning the socket must never mean
   weakening what the certificate is checked against.
8. **Separate connect and read timeouts.**
9. **A streaming byte cap that aborts mid-stream**: the response body is
   read in bounded chunks and the connection is closed the instant the
   accumulated total exceeds ``max_response_bytes`` -- the remainder of the
   body is never requested or buffered.
10. **A MIME allowlist**, checked against the response's ``Content-Type``
    before any body bytes are trusted.
11. **Per-source rate limiting** (:class:`_RateLimiter`), keyed by
    ``source_id`` so one source being rate-limited can never block another.
12. **Errors are always sanitized** via
    :func:`content_machine.connectors.sanitize.sanitize_error` before being
    attached to a result -- the endpoint, hostname, and any response
    fragment never reach a result object, a log, or a human-facing message.
13. **Per-source failure isolation**: :meth:`NetworkFetcher.fetch` never
    raises for an expected failure condition (bad scheme, disallowed host,
    blocked address, timeout, oversized body, disallowed MIME, rate limit,
    denied permission, ...) -- it always returns a :class:`FetchResult`
    with a closed, never-collapsed reason code (mirroring
    ``permissions.AuthorizationDecision``'s "one distinct code per
    invariant" philosophy), so one source's failure can never propagate into
    or corrupt another source's fetch.
14. **The live permission gate.** Immediately before the first connection
    attempt of a fetch, :meth:`NetworkFetcher.fetch` calls
    ``permission_registry.authorize_retrieval(source_id, mode)`` --
    deliberately ``authorize_retrieval``, never ``authorize`` (see that
    method's docstring in ``permissions.py`` for why: it reads its own
    clock at the call site, with no caller-suppliable date parameter, so a
    permission revoked or expired between an earlier "planning" step and
    this actual retrieval is caught here, live, not against a stale
    decision). A denial here is reported as ``permission_denied``, with the
    authorization layer's own
    :class:`~content_machine.connectors.permissions.AuthorizationReasonCode`
    preserved on the result for audit.

**Public surface.** This module exports exactly one public, enforced *entry
point* -- :class:`NetworkFetcher` -- plus the two inert result DTOs a caller
needs to name what :meth:`NetworkFetcher.fetch` returns: :class:`FetchResult`
and :class:`FetchReasonCode`. Neither DTO carries any connection behavior of
its own. Every connection *primitive* -- URL validation, address
classification, DNS resolution, the pinned connection class, the rate
limiter -- stays private (a leading underscore) specifically so no other
module can import and misuse a fetch primitive directly, bypassing the
enforcement this docstring describes. ``tests/test_connectors_network.py``
asserts this module-level contract directly.

**The no-network static scan carve-out (R1/F1).** This is the one file in
``connectors/`` permitted to import ``socket``, ``ssl``, and
``http.client`` -- see the filename-scoped exemption in
``tests/test_connectors_no_network.py`` (search for "R1" / "F1" in that
file). Neither denylist set in that test was weakened to grant this
exemption; the carve-out is an explicit check against this file's path only.

**A permanent loopback/localhost special-case in the SHIPPED default address
predicate is deliberately absent and must never be added** -- see
:func:`_default_address_is_blocked`. Tests that need to reach a local
loopback HTTP(S) test server supply their OWN narrow, test-only
``address_is_blocked`` override (and/or a test-only ``source_allowed_hosts``
entry) via ``NetworkFetcher``'s constructor; the shipped defaults never
special-case ``127.0.0.1`` or ``localhost``.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from content_machine.connectors.permissions import (
    AuthorizationReasonCode,
    PermissionRegistry,
    SourceMode,
)
from content_machine.connectors.sanitize import sanitize_error

__all__ = ["FetchReasonCode", "FetchResult", "NetworkFetcher"]

# --- private constants ------------------------------------------------------

_DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({443})

_DEFAULT_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/xml",
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
        "text/plain",
        "application/json",
    }
)

#: 3xx statuses this module treats as a redirect to follow (manually,
#: bounded, and re-validated -- see the module docstring, point 6).
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

_STREAM_CHUNK_BYTES = 8192


# --- public, inert result / reason-code types --------------------------------
#
# FetchReasonCode and FetchResult are public so a caller can name what
# NetworkFetcher.fetch() returns -- they carry no connection behavior of
# their own (pre-E1 fix; see the module docstring's "Public surface" note).


class FetchReasonCode(StrEnum):
    """Closed set of :meth:`NetworkFetcher.fetch` outcomes, one distinct code
    per invariant -- never collapsed (mirrors
    ``permissions.AuthorizationReasonCode``'s philosophy)."""

    ok = "ok"
    scheme_not_https = "scheme_not_https"
    credential_in_url = "credential_in_url"
    invalid_url = "invalid_url"
    ip_literal_host = "ip_literal_host"
    host_not_allowed = "host_not_allowed"
    port_not_allowed = "port_not_allowed"
    address_blocked = "address_blocked"
    dns_resolution_failed = "dns_resolution_failed"
    permission_denied = "permission_denied"
    rate_limited = "rate_limited"
    connect_failed = "connect_failed"
    connect_timeout = "connect_timeout"
    read_timeout = "read_timeout"
    too_many_redirects = "too_many_redirects"
    invalid_response = "invalid_response"
    mime_not_allowed = "mime_not_allowed"
    response_too_large = "response_too_large"
    fetch_failed = "fetch_failed"


class FetchResult(BaseModel):
    """The explicit, closed outcome of one :meth:`NetworkFetcher.fetch` call.

    Frozen, ``extra="forbid"``, matching every other point-in-time decision
    record in ``connectors`` (``AuthorizationDecision``,
    ``FieldEnforcementDecision``). Never raised as an exception for an
    expected failure condition -- see the module docstring, point 13.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    reason_code: FetchReasonCode
    source_id: str
    #: Preserved for audit when ``reason_code == permission_denied`` -- the
    #: specific invariant ``authorize_retrieval`` reported (not_registered,
    #: status_revoked, status_expired, ...), never collapsed away.
    authorization_reason: AuthorizationReasonCode | None = None
    status_code: int | None = None
    content_type: str | None = None
    body: bytes = b""
    truncated: bool = False
    #: Never a body fragment, never the endpoint -- always the output of
    #: sanitize_error(). See the module docstring, point 12.
    sanitized_error: str | None = None


class _UrlValidation(BaseModel):
    """Result of validating one candidate URL (the original request URL, or
    a redirect hop's ``Location`` target) against the static, cheap checks
    that do not require a network round trip."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    reason_code: FetchReasonCode = FetchReasonCode.ok
    host: str = ""
    port: int = 443
    path_and_query: str = "/"


# --- the address predicate (R5 / Fable F3.2) --------------------------------


def _default_address_is_blocked(ip_text: str) -> bool:
    """PURE, injectable classifier: True if ``ip_text`` must never be
    connected to.

    Fail-closed on anything this function cannot confidently parse as an IP
    address -- including decimal/hex/octal integer encodings of an IPv4
    address (e.g. ``"2130706433"`` / ``"0x7f000001"``, both alternate
    spellings of ``127.0.0.1`` that a hostile resolver could hand back):
    ``ipaddress.ip_address`` rejects those forms outright, and this function
    treats "cannot parse" as "block it" rather than "allow it" -- a text
    value this function does not recognize is never assumed safe.

    Otherwise blocks anything that is not ordinary, globally-routable
    unicast address space: loopback, private (RFC 1918 and friends,
    including link-local and the IPv4-mapped-IPv6 form of any of the above,
    e.g. ``::ffff:127.0.0.1``), unspecified (``0.0.0.0``/``::``), and any
    other IANA special-purpose range are all covered by ``is_global`` being
    False; multicast is checked explicitly because a small number of
    multicast ranges otherwise read as ``is_global=True``.

    **No permanent loopback/localhost special-case exists here, and none may
    ever be added** -- see the module docstring's closing paragraph. A test
    that needs to reach a loopback server supplies its own override.
    """
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return addr.is_multicast or not addr.is_global


def _is_ip_literal(hostname: str) -> bool:
    """True if ``hostname`` (already stripped of any ``[...]`` brackets by
    ``urlsplit``) is itself an IP address literal rather than a DNS name --
    such hosts are rejected outright (module docstring, point 4): a
    per-source allowlist is a set of DNS *names*, and a source configured
    with an IP-literal "hostname" would defeat the very idea of curating
    which names a source may resolve to."""
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _default_resolve_host(host: str, port: int) -> list[str]:
    """Resolve ``host`` to its candidate literal IP addresses, exactly once
    per call -- callers must never call this twice for the same hop and mix
    results (that reintroduces the DNS-rebinding window this module exists
    to close). Injectable so tests can supply a fixed, offline answer set."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    candidates: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        ip_text = str(sockaddr[0])
        if ip_text not in candidates:
            candidates.append(ip_text)
    return candidates


# --- the pinned connection (Fable F3.1) -------------------------------------


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An ``HTTPSConnection`` whose TCP connect step is pinned to a single,
    already-vetted literal IP address chosen ONCE by the caller (see
    :func:`_default_resolve_host`), while TLS certificate verification still
    checks the ORIGINAL hostname -- never the pinned IP. See the module
    docstring, point 7, for why both halves of that sentence matter.
    """

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        super().__init__(host, port, timeout=connect_timeout, context=ssl_context)
        self._pinned_ip = pinned_ip
        self._read_timeout = read_timeout
        # Stored on our own attribute name (rather than relying on the base
        # class's private, unstubbed `_context`) so this works cleanly under
        # mypy without reaching into a superclass implementation detail.
        self._pinned_ssl_context = ssl_context

    def connect(self) -> None:
        # Connect to the literal, pre-vetted IP -- NOT a fresh hostname
        # lookup -- so there is no second DNS answer a hostile resolver
        # could substitute between the address-predicate check and this
        # connect. `self.timeout` here is the CONNECT timeout passed to
        # __init__ above.
        raw_sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        # `server_hostname` is the ORIGINAL hostname (self.host), never the
        # pinned IP: certificate validation must check what the operator
        # configured in the allowlist, not the address we happened to dial.
        self.sock = self._pinned_ssl_context.wrap_socket(raw_sock, server_hostname=self.host)
        # From here on (sending the request, reading status/headers/body),
        # the READ timeout applies, distinct from the connect timeout above.
        self.sock.settimeout(self._read_timeout)


# --- per-source rate limiting ------------------------------------------------


class _RateLimiter:
    """Fixed-window rate limiter, keyed by ``source_id`` so one rate-limited
    source can never block another (module docstring, point 11/13). Pure
    state container with an injectable clock for deterministic tests."""

    def __init__(
        self,
        *,
        max_calls: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._clock = clock
        self._calls_by_source: dict[str, list[float]] = {}

    def allow(self, source_id: str) -> bool:
        now = self._clock()
        history = self._calls_by_source.setdefault(source_id, [])
        cutoff = now - self._window_seconds
        while history and history[0] < cutoff:
            history.pop(0)
        if len(history) >= self._max_calls:
            return False
        history.append(now)
        return True


# --- the single public entry point ------------------------------------------


class NetworkFetcher:
    """The enforced fetch boundary. See the module docstring for the full
    enforcement list; this class is the one public, enforced *entry point*
    this module exports -- construct one instance (typically per batch run)
    and call :meth:`fetch` for every retrieval. (:class:`FetchResult` and
    :class:`FetchReasonCode`, the inert result DTOs :meth:`fetch` returns,
    are also public -- see the module docstring's "Public surface" note --
    but neither performs any connection work of its own; all connection
    machinery in this module stays private.)
    """

    def __init__(
        self,
        *,
        permission_registry: PermissionRegistry,
        source_allowed_hosts: Mapping[str, Iterable[str]],
        allowed_ports: frozenset[int] = _DEFAULT_ALLOWED_PORTS,
        allowed_mime_types: frozenset[str] = _DEFAULT_ALLOWED_MIME_TYPES,
        max_response_bytes: int = 2_000_000,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        max_redirects: int = 4,
        rate_limit_max_calls: int = 5,
        rate_limit_window_seconds: float = 60.0,
        address_is_blocked: Callable[[str], bool] = _default_address_is_blocked,
        resolve_host: Callable[[str, int], list[str]] = _default_resolve_host,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._permission_registry = permission_registry
        self._source_allowed_hosts: dict[str, frozenset[str]] = {
            source_id: frozenset(hosts) for source_id, hosts in source_allowed_hosts.items()
        }
        self._allowed_ports = allowed_ports
        self._allowed_mime_types = allowed_mime_types
        self._max_response_bytes = max_response_bytes
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_redirects = max_redirects
        self._address_is_blocked = address_is_blocked
        self._resolve_host = resolve_host
        self._ssl_context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._rate_limiter = _RateLimiter(
            max_calls=rate_limit_max_calls, window_seconds=rate_limit_window_seconds
        )

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult:
        """Fetch ``url`` on behalf of ``source_id``, enforcing every control
        described in the module docstring. Never raises for an expected
        failure condition -- always returns a closed-reason-code result.

        The rate-limit budget is consumed PER HOP, immediately before that
        hop's DNS resolution and connection attempt -- never before
        ``_validate_url`` and never before ``authorize_retrieval`` (module
        docstring, point 11; pre-E1 fix). One budget unit buys exactly one
        outbound connection attempt (which also covers that hop's DNS
        query), so a statically-invalid URL or a denied permission consumes
        no budget and is never masked as ``rate_limited`` -- and a redirect
        chain can no longer trade one budget unit for
        ``max_redirects + 1`` outbound connects. A refusal at ANY hop,
        including mid-chain, is reported as ``rate_limited``; that reason
        code is intentionally not split per hop-index. Consequently,
        ``too_many_redirects`` is only reachable when at least
        ``max_redirects + 1`` budget units remain in the source's window at
        the start of this ``fetch()`` call; otherwise the chain ends
        ``rate_limited`` at whichever hop the budget runs out on --
        deliberate, because the budget covers every packet-emitting hop and
        is never suspended mid-chain."""
        current_url = url
        for hop_index in range(self._max_redirects + 1):
            validation = self._validate_url(source_id, current_url)
            if not validation.ok:
                return FetchResult(
                    ok=False, reason_code=validation.reason_code, source_id=source_id
                )

            if hop_index == 0:
                # The LIVE permission gate, immediately before the first
                # connection attempt -- authorize_retrieval, never
                # authorize. See the module docstring, point 14.
                decision = self._permission_registry.authorize_retrieval(source_id, mode)
                if not decision.allowed:
                    return FetchResult(
                        ok=False,
                        reason_code=FetchReasonCode.permission_denied,
                        source_id=source_id,
                        authorization_reason=decision.reason_code,
                    )

            if not self._rate_limiter.allow(source_id):
                return FetchResult(
                    ok=False, reason_code=FetchReasonCode.rate_limited, source_id=source_id
                )

            candidates = self._resolve_host(validation.host, validation.port)
            if not candidates:
                return FetchResult(
                    ok=False,
                    reason_code=FetchReasonCode.dns_resolution_failed,
                    source_id=source_id,
                )
            vetted_ip = next(
                (ip for ip in candidates if not self._address_is_blocked(ip)), None
            )
            if vetted_ip is None:
                return FetchResult(
                    ok=False, reason_code=FetchReasonCode.address_blocked, source_id=source_id
                )

            outcome = self._connect_and_fetch_one_hop(
                source_id=source_id,
                host=validation.host,
                port=validation.port,
                vetted_ip=vetted_ip,
                path_and_query=validation.path_and_query,
            )

            if isinstance(outcome, _RedirectHop):
                if outcome.location is None:
                    return FetchResult(
                        ok=False, reason_code=FetchReasonCode.invalid_response, source_id=source_id
                    )
                current_url = outcome.location
                continue

            return outcome

        return FetchResult(
            ok=False, reason_code=FetchReasonCode.too_many_redirects, source_id=source_id
        )

    # -- private helpers ------------------------------------------------

    def _validate_url(self, source_id: str, url_text: str) -> _UrlValidation:
        parsed = urlsplit(url_text)
        if parsed.scheme.lower() != "https":
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.scheme_not_https)
        if parsed.username is not None or parsed.password is not None:
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.credential_in_url)
        hostname = parsed.hostname
        if not hostname:
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.invalid_url)
        if _is_ip_literal(hostname):
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.ip_literal_host)
        allowed_hosts = self._source_allowed_hosts.get(source_id, frozenset())
        if hostname not in allowed_hosts:
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.host_not_allowed)
        port = parsed.port if parsed.port is not None else 443
        if port not in self._allowed_ports:
            return _UrlValidation(ok=False, reason_code=FetchReasonCode.port_not_allowed)
        path_and_query = parsed.path or "/"
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"
        return _UrlValidation(ok=True, host=hostname, port=port, path_and_query=path_and_query)

    def _connect_and_fetch_one_hop(
        self,
        *,
        source_id: str,
        host: str,
        port: int,
        vetted_ip: str,
        path_and_query: str,
    ) -> FetchResult | _RedirectHop:
        connection = _PinnedHTTPSConnection(
            host,
            vetted_ip,
            port,
            ssl_context=self._ssl_context,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
        )
        try:
            connection.connect()
        except TimeoutError as exc:
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.connect_timeout,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )
        except (OSError, ssl.SSLError) as exc:
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.connect_failed,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )

        try:
            connection.request(
                "GET",
                path_and_query,
                headers={
                    "Host": host,
                    "User-Agent": "OpenContentMachine-connectors/1.0",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
        except TimeoutError as exc:
            connection.close()
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.read_timeout,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            connection.close()
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.fetch_failed,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )

        if response.status in _REDIRECT_STATUSES:
            location = response.getheader("Location")
            connection.close()
            return _RedirectHop(location=location)

        content_type_header = response.getheader("Content-Type") or ""
        main_type = content_type_header.split(";", 1)[0].strip().lower()
        if main_type not in self._allowed_mime_types:
            connection.close()
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.mime_not_allowed,
                source_id=source_id,
                status_code=response.status,
                content_type=main_type or None,
            )

        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            while True:
                chunk = response.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._max_response_bytes:
                    # Abort MID-STREAM: stop reading and close immediately,
                    # never request/buffer the rest of the body. Module
                    # docstring, point 9.
                    truncated = True
                    break
                chunks.append(chunk)
        except TimeoutError as exc:
            connection.close()
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.read_timeout,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            connection.close()
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.fetch_failed,
                source_id=source_id,
                sanitized_error=sanitize_error(exc),
            )

        connection.close()

        if truncated:
            return FetchResult(
                ok=False,
                reason_code=FetchReasonCode.response_too_large,
                source_id=source_id,
                status_code=response.status,
                content_type=main_type,
                truncated=True,
            )

        return FetchResult(
            ok=True,
            reason_code=FetchReasonCode.ok,
            source_id=source_id,
            status_code=response.status,
            content_type=main_type,
            body=b"".join(chunks),
        )


class _RedirectHop(BaseModel):
    """Internal-only signal returned by ``_connect_and_fetch_one_hop`` to
    tell :meth:`NetworkFetcher.fetch`'s loop to re-validate and retry at a
    new URL. Never returned from the public ``fetch`` method itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str | None = None
