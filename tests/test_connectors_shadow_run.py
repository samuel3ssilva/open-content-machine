"""Tests for content_machine.connectors.shadow_run (Fable RC-6: operational
conditions C2, C3, C4, plus the mechanical kill criteria).

**Fully offline.** Every test in this file injects a fetcher stub returning a
canned ``content_machine.connectors.network.FetchResult`` -- no test opens a
socket, resolves a real hostname, or reads ``data/private/``. No real arXiv
hostname or feed URL appears anywhere in this file -- every URL is
``example.com``-shaped and synthetic, matching
``tests/test_connectors_arxiv_adapter.py``'s own convention.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from content_machine.config.settings import Settings
from content_machine.connectors import shadow_run as shadow_run_module
from content_machine.connectors.arxiv_adapter import ArxivRssAdapter
from content_machine.connectors.bridge import BLOCKING_SECURITY_FLAGS
from content_machine.connectors.models import (
    AuditEventKind,
    ConnectorAuditEvent,
    DiscoveryRequest,
)
from content_machine.connectors.network import FetchReasonCode, FetchResult
from content_machine.connectors.permissions import (
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)
from content_machine.connectors.registry import (
    PublisherClassification,
    SourceRegistry,
    SourceRegistryEntry,
)
from content_machine.connectors.runner import (
    AdapterDiscoveryOutcome,
    BatchDiscoveryRequest,
    BatchDiscoveryResult,
    BatchStatus,
    SourceCoverage,
    SourceCoverageReport,
)
from content_machine.connectors.shadow_run import (
    SHADOW_RUN_HISTORY_FILENAME,
    KillReason,
    MissingShadowRunOutputDirError,
    RequestCountingFetcher,
    ShadowRunEvaluation,
    ShadowRunRecord,
    build_shadow_run_arxiv_adapter,
    read_shadow_run_history,
    render_shadow_run_report,
    resolve_shadow_run_output_dir,
    run_shadow_discovery,
)
from content_machine.connectors.synthetic import fixtures as synthetic_fixtures
from content_machine.intelligence.models import SecurityFlag

_SOURCE_ID = "arxiv_shadow_synthetic"
_FEED_URL = "https://feed.acme-connectors.example.com/rss.xml"
_RETRIEVED_AT = datetime(2026, 7, 18, 9, 0, 0, tzinfo=UTC)
_WINDOW_START = date(2026, 7, 11)
_WINDOW_END = date(2026, 7, 18)


# --- shared fixture helpers (mirrors test_connectors_arxiv_adapter.py) -----


class _ScriptedFetcher:
    """Test-only fetcher stub: returns one canned ``FetchResult`` regardless
    of input. Performs no network I/O whatsoever."""

    def __init__(self, result: FetchResult) -> None:
        self._result = result

    def fetch(self, *, source_id: str, mode: SourceMode, url: str) -> FetchResult:
        return self._result


def _ok_fetch_result(
    body: bytes, *, content_type: str = "application/rss+xml", status_code: int = 200
) -> FetchResult:
    return FetchResult(
        ok=True,
        reason_code=FetchReasonCode.ok,
        source_id=_SOURCE_ID,
        status_code=status_code,
        content_type=content_type,
        body=body,
    )


def _rss_bytes(items_xml: str) -> bytes:
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<rss><channel><title>Synthetic Feed</title>{items_xml}</channel></rss>"
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


def _adapter(fetcher: object) -> ArxivRssAdapter:
    return ArxivRssAdapter(
        _SOURCE_ID,
        feed_url=_FEED_URL,
        fetcher=fetcher,  # type: ignore[arg-type]
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )


def _registry_entry(source_id: str = _SOURCE_ID) -> SourceRegistryEntry:
    return SourceRegistryEntry(
        source_id=source_id,
        source_group="synthetic",
        publisher_id="vendor-synthetic",
        source_category="preprint_feed",
        source_type="feed",
        publisher_classification=PublisherClassification.independent,
        endpoint_label="synthetic shadow-window feed",
    )


def _permission(source_id: str = _SOURCE_ID, **overrides: object) -> SourcePermission:
    fields: dict[str, object] = {
        "source_id": source_id,
        "approved_mode": SourceMode.discovery,
        "permitted_fields": frozenset(
            {
                "title",
                "canonical_reference",
                "content_type",
                "publication_date",
                "summary_normalized",
            }
        ),
        "retention_policy_id": "policy_default",
        "authorization_owner": "founder",
        "status": PermissionStatus.approved,
    }
    fields.update(overrides)
    return SourcePermission(**fields)  # type: ignore[arg-type]


def _batch_request() -> BatchDiscoveryRequest:
    return BatchDiscoveryRequest(window_start=_WINDOW_START, window_end=_WINDOW_END)


def _wired(fetch_result: FetchResult) -> tuple[ArxivRssAdapter, RequestCountingFetcher]:
    """A source_id-registered, permission-approved ArxivRssAdapter wired
    with a RequestCountingFetcher wrapping a stub that returns
    `fetch_result` unconditionally."""
    counting_fetcher = RequestCountingFetcher(_ScriptedFetcher(fetch_result))
    adapter = _adapter(counting_fetcher)
    return adapter, counting_fetcher


def _run(
    tmp_path: Path,
    fetch_result: FetchResult,
    *,
    source_registry: SourceRegistry | None = None,
    permission_registry: PermissionRegistry | None = None,
    occurred_at: datetime = _RETRIEVED_AT,
) -> tuple[shadow_run_module.ShadowRunReport, RequestCountingFetcher]:
    adapter, fetcher = _wired(fetch_result)
    registry = (
        source_registry if source_registry is not None else SourceRegistry([_registry_entry()])
    )
    permissions = (
        permission_registry
        if permission_registry is not None
        else PermissionRegistry([_permission()])
    )
    report = run_shadow_discovery(
        adapter,
        fetcher,
        permission_registry=permissions,
        source_registry=registry,
        request=_batch_request(),
        occurred_at=occurred_at,
        output_dir=tmp_path,
    )
    return report, fetcher


# --- C2: persistence survives before a second discover() could overwrite ---


def test_c2_two_runs_persist_two_distinct_records(tmp_path: Path) -> None:
    body_1 = _rss_bytes(_item_xml(link="https://example.com/run-one"))
    body_2 = _rss_bytes(_item_xml(link="https://example.com/run-two", title="Second Run Item"))

    report_1, _ = _run(tmp_path, _ok_fetch_result(body_1))
    report_2, _ = _run(tmp_path, _ok_fetch_result(body_2))

    history_path = tmp_path / SHADOW_RUN_HISTORY_FILENAME
    history = read_shadow_run_history(history_path)

    assert len(history) == 2
    # Each record's adapter_audit_events reflects ITS OWN run's byte count --
    # proving the first run's evidence was durably persisted before the
    # second discover() call (which would have overwritten
    # ArxivRssAdapter.audit_events had the harness not captured it first).
    assert f"byte_count={len(body_1)}" in history[0].adapter_audit_events[0].detail
    assert f"byte_count={len(body_2)}" in history[1].adapter_audit_events[0].detail
    assert history[0].adapter_audit_events != history[1].adapter_audit_events
    assert report_1.total_runs_persisted == 1
    assert report_2.total_runs_persisted == 2


def test_c2_persisted_record_matches_returned_report(tmp_path: Path) -> None:
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body))

    history = read_shadow_run_history(tmp_path / SHADOW_RUN_HISTORY_FILENAME)
    assert len(history) == 1
    assert history[0] == report.record
    assert isinstance(history[0].batch_result, BatchDiscoveryResult)


# --- C3: a 403 with an allowed content type is caught, not awaited ---------


def test_c3_403_with_allowed_mime_is_caught_as_kill_trigger(tmp_path: Path) -> None:
    """The whole point of C3: network.py has no 4xx/5xx branch, so a stub
    (standing in for a live server) returning 403 with an allowed XML
    content type produces FetchResult.ok=True -- an ordinary "successful"
    fetch as far as network.py/the adapter are concerned. The harness must
    still catch it."""
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body, status_code=403))

    evaluation = report.record.evaluation
    assert evaluation.status_code_observed == 403
    assert KillReason.blocked_status_code in evaluation.kill_reasons
    assert evaluation.halted is True
    # And the run really did look "successful" at the batch level -- this is
    # exactly the poisoned-success case C3 exists to catch.
    assert report.record.batch_result.status == BatchStatus.all_succeeded


@pytest.mark.parametrize("status_code", [429, 451])
def test_c3_other_blocked_status_codes_are_caught(tmp_path: Path, status_code: int) -> None:
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body, status_code=status_code))
    assert KillReason.blocked_status_code in report.record.evaluation.kill_reasons


def test_c3_ordinary_200_is_not_a_kill_trigger(tmp_path: Path) -> None:
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body, status_code=200))
    assert report.record.evaluation.halted is False
    assert report.record.evaluation.kill_reasons == ()


# --- reason-code kill triggers ----------------------------------------------

_SECURITY_REASON_CODES = [
    FetchReasonCode.address_blocked,
    FetchReasonCode.ip_literal_host,
    FetchReasonCode.host_not_allowed,
    FetchReasonCode.scheme_not_https,
    FetchReasonCode.credential_in_url,
    FetchReasonCode.port_not_allowed,
    FetchReasonCode.too_many_redirects,
    FetchReasonCode.response_too_large,
    FetchReasonCode.mime_not_allowed,
]


@pytest.mark.parametrize("reason_code", _SECURITY_REASON_CODES)
def test_each_security_reason_code_halts(tmp_path: Path, reason_code: FetchReasonCode) -> None:
    fetch_result = FetchResult(ok=False, reason_code=reason_code, source_id=_SOURCE_ID)
    report, _ = _run(tmp_path, fetch_result)

    evaluation = report.record.evaluation
    assert evaluation.halted is True
    assert KillReason.security_reason_code in evaluation.kill_reasons


@pytest.mark.parametrize(
    "reason_code",
    [
        FetchReasonCode.rate_limited,
        FetchReasonCode.connect_failed,
        FetchReasonCode.connect_timeout,
        FetchReasonCode.read_timeout,
        FetchReasonCode.dns_resolution_failed,
        FetchReasonCode.invalid_response,
        FetchReasonCode.fetch_failed,
        FetchReasonCode.invalid_url,
    ],
)
def test_ordinary_transient_reason_codes_do_not_halt(
    tmp_path: Path, reason_code: FetchReasonCode
) -> None:
    """Ordinary/transient failure conditions are NOT RC-6 kill triggers --
    only the nine fetch-boundary security violations and the two
    separately-checked conditions (blocked status code, permission
    denied/expired) are."""
    fetch_result = FetchResult(ok=False, reason_code=reason_code, source_id=_SOURCE_ID)
    report, _ = _run(tmp_path, fetch_result)
    assert report.record.evaluation.halted is False


# --- permission denied / expired: the automatic kill switch -----------------


def test_retrieval_time_permission_denied_halts(tmp_path: Path) -> None:
    fetch_result = FetchResult(
        ok=False, reason_code=FetchReasonCode.permission_denied, source_id=_SOURCE_ID
    )
    report, _ = _run(tmp_path, fetch_result)

    evaluation = report.record.evaluation
    assert evaluation.halted is True
    assert KillReason.permission_denied_or_expired in evaluation.kill_reasons


def test_coverage_level_skip_not_approved_halts(tmp_path: Path) -> None:
    """The source is never even authorized to discover() at all (e.g. an
    unregistered/suspended/revoked permission) -- run_discovery skips it
    before ever calling adapter.discover()."""
    report, fetcher = _run(
        tmp_path,
        _ok_fetch_result(_rss_bytes(_item_xml())),
        source_registry=SourceRegistry([]),  # source_id not registered at all
    )
    evaluation = report.record.evaluation
    assert evaluation.halted is True
    assert KillReason.permission_denied_or_expired in evaluation.kill_reasons
    assert fetcher.call_count == 0
    assert evaluation.request_count == 0


# --- blocking security flag pauses for Founder review -----------------------


def test_blocking_security_flag_pauses_for_founder_review(tmp_path: Path) -> None:
    items_xml = _item_xml(
        title=synthetic_fixtures.HOSTILE_PROMPT_INJECTION,
        link=synthetic_fixtures.HOSTILE_PROMPT_INJECTION_REFERENCE,
        description=synthetic_fixtures.HOSTILE_PROMPT_INJECTION,
    )
    report, _ = _run(tmp_path, _ok_fetch_result(_rss_bytes(items_xml)))

    assert len(report.record.batch_result.results) == 1
    retained = report.record.batch_result.results[0]
    assert set(retained.security_flags) & BLOCKING_SECURITY_FLAGS

    evaluation = report.record.evaluation
    assert evaluation.halted is True
    assert evaluation.founder_review_required is True
    assert KillReason.blocking_security_flag in evaluation.kill_reasons


def test_clean_item_does_not_require_founder_review(tmp_path: Path) -> None:
    report, _ = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))
    assert report.record.evaluation.founder_review_required is False


# --- more than one outbound request -----------------------------------------


class _DoubleFetchAdapter:
    """Test-only adapter double: calls `fetcher.fetch()` TWICE inside one
    `discover()` -- simulating a misbehaving/compromised adapter that does
    not honor a one-request-per-discover ceiling, so the harness's OWN,
    independent request counting (never trusting the adapter) is what
    catches it."""

    def __init__(self, source_id: str, fetcher: RequestCountingFetcher) -> None:
        self.source_id = source_id
        self._fetcher = fetcher
        self._audit_events: tuple[ConnectorAuditEvent, ...] = ()

    @property
    def audit_events(self) -> tuple[ConnectorAuditEvent, ...]:
        return self._audit_events

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        first = self._fetcher.fetch(
            source_id=self.source_id, mode=SourceMode.discovery, url=_FEED_URL
        )
        self._fetcher.fetch(source_id=self.source_id, mode=SourceMode.discovery, url=_FEED_URL)
        self._audit_events = (
            ConnectorAuditEvent(
                source_id=self.source_id,
                event_kind=AuditEventKind.retention,
                reason_code=first.reason_code.value,
                occurred_at=_RETRIEVED_AT,
                detail=f"status_code={first.status_code} byte_count={len(first.body)} "
                "items_parsed=0 dropped_count=0",
            ),
        )
        return AdapterDiscoveryOutcome(results=(), dropped_count=0, truncated=False)


def test_more_than_one_outbound_request_halts(tmp_path: Path) -> None:
    fetcher = RequestCountingFetcher(_ScriptedFetcher(_ok_fetch_result(_rss_bytes(_item_xml()))))
    adapter = _DoubleFetchAdapter(_SOURCE_ID, fetcher)

    report = run_shadow_discovery(
        adapter,
        fetcher,
        permission_registry=PermissionRegistry([_permission()]),
        source_registry=SourceRegistry([_registry_entry()]),
        request=_batch_request(),
        occurred_at=_RETRIEVED_AT,
        output_dir=tmp_path,
    )

    assert fetcher.call_count == 2
    evaluation = report.record.evaluation
    assert evaluation.request_count == 2
    assert evaluation.halted is True
    assert KillReason.multiple_outbound_requests in evaluation.kill_reasons


def test_exactly_one_outbound_request_is_not_a_trigger(tmp_path: Path) -> None:
    report, fetcher = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))
    assert fetcher.call_count == 1
    assert KillReason.multiple_outbound_requests not in report.record.evaluation.kill_reasons


# --- wiring-integrity: a mismatched/unused request counter must be
# detected, never assumed correct --------------------------------------------


def test_correctly_wired_counter_is_not_flagged(tmp_path: Path) -> None:
    """The ordinary, correctly-wired case: `fetcher` IS the instance the
    adapter's own constructor received. No wiring-integrity kill reason."""
    report, fetcher = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))
    assert fetcher.call_count == 1
    assert KillReason.request_counter_not_wired not in report.record.evaluation.kill_reasons
    assert report.record.evaluation.halted is False


def test_mismatched_fetcher_wrapper_is_detected(tmp_path: Path) -> None:
    """The gap the coordinator flagged: the adapter is wired with ONE
    RequestCountingFetcher (which really does see the fetch call), but a
    DIFFERENT, fresh RequestCountingFetcher is passed to
    run_shadow_discovery -- exactly the mistake that would otherwise make
    `fetcher.call_count == 0` and silently defeat the
    "more than one outbound request" check regardless of what the adapter
    actually did.
    """
    body = _rss_bytes(_item_xml())
    used_fetcher = RequestCountingFetcher(_ScriptedFetcher(_ok_fetch_result(body)))
    adapter = _adapter(used_fetcher)
    assert used_fetcher.call_count == 0  # not yet invoked

    unused_fetcher = RequestCountingFetcher(_ScriptedFetcher(_ok_fetch_result(body)))

    report = run_shadow_discovery(
        adapter,
        unused_fetcher,  # deliberately the WRONG wrapper
        permission_registry=PermissionRegistry([_permission()]),
        source_registry=SourceRegistry([_registry_entry()]),
        request=_batch_request(),
        occurred_at=_RETRIEVED_AT,
        output_dir=tmp_path,
    )

    # The adapter really did fetch -- via `used_fetcher`, which this test
    # never handed to run_shadow_discovery.
    assert used_fetcher.call_count == 1
    assert len(adapter.audit_events) == 1

    evaluation = report.record.evaluation
    assert evaluation.request_count == 0
    assert KillReason.request_counter_not_wired in evaluation.kill_reasons
    assert evaluation.halted is True
    # And no fabricated byte-cap number for a run this harness cannot trust.
    assert report.record.byte_measurement.measured is False
    assert report.record.byte_measurement.observed_byte_count is None
    assert report.record.byte_measurement.recommended_cap_bytes is None
    assert report.record.byte_measurement.note == "no measurement available"


def test_mismatched_counter_also_caught_when_the_run_itself_fails(tmp_path: Path) -> None:
    """The wiring-integrity check must not depend on the run having
    succeeded -- a failed fetch still produces exactly one adapter audit
    event (module docstring's "Audit" section), so the mismatch is still
    provable even when the adapter's discover() ultimately raises."""
    used_fetcher = RequestCountingFetcher(
        _ScriptedFetcher(
            FetchResult(
                ok=False, reason_code=FetchReasonCode.fetch_failed, source_id=_SOURCE_ID
            )
        )
    )
    adapter = _adapter(used_fetcher)
    unused_fetcher = RequestCountingFetcher(_ScriptedFetcher(_ok_fetch_result(b"")))

    report = run_shadow_discovery(
        adapter,
        unused_fetcher,
        permission_registry=PermissionRegistry([_permission()]),
        source_registry=SourceRegistry([_registry_entry()]),
        request=_batch_request(),
        occurred_at=_RETRIEVED_AT,
        output_dir=tmp_path,
    )

    assert used_fetcher.call_count == 1
    evaluation = report.record.evaluation
    assert KillReason.request_counter_not_wired in evaluation.kill_reasons


def test_no_fetch_attempt_at_all_is_not_mistaken_for_a_wiring_bug(tmp_path: Path) -> None:
    """A source that is skipped before discover() is ever called (e.g. not
    registered) legitimately has request_count == 0 -- that is NOT a wiring
    bug, and must not be flagged as one (it is already correctly caught by
    `permission_denied_or_expired` via `skipped_not_approved`)."""
    report, fetcher = _run(
        tmp_path,
        _ok_fetch_result(_rss_bytes(_item_xml())),
        source_registry=SourceRegistry([]),
    )
    assert fetcher.call_count == 0
    evaluation = report.record.evaluation
    assert KillReason.request_counter_not_wired not in evaluation.kill_reasons
    assert KillReason.permission_denied_or_expired in evaluation.kill_reasons


# --- C4: byte cap tightens from real, successful data only -----------------


def test_successful_run_reports_byte_count_and_cap_recommendation(tmp_path: Path) -> None:
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body))

    measurement = report.record.byte_measurement
    assert measurement.measured is True
    assert measurement.observed_byte_count == len(body)
    assert measurement.recommended_cap_bytes == len(body) * 2
    assert measurement.note != "no measurement available"


def test_rejected_run_reports_no_measurement_available_never_a_number(tmp_path: Path) -> None:
    fetch_result = FetchResult(
        ok=False, reason_code=FetchReasonCode.response_too_large, source_id=_SOURCE_ID
    )
    report, _ = _run(tmp_path, fetch_result)

    measurement = report.record.byte_measurement
    assert measurement.measured is False
    assert measurement.observed_byte_count is None
    assert measurement.recommended_cap_bytes is None
    assert measurement.note == "no measurement available"


def test_poisoned_success_403_never_produces_a_byte_measurement(tmp_path: Path) -> None:
    """A run that LOOKS successful (BatchStatus.all_succeeded) but carries a
    blocked status_code must never feed a cap recommendation either --
    C3 and C4 must work together, not just independently."""
    body = _rss_bytes(_item_xml())
    report, _ = _run(tmp_path, _ok_fetch_result(body, status_code=403))

    assert report.record.batch_result.status == BatchStatus.all_succeeded
    measurement = report.record.byte_measurement
    assert measurement.measured is False
    assert measurement.note == "no measurement available"


# --- atomic-write rollback: a failure mid-write leaves no partial artifact --


def test_atomic_append_rollback_leaves_no_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history_path = tmp_path / SHADOW_RUN_HISTORY_FILENAME
    original_content = '{"existing": "record"}\n'
    history_path.write_text(original_content, encoding="utf-8")

    real_replace = os.replace
    call_count = 0

    def _flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal call_count
        call_count += 1
        # Let the FIRST os.replace succeed (the backup step: moving the
        # existing file aside) but fail the SECOND (the actual rename of the
        # staged temp file into place) -- exactly "a failure mid-write".
        if call_count == 2:
            raise OSError("simulated failure mid-write")
        real_replace(src, dst)

    monkeypatch.setattr(shadow_run_module.os, "replace", _flaky_replace)

    with pytest.raises(OSError, match="simulated failure mid-write"):
        shadow_run_module._atomic_append_jsonl_line(history_path, '{"new": "record"}')

    # Rolled back: the destination is exactly what it was before the call.
    assert history_path.read_text(encoding="utf-8") == original_content
    # No leftover temp/backup files in the directory.
    leftover = [p for p in tmp_path.iterdir() if p.name != SHADOW_RUN_HISTORY_FILENAME]
    assert leftover == [], f"leftover temp/backup files: {leftover}"


def test_atomic_append_succeeds_when_nothing_fails(tmp_path: Path) -> None:
    history_path = tmp_path / SHADOW_RUN_HISTORY_FILENAME
    shadow_run_module._atomic_append_jsonl_line(history_path, '{"a": 1}')
    shadow_run_module._atomic_append_jsonl_line(history_path, '{"b": 2}')

    lines = history_path.read_text(encoding="utf-8").splitlines()
    assert lines == ['{"a": 1}', '{"b": 2}']
    leftover = [p for p in tmp_path.iterdir() if p.name != SHADOW_RUN_HISTORY_FILENAME]
    assert leftover == []


# --- override frequency: a monitored count, not just a fact -----------------


def _fabricated_override_record(occurred_at: datetime) -> ShadowRunRecord:
    """A synthetic, hand-built ShadowRunRecord carrying one
    `human_reviewed_security_flags_override` audit event, standing in for
    what a real Founder-reviewed override (via
    `bridge.to_source_item_with_audit`, a separate code path this harness
    never calls) would look like once persisted -- so the override-counting
    mechanism can be tested without wiring the whole bridge."""
    override_event = ConnectorAuditEvent(
        source_id=_SOURCE_ID,
        event_kind=AuditEventKind.security,
        reason_code="human_reviewed_security_flags_override",
        occurred_at=occurred_at,
        detail="human override admitted item despite blocking flag(s); reviewer note: ok",
    )
    batch_result = BatchDiscoveryResult(
        status=BatchStatus.all_succeeded,
        results=(),
        failures=(),
        audit_events=(override_event,),
        coverage=SourceCoverageReport(
            sources=(SourceCoverage(source_id=_SOURCE_ID, attempted=True, discovered=0),)
        ),
    )
    evaluation = ShadowRunEvaluation(
        kill_reasons=(), halted=False, founder_review_required=False, request_count=1
    )
    byte_measurement = shadow_run_module.ByteCapMeasurement(
        measured=False, note="no measurement available"
    )
    return ShadowRunRecord(
        source_id=_SOURCE_ID,
        occurred_at=occurred_at,
        adapter_audit_events=(),
        batch_result=batch_result,
        evaluation=evaluation,
        byte_measurement=byte_measurement,
    )


def test_override_count_surfaces_the_count_not_just_the_fact(tmp_path: Path) -> None:
    history_path = tmp_path / SHADOW_RUN_HISTORY_FILENAME
    prior = _fabricated_override_record(_RETRIEVED_AT)
    shadow_run_module._atomic_append_jsonl_line(history_path, prior.model_dump_json())
    prior_2 = _fabricated_override_record(_RETRIEVED_AT)
    shadow_run_module._atomic_append_jsonl_line(history_path, prior_2.model_dump_json())

    report, _ = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))

    # Two prior overrides + zero new ones from this (clean) run.
    assert report.override_count_in_window == 2
    assert report.total_runs_persisted == 3


def test_zero_overrides_reports_zero_not_omitted(tmp_path: Path) -> None:
    report, _ = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))
    assert report.override_count_in_window == 0


# --- output-dir resolution: never a hardcoded default -----------------------


def test_missing_output_dir_configuration_fails_closed() -> None:
    settings = Settings(shadow_run_output_dir=None)
    with pytest.raises(MissingShadowRunOutputDirError):
        resolve_shadow_run_output_dir(settings)


def test_explicit_settings_output_dir_resolves(tmp_path: Path) -> None:
    settings = Settings(shadow_run_output_dir=tmp_path)
    assert resolve_shadow_run_output_dir(settings) == tmp_path


def test_explicit_path_overrides_and_requires_no_settings(tmp_path: Path) -> None:
    assert resolve_shadow_run_output_dir(path=tmp_path) == tmp_path


def test_resolve_output_dir_requires_settings_or_path() -> None:
    with pytest.raises(ValueError):
        resolve_shadow_run_output_dir()


# --- render_shadow_run_report: pure, human-readable, no crash --------------


def test_render_shadow_run_report_is_pure_text(tmp_path: Path) -> None:
    report, _ = _run(tmp_path, _ok_fetch_result(_rss_bytes(_item_xml())))
    rendered = render_shadow_run_report(report)
    assert "Shadow-Run Report" in rendered
    assert _SOURCE_ID in rendered
    assert "halted: False" in rendered


# --- convenience wiring: build_shadow_run_arxiv_adapter ---------------------


def test_build_shadow_run_arxiv_adapter_wires_a_counting_fetcher() -> None:
    from pydantic import SecretStr

    from content_machine.connectors.private_config import (
        PrivateSourceConfig,
        PrivateSourceEndpoint,
    )

    private_config = PrivateSourceConfig(
        endpoints=(
            PrivateSourceEndpoint(
                source_id=_SOURCE_ID,
                hostname=SecretStr("feed.acme-connectors.example.com"),
                endpoint=SecretStr(_FEED_URL),
            ),
        )
    )
    permission_registry = PermissionRegistry([_permission()])

    adapter, fetcher = build_shadow_run_arxiv_adapter(
        _SOURCE_ID,
        private_config,
        permission_registry,
        source_group="synthetic",
        discovery_run_id="synthetic-run-2026-w29",
        retrieved_at=_RETRIEVED_AT,
    )

    assert isinstance(adapter, ArxivRssAdapter)
    assert isinstance(fetcher, RequestCountingFetcher)
    assert fetcher.call_count == 0


def test_build_shadow_run_arxiv_adapter_raises_on_unknown_source() -> None:
    from content_machine.connectors.arxiv_adapter import UnknownPrivateSourceEndpointError
    from content_machine.connectors.private_config import PrivateSourceConfig

    with pytest.raises(UnknownPrivateSourceEndpointError):
        build_shadow_run_arxiv_adapter(
            "unknown-source",
            PrivateSourceConfig(endpoints=()),
            PermissionRegistry([]),
            source_group="synthetic",
            discovery_run_id="synthetic-run-2026-w29",
            retrieved_at=_RETRIEVED_AT,
        )


# --- sanity: SecurityFlag import actually used somewhere (avoid unused) ----


def test_blocking_security_flags_contains_instruction_shaped_text() -> None:
    assert SecurityFlag.instruction_shaped_text in BLOCKING_SECURITY_FLAGS
