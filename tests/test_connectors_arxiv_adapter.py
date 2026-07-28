"""Tests for content_machine.connectors.arxiv_adapter (Gate E1 pre-network
approval; Fable rulings RC-2/RC-4/RC-5): the first real-adapter SHAPE in this
codebase.

**Fully offline.** Every test in this file injects a fetcher stub (either a
bare ``_ScriptedFetcher`` returning a canned
``content_machine.connectors.network.FetchResult``, or -- for the one test
that needs the REAL live-permission-gate logic -- a real ``NetworkFetcher``
whose ``resolve_host`` is a spy that must never be called). No test in this
file opens a socket, resolves a real hostname, or reads
``data/private/``. No real arXiv hostname or feed URL appears anywhere in
this file -- every URL is ``example.com``-shaped and synthetic, per RC-4.
Every author-shaped name is invented and distinctive so the RC-3
non-retention test can prove it never leaks.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta

import pytest

from content_machine.connectors import arxiv_adapter
from content_machine.connectors.arxiv_adapter import (
    ALLOWED_MIME_TYPES,
    ALLOWED_PORTS,
    MAX_ITEMS,
    MAX_REDIRECTS,
    MAX_REQUESTS_PER_DISCOVER,
    MAX_RESPONSE_BYTES,
    RATE_LIMIT_MAX_CALLS,
    RATE_LIMIT_WINDOW_SECONDS,
    ArxivRssAdapter,
    UnknownPrivateSourceEndpointError,
    build_arxiv_rss_adapter,
)
from content_machine.connectors.bridge import (
    AssessmentProvenance,
    AuthoredAssessment,
    UnreviewedSecurityFlagsError,
    to_source_item,
)
from content_machine.connectors.failures import FailureKind
from content_machine.connectors.models import AuditEventKind, DiscoveryRequest
from content_machine.connectors.network import FetchReasonCode, FetchResult, NetworkFetcher
from content_machine.connectors.permissions import (
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)
from content_machine.connectors.private_config import PrivateSourceConfig, PrivateSourceEndpoint
from content_machine.connectors.registry import (
    PublisherClassification,
    SourceRegistry,
    SourceRegistryEntry,
)
from content_machine.connectors.runner import (
    BatchDiscoveryRequest,
    BatchStatus,
    ConnectorAdapterError,
    run_discovery,
)
from content_machine.connectors.synthetic import fixtures as synthetic_fixtures
from content_machine.intelligence.models import SecurityFlag

_RETRIEVED_AT = datetime(2026, 7, 18, 9, 0, 0, tzinfo=UTC)
_WINDOW_START = date(2026, 7, 11)
_WINDOW_END = date(2026, 7, 18)
_SOURCE_ID = "arxiv_shadow_synthetic"
_FEED_URL = "https://feed.acme-connectors.example.com/rss.xml"


def _request() -> DiscoveryRequest:
    return DiscoveryRequest(
        source_id=_SOURCE_ID, window_start=_WINDOW_START, window_end=_WINDOW_END
    )


class _ScriptedFetcher:
    """Test-only fetcher stub: returns one canned ``FetchResult`` regardless
    of input, and records every call so a test can prove exactly-once
    invocation (no retry) and the exact ``source_id``/``mode``/``url`` an
    adapter passed. Performs no network I/O whatsoever."""

    def __init__(self, result: FetchResult) -> None:
        self._result = result
        self.calls: list[tuple[str, SourceMode, str]] = []

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult:
        self.calls.append((source_id, mode, url))
        return self._result


def _ok_fetch_result(body: bytes, *, content_type: str = "application/rss+xml") -> FetchResult:
    return FetchResult(
        ok=True,
        reason_code=FetchReasonCode.ok,
        source_id=_SOURCE_ID,
        status_code=200,
        content_type=content_type,
        body=body,
    )


def _adapter(fetcher: _ScriptedFetcher) -> ArxivRssAdapter:
    return ArxivRssAdapter(
        _SOURCE_ID,
        feed_url=_FEED_URL,
        fetcher=fetcher,
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )


def _rss_bytes(items_xml: str, *, extra_root_attrs: str = "") -> bytes:
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss{extra_root_attrs}><channel><title>Synthetic Feed</title>{items_xml}"
        "</channel></rss>"
    )
    return xml_text.encode("utf-8")


def _item_xml(
    *,
    title: str = "Synthetic Item",
    link: str = "https://example.com/item-1",
    description: str = "A synthetic item description.",
    pub_date: str = "Wed, 15 Jul 2026 10:00:00 GMT",
) -> str:
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<description>{description}</description><pubDate>{pub_date}</pubDate></item>"
    )


# --- pinned configuration constants (hand-written literals) -----------------


def test_pinned_config_constants_are_exactly_as_approved() -> None:
    """Hand-written literal table (RC-2/RC-4/RC-5) -- must never be derived
    from the constants' own implementation."""
    assert MAX_REQUESTS_PER_DISCOVER == 1
    assert MAX_REDIRECTS == 0
    assert ALLOWED_PORTS == frozenset({443})
    assert ALLOWED_MIME_TYPES == frozenset(
        {"application/rss+xml", "text/xml", "application/xml"}
    )
    assert RATE_LIMIT_MAX_CALLS == 1
    assert RATE_LIMIT_WINDOW_SECONDS == 3.0
    assert MAX_RESPONSE_BYTES == 2_000_000
    assert MAX_ITEMS == 20


def test_fetch_reason_to_failure_kind_mapping_is_complete() -> None:
    """Every FetchReasonCode member except `ok` (handled separately, the
    success path) must appear in the mapping exactly once."""
    mapped = set(arxiv_adapter._FETCH_REASON_TO_FAILURE_KIND)
    assert mapped == set(FetchReasonCode) - {FetchReasonCode.ok}


# --- successful discovery ----------------------------------------------


def test_successful_discover_calls_fetch_exactly_once_with_expected_args() -> None:
    body = _rss_bytes(_item_xml())
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert fetcher.calls == [(_SOURCE_ID, SourceMode.discovery, _FEED_URL)]
    assert len(outcome.results) == 1
    assert outcome.results[0].source_id == _SOURCE_ID
    assert outcome.results[0].canonical_reference == "https://example.com/item-1"
    assert outcome.results[0].content_type == "application/rss+xml"
    assert outcome.results[0].summary_provenance.value == "source_supplied"
    assert outcome.results[0].publication_date == date(2026, 7, 15)
    assert outcome.dropped_count == 0
    assert outcome.truncated is False


def test_no_retry_on_failed_fetch() -> None:
    fetcher = _ScriptedFetcher(
        FetchResult(
            ok=False, reason_code=FetchReasonCode.fetch_failed, source_id=_SOURCE_ID
        )
    )
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError):
        adapter.discover(_request())

    assert len(fetcher.calls) == 1, "discover() must call fetch() exactly once, never retry"


def test_item_with_no_link_is_dropped_not_a_whole_source_failure() -> None:
    body = _rss_bytes(_item_xml() + _item_xml(link=""))
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == 1
    assert outcome.dropped_count == 1
    assert outcome.truncated is False


def test_publication_date_parses_fail_soft_to_none_on_bad_date() -> None:
    body = _rss_bytes(_item_xml(pub_date="not a real date"))
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == 1
    assert outcome.results[0].publication_date is None


def test_missing_pub_date_element_parses_fail_soft_to_none() -> None:
    items_xml = (
        "<item><title>No Date</title><link>https://example.com/no-date</link>"
        "<description>desc</description></item>"
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(_rss_bytes(items_xml)))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == 1
    assert outcome.results[0].publication_date is None


# --- item cap (non-silent truncation) ------------------------------------


def test_item_cap_at_twenty_with_non_silent_truncation() -> None:
    items_xml = "".join(
        _item_xml(title=f"Item {i}", link=f"https://example.com/item-{i}") for i in range(25)
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(_rss_bytes(items_xml)))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == MAX_ITEMS
    assert outcome.truncated is True
    assert outcome.dropped_count == 5
    # Deterministic: the first 20 in feed order, never resorted.
    assert [r.canonical_reference for r in outcome.results] == [
        f"https://example.com/item-{i}" for i in range(20)
    ]


# --- UTF-8 malformed-encoding handling ------------------------------------


def test_utf8_decode_errors_replace_surfaces_malformed_encoding_flag() -> None:
    prefix = (
        b'<?xml version="1.0"?><rss><channel><item><title>T</title>'
        b"<link>https://example.com/bad-encoding</link><description>Hello "
    )
    invalid_utf8_bytes = b"\xff\xfe"  # not valid UTF-8 on its own
    suffix = b"world</description></item></channel></rss>"
    body = prefix + invalid_utf8_bytes + suffix

    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == 1
    assert SecurityFlag.malformed_encoding in outcome.results[0].security_flags
    assert "�" not in outcome.results[0].summary_normalized, (
        "the replacement character itself must be stripped by sanitize_text, "
        "only the flag survives"
    )


# --- RC-3: author fields are never even read, let alone retained -----------


def test_rc3_author_fields_never_retained() -> None:
    distinctive_name = "Zzyzx Q. Fakeauthor-Synthetic-9182"
    items_xml = (
        '<item xmlns:dc="http://purl.org/dc/elements/1.1/">'
        "<title>RC-3 Probe Item</title>"
        "<link>https://example.com/rc3-item</link>"
        "<description>a synthetic item with an author byline elsewhere</description>"
        f"<dc:creator>{distinctive_name}</dc:creator>"
        f"<managingEditor>editor@example.com ({distinctive_name})</managingEditor>"
        f"<dc:rights>Copyright {distinctive_name}</dc:rights>"
        "</item>"
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(_rss_bytes(items_xml)))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())

    assert len(outcome.results) == 1
    result = outcome.results[0]
    serialized_result = result.model_dump_json()
    assert distinctive_name not in serialized_result

    for event in adapter.audit_events:
        assert distinctive_name not in event.model_dump_json()


# --- audit trail (requirement 8) -----------------------------------------


def test_audit_events_carry_reason_code_status_byte_count_and_items() -> None:
    body = _rss_bytes(_item_xml() + _item_xml(link=""))  # 1 kept, 1 dropped
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    assert adapter.audit_events == ()
    adapter.discover(_request())

    assert len(adapter.audit_events) == 1
    event = adapter.audit_events[0]
    assert event.source_id == _SOURCE_ID
    assert event.event_kind == AuditEventKind.retention
    assert event.reason_code == "ok"
    assert "status_code=200" in event.detail
    assert f"byte_count={len(body)}" in event.detail
    assert "items_parsed=1" in event.detail
    assert "dropped_count=1" in event.detail


def test_audit_event_on_failed_fetch_is_kind_failure() -> None:
    fetcher = _ScriptedFetcher(
        FetchResult(
            ok=False, reason_code=FetchReasonCode.too_many_redirects, source_id=_SOURCE_ID
        )
    )
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError):
        adapter.discover(_request())

    assert len(adapter.audit_events) == 1
    event = adapter.audit_events[0]
    assert event.event_kind == AuditEventKind.failure
    assert event.reason_code == "too_many_redirects"


# --- network.py-shaped stub failure cases (requirement 9) -----------------


def test_301_stub_reason_too_many_redirects_raises_failure() -> None:
    fetcher = _ScriptedFetcher(
        FetchResult(
            ok=False,
            reason_code=FetchReasonCode.too_many_redirects,
            source_id=_SOURCE_ID,
            status_code=301,
        )
    )
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.unavailable
    assert adapter.audit_events[0].reason_code == "too_many_redirects"


def test_oversized_stub_response_too_large_raises_failure() -> None:
    fetcher = _ScriptedFetcher(
        FetchResult(
            ok=False,
            reason_code=FetchReasonCode.response_too_large,
            source_id=_SOURCE_ID,
            status_code=200,
            truncated=True,
        )
    )
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.invalid_response
    assert adapter.audit_events[0].reason_code == "response_too_large"


def test_wrong_mime_stub_mime_not_allowed_raises_failure() -> None:
    fetcher = _ScriptedFetcher(
        FetchResult(
            ok=False,
            reason_code=FetchReasonCode.mime_not_allowed,
            source_id=_SOURCE_ID,
            status_code=200,
            content_type="text/html",
        )
    )
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.unsupported_content
    assert adapter.audit_events[0].reason_code == "mime_not_allowed"


# --- the live permission gate: expired permission, resolver never called ---


def test_expired_permission_denied_resolver_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RC-2/RC-5's kill switch: `expires_at` set to the end of the 7-day
    shadow window is enforced by `NetworkFetcher.fetch()`'s LIVE
    `authorize_retrieval` call -- and that check runs before any DNS
    resolution, so a resolver spy must never be invoked. No socket is ever
    reached: the denial happens before `resolve_host` is even called."""
    from content_machine.connectors import permissions as permissions_module

    expires_at = _WINDOW_START + timedelta(days=7)
    registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=_SOURCE_ID,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset(
                    {
                        "title",
                        "canonical_reference",
                        "content_type",
                        "publication_date",
                        "summary_normalized",
                    }
                ),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
                expires_at=expires_at,
            )
        ]
    )
    # "Today" is well past the shadow window's expiry.
    monkeypatch.setattr(permissions_module, "_today_utc", lambda: expires_at + timedelta(days=1))

    resolve_calls: list[tuple[str, int]] = []

    def spy_resolve_host(host: str, port: int) -> list[str]:
        resolve_calls.append((host, port))
        return ["203.0.113.10"]  # TEST-NET-3 (RFC 5737); never dialed either way

    fetcher = NetworkFetcher(
        permission_registry=registry,
        source_allowed_hosts={_SOURCE_ID: frozenset({"feed.acme-connectors.example.com"})},
        allowed_ports=ALLOWED_PORTS,
        allowed_mime_types=ALLOWED_MIME_TYPES,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_redirects=MAX_REDIRECTS,
        rate_limit_max_calls=RATE_LIMIT_MAX_CALLS,
        rate_limit_window_seconds=RATE_LIMIT_WINDOW_SECONDS,
        resolve_host=spy_resolve_host,
    )
    adapter = ArxivRssAdapter(
        _SOURCE_ID,
        feed_url=_FEED_URL,
        fetcher=fetcher,
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())

    assert excinfo.value.kind == FailureKind.permission_revoked
    assert resolve_calls == [], "DNS resolution must never be reached once permission is denied"
    assert adapter.audit_events[0].reason_code == "permission_denied"


def test_shadow_window_permission_is_allowed_before_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complements the expired-permission test above: as of a date ON OR
    BEFORE `expires_at`, the same permission is allowed. Checked directly
    against `PermissionRegistry.authorize_retrieval` (no fetcher/adapter
    involved), so this never opens a socket either."""
    from content_machine.connectors import permissions as permissions_module

    expires_at = _WINDOW_START + timedelta(days=7)
    registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=_SOURCE_ID,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset({"title"}),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
                expires_at=expires_at,
            )
        ]
    )
    monkeypatch.setattr(permissions_module, "_today_utc", lambda: expires_at)
    decision = registry.authorize_retrieval(_SOURCE_ID, SourceMode.discovery)
    assert decision.allowed is True


def test_source_registered_with_matching_mode_and_seven_day_expiry() -> None:
    """Requirement 1: the source must be present in SourceRegistry and
    PermissionRegistry with matching mode, and expires_at set to the end of
    the 7-day shadow window."""
    entry = SourceRegistryEntry(
        source_id=_SOURCE_ID,
        source_group="synthetic",
        publisher_id="arxiv-shadow-synthetic",
        source_category="preprint_feed",
        source_type="feed",
        publisher_classification=PublisherClassification.independent,
        endpoint_label="synthetic shadow-window feed",
    )
    source_registry = SourceRegistry([entry])
    expires_at = _WINDOW_START + timedelta(days=7)
    permission_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=_SOURCE_ID,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset(
                    {
                        "title",
                        "canonical_reference",
                        "content_type",
                        "publication_date",
                        "summary_normalized",
                    }
                ),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
                expires_at=expires_at,
            )
        ]
    )

    assert _SOURCE_ID in source_registry
    decision = permission_registry.authorize(_SOURCE_ID, SourceMode.discovery)
    assert decision.allowed is True
    assert permission_registry.get(_SOURCE_ID).expires_at == _WINDOW_START + timedelta(days=7)


# --- source_id mismatch -> whole-source failure (requirement 9) ------------


class _IdentitySpoofingProxy:
    """Test-only wrapper: reports a DIFFERENT `source_id` to `run_discovery`
    than the one baked into the real adapter's own results -- proving
    `run_discovery`'s existing identity-binding check (Gate D round-1, B1)
    still fires against THIS adapter's real output, not only against the
    synthetic adapters' own tests."""

    def __init__(self, real_adapter: ArxivRssAdapter, claimed_source_id: str) -> None:
        self.source_id = claimed_source_id
        self._real_adapter = real_adapter

    def discover(self, request: DiscoveryRequest) -> object:
        return self._real_adapter.discover(request)


def test_source_id_mismatch_fails_the_whole_source() -> None:
    real_source_id = "arxiv_shadow_real"
    claimed_source_id = "arxiv_shadow_other"
    body = _rss_bytes(_item_xml())
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    real_adapter = ArxivRssAdapter(
        real_source_id,
        feed_url=_FEED_URL,
        fetcher=fetcher,
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )
    proxy = _IdentitySpoofingProxy(real_adapter, claimed_source_id)

    def _entry(source_id: str) -> SourceRegistryEntry:
        return SourceRegistryEntry(
            source_id=source_id,
            source_group="synthetic",
            publisher_id="vendor-synthetic",
            source_category="preprint_feed",
            source_type="feed",
            publisher_classification=PublisherClassification.independent,
            endpoint_label="synthetic feed",
        )

    def _permission(source_id: str) -> SourcePermission:
        return SourcePermission(
            source_id=source_id,
            approved_mode=SourceMode.discovery,
            permitted_fields=frozenset(
                {
                    "title",
                    "canonical_reference",
                    "content_type",
                    "publication_date",
                    "summary_normalized",
                }
            ),
            retention_policy_id="policy_default",
            authorization_owner="founder",
            status=PermissionStatus.approved,
        )

    source_registry = SourceRegistry([_entry(claimed_source_id)])
    permission_registry = PermissionRegistry([_permission(claimed_source_id)])
    batch_request = BatchDiscoveryRequest(window_start=_WINDOW_START, window_end=_WINDOW_END)

    result = run_discovery(
        [proxy],
        permission_registry,
        source_registry,
        batch_request,
        occurred_at=_RETRIEVED_AT,
    )

    assert result.results == ()
    assert len(result.failures) == 1
    assert result.failures[0].kind == FailureKind.source_id_mismatch
    assert result.status != BatchStatus.all_succeeded


# --- hostile abstract: RC-1-R3 corpus end to end, blocked at the bridge -----


def test_hostile_abstract_flagged_and_blocked_at_the_bridge() -> None:
    items_xml = _item_xml(
        title=synthetic_fixtures.HOSTILE_PROMPT_INJECTION,
        link=synthetic_fixtures.HOSTILE_PROMPT_INJECTION_REFERENCE,
        description=synthetic_fixtures.HOSTILE_PROMPT_INJECTION,
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(_rss_bytes(items_xml)))
    adapter = _adapter(fetcher)

    outcome = adapter.discover(_request())
    assert len(outcome.results) == 1
    result = outcome.results[0]
    assert SecurityFlag.instruction_shaped_text in result.security_flags

    registry_entry = SourceRegistryEntry(
        source_id=_SOURCE_ID,
        source_group="synthetic",
        publisher_id="vendor-synthetic",
        source_category="preprint_feed",
        source_type="feed",
        publisher_classification=PublisherClassification.independent,
        endpoint_label="synthetic feed",
    )
    permission_registry = PermissionRegistry(
        [
            SourcePermission(
                source_id=_SOURCE_ID,
                approved_mode=SourceMode.discovery,
                permitted_fields=frozenset(
                    {
                        "title",
                        "canonical_reference",
                        "content_type",
                        "publication_date",
                        "summary_normalized",
                    }
                ),
                retention_policy_id="policy_default",
                authorization_owner="founder",
                status=PermissionStatus.approved,
            )
        ]
    )
    assessment = AuthoredAssessment(
        evidence_type="announcement",
        change_class="material_change",
        change_class_rationale="n/a",
        action_required="none",
        experiment_affordance="not_testable",
        contains_benefit_or_performance_claim=False,
        claim_directly_verifiable_in_artifact=False,
        topic_tags=("agents",),
        subject_entity_ids=("vendor-synthetic",),
        provenance=AssessmentProvenance.human_authored,
    )

    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result,
            assessment,
            registry_entry,
            detection_date=_WINDOW_END,
            permission_registry=permission_registry,
        )


# --- hostile XML: DTD/entity expansion must fail closed --------------------


_BILLION_LAUGHS_BODY = (
    b'<?xml version="1.0"?>\n'
    b"<!DOCTYPE lolz [\n"
    b' <!ENTITY lol "lol">\n'
    b' <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
    b' <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">\n'
    b' <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
    b' <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">\n'
    b' <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">\n'
    b' <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">\n'
    b' <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">\n'
    b' <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">\n'
    b' <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">\n'
    b"]>\n"
    b"<rss><channel><item><title>&lol9;</title>"
    b"<link>https://example.com/bomb</link></item></channel></rss>\n"
)


def test_billion_laughs_feed_fails_closed_in_bounded_time() -> None:
    """Pinned, measured behavior (not assumed): CPython's bundled expat
    enforces an entity-amplification ceiling and raises
    xml.etree.ElementTree.ParseError well under a second -- verified
    directly against this project's Python interpreter before this test was
    written. This test proves BOTH that it fails closed (a
    ConnectorAdapterError, not a parsed/poisoned result) AND that it does so
    within a small, generous wall-clock bound (never hangs)."""
    fetcher = _ScriptedFetcher(_ok_fetch_result(_BILLION_LAUGHS_BODY))
    adapter = _adapter(fetcher)

    started = time.monotonic()
    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    elapsed = time.monotonic() - started

    assert excinfo.value.kind == FailureKind.invalid_response
    assert elapsed < 5.0, "billion-laughs parse must fail fast, never hang"


def test_undefined_entity_fails_closed() -> None:
    body = (
        b"<rss><channel><item><title>&undefined_synthetic_entity;</title>"
        b"<link>https://example.com/undefined-entity</link></item></channel></rss>"
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.invalid_response


def test_malformed_xml_fails_closed() -> None:
    body = b"<rss><channel><item><title>unterminated"
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.invalid_response


def test_external_entity_reference_is_never_resolved() -> None:
    """XXE probe: ElementTree does not resolve external (SYSTEM) entities at
    all -- this must fail closed exactly like an undefined entity, never
    silently succeed with file content substituted in."""
    body = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n'
        b"<rss><channel><item><title>&xxe;</title>"
        b"<link>https://example.com/xxe</link></item></channel></rss>\n"
    )
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.invalid_response


def test_wrong_root_element_fails_closed() -> None:
    body = b"<html><body>not a feed</body></html>"
    fetcher = _ScriptedFetcher(_ok_fetch_result(body))
    adapter = _adapter(fetcher)

    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_request())
    assert excinfo.value.kind == FailureKind.invalid_response


# --- ET.fromstring behavior, pinned directly (belt-and-suspenders) ---------


def test_pinned_stdlib_parser_behavior_directly() -> None:
    """Direct, adapter-independent pin of the exact stdlib behavior this
    module's docstring describes and relies on -- if a future CPython
    regresses this protection, this test (not just the slower
    adapter-level test above) fails first and says why."""
    with pytest.raises(ET.ParseError):
        ET.fromstring(_BILLION_LAUGHS_BODY)


# --- the feed body is never persisted anywhere -----------------------------


def test_feed_body_never_persisted_no_body_field_exists() -> None:
    from content_machine.connectors.models import DiscoveryResult
    from content_machine.connectors.runner import AdapterDiscoveryOutcome

    assert "body" not in DiscoveryResult.model_fields
    assert "body" not in AdapterDiscoveryOutcome.model_fields


# --- private-config wiring (requirement 2), never calling fetch()/discover() -


def test_build_arxiv_rss_adapter_wires_fetcher_from_private_config() -> None:
    config = PrivateSourceConfig(
        endpoints=[
            PrivateSourceEndpoint(
                source_id=_SOURCE_ID,
                hostname="feed.acme-connectors.example.com",
                endpoint=_FEED_URL,
            )
        ]
    )
    permission_registry = PermissionRegistry([])

    adapter = build_arxiv_rss_adapter(
        _SOURCE_ID,
        config,
        permission_registry,
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )

    assert isinstance(adapter, ArxivRssAdapter)
    assert adapter._feed_url == _FEED_URL  # noqa: SLF001 - wiring proof, never called
    fetcher = adapter._fetcher  # noqa: SLF001 - wiring proof, never called
    assert isinstance(fetcher, NetworkFetcher)
    assert fetcher._source_allowed_hosts == {  # noqa: SLF001
        _SOURCE_ID: frozenset({"feed.acme-connectors.example.com"})
    }
    assert fetcher._allowed_ports == ALLOWED_PORTS  # noqa: SLF001
    assert fetcher._allowed_mime_types == ALLOWED_MIME_TYPES  # noqa: SLF001
    assert fetcher._max_response_bytes == MAX_RESPONSE_BYTES  # noqa: SLF001
    assert fetcher._max_redirects == MAX_REDIRECTS  # noqa: SLF001


def test_build_arxiv_rss_adapter_unknown_source_id_fails_closed() -> None:
    config = PrivateSourceConfig(
        endpoints=[
            PrivateSourceEndpoint(
                source_id="some-other-source",
                hostname="feed.acme-connectors.example.com",
                endpoint=_FEED_URL,
            )
        ]
    )
    with pytest.raises(UnknownPrivateSourceEndpointError):
        build_arxiv_rss_adapter(
            _SOURCE_ID,
            config,
            PermissionRegistry([]),
            source_group="synthetic",
            discovery_run_id="synthetic-run-2026-w29",
            retrieved_at=_RETRIEVED_AT,
        )
