"""Connector Security Foundation (Gate D) -- contracts and safety scaffold
for future external sources (RSS, email digests, vendor feeds, etc.).

Nothing in this package performs network I/O in this gate: it ships
contracts, a permission model, a retention policy, a sanitizer, and a
failure taxonomy only. ``connectors`` is a sibling of
:mod:`content_machine.providers` and will be the ONLY package permitted to
hold network I/O in a future gate; nothing shipped here has a vendor or
network SDK import, lazy or otherwise.

Dependency direction (to be recorded in ADR 0005 in a later Gate D commit):
``connectors`` may import :mod:`content_machine.config` and, narrowly,
:mod:`content_machine.intelligence.models` (shared taxonomy types such as
``SourceType``/``EvidenceType``, reused rather than duplicated).
:mod:`content_machine.intelligence.normalize` is reserved for ``bridge.py``
(a later commit). Nothing in ``content_machine.intelligence``,
``content_machine.audience``, or ``content_machine.privacy`` may import
``connectors`` -- Gate D is additive and must never change ranking or
rendering behavior.

Gate D shipped in three commits -- the contract layer (``models``,
``registry``, ``permissions``, ``retention``, ``sanitize``, ``failures``);
the synthetic adapter harness, batch runner, and pipeline bridge
(``runner.py``, ``bridge.py``, ``synthetic/``); and this package's docs and
ADR -- all of which have now shipped (Gate D round-2 correction, D2: this
docstring previously described commit 1 only and called ``runner.py``/
``bridge.py``/``synthetic/`` future work, which they no longer are). This
``__init__`` exports the primary contract, permission, retention, sanitize,
and workflow (``runner``/``bridge``) surface -- the models, decisions, and
entry points a caller outside this package needs to run a discovery batch
and hand its results to the intelligence pipeline. It does NOT re-export
every internal helper or narrow validation error defined in the submodules
(e.g. ``bridge.derive_item_id`` or ``bridge``'s per-field ``ValueError``
subclasses); those remain reachable via their owning submodule for callers
who need them, without cluttering this package's public surface.
"""

from __future__ import annotations

from content_machine.connectors.bridge import (
    AssessmentProvenance,
    AuthoredAssessment,
    MissingReviewerNoteError,
    UnreviewedSecurityFlagsError,
    to_source_item,
    to_source_item_with_audit,
)
from content_machine.connectors.failures import FailureKind, SourceFailure
from content_machine.connectors.models import (
    ALLOWED_CONTENT_TYPES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ITEMS_PER_SOURCE,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MAX_REQUESTS_PER_RUN,
    DEFAULT_TIMEOUT_SECONDS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    AuditEventKind,
    ConnectorAuditEvent,
    ContentDisposedError,
    DiscoveryRequest,
    DiscoveryResult,
    ExtractionResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
    TemporaryContentHandle,
    TriageCandidate,
    VerificationOutcome,
    VerificationRequest,
    VerificationUpgrade,
    triage,
)
from content_machine.connectors.permissions import (
    AuthorizationDecision,
    AuthorizationReasonCode,
    FieldEnforcementDecision,
    FieldEnforcementReasonCode,
    PermissionRegistry,
    PermissionStatus,
    SourceMode,
    SourcePermission,
)
from content_machine.connectors.registry import (
    DuplicateSourceIdError,
    PublisherClassification,
    SourceRegistry,
    SourceRegistryEntry,
    UnknownSourceError,
)
from content_machine.connectors.retention import (
    RETENTION_POLICIES,
    DisposalRecord,
    RetentionClass,
    RetentionPolicy,
)
from content_machine.connectors.runner import (
    BatchDiscoveryRequest,
    BatchDiscoveryResult,
    ConnectorAdapter,
    ConnectorAdapterError,
    SourceCoverage,
    SourceCoverageReport,
    format_coverage_report,
    run_discovery,
)
from content_machine.connectors.sanitize import (
    SanitizedText,
    SecurityFlag,
    sanitize_error,
    sanitize_text,
)

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ITEMS_PER_SOURCE",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_REQUESTS_PER_RUN",
    "DEFAULT_TIMEOUT_SECONDS",
    "RETENTION_POLICIES",
    "SUMMARY_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "AssessmentProvenance",
    "AuditEventKind",
    "AuthoredAssessment",
    "AuthorizationDecision",
    "AuthorizationReasonCode",
    "BatchDiscoveryRequest",
    "BatchDiscoveryResult",
    "ConnectorAdapter",
    "ConnectorAdapterError",
    "ConnectorAuditEvent",
    "ContentDisposedError",
    "DiscoveryRequest",
    "DiscoveryResult",
    "DisposalRecord",
    "DuplicateSourceIdError",
    "ExtractionResult",
    "FailureKind",
    "FieldEnforcementDecision",
    "FieldEnforcementReasonCode",
    "MissingReviewerNoteError",
    "PermissionRef",
    "PermissionRegistry",
    "PermissionStatus",
    "ProvenanceMetadata",
    "PublisherClassification",
    "RetentionClass",
    "RetentionPolicy",
    "SanitizedText",
    "SecurityFlag",
    "SourceCoverage",
    "SourceCoverageReport",
    "SourceFailure",
    "SourceMode",
    "SourcePermission",
    "SourceRegistry",
    "SourceRegistryEntry",
    "SummaryProvenance",
    "TemporaryContentHandle",
    "TriageCandidate",
    "UnknownSourceError",
    "UnreviewedSecurityFlagsError",
    "VerificationOutcome",
    "VerificationRequest",
    "VerificationUpgrade",
    "format_coverage_report",
    "run_discovery",
    "sanitize_error",
    "sanitize_text",
    "to_source_item",
    "to_source_item_with_audit",
    "triage",
]
