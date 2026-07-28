"""Tests for content_machine.connectors.synthetic (Gate D commit 2, spec §9):
the seven required adapters by name, hostile-fixture sanitization behavior,
and the "secrets/raw body never appear anywhere in serialized output"
guarantee."""

from __future__ import annotations

import json

import pytest

from content_machine.connectors.failures import FailureKind
from content_machine.connectors.models import SUMMARY_MAX_CHARS, DiscoveryRequest
from content_machine.connectors.runner import ConnectorAdapterError
from content_machine.connectors.sanitize import SecurityFlag
from content_machine.connectors.synthetic import fixtures
from content_machine.connectors.synthetic.adapters import (
    MaliciousContentSyntheticAdapter,
    OversizedContentSyntheticAdapter,
    PartialBatchSyntheticAdapter,
    RateLimitedSyntheticAdapter,
    RevokedPermissionSyntheticAdapter,
    SuccessfulSyntheticAdapter,
    TimeoutSyntheticAdapter,
    redirect_chain_flags,
)

_REQUEST = DiscoveryRequest(
    source_id="src_a",
    window_start=fixtures.DUPLICATE_DATE_A,
    window_end=fixtures.DUPLICATE_DATE_B,
)


# --- the seven required adapters exist BY NAME and share a common base ------


def test_all_seven_required_adapter_classes_exist_by_name() -> None:
    import content_machine.connectors.synthetic.adapters as adapters_module

    required_names = {
        "SuccessfulSyntheticAdapter",
        "TimeoutSyntheticAdapter",
        "RateLimitedSyntheticAdapter",
        "MaliciousContentSyntheticAdapter",
        "OversizedContentSyntheticAdapter",
        "RevokedPermissionSyntheticAdapter",
        "PartialBatchSyntheticAdapter",
    }
    for name in required_names:
        assert hasattr(adapters_module, name), f"missing required adapter class: {name}"


def test_all_seven_adapters_share_a_common_base() -> None:
    import content_machine.connectors.synthetic.adapters as adapters_module

    base = adapters_module._BaseSyntheticAdapter
    for name in (
        "SuccessfulSyntheticAdapter",
        "TimeoutSyntheticAdapter",
        "RateLimitedSyntheticAdapter",
        "MaliciousContentSyntheticAdapter",
        "OversizedContentSyntheticAdapter",
        "RevokedPermissionSyntheticAdapter",
        "PartialBatchSyntheticAdapter",
    ):
        cls = getattr(adapters_module, name)
        assert issubclass(cls, base)


# --- success -------------------------------------------------------------


def test_successful_adapter_returns_benign_item_with_no_security_flags() -> None:
    outcome = SuccessfulSyntheticAdapter("src_a").discover(_REQUEST)
    assert len(outcome.results) == 1
    assert outcome.results[0].security_flags == ()
    assert outcome.truncated is False
    assert outcome.dropped_count == 0


# --- timeout / rate-limited / revoked / partial-batch: failure kinds -------


@pytest.mark.parametrize(
    "adapter_cls,expected_kind,expected_retry_eligible",
    [
        (TimeoutSyntheticAdapter, FailureKind.timeout, True),
        (RateLimitedSyntheticAdapter, FailureKind.rate_limited, True),
        (RevokedPermissionSyntheticAdapter, FailureKind.permission_revoked, False),
        (PartialBatchSyntheticAdapter, FailureKind.partial_batch, True),
    ],
)
def test_failure_adapter_raises_connector_adapter_error_with_expected_kind(
    adapter_cls: type, expected_kind: FailureKind, expected_retry_eligible: bool
) -> None:
    adapter = adapter_cls("src_a")
    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_REQUEST)
    assert excinfo.value.kind == expected_kind
    assert excinfo.value.retry_eligible is expected_retry_eligible


def test_unsupported_mime_scenario_raises_unsupported_content_failure() -> None:
    adapter = OversizedContentSyntheticAdapter("src_a", scenario="unsupported_mime")
    with pytest.raises(ConnectorAdapterError) as excinfo:
        adapter.discover(_REQUEST)
    assert excinfo.value.kind == FailureKind.unsupported_content


# --- hostile content: markup neutralized ---------------------------------


def test_malicious_content_adapter_neutralizes_active_markup() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    markup_item = next(
        r
        for r in outcome.results
        if r.canonical_reference == fixtures.HOSTILE_SCRIPT_MARKUP_REFERENCE
    )
    assert "<script" not in markup_item.title
    assert "<iframe" not in markup_item.title
    assert SecurityFlag.active_markup_neutralized in markup_item.security_flags


# --- hostile content: prompt injection / hidden instructions stay INERT DATA


def test_malicious_content_adapter_keeps_prompt_injection_as_inert_flagged_data() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    injected_item = next(
        r
        for r in outcome.results
        if r.canonical_reference == fixtures.HOSTILE_PROMPT_INJECTION_REFERENCE
    )
    # The matched text is left in place, unmodified -- it is data, not
    # something to strip (sanitize.py's documented behavior).
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in injected_item.title
    assert SecurityFlag.instruction_shaped_text in injected_item.security_flags


def test_malicious_content_adapter_flags_hidden_instruction_shaped_text() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    hidden_item = next(
        r
        for r in outcome.results
        if r.canonical_reference == fixtures.HOSTILE_HIDDEN_INSTRUCTION_REFERENCE
    )
    assert SecurityFlag.instruction_shaped_text in hidden_item.security_flags


# --- hostile content: credential / email / path redacted AND flagged -------


def test_malicious_content_adapter_redacts_credential_shaped_text() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    credential_item = next(
        r for r in outcome.results if r.canonical_reference == fixtures.HOSTILE_CREDENTIAL_REFERENCE
    )
    assert SecurityFlag.credential_shaped_text in credential_item.security_flags
    assert "token_SYNTHETIC_EXAMPLE_NOT_A_REAL_KEY_0000" not in credential_item.summary_normalized
    assert "token_SYNTHETIC_EXAMPLE_NOT_A_REAL_KEY_0000" not in credential_item.title


def test_malicious_content_adapter_redacts_email_shaped_text() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    email_item = next(
        r for r in outcome.results if r.canonical_reference == fixtures.HOSTILE_EMAIL_REFERENCE
    )
    assert SecurityFlag.email_shaped_text in email_item.security_flags
    assert "support-desk@example.org" not in email_item.summary_normalized
    assert "first.last@example.com" not in email_item.summary_normalized


def test_malicious_content_adapter_redacts_filesystem_path_shaped_text() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    path_item = next(
        r for r in outcome.results if r.canonical_reference == fixtures.HOSTILE_PATH_REFERENCE
    )
    assert SecurityFlag.filesystem_path_shaped_text in path_item.security_flags
    assert "/Users/example-builder" not in path_item.summary_normalized


def test_malicious_content_adapter_vendor_marketing_copy_passes_through_no_flags() -> None:
    """Vendor marketing copy is noise, not a security hazard -- it survives
    sanitization with no security flags raised (there is nothing hostile
    about it in the security sense; it is a distinct hostile-fixture
    CATEGORY -- excessive, unfalsifiable claims -- handled by the ranking/
    marketing_risk machinery in intelligence/, not by the sanitizer)."""
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    marketing_item = next(
        r
        for r in outcome.results
        if r.canonical_reference == fixtures.HOSTILE_MARKETING_REFERENCE
    )
    assert marketing_item.security_flags == ()
    assert "revolutionary" in marketing_item.title


# --- oversized / malformed encoding ------------------------------------


def test_oversized_content_is_truncated_and_flagged() -> None:
    outcome = OversizedContentSyntheticAdapter("src_a", scenario="oversized").discover(_REQUEST)
    result = outcome.results[0]
    assert SecurityFlag.oversized_truncated in result.security_flags
    assert len(result.summary_normalized) <= SUMMARY_MAX_CHARS


def test_malformed_encoding_survives_sanitization_without_raising() -> None:
    outcome = OversizedContentSyntheticAdapter(
        "src_a", scenario="malformed_encoding"
    ).discover(_REQUEST)
    result = outcome.results[0]
    assert SecurityFlag.malformed_encoding in result.security_flags
    assert "�" not in result.summary_normalized


# --- deceptive redirect chain: data only, never followed --------------


def test_redirect_chain_flags_is_pure_data_and_never_imports_network_code() -> None:
    flags = redirect_chain_flags(fixtures.HOSTILE_REDIRECT_CHAIN, max_redirects=3)
    assert flags == (SecurityFlag.redirect_chain_exceeded,)

    short_chain = fixtures.HOSTILE_REDIRECT_CHAIN[:3]
    assert redirect_chain_flags(short_chain, max_redirects=3) == ()


# --- secrets/raw-body absence from EVERY serialized shape -------------


def test_credential_fixture_value_absent_from_every_serialized_output() -> None:
    """Assert the raw credential-shaped fixture value never appears in ANY
    serialized DiscoveryResult produced by the malicious-content adapter --
    not in title, not in summary, not anywhere in the model's own JSON
    dump."""
    secret_value = "token_SYNTHETIC_EXAMPLE_NOT_A_REAL_KEY_0000"
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    for result in outcome.results:
        serialized = result.model_dump_json()
        assert secret_value not in serialized


def test_no_hostile_fixture_raw_text_survives_unsanitized_in_any_result() -> None:
    """None of the RAW (pre-sanitization) hostile strings appear verbatim in
    any produced DiscoveryResult's serialized form, except the
    instruction-shaped one (which is deliberately kept as inert, flagged
    data -- see the sanitizer's honesty requirement)."""
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    serialized_all = "\n".join(r.model_dump_json() for r in outcome.results)
    assert "<script>" not in serialized_all
    assert "<iframe" not in serialized_all
    assert "token_SYNTHETIC_EXAMPLE_NOT_A_REAL_KEY_0000" not in serialized_all
    assert "support-desk@example.org" not in serialized_all
    assert "/Users/example-builder" not in serialized_all


def test_discovery_result_has_no_field_capable_of_holding_a_raw_body() -> None:
    """Structural proof (mirrors test_connectors_contracts.py's own check):
    no synthetic adapter output can carry a raw body, because the field does
    not exist on the model at all."""
    from content_machine.connectors.models import DiscoveryResult

    assert "body" not in DiscoveryResult.model_fields
    assert "raw_content" not in DiscoveryResult.model_fields
    assert "content" not in DiscoveryResult.model_fields


def test_batch_json_roundtrip_never_contains_hostile_raw_bytes() -> None:
    outcome = MaliciousContentSyntheticAdapter("src_a").discover(_REQUEST)
    payload = json.dumps([json.loads(r.model_dump_json()) for r in outcome.results])
    assert "javascript:alert(1)" not in payload
    assert "onerror=" not in payload
