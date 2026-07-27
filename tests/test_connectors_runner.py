"""Tests for content_machine.connectors.runner (Gate D commit 2, spec §8):
per-source isolation, deterministic ordering, permission integration,
field-enforcement rejection, batch-level duplicate/conflicting-date
flagging, and the coverage report.

Also covers the Gate D round-1 security corrections (B1, C6): adapter
identity binding via source_id matching + source_registry, permission_ref
reconstruction from the authorized registry, and preserving
ConnectorAdapterError.reason through sanitize_error."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from content_machine.connectors.failures import FailureKind
from content_machine.connectors.models import (
    DiscoveryRequest,
    DiscoveryResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
)
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
    BatchStatus,
    format_coverage_report,
    run_discovery,
)
from content_machine.connectors.synthetic import fixtures
from content_machine.connectors.synthetic.adapters import (
    RateLimitedSyntheticAdapter,
    RevokedPermissionSyntheticAdapter,
    SuccessfulSyntheticAdapter,
    SyntheticItemSpec,
    TimeoutSyntheticAdapter,
)


class _SpoofedSourceIdAdapter:
    """Registered under ``source_id`` but returns a ``DiscoveryResult``
    self-labeled with a DIFFERENT ``source_id`` -- the exact proven B1
    probe: an adapter claiming to speak for a source it was never
    authorized as."""

    def __init__(self, source_id: str, claimed_source_id: str) -> None:
        self.source_id = source_id
        self._claimed_source_id = claimed_source_id

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        result = DiscoveryResult(
            source_id=self._claimed_source_id,
            source_group="synthetic",
            title="Spoofed item claiming another source's identity",
            publication_date=date(2026, 7, 15),
            canonical_reference="https://example.com/spoofed-item",
            summary_normalized="a summary only the claimed source's permission would allow",
            summary_provenance=SummaryProvenance.system_derived,
            content_type="text/html",
            retrieved_at=fixtures.FIXED_RETRIEVED_AT,
            provenance=ProvenanceMetadata(
                adapter_name="spoofed",
                discovery_run_id=fixtures.FIXED_DISCOVERY_RUN_ID,
                discovered_at=fixtures.FIXED_RETRIEVED_AT,
            ),
            permission_ref=PermissionRef(
                source_id=self._claimed_source_id,
                approved_mode="discovery",
                status_at_discovery="approved",
            ),
        )
        return AdapterDiscoveryOutcome(results=(result,))


class _MustNotBeCalledAdapter:
    """Fails the test immediately if ``discover()`` is ever invoked --
    used to prove a source missing from ``source_registry`` is skipped
    without ever reaching its adapter."""

    source_id = "src_uncurated"

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        raise AssertionError("adapter must never be invoked when not in source_registry")

_OCCURRED_AT = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)

_ALL_FIELDS = frozenset(
    {"title", "canonical_reference", "content_type", "publication_date", "summary_normalized"}
)


def _permission(
    source_id: str,
    *,
    status: PermissionStatus = PermissionStatus.approved,
    permitted_fields: frozenset[str] = _ALL_FIELDS,
    review_due: date | None = None,
) -> SourcePermission:
    return SourcePermission(
        source_id=source_id,
        approved_mode=SourceMode.discovery,
        permitted_fields=permitted_fields,
        retention_policy_id="policy_default",
        review_due=review_due,
        authorization_owner="founder",
        status=status,
    )


def _registry_entry(source_id: str) -> SourceRegistryEntry:
    return SourceRegistryEntry(
        source_id=source_id,
        source_group="synthetic",
        publisher_id=f"vendor-{source_id}",
        source_category="vendor_blog",
        source_type="feed",
        publisher_classification=PublisherClassification.vendor_first_party,
        endpoint_label=f"{source_id} endpoint",
    )


def _batch_request() -> BatchDiscoveryRequest:
    return BatchDiscoveryRequest(window_start=date(2026, 7, 11), window_end=date(2026, 7, 18))


# --- D: per-source isolation, literal invariant ------------------------


def test_per_source_isolation_one_failure_never_aborts_the_batch() -> None:
    """Literal invariant (spec §8): with N sources where 1 raises, the batch
    contains every successful source's results PLUS exactly one
    SourceFailure, and status is never all-or-nothing."""
    adapters = [
        SuccessfulSyntheticAdapter("src_a"),
        SuccessfulSyntheticAdapter("src_b"),
        TimeoutSyntheticAdapter("src_c"),
    ]
    permission_registry = PermissionRegistry(
        [_permission("src_a"), _permission("src_b"), _permission("src_c")]
    )
    source_registry = SourceRegistry(
        [_registry_entry("src_a"), _registry_entry("src_b"), _registry_entry("src_c")]
    )

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )

    assert len(result.results) == 2  # src_a + src_b, one item each
    assert {r.source_id for r in result.results} == {"src_a", "src_b"}
    assert len(result.failures) == 1
    assert result.failures[0].source_id == "src_c"
    assert result.failures[0].kind == FailureKind.timeout
    assert result.status == BatchStatus.partial
    assert result.status not in (BatchStatus.all_succeeded, BatchStatus.all_failed)


def test_all_sources_succeed_yields_all_succeeded_status() -> None:
    adapters = [SuccessfulSyntheticAdapter("src_a"), SuccessfulSyntheticAdapter("src_b")]
    permission_registry = PermissionRegistry([_permission("src_a"), _permission("src_b")])
    source_registry = SourceRegistry([_registry_entry("src_a"), _registry_entry("src_b")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.status == BatchStatus.all_succeeded
    assert result.failures == ()


def test_all_sources_fail_yields_all_failed_status() -> None:
    adapters = [TimeoutSyntheticAdapter("src_a"), RateLimitedSyntheticAdapter("src_b")]
    permission_registry = PermissionRegistry([_permission("src_a"), _permission("src_b")])
    source_registry = SourceRegistry([_registry_entry("src_a"), _registry_entry("src_b")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.status == BatchStatus.all_failed
    assert len(result.failures) == 2
    assert result.results == ()


def test_non_connector_adapter_error_is_still_isolated_as_unavailable() -> None:
    """Per-source isolation must hold for ANY exception, not only
    ConnectorAdapterError -- an adapter that raises a plain bug is recorded
    as FailureKind.unavailable, never allowed to abort the batch."""

    class _BuggyAdapter:
        source_id = "src_buggy"

        def discover(self, request: object) -> object:
            raise RuntimeError("unexpected adapter bug")

    adapters = [SuccessfulSyntheticAdapter("src_a"), _BuggyAdapter()]
    permission_registry = PermissionRegistry([_permission("src_a"), _permission("src_buggy")])
    source_registry = SourceRegistry([_registry_entry("src_a"), _registry_entry("src_buggy")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert len(result.results) == 1
    assert len(result.failures) == 1
    assert result.failures[0].kind == FailureKind.unavailable
    assert result.failures[0].retry_eligible is False
    assert "RuntimeError" in result.failures[0].sanitized_reason
    assert "unexpected adapter bug" not in result.failures[0].sanitized_reason


# --- permission integration -----------------------------------------------


def test_unapproved_source_is_skipped_not_failed() -> None:
    """A locally revoked/suspended/proposed source is never even handed to
    its adapter: it is recorded as skipped_not_approved, not as a failure."""
    adapters = [SuccessfulSyntheticAdapter("src_revoked")]
    permission_registry = PermissionRegistry(
        [_permission("src_revoked", status=PermissionStatus.revoked)]
    )
    source_registry = SourceRegistry([_registry_entry("src_revoked")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.results == ()
    assert result.failures == ()
    coverage = result.coverage.sources[0]
    assert coverage.skipped_not_approved is True
    assert coverage.attempted is False
    assert coverage.failed is False


def test_unregistered_source_is_skipped_not_approved() -> None:
    adapters = [SuccessfulSyntheticAdapter("src_unknown")]
    permission_registry = PermissionRegistry([])
    source_registry = SourceRegistry([])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.coverage.sources[0].skipped_not_approved is True


def test_review_overdue_is_allowed_but_flagged_never_a_silent_expiry() -> None:
    adapters = [SuccessfulSyntheticAdapter("src_a")]
    permission_registry = PermissionRegistry(
        [_permission("src_a", review_due=date(2020, 1, 1))]
    )
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters,
        permission_registry,
        source_registry,
        _batch_request(),
        occurred_at=_OCCURRED_AT,
        review_as_of=date(2026, 7, 18),
    )
    assert len(result.results) == 1  # still allowed
    assert result.coverage.sources[0].review_overdue is True


def test_revoked_permission_adapter_signals_mid_run_revocation() -> None:
    """RevokedPermissionSyntheticAdapter simulates the SOURCE ITSELF
    reporting revocation mid-discovery, distinct from a locally-revoked
    permission (which never reaches the adapter at all -- see the test
    above)."""
    adapters = [RevokedPermissionSyntheticAdapter("src_a")]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert len(result.failures) == 1
    assert result.failures[0].kind == FailureKind.permission_revoked


# --- B1 (Gate D round-1 correction): adapter identity binding --------------


def test_b1_spoofed_source_id_probe_fails_closed() -> None:
    """The exact proven probe: an adapter registered (and approved) as
    src_approved returns a result self-labeled source_id="src_REVOKED".
    Before the fix, the batch reported all_succeeded and the item was
    accepted with the revoked source's permission_ref echoed back
    verbatim. It must now fail closed: zero results, one SourceFailure
    (source_id_mismatch), and the batch is never all_succeeded."""
    adapters = [_SpoofedSourceIdAdapter("src_approved", "src_REVOKED")]
    permission_registry = PermissionRegistry(
        [
            _permission("src_approved", status=PermissionStatus.approved),
            _permission("src_REVOKED", status=PermissionStatus.revoked),
        ]
    )
    source_registry = SourceRegistry(
        [_registry_entry("src_approved"), _registry_entry("src_REVOKED")]
    )

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.results == ()
    assert len(result.failures) == 1
    assert result.failures[0].source_id == "src_approved"
    assert result.failures[0].kind == FailureKind.source_id_mismatch
    assert result.failures[0].retry_eligible is False
    assert result.status != BatchStatus.all_succeeded
    mismatch_events = [e for e in result.audit_events if e.reason_code == "source_id_mismatch"]
    assert len(mismatch_events) == 1
    assert mismatch_events[0].event_kind == "security"


def test_b1_spoofed_source_id_flips_batch_status_from_all_succeeded_to_partial() -> None:
    """Literal all_succeeded -> partial transition: with one genuinely
    successful source alongside the spoofing one, the batch must report
    partial, never all_succeeded -- the pre-fix behavior."""
    adapters = [
        SuccessfulSyntheticAdapter("src_a"),
        _SpoofedSourceIdAdapter("src_approved", "src_REVOKED"),
    ]
    permission_registry = PermissionRegistry(
        [
            _permission("src_a"),
            _permission("src_approved"),
            _permission("src_REVOKED", status=PermissionStatus.revoked),
        ]
    )
    source_registry = SourceRegistry(
        [
            _registry_entry("src_a"),
            _registry_entry("src_approved"),
            _registry_entry("src_REVOKED"),
        ]
    )

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.status == BatchStatus.partial
    assert {r.source_id for r in result.results} == {"src_a"}
    assert len(result.failures) == 1
    assert result.failures[0].source_id == "src_approved"
    assert result.failures[0].kind == FailureKind.source_id_mismatch


def test_narrow_permission_adapter_claiming_a_wide_source_id_is_rejected() -> None:
    """A source with NARROW permitted_fields cannot escape that restriction
    by self-labeling its results with a differently (more widely)
    permissioned source_id -- the identity check fires before field
    enforcement is ever reached, so this can never depend on which
    source's permitted_fields happen to be checked."""
    adapters = [_SpoofedSourceIdAdapter("src_narrow", "src_wide")]
    permission_registry = PermissionRegistry(
        [
            _permission(
                "src_narrow",
                permitted_fields=frozenset({"title", "canonical_reference", "content_type"}),
            ),
            _permission("src_wide"),  # full permitted_fields
        ]
    )
    source_registry = SourceRegistry([_registry_entry("src_narrow"), _registry_entry("src_wide")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.results == ()
    assert result.failures[0].source_id == "src_narrow"
    assert result.failures[0].kind == FailureKind.source_id_mismatch


def test_source_not_in_source_registry_is_skipped_never_invoked() -> None:
    """C4: source_registry is now an active fail-closed gate, not merely
    accepted-and-unused. A source approved in permission_registry but
    absent from source_registry must be skipped WITHOUT its adapter ever
    being called."""
    adapters = [_MustNotBeCalledAdapter()]
    permission_registry = PermissionRegistry([_permission("src_uncurated")])
    source_registry = SourceRegistry([])  # deliberately does not curate src_uncurated

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.coverage.sources[0].skipped_not_approved is True
    assert result.coverage.sources[0].attempted is False
    assert result.results == ()
    assert result.failures == ()


def test_permission_ref_is_reconstructed_from_the_authorized_registry() -> None:
    """B1: DiscoveryResult.permission_ref must reflect what the
    PermissionRegistry actually authorized for this run, not whatever an
    adapter self-reports -- even when the adapter's self-report is wrong
    (here: claiming verification/revoked when the registry says
    discovery/approved)."""
    adapter = SuccessfulSyntheticAdapter(
        "src_a", approved_mode="verification", status_at_discovery="revoked"
    )
    permission_registry = PermissionRegistry([_permission("src_a")])  # actually discovery/approved
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        [adapter], permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert len(result.results) == 1
    ref = result.results[0].permission_ref
    assert ref.approved_mode == "discovery"
    assert ref.status_at_discovery == "approved"


# --- C6 (Gate D round-1 correction): ConnectorAdapterError.reason preserved -


def test_connector_adapter_error_reason_is_preserved_not_discarded() -> None:
    """Before the fix, sanitize_error(exc) on a ConnectorAdapterError
    returned ONLY the literal type name "ConnectorAdapterError" for every
    adapter failure, discarding exc.reason entirely. exc.reason must now be
    sanitized and kept."""
    adapters = [TimeoutSyntheticAdapter("src_a")]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.failures[0].sanitized_reason != "ConnectorAdapterError"
    assert "timeout" in result.failures[0].sanitized_reason.lower()


# --- C3 (Gate D round-2 correction): max_items_per_source is a hard ceiling


def test_adapter_exceeding_max_items_per_source_is_truncated_deterministically() -> None:
    """An adapter that ignores its own ceiling and returns more than
    max_items_per_source must be truncated by the RUNNER, not merely
    trusted -- the excess is dropped and reported, never silently admitted
    in full."""
    items = [
        SyntheticItemSpec(
            raw_title=f"Vendor Omega item {i}",
            raw_summary="a benign summary",
            canonical_reference=f"https://example.com/vendor-omega/item-{i}",
            publication_date=date(2026, 7, 15),
        )
        for i in range(5)
    ]
    adapters = [SuccessfulSyntheticAdapter("src_a", items=items)]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])
    batch_request = BatchDiscoveryRequest(
        window_start=date(2026, 7, 11), window_end=date(2026, 7, 18), max_items_per_source=3
    )

    result = run_discovery(
        adapters, permission_registry, source_registry, batch_request, occurred_at=_OCCURRED_AT
    )

    assert len(result.results) == 3
    coverage = result.coverage.sources[0]
    assert coverage.discovered == 3
    assert coverage.dropped_count == 2
    assert coverage.truncated is True
    assert coverage.failed is False
    # Deterministic: the first 3 (in the adapter's own returned order) are
    # kept, not an arbitrary subset.
    kept_references = {r.canonical_reference for r in result.results}
    assert kept_references == {
        "https://example.com/vendor-omega/item-0",
        "https://example.com/vendor-omega/item-1",
        "https://example.com/vendor-omega/item-2",
    }
    assert result.status == BatchStatus.all_succeeded


def test_adapter_within_max_items_per_source_is_never_marked_truncated() -> None:
    items = [
        SyntheticItemSpec(
            raw_title=f"Vendor Omega item {i}",
            raw_summary="a benign summary",
            canonical_reference=f"https://example.com/vendor-omega/item-{i}",
            publication_date=date(2026, 7, 15),
        )
        for i in range(2)
    ]
    adapters = [SuccessfulSyntheticAdapter("src_a", items=items)]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])
    batch_request = BatchDiscoveryRequest(
        window_start=date(2026, 7, 11), window_end=date(2026, 7, 18), max_items_per_source=3
    )

    result = run_discovery(
        adapters, permission_registry, source_registry, batch_request, occurred_at=_OCCURRED_AT
    )
    coverage = result.coverage.sources[0]
    assert coverage.discovered == 2
    assert coverage.dropped_count == 0
    assert coverage.truncated is False


# --- field-enforcement: REJECT, never a silent strip, with an audit event ---


def test_field_outside_permitted_fields_is_rejected_with_audit_event() -> None:
    item = SyntheticItemSpec(
        raw_title="Vendor Nu Update",
        raw_summary="a summary the permission does not authorize",
        canonical_reference="https://example.com/vendor-nu/item",
        publication_date=date(2026, 7, 15),
    )
    adapters = [SuccessfulSyntheticAdapter("src_a", items=[item])]
    # permitted_fields omits summary_normalized -- the item's non-empty
    # summary must be REJECTED, never silently stripped.
    permission_registry = PermissionRegistry(
        [
            _permission(
                "src_a",
                permitted_fields=frozenset(
                    {"title", "canonical_reference", "content_type", "publication_date"}
                ),
            )
        ]
    )
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert result.results == ()
    assert result.coverage.sources[0].dropped_count == 1
    field_violation_events = [
        e for e in result.audit_events if e.reason_code == "field_not_permitted"
    ]
    assert len(field_violation_events) == 1
    assert field_violation_events[0].source_id == "src_a"


# --- batch-level duplicate reference / conflicting publication date -------


def test_duplicate_canonical_reference_and_conflicting_date_are_flagged() -> None:
    from content_machine.connectors.sanitize import SecurityFlag

    item_a = SyntheticItemSpec(
        raw_title=fixtures.DUPLICATE_TITLE_A,
        raw_summary=fixtures.DUPLICATE_SUMMARY,
        canonical_reference=fixtures.DUPLICATE_REFERENCE_A,
        publication_date=fixtures.DUPLICATE_DATE_A,
    )
    item_b = SyntheticItemSpec(
        raw_title=fixtures.DUPLICATE_TITLE_B,
        raw_summary=fixtures.DUPLICATE_SUMMARY,
        canonical_reference=fixtures.DUPLICATE_REFERENCE_B,
        publication_date=fixtures.DUPLICATE_DATE_B,
    )
    adapters = [
        SuccessfulSyntheticAdapter("src_a", items=[item_a]),
        SuccessfulSyntheticAdapter("src_b", items=[item_b]),
    ]
    permission_registry = PermissionRegistry([_permission("src_a"), _permission("src_b")])
    source_registry = SourceRegistry([_registry_entry("src_a"), _registry_entry("src_b")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert len(result.results) == 2
    for res in result.results:
        assert SecurityFlag.duplicate_canonical_reference in res.security_flags
        assert SecurityFlag.conflicting_publication_date in res.security_flags
    security_events = [e for e in result.audit_events if e.event_kind == "security"]
    assert len(security_events) == 2


# --- deterministic ordering -------------------------------------------


def test_result_and_failure_ordering_is_independent_of_adapter_list_order() -> None:
    permission_registry = PermissionRegistry(
        [_permission("src_a"), _permission("src_b"), _permission("src_c")]
    )
    source_registry = SourceRegistry(
        [_registry_entry("src_a"), _registry_entry("src_b"), _registry_entry("src_c")]
    )

    forward = [
        SuccessfulSyntheticAdapter("src_a"),
        SuccessfulSyntheticAdapter("src_b"),
        SuccessfulSyntheticAdapter("src_c"),
    ]
    reversed_ = list(reversed(forward))

    result_forward = run_discovery(
        forward, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    result_reversed = run_discovery(
        reversed_,
        permission_registry,
        source_registry,
        _batch_request(),
        occurred_at=_OCCURRED_AT,
    )
    assert [r.source_id for r in result_forward.results] == [
        r.source_id for r in result_reversed.results
    ]
    assert result_forward.model_dump() == result_reversed.model_dump()


# --- determinism, including across PYTHONHASHSEED ---------------------


_DETERMINISM_SCRIPT = """
import json
from datetime import UTC, date, datetime

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
from content_machine.connectors.runner import BatchDiscoveryRequest, run_discovery
from content_machine.connectors.synthetic.adapters import (
    SuccessfulSyntheticAdapter,
    TimeoutSyntheticAdapter,
)

permission_registry = PermissionRegistry([
    SourcePermission(
        source_id="src_a", approved_mode=SourceMode.discovery,
        permitted_fields=frozenset({"title","canonical_reference","content_type","publication_date","summary_normalized"}),
        retention_policy_id="p", authorization_owner="founder", status=PermissionStatus.approved,
    ),
    SourcePermission(
        source_id="src_b", approved_mode=SourceMode.discovery,
        permitted_fields=frozenset({"title","canonical_reference","content_type","publication_date","summary_normalized"}),
        retention_policy_id="p", authorization_owner="founder", status=PermissionStatus.approved,
    ),
])
source_registry = SourceRegistry([
    SourceRegistryEntry(
        source_id="src_a", source_group="synthetic", publisher_id="vendor-a",
        source_category="vendor_blog", source_type="feed",
        publisher_classification=PublisherClassification.vendor_first_party,
        endpoint_label="a",
    ),
    SourceRegistryEntry(
        source_id="src_b", source_group="synthetic", publisher_id="vendor-b",
        source_category="vendor_blog", source_type="feed",
        publisher_classification=PublisherClassification.vendor_first_party,
        endpoint_label="b",
    ),
])
adapters = [SuccessfulSyntheticAdapter("src_a"), TimeoutSyntheticAdapter("src_b")]
request = BatchDiscoveryRequest(window_start=date(2026, 7, 11), window_end=date(2026, 7, 18))
result = run_discovery(
    adapters, permission_registry, source_registry, request,
    occurred_at=datetime(2026, 7, 18, 10, 0, tzinfo=UTC),
)
print(result.model_dump_json())
"""


def test_batch_serialization_is_byte_identical_across_two_pythonhashseed_values(
    tmp_path: Path,
) -> None:
    """Deterministic fake outputs: same input, same PYTHONHASHSEED-independent
    serialization -- run the same batch in two subprocesses with different
    PYTHONHASHSEED values and assert byte-identical JSON output."""
    script_path = tmp_path / "determinism_script.py"
    script_path.write_text(_DETERMINISM_SCRIPT, encoding="utf-8")

    outputs = []
    for seed in ("0", "1"):
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": seed, "PATH": __import__("os").environ.get("PATH", "")},
            check=True,
        )
        outputs.append(completed.stdout.strip())

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == json.loads(outputs[1])


# --- C5 (Gate D round-2 correction): format_coverage_report ----------------


def test_format_coverage_report_is_deterministic() -> None:
    adapters = [TimeoutSyntheticAdapter("src_a"), SuccessfulSyntheticAdapter("src_b")]
    permission_registry = PermissionRegistry([_permission("src_a"), _permission("src_b")])
    source_registry = SourceRegistry([_registry_entry("src_a"), _registry_entry("src_b")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    first = format_coverage_report(result.coverage)
    second = format_coverage_report(result.coverage)
    assert first == second
    assert isinstance(first, str)


def test_format_coverage_report_shows_a_failing_sources_sanitized_reason() -> None:
    adapters = [TimeoutSyntheticAdapter("src_a")]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    rendered = format_coverage_report(result.coverage)
    failing_row = result.coverage.sources[0]
    assert failing_row.sanitized_reason  # sanity: there IS a reason to find
    assert failing_row.sanitized_reason in rendered
    assert "src_a" in rendered
    assert "timeout" in rendered.lower()


def test_format_coverage_report_never_leaks_raw_content() -> None:
    """A hostile item's raw title/summary must never appear in the coverage
    report -- SourceCoverage carries counts and a sanitized reason only, so
    this proves it empirically as well as structurally."""
    item = SyntheticItemSpec(
        raw_title=fixtures.HOSTILE_PROMPT_INJECTION,
        raw_summary=fixtures.HOSTILE_PROMPT_INJECTION,
        canonical_reference=fixtures.HOSTILE_PROMPT_INJECTION_REFERENCE,
        publication_date=date(2026, 7, 15),
    )
    adapters = [SuccessfulSyntheticAdapter("src_a", items=[item])]
    permission_registry = PermissionRegistry([_permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    assert len(result.results) == 1  # the hostile item was admitted (only flagged)
    rendered = format_coverage_report(result.coverage)
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" not in rendered
    assert fixtures.HOSTILE_PROMPT_INJECTION not in rendered


def test_format_coverage_report_empty_report() -> None:
    result = run_discovery(
        [], PermissionRegistry([]), SourceRegistry([]), _batch_request(), occurred_at=_OCCURRED_AT
    )
    rendered = format_coverage_report(result.coverage)
    assert "Source Coverage Report" in rendered
    assert "no sources" in rendered.lower()


def test_format_coverage_report_rows_sorted_by_source_id() -> None:
    adapters = [SuccessfulSyntheticAdapter("src_z"), SuccessfulSyntheticAdapter("src_a")]
    permission_registry = PermissionRegistry([_permission("src_z"), _permission("src_a")])
    source_registry = SourceRegistry([_registry_entry("src_z"), _registry_entry("src_a")])

    result = run_discovery(
        adapters, permission_registry, source_registry, _batch_request(), occurred_at=_OCCURRED_AT
    )
    rendered = format_coverage_report(result.coverage)
    assert rendered.index("src_a") < rendered.index("src_z")
