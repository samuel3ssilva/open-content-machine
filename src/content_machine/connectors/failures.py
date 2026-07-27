"""Failure taxonomy and per-source failure record (Gate D §8, contracts only).

This module defines the closed set of ways a source can fail and the record
shape for one such failure. It intentionally does NOT include
``run_discovery``/batch orchestration -- that is ``runner.py``, a later Gate D
commit -- so nothing here has retry logic, isolation logic, or coverage
reporting; it is contracts only.

**No automatic retry anywhere.** ``retry_eligible`` is advisory metadata for a
future, explicitly-triggered manual re-run. Nothing in this gate reads it to
schedule or perform a retry.

**Gate D round-1 security correction (B1).** ``FailureKind.source_id_mismatch``
was added after a Fable security review proved an adapter could report a
``DiscoveryResult.source_id`` different from its own registered ``source_id``
and have the mismatch go completely unchecked (a permission-bypass path --
see ``runner.run_discovery``'s identity-binding check and
``docs/adr/0005-connector-security-foundation.md``'s round-1 findings
section). This is the one addition to the taxonomy since Gate D's original
commit; every other member is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FailureKind(StrEnum):
    """The closed set of ways discovery or verification of one source can fail."""

    timeout = "timeout"
    rate_limited = "rate_limited"
    unavailable = "unavailable"
    invalid_response = "invalid_response"
    unsupported_content = "unsupported_content"
    extraction_failure = "extraction_failure"
    permission_revoked = "permission_revoked"
    partial_batch = "partial_batch"
    #: Gate D round-1 correction (B1): an adapter returned at least one
    #: DiscoveryResult whose source_id did not match the adapter's own
    #: registered source_id. Treated as adapter compromise, never a
    #: retry-eligible transient condition.
    source_id_mismatch = "source_id_mismatch"


class SourceFailure(BaseModel):
    """One source's failure within a run.

    ``sanitized_reason`` MUST already have passed through
    :func:`content_machine.connectors.sanitize.sanitize_error` (or an
    equivalent scrub) before construction -- this model does not sanitize its
    own input, it only bounds the length of an already-safe string.

    Frozen (Gate D round-1 correction, B3): a persisted audit-adjacent record
    must not be mutable after construction, including via ``model_copy()``
    with ``update=``, which bypasses field validation entirely.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    kind: FailureKind
    sanitized_reason: str = Field(max_length=200)
    retry_eligible: bool
    occurred_at: datetime
