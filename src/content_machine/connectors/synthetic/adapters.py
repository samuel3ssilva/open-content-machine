"""The seven synthetic, network-free adapters required by spec §9, plus the
common base they share. Every adapter is deterministic: no ``time.time``, no
``datetime.now``/``today``/``utcnow``, no ``random``, no ``uuid4``, no
``hash()`` on strings, and no set-iteration order feeding serialized output
(rule F) -- every timestamp is either a fixed fixture constant
(``fixtures.FIXED_RETRIEVED_AT``) or a value the caller supplies at
construction time.

Each adapter implements :class:`content_machine.connectors.runner.ConnectorAdapter`
(a ``source_id`` attribute + ``discover(request) -> AdapterDiscoveryOutcome``)
so :func:`content_machine.connectors.runner.run_discovery` can drive any of
them without knowing they are synthetic. A SUCCESS adapter returns an
``AdapterDiscoveryOutcome``; a FAILURE adapter raises
:class:`content_machine.connectors.runner.ConnectorAdapterError` -- there is
no third channel.

Raw hostile/benign text lives in :mod:`content_machine.connectors.synthetic.fixtures`
(see that module's CI-safe encoding rule); every adapter that constructs a
``DiscoveryResult`` from raw text runs it through
:func:`content_machine.connectors.sanitize.sanitize_text` first -- this
module never puts unsanitized text into a ``DiscoveryResult`` field.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from content_machine.connectors.failures import FailureKind
from content_machine.connectors.models import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    DiscoveryRequest,
    DiscoveryResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
)
from content_machine.connectors.runner import AdapterDiscoveryOutcome, ConnectorAdapterError
from content_machine.connectors.sanitize import SecurityFlag, sanitize_text
from content_machine.connectors.synthetic import fixtures

_DEFAULT_MAX_REDIRECTS = 3


def redirect_chain_flags(
    chain: Sequence[str], *, max_redirects: int = _DEFAULT_MAX_REDIRECTS
) -> tuple[SecurityFlag, ...]:
    """Pure, deterministic check of a redirect chain's HOP COUNT against
    ``max_redirects``. ``chain`` is plain DATA (a tuple of hop labels) --
    this function never resolves, fetches, or follows any of them; there is
    no network-capable code anywhere in this module (see the no-network
    tests). A chain of N URLs has N-1 hops (the first entry is the starting
    reference, not a redirect)."""
    hop_count = max(len(chain) - 1, 0)
    if hop_count > max_redirects:
        return (SecurityFlag.redirect_chain_exceeded,)
    return ()


@dataclass(frozen=True)
class SyntheticItemSpec:
    """One raw (pre-sanitization) item a :class:`_BaseSyntheticAdapter`
    subclass can turn into a :class:`DiscoveryResult`. Plain, frozen
    ``dataclass`` (not Pydantic) -- this is adapter-construction plumbing
    local to ``synthetic/``, never itself serialized."""

    raw_title: str
    raw_summary: str
    canonical_reference: str
    publication_date: date | None = None
    content_type: str = "text/html"
    summary_provenance: SummaryProvenance = SummaryProvenance.system_derived
    extra_flags: tuple[SecurityFlag, ...] = field(default_factory=tuple)


class _BaseSyntheticAdapter:
    """Shared scaffolding for the seven synthetic adapters: a fixed,
    caller-supplied identity, a shared ``retrieved_at``, and a helper that
    sanitizes raw text into one ``DiscoveryResult``. Intentionally private
    (module-local) -- the spec requires the seven PUBLIC subclass names, not
    a public base."""

    def __init__(
        self,
        source_id: str,
        *,
        source_group: str = "synthetic",
        discovery_run_id: str = fixtures.FIXED_DISCOVERY_RUN_ID,
        retrieved_at: datetime = fixtures.FIXED_RETRIEVED_AT,
        approved_mode: str = "discovery",
        status_at_discovery: str = "approved",
    ) -> None:
        self.source_id = source_id
        self._source_group = source_group
        self._discovery_run_id = discovery_run_id
        self._retrieved_at = retrieved_at
        self._approved_mode = approved_mode
        self._status_at_discovery = status_at_discovery

    def _provenance(self) -> ProvenanceMetadata:
        return ProvenanceMetadata(
            adapter_name=type(self).__name__,
            discovery_run_id=self._discovery_run_id,
            discovered_at=self._retrieved_at,
        )

    def _permission_ref(self) -> PermissionRef:
        return PermissionRef(
            source_id=self.source_id,
            approved_mode=self._approved_mode,
            status_at_discovery=self._status_at_discovery,
        )

    def _build_result(self, spec: SyntheticItemSpec) -> DiscoveryResult:
        title_clean = sanitize_text(spec.raw_title, max_chars=TITLE_MAX_CHARS)
        summary_clean = sanitize_text(spec.raw_summary, max_chars=SUMMARY_MAX_CHARS)
        flags = tuple(
            sorted(
                set(title_clean.flags) | set(summary_clean.flags) | set(spec.extra_flags),
                key=lambda flag: flag.value,
            )
        )
        title_text = title_clean.text or "(untitled synthetic item)"
        return DiscoveryResult(
            source_id=self.source_id,
            source_group=self._source_group,
            title=title_text,
            publication_date=spec.publication_date,
            canonical_reference=spec.canonical_reference,
            summary_normalized=summary_clean.text,
            summary_provenance=spec.summary_provenance,
            content_type=spec.content_type,
            retrieved_at=self._retrieved_at,
            provenance=self._provenance(),
            permission_ref=self._permission_ref(),
            security_flags=flags,
        )


# --- 1. success ---------------------------------------------------------


class SuccessfulSyntheticAdapter(_BaseSyntheticAdapter):
    """Returns one or more benign (or caller-supplied) items with zero
    security flags -- the baseline "everything worked" adapter. Also used,
    with an explicit ``items`` override, to construct the duplicate-
    canonical-reference / conflicting-publication-date batch-level scenario
    (see ``fixtures.DUPLICATE_REFERENCE_A``)."""

    def __init__(
        self,
        source_id: str,
        *,
        items: Sequence[SyntheticItemSpec] | None = None,
        source_group: str = "synthetic",
        discovery_run_id: str = fixtures.FIXED_DISCOVERY_RUN_ID,
        retrieved_at: datetime = fixtures.FIXED_RETRIEVED_AT,
        approved_mode: str = "discovery",
        status_at_discovery: str = "approved",
    ) -> None:
        super().__init__(
            source_id,
            source_group=source_group,
            discovery_run_id=discovery_run_id,
            retrieved_at=retrieved_at,
            approved_mode=approved_mode,
            status_at_discovery=status_at_discovery,
        )
        self._items = tuple(items) if items is not None else (
            SyntheticItemSpec(
                raw_title=fixtures.BENIGN_TITLE,
                raw_summary=fixtures.BENIGN_SUMMARY,
                canonical_reference=fixtures.BENIGN_CANONICAL_REFERENCE,
                publication_date=date(2026, 7, 13),
                content_type="text/html",
                summary_provenance=SummaryProvenance.system_derived,
            ),
        )

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        results = tuple(self._build_result(spec) for spec in self._items)
        return AdapterDiscoveryOutcome(results=results, dropped_count=0, truncated=False)


# --- 2. timeout -----------------------------------------------------------


class TimeoutSyntheticAdapter(_BaseSyntheticAdapter):
    """Simulates the source timing out before returning anything."""

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        raise ConnectorAdapterError(
            FailureKind.timeout,
            "synthetic adapter simulated a discovery timeout",
            retry_eligible=True,
        )


# --- 3. rate limited --------------------------------------------------------


class RateLimitedSyntheticAdapter(_BaseSyntheticAdapter):
    """Simulates the source rejecting the request as rate-limited."""

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        raise ConnectorAdapterError(
            FailureKind.rate_limited,
            "synthetic adapter simulated a rate-limit rejection",
            retry_eligible=True,
        )


# --- 4. malicious content ----------------------------------------------


class MaliciousContentSyntheticAdapter(_BaseSyntheticAdapter):
    """Returns items built from EVERY hostile-text fixture (markup, prompt
    injection, hidden instructions, embedded credential, email-shaped,
    filesystem-path-shaped, vendor marketing) -- each sanitized by
    :meth:`_build_result` exactly like a real adapter's output would be, so
    the sanitizer is exercised on genuinely hostile input rather than a
    stand-in."""

    _RAW_SPECS: tuple[tuple[str, str, str], ...] = (
        (fixtures.HOSTILE_SCRIPT_MARKUP, "", fixtures.HOSTILE_SCRIPT_MARKUP_REFERENCE),
        (
            fixtures.HOSTILE_PROMPT_INJECTION,
            "",
            fixtures.HOSTILE_PROMPT_INJECTION_REFERENCE,
        ),
        (
            fixtures.HOSTILE_HIDDEN_INSTRUCTION,
            "",
            fixtures.HOSTILE_HIDDEN_INSTRUCTION_REFERENCE,
        ),
        (
            "VendorDelta Support Thread",
            fixtures.HOSTILE_CREDENTIAL_PROSE,
            fixtures.HOSTILE_CREDENTIAL_REFERENCE,
        ),
        (
            "VendorEpsilon Release Support",
            fixtures.HOSTILE_EMAIL_PROSE,
            fixtures.HOSTILE_EMAIL_REFERENCE,
        ),
        (
            "VendorZeta Changelog",
            fixtures.HOSTILE_PATH_PROSE,
            fixtures.HOSTILE_PATH_REFERENCE,
        ),
        (
            fixtures.HOSTILE_MARKETING_COPY,
            "",
            fixtures.HOSTILE_MARKETING_REFERENCE,
        ),
    )

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        results = []
        for raw_title, raw_summary, reference in self._RAW_SPECS:
            spec = SyntheticItemSpec(
                raw_title=raw_title,
                raw_summary=raw_summary or "synthetic hostile-content fixture item",
                canonical_reference=reference,
                publication_date=date(2026, 7, 15),
                content_type="text/html",
                summary_provenance=SummaryProvenance.source_supplied,
            )
            results.append(self._build_result(spec))
        return AdapterDiscoveryOutcome(results=tuple(results), dropped_count=0, truncated=False)


# --- 5. oversized / malformed / unsupported-mime content --------------------


class OversizedContentSyntheticAdapter(_BaseSyntheticAdapter):
    """Simulates three distinct "the content itself is problematic" cases,
    selected by ``scenario`` at construction time:

    * ``"oversized"`` (default): a title/summary far longer than
      ``TITLE_MAX_CHARS``/``SUMMARY_MAX_CHARS`` -- ``sanitize_text`` truncates
      and flags ``oversized_truncated``.
    * ``"malformed_encoding"``: text containing the Unicode replacement
      character, surviving sanitization (flagged, not raised).
    * ``"unsupported_mime"``: the source claims a content type outside
      ``ALLOWED_CONTENT_TYPES`` -- raised as a
      ``FailureKind.unsupported_content`` failure (the allowlist is enforced
      by ``DiscoveryResult`` itself; an adapter that would violate it must
      never attempt construction, so this is a discovery-time failure, not a
      validation error).
    """

    def __init__(
        self,
        source_id: str,
        *,
        scenario: Literal["oversized", "malformed_encoding", "unsupported_mime"] = "oversized",
        source_group: str = "synthetic",
        discovery_run_id: str = fixtures.FIXED_DISCOVERY_RUN_ID,
        retrieved_at: datetime = fixtures.FIXED_RETRIEVED_AT,
        approved_mode: str = "discovery",
        status_at_discovery: str = "approved",
    ) -> None:
        super().__init__(
            source_id,
            source_group=source_group,
            discovery_run_id=discovery_run_id,
            retrieved_at=retrieved_at,
            approved_mode=approved_mode,
            status_at_discovery=status_at_discovery,
        )
        self._scenario = scenario

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        if self._scenario == "unsupported_mime":
            raise ConnectorAdapterError(
                FailureKind.unsupported_content,
                "synthetic adapter reported a content type outside the allowlist",
                retry_eligible=False,
            )

        if self._scenario == "malformed_encoding":
            spec = SyntheticItemSpec(
                raw_title="VendorIota Release Notes",
                raw_summary=fixtures.HOSTILE_MALFORMED_ENCODING_TEXT,
                canonical_reference=fixtures.HOSTILE_MALFORMED_ENCODING_REFERENCE,
                publication_date=date(2026, 7, 16),
                summary_provenance=SummaryProvenance.source_supplied,
            )
        else:
            spec = SyntheticItemSpec(
                raw_title="VendorTheta Feed Entry",
                raw_summary=fixtures.HOSTILE_OVERSIZED_TEXT,
                canonical_reference=fixtures.HOSTILE_OVERSIZED_REFERENCE,
                publication_date=date(2026, 7, 17),
                summary_provenance=SummaryProvenance.source_supplied,
            )
        return AdapterDiscoveryOutcome(results=(self._build_result(spec),))


# --- 6. revoked permission ---------------------------------------------


class RevokedPermissionSyntheticAdapter(_BaseSyntheticAdapter):
    """Simulates the SOURCE ITSELF reporting, mid-discovery, that access has
    been revoked -- distinct from the LOCAL permission registry rejecting a
    source before the adapter is ever called (that path is exercised with
    any adapter, e.g. :class:`SuccessfulSyntheticAdapter`, paired with a
    ``SourcePermission`` whose ``status`` is ``revoked`` in
    ``tests/test_connectors_runner.py``)."""

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        raise ConnectorAdapterError(
            FailureKind.permission_revoked,
            "synthetic adapter reported its access was revoked mid-discovery",
            retry_eligible=False,
        )


# --- 7. partial batch -----------------------------------------------------


class PartialBatchSyntheticAdapter(_BaseSyntheticAdapter):
    """Simulates an adapter that breaks PARTWAY through producing its
    source's batch. Per the runner module docstring's assumption 4, whatever
    partial, in-process results the adapter may have accumulated are
    discarded entirely -- they are never surfaced, because a partial,
    unbounded-provenance result set cannot be trusted to represent "what
    happened". This source therefore contributes zero results and exactly
    one ``SourceFailure`` for this run."""

    def discover(self, request: DiscoveryRequest) -> AdapterDiscoveryOutcome:
        raise ConnectorAdapterError(
            FailureKind.partial_batch,
            "synthetic adapter failed partway through producing this source's batch",
            retry_eligible=True,
        )
