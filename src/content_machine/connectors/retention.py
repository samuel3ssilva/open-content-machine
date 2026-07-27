"""Retention classes, policy defaults, and disposal records (Gate D §6).

Every value a connector ever produces belongs to exactly one
:class:`RetentionClass`, and that class alone decides whether the value may
be persisted and whether disposal is required. This module is deliberately
tiny and has no dependency on the rest of ``connectors`` -- it is the shared
vocabulary that :mod:`content_machine.connectors.models` (``DisposalRecord``
usage inside ``TemporaryContentHandle``) and, in a later commit,
``runner.py``/``bridge.py`` build on.

**No-TTL statement (must also land in ADR 0005 in a later commit).** Audit
and error rows are persistable with no time-to-live in this gate: they
accumulate exactly like the existing Intelligence Brief library's audit
trail (``intelligence.library.AuditRow``). This is a deliberate choice, not
an oversight -- silence here would be a real gap, so it is stated explicitly
rather than left implicit.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RetentionClass(StrEnum):
    """The closed set of value classes a connector can ever produce."""

    metadata = "metadata"
    normalized_summary = "normalized_summary"
    temporary_full_content = "temporary_full_content"
    extraction_artifact = "extraction_artifact"
    audit_log = "audit_log"
    error_record = "error_record"


class RetentionPolicy(BaseModel):
    """One retention class's persistence and disposal rule. Frozen (Gate D
    round-1 correction, B3): the policy table is a fixed constant, never
    mutated after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retention_class: RetentionClass
    persistable: bool
    disposal_required: bool


#: The full, explicit policy table -- a constant, never computed. Every
#: :class:`RetentionClass` member MUST appear here exactly once (see
#: ``test_connectors_retention.py::test_every_retention_class_has_a_policy``).
#:
#: - ``metadata`` / ``normalized_summary``: small, body-free, persistable
#:   indefinitely (no TTL in this gate, see module docstring).
#: - ``temporary_full_content``: the one class that may NEVER be persisted --
#:   raw retrieved content lives only inside a
#:   ``models.TemporaryContentHandle`` and must be disposed after
#:   minimization.
#: - ``extraction_artifact``: the body-free result of minimizing temporary
#:   content (``models.ExtractionResult``); persistable, no raw body to
#:   dispose of.
#: - ``audit_log`` / ``error_record``: persistable, no TTL in this gate, and
#:   -- by construction of ``models.ConnectorAuditEvent`` and
#:   ``sanitize.sanitize_error`` -- never carry a source body.
RETENTION_POLICIES: dict[RetentionClass, RetentionPolicy] = {
    RetentionClass.metadata: RetentionPolicy(
        retention_class=RetentionClass.metadata, persistable=True, disposal_required=False
    ),
    RetentionClass.normalized_summary: RetentionPolicy(
        retention_class=RetentionClass.normalized_summary,
        persistable=True,
        disposal_required=False,
    ),
    RetentionClass.temporary_full_content: RetentionPolicy(
        retention_class=RetentionClass.temporary_full_content,
        persistable=False,
        disposal_required=True,
    ),
    RetentionClass.extraction_artifact: RetentionPolicy(
        retention_class=RetentionClass.extraction_artifact,
        persistable=True,
        disposal_required=False,
    ),
    RetentionClass.audit_log: RetentionPolicy(
        retention_class=RetentionClass.audit_log, persistable=True, disposal_required=False
    ),
    RetentionClass.error_record: RetentionPolicy(
        retention_class=RetentionClass.error_record, persistable=True, disposal_required=False
    ),
}


class DisposalRecord(BaseModel):
    """Proof that a piece of ``temporary_full_content`` was disposed of.

    Carries no body, ever -- only ``byte_count`` (a size, not the content)
    and a short ``reason``. Frozen (Gate D round-1 correction, B3): a
    disposal record is evidence of a past event and must never be mutated
    after construction, including via ``model_copy(update=...)``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_reference: str
    retention_class: RetentionClass
    byte_count: int = Field(ge=0)
    disposed: bool
    reason: str = Field(max_length=200)
