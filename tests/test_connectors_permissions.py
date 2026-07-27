"""Tests for content_machine.connectors.permissions (Gate D §4): fail-closed
authorization, one distinct reason code per invariant, and REJECT-not-strip
enforcement of permitted_fields."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from content_machine.connectors.models import (
    DiscoveryResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
)
from content_machine.connectors.permissions import (
    AuthorizationReasonCode,
    FieldEnforcementReasonCode,
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _permission(
    *,
    source_id: str = "src_vendor_alpha",
    approved_mode: SourceMode = SourceMode.discovery,
    status: PermissionStatus = PermissionStatus.approved,
    permitted_fields: frozenset[str] = frozenset(
        {"title", "canonical_reference", "content_type", "publication_date", "summary_normalized"}
    ),
    review_due: date | None = None,
) -> SourcePermission:
    return SourcePermission(
        source_id=source_id,
        approved_mode=approved_mode,
        permitted_fields=permitted_fields,
        retention_policy_id="policy_default",
        review_due=review_due,
        authorization_owner="founder",
        status=status,
    )


def _discovery_result(
    *, source_id: str = "src_vendor_alpha", summary_normalized: str = "a summary"
) -> DiscoveryResult:
    return DiscoveryResult(
        source_id=source_id,
        source_group="grp_vendor_alpha",
        title="Vendor Alpha update",
        publication_date=date(2026, 7, 18),
        canonical_reference="https://example.com/a",
        summary_normalized=summary_normalized,
        summary_provenance=SummaryProvenance.system_derived,
        content_type="text/html",
        retrieved_at=_NOW,
        provenance=ProvenanceMetadata(
            adapter_name="synthetic", discovery_run_id="run_1", discovered_at=_NOW
        ),
        permission_ref=PermissionRef(
            source_id=source_id, approved_mode="discovery", status_at_discovery="approved"
        ),
    )


# --- authorize(): fail-closed, one distinct reason code per invariant -----


def test_authorize_unknown_source_is_rejected_as_not_registered() -> None:
    registry = PermissionRegistry([])
    decision = registry.authorize("src_unknown", SourceMode.discovery)
    assert decision.allowed is False
    assert decision.reason_code == AuthorizationReasonCode.not_registered


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        (PermissionStatus.proposed, AuthorizationReasonCode.status_proposed),
        (PermissionStatus.suspended, AuthorizationReasonCode.status_suspended),
        (PermissionStatus.revoked, AuthorizationReasonCode.status_revoked),
    ],
)
def test_authorize_non_approved_status_rejected_with_distinct_reason_code(
    status: PermissionStatus, expected_reason: AuthorizationReasonCode
) -> None:
    registry = PermissionRegistry([_permission(status=status)])
    decision = registry.authorize("src_vendor_alpha", SourceMode.discovery)
    assert decision.allowed is False
    assert decision.reason_code == expected_reason


def test_authorize_reason_codes_are_all_distinct() -> None:
    """Constraint #10: proposed/suspended/revoked/unknown/mode-mismatch each
    get a DISTINCT reason code -- never collapsed into one generic denial."""
    codes = {
        AuthorizationReasonCode.not_registered,
        AuthorizationReasonCode.status_proposed,
        AuthorizationReasonCode.status_suspended,
        AuthorizationReasonCode.status_revoked,
        AuthorizationReasonCode.mode_mismatch,
        AuthorizationReasonCode.approved,
    }
    assert len(codes) == 6


def test_authorize_mode_mismatch_rejected() -> None:
    registry = PermissionRegistry([_permission(approved_mode=SourceMode.discovery)])
    decision = registry.authorize("src_vendor_alpha", SourceMode.verification)
    assert decision.allowed is False
    assert decision.reason_code == AuthorizationReasonCode.mode_mismatch


def test_authorize_both_mode_permits_either_requested_mode() -> None:
    registry = PermissionRegistry([_permission(approved_mode=SourceMode.both)])
    for mode in (SourceMode.discovery, SourceMode.verification):
        decision = registry.authorize("src_vendor_alpha", mode)
        assert decision.allowed is True
        assert decision.reason_code == AuthorizationReasonCode.approved


def test_authorize_approved_matching_mode_is_allowed() -> None:
    registry = PermissionRegistry([_permission(approved_mode=SourceMode.discovery)])
    decision = registry.authorize("src_vendor_alpha", SourceMode.discovery)
    assert decision.allowed is True
    assert decision.reason_code == AuthorizationReasonCode.approved


# --- review_due: never a silent expiry -------------------------------------


def test_authorize_overdue_review_is_still_allowed_but_flagged() -> None:
    registry = PermissionRegistry([_permission(review_due=date(2026, 1, 1))])
    decision = registry.authorize("src_vendor_alpha", SourceMode.discovery, as_of=date(2026, 7, 20))
    assert decision.allowed is True
    assert decision.review_overdue is True


def test_authorize_future_review_due_is_not_overdue() -> None:
    registry = PermissionRegistry([_permission(review_due=date(2027, 1, 1))])
    decision = registry.authorize("src_vendor_alpha", SourceMode.discovery, as_of=date(2026, 7, 20))
    assert decision.review_overdue is False


def test_authorize_without_as_of_never_reports_overdue() -> None:
    registry = PermissionRegistry([_permission(review_due=date(2020, 1, 1))])
    decision = registry.authorize("src_vendor_alpha", SourceMode.discovery)
    assert decision.review_overdue is False


# --- permitted_fields enforcement: REJECT + audit, never a silent strip ---


def test_enforce_discovery_fields_allows_when_all_populated_fields_permitted() -> None:
    registry = PermissionRegistry([_permission()])
    decision = registry.enforce_discovery_fields(_discovery_result())
    assert decision.allowed is True
    assert decision.reason_code == FieldEnforcementReasonCode.ok
    assert decision.violating_fields == ()


def test_enforce_discovery_fields_rejects_unpermitted_populated_field() -> None:
    registry = PermissionRegistry(
        [_permission(permitted_fields=frozenset({"title", "canonical_reference", "content_type"}))]
    )
    decision = registry.enforce_discovery_fields(_discovery_result())
    assert decision.allowed is False
    assert decision.reason_code == FieldEnforcementReasonCode.field_not_permitted
    # publication_date and summary_normalized are populated but not permitted
    assert "publication_date" in decision.violating_fields
    assert "summary_normalized" in decision.violating_fields


def test_enforce_discovery_fields_does_not_flag_empty_optional_fields() -> None:
    """A field the permission omits is fine as long as the result carries no
    content for it -- rationale: a silently stripped publication_date would
    mis-window an item with no visible symptom, but an ABSENT date that was
    never populated in the first place is not a violation."""
    registry = PermissionRegistry(
        [_permission(permitted_fields=frozenset({"title", "canonical_reference", "content_type"}))]
    )
    result = DiscoveryResult(
        source_id="src_vendor_alpha",
        source_group="grp_vendor_alpha",
        title="Vendor Alpha update",
        publication_date=None,
        canonical_reference="https://example.com/a",
        summary_normalized="",
        summary_provenance=SummaryProvenance.system_derived,
        content_type="text/html",
        retrieved_at=_NOW,
        provenance=ProvenanceMetadata(
            adapter_name="synthetic", discovery_run_id="run_1", discovered_at=_NOW
        ),
        permission_ref=PermissionRef(
            source_id="src_vendor_alpha", approved_mode="discovery", status_at_discovery="approved"
        ),
    )
    decision = registry.enforce_discovery_fields(result)
    assert decision.allowed is True


def test_enforce_discovery_fields_rejects_non_approved_status_even_with_permitted_fields() -> None:
    """Gate D round-1 correction (B1): enforce_discovery_fields independently
    re-checks status == approved -- so it can never disagree with authorize()
    having already gated the same source, even if permitted_fields would
    otherwise allow every field the result populates."""
    registry = PermissionRegistry([_permission(status=PermissionStatus.revoked)])
    decision = registry.enforce_discovery_fields(_discovery_result())
    assert decision.allowed is False
    assert decision.reason_code == FieldEnforcementReasonCode.status_not_approved


def test_enforce_discovery_fields_unregistered_source_rejected() -> None:
    registry = PermissionRegistry([])
    decision = registry.enforce_discovery_fields(_discovery_result())
    assert decision.allowed is False
    assert decision.reason_code == FieldEnforcementReasonCode.not_registered


def test_enforce_discovery_fields_never_mutates_the_result() -> None:
    """Enforcement is REJECT, never a silent strip -- proven by showing the
    DiscoveryResult passed in is untouched after a rejecting enforcement
    call."""
    registry = PermissionRegistry(
        [_permission(permitted_fields=frozenset({"title", "canonical_reference", "content_type"}))]
    )
    result = _discovery_result()
    before = result.model_dump_json()
    registry.enforce_discovery_fields(result)
    after = result.model_dump_json()
    assert before == after


# --- SourcePermission / registry construction shape ------------------------


def test_source_permission_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SourcePermission(
            **_permission().model_dump(),
            unexpected_field="nope",  # type: ignore[call-arg]
        )


def test_permission_registry_rejects_duplicate_source_id() -> None:
    permission = _permission()
    with pytest.raises(ValueError, match="duplicate"):
        PermissionRegistry([permission, permission])


# --- get(): the authoritative SourcePermission, for B1's permission_ref fix -


def test_get_returns_the_raw_source_permission() -> None:
    permission = _permission(source_id="src_vendor_alpha")
    registry = PermissionRegistry([permission])
    assert registry.get("src_vendor_alpha") == permission


def test_get_returns_none_for_unregistered_source() -> None:
    registry = PermissionRegistry([])
    assert registry.get("src_unknown") is None


# --- B3 (Gate D round-1 correction): SourcePermission is frozen ------------


def test_source_permission_is_frozen() -> None:
    permission = _permission()
    with pytest.raises(ValidationError):
        permission.status = PermissionStatus.revoked  # type: ignore[misc]
