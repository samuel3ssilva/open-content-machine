"""Tests for content_machine.connectors.failures (Gate D §8, contracts only:
no runner.py / no retry orchestration ships in this commit)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from content_machine.connectors.failures import FailureKind, SourceFailure

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def test_failure_kind_has_the_spec_taxonomy() -> None:
    """``source_id_mismatch`` is the one addition since Gate D's original
    commit (round-1 security correction, B1) -- see failures.py's module
    docstring."""
    assert {member.value for member in FailureKind} == {
        "timeout",
        "rate_limited",
        "unavailable",
        "invalid_response",
        "unsupported_content",
        "extraction_failure",
        "permission_revoked",
        "partial_batch",
        "source_id_mismatch",
    }


def test_source_failure_construction() -> None:
    failure = SourceFailure(
        source_id="src_vendor_alpha",
        kind=FailureKind.timeout,
        sanitized_reason="request exceeded the configured timeout",
        retry_eligible=True,
        occurred_at=_NOW,
    )
    assert failure.kind == FailureKind.timeout
    assert failure.retry_eligible is True


def test_source_failure_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SourceFailure(
            source_id="src_vendor_alpha",
            kind=FailureKind.timeout,
            sanitized_reason="timed out",
            retry_eligible=True,
            occurred_at=_NOW,
            unexpected_field="nope",  # type: ignore[call-arg]
        )


def test_source_failure_sanitized_reason_is_length_capped() -> None:
    with pytest.raises(ValidationError):
        SourceFailure(
            source_id="src_vendor_alpha",
            kind=FailureKind.unavailable,
            sanitized_reason="x" * 500,
            retry_eligible=False,
            occurred_at=_NOW,
        )


# --- B3 (Gate D round-1 correction): frozen, so post-construction mutation
# -- including the model_copy(update=...) bypass -- raises ------------------


def test_source_failure_is_frozen_direct_mutation_raises() -> None:
    failure = SourceFailure(
        source_id="src_vendor_alpha",
        kind=FailureKind.timeout,
        sanitized_reason="request exceeded the configured timeout",
        retry_eligible=True,
        occurred_at=_NOW,
    )
    with pytest.raises(ValidationError):
        failure.kind = FailureKind.unavailable  # type: ignore[misc]
