"""Tests for content_machine.connectors.network (Gate E0, E0.4; Fable rulings
F3.1/F3.2/F3.3): the enforced network-fetch boundary.

Every test in this file that performs real socket I/O talks ONLY to a
loopback HTTPS test server this file starts itself (``_LoopbackTLSServer``,
bound to ``127.0.0.1`` on an OS-assigned ephemeral port, using a synthetic,
throwaway self-signed certificate generated AT TEST-RUN TIME by the
session-scoped ``_generate_loopback_tls_material`` fixture below, via the
system ``openssl`` CLI, into a pytest-managed temp directory -- never
written into the repo and never committed. It is generated fresh every test
run with ``CN=localhost`` / ``SAN=DNS:localhost`` and deliberately NO IP
SAN -- see
``test_certificate_is_verified_against_original_hostname_not_pinned_ip``
for why the missing IP SAN is load-bearing. No test in this file makes a
request to any real external host, and the shipped default address
predicate is never overridden to permit loopback except via an explicit,
narrow, test-only override passed into ``NetworkFetcher``'s constructor, per
``network.py``'s own module docstring.
"""

from __future__ import annotations

import http.server
import json
import shutil
import ssl
import subprocess
import threading
import time
import types
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest

from content_machine.connectors import network as network_module
from content_machine.connectors.network import FetchReasonCode, FetchResult, NetworkFetcher
from content_machine.connectors.permissions import (
    AuthorizationReasonCode,
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)

#: Populated once per test session by the autouse ``_generate_loopback_tls_material``
#: fixture below, before any test in this module runs. Never committed --
#: generated fresh, per session, into a pytest-managed temp directory.
_TEST_CERT: Path | None = None
_TEST_KEY: Path | None = None


@pytest.fixture(scope="session", autouse=True)
def _generate_loopback_tls_material(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Generate a throwaway, self-signed TLS cert/key pair for the loopback
    test server, once per test session, via the system ``openssl`` CLI
    (present locally and on GitHub Actions runners), into a pytest-managed
    temp directory -- never into the repo, never committed.

    ``CN=localhost`` / ``SAN=DNS:localhost`` ONLY -- deliberately NO IP SAN.
    That absence is load-bearing: it is exactly what proves
    ``test_certificate_is_verified_against_original_hostname_not_pinned_ip``,
    because a fetcher that regressed to verifying TLS against the pinned IP
    instead of the original hostname would find no SAN entry to match and
    the connection would fail.

    Fails loudly (raises) if ``openssl`` is unavailable or the invocation
    fails -- deliberately never ``pytest.skip``. A skipped security test
    silently reads as a pass in CI, which is worse than a loud failure.
    """
    global _TEST_CERT, _TEST_KEY

    openssl = shutil.which("openssl")
    if openssl is None:
        raise RuntimeError(
            "openssl was not found on PATH. It is required to generate "
            "throwaway TLS test material for tests/test_connectors_network.py "
            "at run time (no cert/key material is committed to the repo). "
            "Install openssl -- it is present by default on GitHub Actions "
            "runners and was expected to be present locally."
        )

    tmp_dir = tmp_path_factory.mktemp("loopback_tls")
    cert_path = tmp_dir / "network_loopback_cert.crt"
    key_path = tmp_dir / "network_loopback_key.pkey"

    subprocess.run(  # noqa: S603 - test-only, fixed argv, no shell, no untrusted input
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )

    _TEST_CERT = cert_path
    _TEST_KEY = key_path
    yield


# --- loopback-only HTTPS test server -----------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib-mandated name
        self.server.on_get(self)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep test output quiet


class _LoopbackTLSServer:
    """A minimal HTTPS server bound to 127.0.0.1:<ephemeral port>, using the
    checked-in synthetic test certificate. Every test supplies its own
    ``on_get`` handler to control the response shape (redirect, oversized
    body, hang, wrong MIME, ...)."""

    def __init__(self, on_get: Callable[[http.server.BaseHTTPRequestHandler], None]) -> None:
        assert _TEST_CERT is not None and _TEST_KEY is not None, (
            "_generate_loopback_tls_material (session-scoped, autouse) must "
            "have run before any test starts a _LoopbackTLSServer"
        )
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.on_get = on_get  # type: ignore[attr-defined]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(_TEST_CERT), keyfile=str(_TEST_KEY))
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __enter__(self) -> _LoopbackTLSServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def _client_ssl_context() -> ssl.SSLContext:
    """A client context that trusts ONLY the synthetic test certificate --
    never the real system CA store weakened, just a separate, test-only
    trust root pointed at our own throwaway loopback cert."""
    assert _TEST_CERT is not None, (
        "_generate_loopback_tls_material (session-scoped, autouse) must "
        "have run before any test builds a client SSL context"
    )
    return ssl.create_default_context(cafile=str(_TEST_CERT))


def _allow_only_loopback(ip_text: str) -> bool:
    """TEST-ONLY address predicate override: permits ONLY 127.0.0.1 (where
    ``_LoopbackTLSServer`` listens), blocks everything else -- including the
    private-range addresses used to simulate a rebinding/redirect attack.
    Never the shipped default (see network.py's module docstring)."""
    return ip_text != "127.0.0.1"


def _ok_permission_registry(source_id: str = "src_test") -> PermissionRegistry:
    return PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
            )
        ]
    )


def _fetcher(
    *,
    port: int,
    source_allowed_hosts: dict[str, frozenset[str]] | None = None,
    permission_registry: PermissionRegistry | None = None,
    resolve_host: Callable[[str, int], list[str]] | None = None,
    address_is_blocked: Callable[[str], bool] = _allow_only_loopback,
    **kwargs: object,
) -> NetworkFetcher:
    return NetworkFetcher(
        permission_registry=permission_registry or _ok_permission_registry(),
        source_allowed_hosts=source_allowed_hosts or {"src_test": frozenset({"localhost"})},
        allowed_ports=frozenset({port}),
        address_is_blocked=address_is_blocked,
        resolve_host=resolve_host or (lambda host, p: ["127.0.0.1"]),
        ssl_context=_client_ssl_context(),
        **kwargs,  # type: ignore[arg-type]
    )


def _plain_ok_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
    body = b'{"ok": true}'
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# --- 1b. public surface -----------------------------------------------


def test_module_public_surface_is_exactly_three_names() -> None:
    """R1/F1-adjacent contract for network.py itself: the only names defined
    in this module and not underscore-prefixed must be the one enforced
    entry point (``NetworkFetcher``) plus its two inert result DTOs
    (``FetchResult``, ``FetchReasonCode``) -- every connection PRIMITIVE
    stays private so no other module can import and misuse it directly."""
    public_names = [
        name
        for name, obj in vars(network_module).items()
        if not name.startswith("_")
        and not isinstance(obj, types.ModuleType)
        and getattr(obj, "__module__", None) == network_module.__name__
    ]
    assert sorted(public_names) == ["FetchReasonCode", "FetchResult", "NetworkFetcher"]
    assert network_module.__all__ == ["FetchReasonCode", "FetchResult", "NetworkFetcher"]


# --- 1a. the address predicate: hand-written literal table -------------


@pytest.mark.parametrize(
    "hostile_ip",
    [
        "127.0.0.1",
        "::1",
        "10.0.0.5",
        "172.16.0.5",
        "192.168.1.5",
        "169.254.1.1",
        "fe80::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "2130706433",  # decimal encoding of 127.0.0.1
        "0x7f000001",  # hex encoding of 127.0.0.1
    ],
)
def test_default_address_predicate_blocks_every_hostile_literal(hostile_ip: str) -> None:
    """This literal table is hand-written here, independent of the
    predicate's own implementation constants (spec §1a) -- it must never be
    derived from the same ranges the checker itself uses."""
    assert network_module._default_address_is_blocked(hostile_ip) is True


def test_default_address_predicate_does_not_block_every_address() -> None:
    """Non-triviality check: the predicate is not simply `return True`
    unconditionally. This literal is used ONLY as a pure-function input to
    an IP-classification check -- no test in this file ever connects to it."""
    assert network_module._default_address_is_blocked("93.184.216.5") is False


def test_default_address_predicate_blocks_multicast_despite_is_global_quirk() -> None:
    """ipaddress.ip_address("224.0.0.1").is_global is True (a documented
    stdlib quirk for multicast ranges); the predicate must not connect to a
    multicast address just because of that, so multicast is checked
    explicitly and separately."""
    assert network_module._default_address_is_blocked("224.0.0.1") is True


def test_no_permanent_loopback_special_case_in_shipped_predicate() -> None:
    """A permanent 127.0.0.1/localhost special-case in shipped allowlist
    logic is a live SSRF hole and is forbidden (spec §1a). Loopback must be
    blocked by the SHIPPED DEFAULT with no override."""
    assert network_module._default_address_is_blocked("127.0.0.1") is True
    assert network_module._default_address_is_blocked("::1") is True


def test_is_ip_literal_host_detection() -> None:
    assert network_module._is_ip_literal("127.0.0.1") is True
    assert network_module._is_ip_literal("::1") is True
    assert network_module._is_ip_literal("example.com") is False
    assert network_module._is_ip_literal("2130706433") is False  # not IP-literal SHAPED


# --- static validation: scheme / credentials / IP-literal / allowlist / port


def test_fetch_rejects_non_https_scheme() -> None:
    fetcher = _fetcher(port=1)
    result = fetcher.fetch(source_id="src_test", mode=SourceMode.discovery, url="http://localhost/feed")
    assert result.ok is False
    assert result.reason_code == "scheme_not_https"


def test_fetch_rejects_credential_bearing_url() -> None:
    fetcher = _fetcher(port=1)
    result = fetcher.fetch(
        source_id="src_test",
        mode=SourceMode.discovery,
        url="https://user:pass@localhost/feed",
    )
    assert result.ok is False
    assert result.reason_code == "credential_in_url"


def test_fetch_rejects_ip_literal_host() -> None:
    fetcher = _fetcher(port=1, source_allowed_hosts={"src_test": frozenset({"127.0.0.1"})})
    result = fetcher.fetch(source_id="src_test", mode=SourceMode.discovery, url="https://127.0.0.1/feed")
    assert result.ok is False
    assert result.reason_code == "ip_literal_host"


def test_fetch_rejects_host_not_in_this_sources_allowlist() -> None:
    fetcher = _fetcher(port=1, source_allowed_hosts={"src_test": frozenset({"other.example.com"})})
    result = fetcher.fetch(source_id="src_test", mode=SourceMode.discovery, url="https://localhost/feed")
    assert result.ok is False
    assert result.reason_code == "host_not_allowed"


def test_per_source_allowlist_is_never_a_single_global_pool() -> None:
    """Fable F3.3: a hostile discovery result naming source A must never be
    able to point retrieval at source B's allowed host, because there is no
    shared pool -- each source_id's allowlist is looked up independently."""
    fetcher = _fetcher(
        port=1,
        source_allowed_hosts={
            "src_a": frozenset({"a.example.com"}),
            "src_b": frozenset({"b.example.com"}),
        },
    )
    # src_a asking for src_b's allowed host must be rejected.
    result = fetcher.fetch(source_id="src_a", mode=SourceMode.discovery, url="https://b.example.com/feed")
    assert result.ok is False
    assert result.reason_code == "host_not_allowed"


def test_fetch_rejects_port_outside_allowlist() -> None:
    fetcher = NetworkFetcher(
        permission_registry=_ok_permission_registry(),
        source_allowed_hosts={"src_test": frozenset({"localhost"})},
        allowed_ports=frozenset({443}),
        address_is_blocked=_allow_only_loopback,
        resolve_host=lambda host, p: ["127.0.0.1"],
        ssl_context=_client_ssl_context(),
    )
    result = fetcher.fetch(
        source_id="src_test", mode=SourceMode.discovery, url="https://localhost:9999/feed"
    )
    assert result.ok is False
    assert result.reason_code == "port_not_allowed"


def test_fetch_rejects_missing_hostname() -> None:
    fetcher = _fetcher(port=1)
    result = fetcher.fetch(source_id="src_test", mode=SourceMode.discovery, url="https:///feed")
    assert result.ok is False
    assert result.reason_code == "invalid_url"


# --- the live permission gate (spec §3) ---------------------------------


def test_fetch_calls_authorize_retrieval_never_authorize(monkeypatch: pytest.MonkeyPatch) -> None:
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        registry = _ok_permission_registry()
        retrieval_calls: list[tuple[str, SourceMode]] = []
        authorize_calls: list[tuple[str, SourceMode]] = []
        original_retrieval = registry.authorize_retrieval
        original_authorize = registry.authorize

        def spy_retrieval(source_id: str, mode: SourceMode) -> object:
            retrieval_calls.append((source_id, mode))
            return original_retrieval(source_id, mode)

        def spy_authorize(source_id: str, mode: SourceMode, *, as_of: date | None = None) -> object:
            authorize_calls.append((source_id, mode))
            return original_authorize(source_id, mode, as_of=as_of)

        monkeypatch.setattr(registry, "authorize_retrieval", spy_retrieval)
        monkeypatch.setattr(registry, "authorize", spy_authorize)

        fetcher = _fetcher(port=server.port, permission_registry=registry)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )

        assert result.ok is True
        assert retrieval_calls == [("src_test", SourceMode.discovery)]
        assert authorize_calls == []


def test_fetch_blocks_when_permission_revoked_between_planning_and_fetch() -> None:
    """Simulates the exact timeline the ticket is worried about: a
    "planning" step (e.g. discovery-time authorize()) sees an approved
    permission, but by the time fetch() actually runs -- against
    whatever PermissionRegistry state is CURRENT at call time -- the
    permission has been revoked. Because SourcePermission/PermissionRegistry
    are frozen/immutable (Gate D B3), "revoked mid-flight" is modeled as a
    fresh registry built with the updated status, exactly as a real caller
    would rebuild it after a revocation."""
    source_id = "src_test"
    planning_time_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
            )
        ]
    )
    # "Planning" step succeeds against the registry as it stood then.
    planning_decision = planning_time_registry.authorize(source_id, SourceMode.discovery)
    assert planning_decision.allowed is True

    # By fetch time, the permission has been revoked -- a NEW registry,
    # never an in-place mutation of the old one.
    fetch_time_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.revoked,
            )
        ]
    )
    fetcher = _fetcher(port=443, permission_registry=fetch_time_registry)
    result = fetcher.fetch(
        source_id=source_id, mode=SourceMode.discovery, url="https://localhost/feed"
    )
    assert result.ok is False
    assert result.reason_code == "permission_denied"
    assert result.authorization_reason == AuthorizationReasonCode.status_revoked


def test_fetch_blocks_when_permission_expired_by_fetch_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from content_machine.connectors import permissions as permissions_module

    source_id = "src_test"
    registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
                expires_at=date(2026, 1, 1),
            )
        ]
    )
    monkeypatch.setattr(permissions_module, "_today_utc", lambda: date(2026, 7, 27))

    fetcher = _fetcher(port=443, permission_registry=registry)
    result = fetcher.fetch(
        source_id=source_id, mode=SourceMode.discovery, url="https://localhost/feed"
    )
    assert result.ok is False
    assert result.reason_code == "permission_denied"
    assert result.authorization_reason == AuthorizationReasonCode.status_expired


# --- end-to-end proofs against the loopback test server -----------------


def test_fetch_succeeds_end_to_end_against_loopback_server() -> None:
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(port=server.port)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is True
        assert result.status_code == 200
        assert result.content_type == "application/json"
        assert json.loads(result.body) == {"ok": True}


def test_certificate_is_verified_against_original_hostname_not_pinned_ip() -> None:
    """The subtlest requirement in the ticket: pinning the socket to a
    literal IP must not weaken what the TLS certificate is checked against.
    The checked-in test certificate's SAN is `DNS:localhost` ONLY (no IP
    SAN -- see this file's module docstring). If _PinnedHTTPSConnection.connect
    ever regressed to passing the pinned IP as `server_hostname` instead of
    the original hostname, certificate verification would fail here because
    "127.0.0.1" matches no SAN on this certificate."""
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(port=server.port, resolve_host=lambda host, p: ["127.0.0.1"])
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is True, (
            "cert verification against the original hostname must succeed "
            "even though the socket connected to the pinned IP"
        )


def test_dns_rebinding_pin_defeats_a_second_different_answer() -> None:
    """Fable F3.1. Resolution happens exactly ONCE per hop and the
    connection is pinned to that one vetted address -- a resolver that
    would answer differently on a hypothetical second call must never
    affect the outcome, because there must never BE a second call."""
    call_count = 0

    def rebinding_resolver(host: str, port: int) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ["127.0.0.1"]  # the address our test server actually listens on
        # A hostile second answer -- must never be consulted.
        return ["10.0.0.5"]

    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(port=server.port, resolve_host=rebinding_resolver)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is True
        assert call_count == 1, "resolve_host must be called exactly once per hop"


def test_connection_dials_the_vetted_ip_not_a_fresh_hostname_resolution() -> None:
    """Fable F3.1: resolution happens exactly once per hop and the
    connection must be PINNED to that one vetted address -- dialing the
    literal IP, never re-resolving the hostname at connect time.

    This test exists because the sibling
    ``test_dns_rebinding_pin_defeats_a_second_different_answer`` does NOT
    falsify a ``connect()`` that silently re-resolves ``self.host`` instead
    of dialing ``self._pinned_ip``. That sibling test only counts calls to
    the injected ``resolve_host`` -- and ``resolve_host`` is invoked exactly
    once inside ``fetch()`` (to produce ``vetted_ip``) regardless of whether
    ``_PinnedHTTPSConnection.connect`` subsequently honors that value or
    ignores it and dials ``self.host`` (which triggers its own, separate,
    OS-level hostname lookup via ``socket.create_connection``). Because that
    sibling test's hostname happens to resolve to the same loopback server
    the test expects, a regressed ``connect()`` that re-resolves the
    hostname would still land in the right place and the test would stay
    green -- proving only "the injectable resolver was called once", not
    "the socket dialed the address that resolver returned" (the actual
    control per Fable F3.1: resolve once and pin, so no second DNS answer
    can be substituted between the address check and the connect).

    To falsify that exact mutation, this test makes the vetted IP and the
    hostname's real resolution point at DIFFERENT places: ``resolve_host``
    is rigged to return, as its SOLE candidate, ``::1`` -- the IPv6
    loopback address, whose interface is always up but on which nothing
    listens (the test server below binds ``127.0.0.1`` only, IPv4) --
    while the hostname ``localhost`` still resolves normally (via the
    real OS resolver) to the loopback test server on ``127.0.0.1``. Because
    the ``::1`` interface is genuinely up, the kernel refuses the
    connection immediately (no listener on that port) rather than the
    connection silently hanging -- deterministic and fast, unlike an
    unassigned IPv4 loopback alias (e.g. ``127.0.0.7``) which this test
    was confirmed to leave blackholed (timing out) rather than refused on
    at least one development platform. A correctly pinned fetcher must
    dial the bogus vetted IP and fail immediately with connection-refused
    (``connect_failed``); nothing about this test's control flow can ever
    reach the real server. A fetcher whose ``connect()`` regressed to
    dialing ``self.host`` instead of ``self._pinned_ip`` would silently
    re-resolve "localhost", land on the real test server, and wrongly
    report ``ok=True`` -- which is exactly the failure mode this test is
    designed to catch.
    """

    def resolver_returns_a_sole_unreachable_loopback_ip(host: str, port: int) -> list[str]:
        return ["::1"]

    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(
            port=server.port,
            resolve_host=resolver_returns_a_sole_unreachable_loopback_ip,
            # Narrow, test-only override: admits ONLY the bogus vetted IP
            # this test rigged resolve_host to return -- never the real
            # server's 127.0.0.1 -- so nothing in this test's own plumbing
            # can accidentally route the "correctly pinned" path to the
            # live server.
            address_is_blocked=lambda ip_text: ip_text != "::1",
            # Short and explicit: loopback connection-refused is immediate
            # on every platform this suite runs on, but pin a small timeout
            # anyway so this test can never hang if that assumption ever
            # stops holding somewhere.
            connect_timeout=2.0,
        )
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is False, (
            "connect() must have dialed the vetted IP (::1, where nothing "
            "listens), not re-resolved 'localhost' to the real server -- "
            "ok=True here means the pin was bypassed"
        )
        assert result.reason_code == "connect_failed", (
            f"expected connect_failed (connection refused on the vetted, "
            f"unreachable IP), got {result.reason_code!r}"
        )


def test_streaming_byte_cap_aborts_mid_stream_proven_by_servers_own_counter() -> None:
    """Proof standard (spec §5): "an exception was raised" is not proof --
    the test server's OWN sent-byte counter must be strictly less than the
    full intended body size, proving the client stopped requesting/reading
    (and closed the connection) before the server finished sending."""
    chunk = b"x" * 2000
    num_chunks = 20
    full_body_size = len(chunk) * num_chunks  # 40000 bytes
    bytes_written = {"count": 0}

    def slow_oversized_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(full_body_size))
        handler.end_headers()
        for _ in range(num_chunks):
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                bytes_written["count"] += len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(0.05)

    with _LoopbackTLSServer(slow_oversized_handler) as server:
        fetcher = _fetcher(port=server.port, max_response_bytes=1000)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is False
        assert result.reason_code == "response_too_large"
        assert result.truncated is True

    # Give the server thread a brief moment to notice the closed socket and
    # stop writing, then assert it never got anywhere near the full body.
    time.sleep(0.3)
    assert bytes_written["count"] < full_body_size, (
        "server must not have been allowed to finish sending the full body"
    )


def test_mime_not_allowed_rejects_before_trusting_body() -> None:
    def wrong_mime_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
        body = b"<html><body>not a feed</body></html>"
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with _LoopbackTLSServer(wrong_mime_handler) as server:
        fetcher = _fetcher(
            port=server.port,
            allowed_mime_types=frozenset({"application/json"}),
        )
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is False
        assert result.reason_code == "mime_not_allowed"
        assert result.body == b""


def test_read_timeout_is_bounded_by_wall_clock() -> None:
    """Proof standard (spec §5): prove by bounded wall-clock, not merely
    that an exception occurred. The server accepts the TCP+TLS connection
    fully (so this genuinely exercises the READ timeout, not connect) and
    then withholds any response at all."""

    def hang_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
        time.sleep(5)  # much longer than the fetcher's configured read_timeout
        handler.send_response(200)
        handler.end_headers()

    with _LoopbackTLSServer(hang_handler) as server:
        fetcher = _fetcher(port=server.port, connect_timeout=2.0, read_timeout=0.3)
        started = time.monotonic()
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        elapsed = time.monotonic() - started

        assert result.ok is False
        assert result.reason_code == "read_timeout"
        assert elapsed < 2.0, "must not have waited anywhere near the 5s hang"


def test_connect_timeout_value_is_passed_to_the_underlying_connect_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine connect-phase HANG cannot be reproduced deterministically
    on loopback (there is no packet loss/firewall drop to trigger it, unlike
    on a real network) -- this is a well-known, industry-standard testing
    limitation, not something specific to this module. Instead this test
    proves STRUCTURALLY that the configured connect_timeout is exactly what
    gets passed to socket.create_connection, which is what bounds a real
    connect-phase hang in production. The read-timeout test above provides
    the genuine, wall-clock-bounded, end-to-end proof for the sibling
    timeout, over the same connection code path."""
    captured_timeout: dict[str, object] = {}
    real_create_connection = network_module.socket.create_connection

    def spy_create_connection(address: tuple[str, int], timeout: object = None) -> object:
        captured_timeout["timeout"] = timeout
        return real_create_connection(address, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr(network_module.socket, "create_connection", spy_create_connection)

    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(port=server.port, connect_timeout=1.75, read_timeout=5.0)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert result.ok is True
        assert captured_timeout["timeout"] == 1.75


def test_redirect_to_allowed_host_is_followed_and_revalidated() -> None:
    def ok_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
        _plain_ok_handler(handler)

    with _LoopbackTLSServer(ok_handler) as target_server:

        def redirecting_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(302)
            handler.send_header("Location", f"https://localhost:{target_server.port}/final")
            handler.end_headers()

        with _LoopbackTLSServer(redirecting_handler) as origin_server:
            fetcher = NetworkFetcher(
                permission_registry=_ok_permission_registry(),
                source_allowed_hosts={"src_test": frozenset({"localhost"})},
                allowed_ports=frozenset({origin_server.port, target_server.port}),
                address_is_blocked=_allow_only_loopback,
                resolve_host=lambda host, p: ["127.0.0.1"],
                ssl_context=_client_ssl_context(),
            )
            result = fetcher.fetch(
                source_id="src_test",
                mode=SourceMode.discovery,
                url=f"https://localhost:{origin_server.port}/start",
            )
            assert result.ok is True
            assert result.status_code == 200


def test_redirect_to_disallowed_host_is_rejected_at_that_hop() -> None:
    """Proof standard (spec §5): a redirect to a disallowed host must be
    rejected at that hop, not silently followed."""

    def redirecting_to_disallowed_host(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", "https://not-allowed.example.com/evil")
        handler.end_headers()

    with _LoopbackTLSServer(redirecting_to_disallowed_host) as server:
        fetcher = _fetcher(port=server.port)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/start",
        )
        assert result.ok is False
        assert result.reason_code == "host_not_allowed"


def test_redirect_to_private_ip_address_is_rejected_at_that_hop() -> None:
    """Proof standard (spec §5): a redirect that resolves to a private IP
    must be rejected at that hop via the same address predicate."""

    def resolver(host: str, port: int) -> list[str]:
        if host == "internal.localdomain.test":
            return ["10.0.0.5"]  # simulated private address
        return ["127.0.0.1"]

    with _LoopbackTLSServer(lambda handler: None) as server:

        def redirecting_to_private_looking_host(
            handler: http.server.BaseHTTPRequestHandler,
        ) -> None:
            handler.send_response(302)
            handler.send_header(
                "Location", f"https://internal.localdomain.test:{server.port}/evil"
            )
            handler.end_headers()

        server._httpd.on_get = redirecting_to_private_looking_host  # type: ignore[attr-defined]

        fetcher = _fetcher(
            port=server.port,
            source_allowed_hosts={
                "src_test": frozenset({"localhost", "internal.localdomain.test"})
            },
            resolve_host=resolver,
        )
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/start",
        )
        assert result.ok is False
        assert result.reason_code == "address_blocked"


def test_too_many_redirects_is_bounded() -> None:
    def infinite_redirect(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", handler.path.replace("/start", "/start"))
        # Re-issue an absolute redirect back to the same server so it loops.
        handler.end_headers()

    with _LoopbackTLSServer(infinite_redirect) as server:
        # Rewrite the handler now that we know our own port (needs the
        # closure to see the live port value).
        def looping_handler(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(302)
            handler.send_header("Location", f"https://localhost:{server.port}/start")
            handler.end_headers()

        server._httpd.on_get = looping_handler  # type: ignore[attr-defined]

        fetcher = _fetcher(port=server.port, max_redirects=3)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/start",
        )
        assert result.ok is False
        assert result.reason_code == "too_many_redirects"


# --- rate limiting and per-source failure isolation ---------------------


def test_rate_limit_is_enforced_per_source_never_globally() -> None:
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = NetworkFetcher(
            permission_registry=PermissionRegistry(
                [
                    SourcePermission(
                        source_id="src_a",
                        approved_mode=SourceMode.discovery,
                        permitted_fields=frozenset({"title"}),
                        retention_policy_id="policy_default",
                        authorization_owner="founder",
                        status=PermissionStatus.approved,
                    ),
                    SourcePermission(
                        source_id="src_b",
                        approved_mode=SourceMode.discovery,
                        permitted_fields=frozenset({"title"}),
                        retention_policy_id="policy_default",
                        authorization_owner="founder",
                        status=PermissionStatus.approved,
                    ),
                ]
            ),
            source_allowed_hosts={
                "src_a": frozenset({"localhost"}),
                "src_b": frozenset({"localhost"}),
            },
            allowed_ports=frozenset({server.port}),
            address_is_blocked=_allow_only_loopback,
            resolve_host=lambda host, p: ["127.0.0.1"],
            ssl_context=_client_ssl_context(),
            rate_limit_max_calls=1,
            rate_limit_window_seconds=60.0,
        )
        url = f"https://localhost:{server.port}/feed"

        first = fetcher.fetch(source_id="src_a", mode=SourceMode.discovery, url=url)
        assert first.ok is True

        second_same_source = fetcher.fetch(source_id="src_a", mode=SourceMode.discovery, url=url)
        assert second_same_source.ok is False
        assert second_same_source.reason_code == "rate_limited"

        # A DIFFERENT source must be unaffected by src_a's rate limit.
        other_source = fetcher.fetch(source_id="src_b", mode=SourceMode.discovery, url=url)
        assert other_source.ok is True


# --- pre-E1 fix: rate-limit consumption ordering and per-hop semantics --


def test_rate_limit_budget_survives_statically_invalid_urls() -> None:
    """Pre-E1 fix: a URL that fails ``_validate_url`` (e.g. a disallowed
    host) must consume NO rate-limit budget, so its rejection reason is
    never masked as ``rate_limited``. Mutation: hoist ``allow()`` back
    above ``_validate_url`` -> the final, valid fetch below would itself
    return ``rate_limited`` instead of succeeding."""
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(
            port=server.port, rate_limit_max_calls=1, rate_limit_window_seconds=60.0
        )

        for _ in range(3):
            invalid_result = fetcher.fetch(
                source_id="src_test",
                mode=SourceMode.discovery,
                url="https://not-allowed.example.com/evil",
            )
            assert invalid_result.ok is False
            assert invalid_result.reason_code == "host_not_allowed"

        valid_result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
        assert valid_result.ok is True


def test_rate_limit_budget_is_consumed_once_per_redirect_hop() -> None:
    """Pre-E1 fix: the rate-limit budget is consumed PER HOP, not once per
    call to ``fetch()``. A 3-hop redirect chain (3 outbound connects)
    against a 2-call budget must be refused mid-chain. Mutation: move
    ``allow()`` back outside the per-hop loop -> the whole chain completes
    on a single budget unit."""
    with _LoopbackTLSServer(_plain_ok_handler) as final_server:

        def redirect_to_final(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(302)
            handler.send_header("Location", f"https://localhost:{final_server.port}/final")
            handler.end_headers()

        with _LoopbackTLSServer(redirect_to_final) as mid_server:

            def redirect_to_mid(handler: http.server.BaseHTTPRequestHandler) -> None:
                handler.send_response(302)
                handler.send_header("Location", f"https://localhost:{mid_server.port}/mid")
                handler.end_headers()

            with _LoopbackTLSServer(redirect_to_mid) as origin_server:
                fetcher = NetworkFetcher(
                    permission_registry=_ok_permission_registry(),
                    source_allowed_hosts={"src_test": frozenset({"localhost"})},
                    allowed_ports=frozenset(
                        {origin_server.port, mid_server.port, final_server.port}
                    ),
                    address_is_blocked=_allow_only_loopback,
                    resolve_host=lambda host, p: ["127.0.0.1"],
                    ssl_context=_client_ssl_context(),
                    rate_limit_max_calls=2,
                    rate_limit_window_seconds=60.0,
                )
                result = fetcher.fetch(
                    source_id="src_test",
                    mode=SourceMode.discovery,
                    url=f"https://localhost:{origin_server.port}/start",
                )
                assert result.ok is False
                assert result.reason_code == "rate_limited"


def test_permission_denied_never_masked_as_rate_limited_when_budget_exhausted() -> None:
    """Pre-E1 fix: ``authorize_retrieval`` runs BEFORE ``allow()`` consumes
    any budget, so a revoked source with a zero-capacity rate-limit window
    still returns ``permission_denied``, with ``authorization_reason``
    populated -- never ``rate_limited``. Mutation: put ``allow()`` before
    ``authorize_retrieval`` -> this returns ``rate_limited`` with
    ``authorization_reason`` left ``None``."""
    source_id = "src_test"
    revoked_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=source_id,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.revoked,
            )
        ]
    )
    fetcher = _fetcher(
        port=443,
        permission_registry=revoked_registry,
        rate_limit_max_calls=0,
        rate_limit_window_seconds=60.0,
    )
    result = fetcher.fetch(
        source_id=source_id, mode=SourceMode.discovery, url="https://localhost/feed"
    )
    assert result.ok is False
    assert result.reason_code == "permission_denied"
    assert result.authorization_reason == AuthorizationReasonCode.status_revoked


def test_fetch_result_and_reason_code_are_importable_public_types() -> None:
    """Public-contract proof for the pre-E1 rename: a caller can import
    ``FetchReasonCode``/``FetchResult`` from ``network`` directly (no
    private name needed) and name what ``fetch()`` returns."""
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = _fetcher(port=server.port)
        result = fetcher.fetch(
            source_id="src_test",
            mode=SourceMode.discovery,
            url=f"https://localhost:{server.port}/feed",
        )
    assert isinstance(result, FetchResult)
    assert isinstance(result.reason_code, FetchReasonCode)


def test_per_source_failure_isolation_one_sources_rejection_does_not_affect_another() -> None:
    with _LoopbackTLSServer(_plain_ok_handler) as server:
        fetcher = NetworkFetcher(
            permission_registry=PermissionRegistry(
                [
                    SourcePermission(
                        source_id="src_broken",
                        approved_mode=SourceMode.discovery,
                        permitted_fields=frozenset({"title"}),
                        retention_policy_id="policy_default",
                        authorization_owner="founder",
                        status=PermissionStatus.approved,
                    ),
                    SourcePermission(
                        source_id="src_healthy",
                        approved_mode=SourceMode.discovery,
                        permitted_fields=frozenset({"title"}),
                        retention_policy_id="policy_default",
                        authorization_owner="founder",
                        status=PermissionStatus.approved,
                    ),
                ]
            ),
            # src_broken has NO allowed hosts at all.
            source_allowed_hosts={"src_healthy": frozenset({"localhost"})},
            allowed_ports=frozenset({server.port}),
            address_is_blocked=_allow_only_loopback,
            resolve_host=lambda host, p: ["127.0.0.1"],
            ssl_context=_client_ssl_context(),
        )
        url = f"https://localhost:{server.port}/feed"

        broken_result = fetcher.fetch(source_id="src_broken", mode=SourceMode.discovery, url=url)
        assert broken_result.ok is False
        assert broken_result.reason_code == "host_not_allowed"

        healthy_result = fetcher.fetch(source_id="src_healthy", mode=SourceMode.discovery, url=url)
        assert healthy_result.ok is True


# --- sanitized errors never contain the endpoint -------------------------


def test_connect_failed_error_is_sanitized_and_never_contains_the_host() -> None:
    """Nothing is listening on this loopback port, so the raw connect
    attempt fails immediately (ConnectionRefusedError) -- the resulting
    sanitized_error must never contain the hostname/URL."""
    fetcher = NetworkFetcher(
        permission_registry=_ok_permission_registry(),
        source_allowed_hosts={"src_test": frozenset({"localhost"})},
        allowed_ports=frozenset({65535}),
        address_is_blocked=_allow_only_loopback,
        resolve_host=lambda host, p: ["127.0.0.1"],
        ssl_context=_client_ssl_context(),
    )
    result = fetcher.fetch(
        source_id="src_test",
        mode=SourceMode.discovery,
        url="https://localhost:65535/feed",
    )
    assert result.ok is False
    assert result.reason_code in ("connect_failed", "fetch_failed")
    assert result.sanitized_error is not None
    assert "localhost" not in result.sanitized_error
    assert "65535" not in result.sanitized_error
