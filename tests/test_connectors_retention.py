"""Tests for content_machine.connectors.retention (Gate D §6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from content_machine.connectors.retention import (
    RETENTION_POLICIES,
    DisposalRecord,
    RetentionClass,
)


def test_every_retention_class_has_exactly_one_policy() -> None:
    assert set(RETENTION_POLICIES.keys()) == set(RetentionClass)
    for retention_class, policy in RETENTION_POLICIES.items():
        assert policy.retention_class == retention_class


def test_temporary_full_content_is_never_persistable_and_must_be_disposed() -> None:
    policy = RETENTION_POLICIES[RetentionClass.temporary_full_content]
    assert policy.persistable is False
    assert policy.disposal_required is True


@pytest.mark.parametrize(
    "retention_class",
    [
        RetentionClass.metadata,
        RetentionClass.normalized_summary,
        RetentionClass.extraction_artifact,
        RetentionClass.audit_log,
        RetentionClass.error_record,
    ],
)
def test_non_temporary_classes_are_persistable_without_mandatory_disposal(
    retention_class: RetentionClass,
) -> None:
    policy = RETENTION_POLICIES[retention_class]
    assert policy.persistable is True
    assert policy.disposal_required is False


def test_disposal_record_has_no_body_field() -> None:
    field_names = set(DisposalRecord.model_fields)
    for forbidden in ("body", "raw", "content"):
        assert forbidden not in field_names


def test_disposal_record_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        DisposalRecord(
            canonical_reference="https://example.com/a",
            retention_class=RetentionClass.temporary_full_content,
            byte_count=10,
            disposed=True,
            reason="minimized",
            unexpected_field="nope",  # type: ignore[call-arg]
        )


def test_disposal_record_byte_count_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        DisposalRecord(
            canonical_reference="https://example.com/a",
            retention_class=RetentionClass.temporary_full_content,
            byte_count=-1,
            disposed=True,
            reason="minimized",
        )
