"""Discovery, verification, triage, and audit contracts (Gate D §2, §5).

This is the base contracts layer of ``connectors``: :mod:`registry`,
:mod:`permissions`, and :mod:`failures` all import FROM this module (or are
imported BY it, for :mod:`sanitize` and :mod:`retention` only); this module
never imports :mod:`registry`, :mod:`permissions`, or :mod:`failures`, so the
package has one clean dependency direction and no cycles.

Reuses :data:`content_machine.intelligence.models.EvidenceType` in
:class:`VerificationUpgrade` rather than inventing a parallel evidence
vocabulary -- a second, connector-local taxonomy for the exact same concept
is precisely the "second ranking system" the spec warns ``triage()`` away
from becoming. This is a read-only import of a plain ``Literal`` alias;
nothing in ``intelligence`` imports ``connectors``.

Three families of contract live here:

* **Bounded discovery** -- :class:`DiscoveryRequest` / :class:`DiscoveryResult`.
  ``DiscoveryResult`` is an allowlist with NO body/raw-content field: full-body
  persistence at discovery time is structurally impossible, not merely
  discouraged, because the field does not exist on the model.
* **Deep verification** -- :class:`VerificationRequest`,
  :class:`TemporaryContentHandle` (transient, in-memory-only, disposed via
  ``minimize()``), :class:`ExtractionResult`, :class:`VerificationUpgrade`,
  :class:`VerificationOutcome`. **Honest scope statement (Gate D round-2
  correction, D2):** Gate D ships ``VerificationUpgrade`` as a typed,
  bounded CONTRACT only -- a value an ``ExtractionResult`` can carry -- with
  NO application path anywhere in this codebase. Earlier text here claimed
  it "is applied to the AUTHORED ASSESSMENT before bridging (in a later
  commit's ``bridge.py``)"; no such application code exists, ``bridge.py``
  has zero references to ``VerificationUpgrade``, and no
  ``apply_upgrade``-shaped function is being added in this correction round
  (the tree is already security-approved, and the orchestrator's decision
  is not to add unreviewed admission-adjacent code after that approval).
  Wiring an upgrade into an ``AuthoredAssessment`` before it crosses
  :func:`bridge.to_source_item` is first-real-adapter-gate work, deferred
  alongside the other items in ADR 0005's deferred list. Whatever gate
  wires this must never mutate an already-ranked topic, an already-written
  brief, or any library entry -- verification happens strictly before a
  topic exists in the M1-M7 pipeline, never after.
* **Triage** -- :class:`TriageCandidate` / :func:`triage`, the deterministic
  product path that narrows N discovered items to K candidates worth a
  human's authoring time. Triage NEVER assigns evidence, tier, relevance, or
  any ranking field -- it only counts tag-token overlap.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from content_machine.connectors.retention import DisposalRecord, RetentionClass
from content_machine.intelligence.models import EvidenceType, SecurityFlag

# --- §2 pinned literal constants ---------------------------------------------
# Every constant below has a dedicated test asserting its LITERAL value in
# tests/test_connectors_contracts.py (Gate C precedent: `assert STALE_WEEKS ==
# 8`) -- a test that derives the expected value from the constant itself is a
# tautology and does not count.

#: Verification request default/ceiling for response size, in bytes.
DEFAULT_MAX_BYTES = 2_000_000
#: Verification request default/ceiling for retrieval timeout, in seconds.
DEFAULT_TIMEOUT_SECONDS = 20
#: Verification request default/ceiling for redirect hops.
DEFAULT_MAX_REDIRECTS = 3
#: Discovery request default/ceiling for items returned per source.
DEFAULT_MAX_ITEMS_PER_SOURCE = 50
#: Discovery request default/ceiling for total adapter requests in one run.
DEFAULT_MAX_REQUESTS_PER_RUN = 200
#: Max length of ``DiscoveryResult.summary_normalized`` -- aligns with
#: ``intelligence.library.NORMALIZED_SUMMARY_MAX_CHARS``.
SUMMARY_MAX_CHARS = 280
#: Max length of ``DiscoveryResult.title``.
TITLE_MAX_CHARS = 300
#: Max length of ``DiscoveryResult.canonical_reference`` (Gate D round-1
#: correction, C3): this field was previously unbounded, and it is the
#: sha256 input ``bridge.derive_item_id`` hashes and the value that persists
#: as ``SourceItem.stable_reference`` -- a 2,048-char cap comfortably covers
#: real-world URLs (RFC 7230/common browser limits sit well above typical
#: URLs, and this is a defensive ceiling, not a claim about what a "valid"
#: URL looks like) while bounding one adversarial source's contribution to
#: storage.
CANONICAL_REFERENCE_MAX_CHARS = 2_048
#: The closed allowlist of content types a connector may ever claim to have
#: discovered or verified.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
        "application/json",
        "text/html",
        "text/plain",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Any ASCII control character or whitespace (Gate D round-1 correction,
#: C3): a canonical_reference is a URL, which has no legitimate reason to
#: contain a control character or embedded whitespace/newline.
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\x00-\x20\x7f]")
#: The only schemes ``canonical_reference`` may ever declare (Gate D
#: round-1 correction, C3) -- this module has no fetch path, so this is a
#: shape/allowlist check, not a claim that the scheme is ever dereferenced.
_CANONICAL_REFERENCE_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase, alnum-only token set -- deliberately tiny and local rather
    than importing ``intelligence.normalize.normalize_text``, per this
    package's dependency rule that ``intelligence.normalize`` is reserved for
    ``bridge.py`` (a later commit)."""
    return frozenset(_TOKEN_RE.findall(text.lower()))


# --- provenance / permission reference (closed, scalar-only) ----------------


class ProvenanceMetadata(BaseModel):
    """Closed, scalar-only record of where and how a :class:`DiscoveryResult`
    was produced. Never a ``dict[str, Any]`` -- an open shape is exactly
    where a raw body could sneak back in. Frozen (Gate D round-1 correction,
    B3): a provenance snapshot, never mutated after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_name: str = Field(min_length=1, max_length=100)
    discovery_run_id: str = Field(min_length=1, max_length=100)
    discovered_at: datetime


class PermissionRef(BaseModel):
    """Closed, point-in-time snapshot of the permission that authorized one
    :class:`DiscoveryResult`.

    Deliberately stores ``approved_mode``/``status_at_discovery`` as plain,
    length-capped strings rather than the live ``SourceMode``/
    ``PermissionStatus`` enums from :mod:`content_machine.connectors.permissions`:
    this module is the base contracts layer that ``permissions.py`` imports
    FROM (for ``DiscoveryResult`` field enforcement and audit events), so
    importing ``permissions`` back into this module would create a cycle.
    This ref is an audit snapshot, not a live permission handle -- a caller
    that needs the live object looks it up in a ``PermissionRegistry`` by
    ``source_id``.

    Frozen (Gate D round-1 correction, B3): a point-in-time snapshot, never
    mutated after construction. Also see ``runner.run_discovery`` (B1): the
    runner now constructs this ref itself, from the AUTHORIZED
    ``PermissionRegistry`` entry, rather than trusting whatever an adapter
    self-reports on the ``DiscoveryResult`` it returns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    approved_mode: str = Field(min_length=1, max_length=20)
    status_at_discovery: str = Field(min_length=1, max_length=20)


# --- audit ---------------------------------------------------------------


class AuditEventKind(StrEnum):
    """The four event families ``ConnectorAuditEvent`` covers.

    One closed schema for all four (per spec §8) rather than a schema per
    family -- ``reason_code`` carries the family-specific machine code (e.g.
    an ``AuthorizationReasonCode`` or ``FailureKind`` value) as a plain
    string, since a single field cannot be typed against four different
    enums without a discriminated union this gate does not need.
    """

    permission = "permission"
    retention = "retention"
    failure = "failure"
    security = "security"


class ConnectorAuditEvent(BaseModel):
    """One audit row. Never carries a source body -- ``detail`` is a short,
    already-sanitized human-readable note, never raw content.

    Frozen (Gate D round-1 correction, B3): an audit row is a record of a
    past event and must never be mutable after construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    event_kind: AuditEventKind
    reason_code: str = Field(min_length=1, max_length=64)
    occurred_at: datetime
    detail: str = Field(default="", max_length=280)


# --- §5.1 bounded discovery request ------------------------------------------


class DiscoveryRequest(BaseModel):
    """Makes "bounded" mechanical: every discovery call is scoped to one
    source and one window, with explicit item/request ceilings.

    ``window_start``/``window_end`` follow the weekly window convention used
    elsewhere in this codebase: inclusive start, exclusive end.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: str
    window_start: date
    window_end: date
    max_items_per_source: int = Field(default=DEFAULT_MAX_ITEMS_PER_SOURCE, gt=0)
    max_requests: int = Field(default=DEFAULT_MAX_REQUESTS_PER_RUN, gt=0)

    @field_validator("window_end")
    @classmethod
    def _window_end_after_start(cls, window_end: date, info: ValidationInfo) -> date:
        window_start = info.data.get("window_start")
        if window_start is not None and window_end <= window_start:
            raise ValueError(
                "window_end must be strictly after window_start (inclusive start, exclusive end)"
            )
        return window_end


class SummaryProvenance(StrEnum):
    """Whether a discovery result's summary is the source's own words or was
    derived by the system -- so a vendor's own blurb is never rendered as
    system analysis."""

    source_supplied = "source_supplied"
    system_derived = "system_derived"


class DiscoveryResult(BaseModel):
    """One bounded, allowlisted fact-sheet about a discovered artifact.

    There is deliberately NO body/raw/content field: full-body persistence in
    discovery is structurally impossible, not merely discouraged, because the
    field does not exist on this model. ``provenance`` and ``permission_ref``
    are closed, scalar-only models -- never an open ``dict[str, Any]``, which
    is exactly where a body would come back in.

    No ``publisher_classification`` here: that is a property of the curated
    source, decided before any retrieval, and lives on
    ``registry.SourceRegistryEntry`` instead.

    Frozen (Gate D round-1 correction, B3): every bound above (length caps,
    the content_type allowlist, and canonical_reference's cap/scheme/
    control-character checks below) is enforced by CONSTRUCTION -- a
    validator that runs once, when the object is made. Frozen closes the
    other half of that story: without it, ``model_copy(update=...)`` (or
    plain attribute assignment) could set any field to an out-of-bound value
    AFTER construction with no validator ever running again. Enforced by
    construction plus immutability together, not by any single mechanism
    that would make an out-of-bound instance impossible to obtain in
    principle (``model_construct()`` still bypasses validation, as it does
    for every Pydantic model) -- see the round-1 findings section of
    ADR 0005 for why this is described this way rather than as "structural."
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_group: str
    title: str = Field(min_length=1, max_length=TITLE_MAX_CHARS)
    publication_date: date | None = None
    canonical_reference: str = Field(min_length=1, max_length=CANONICAL_REFERENCE_MAX_CHARS)
    summary_normalized: str = Field(default="", max_length=SUMMARY_MAX_CHARS)
    summary_provenance: SummaryProvenance
    content_type: str
    retrieved_at: datetime
    provenance: ProvenanceMetadata
    permission_ref: PermissionRef
    security_flags: tuple[SecurityFlag, ...] = Field(default_factory=tuple)

    @field_validator("content_type")
    @classmethod
    def _content_type_allowed(cls, content_type: str) -> str:
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError("content_type is not in the ALLOWED_CONTENT_TYPES allowlist")
        return content_type

    @field_validator("canonical_reference")
    @classmethod
    def _canonical_reference_is_bounded_and_well_formed(cls, value: str) -> str:
        """Gate D round-1 correction (C3): ``canonical_reference`` was
        previously unbounded and unsanitized -- unlike ``title`` and
        ``summary_normalized``, it never passes through
        ``sanitize.sanitize_text``. It is the sha256 input
        ``bridge.derive_item_id`` hashes and persists as
        ``SourceItem.stable_reference``, so it gets its own, narrower
        checks here rather than the general text sanitizer: length (the
        ``max_length`` on the field itself), an http/https scheme
        allowlist, and no embedded control or whitespace character.
        """
        if _CONTROL_OR_WHITESPACE_RE.search(value):
            raise ValueError(
                "canonical_reference must not contain control or whitespace characters"
            )
        scheme, sep, _rest = value.partition("://")
        if not sep or scheme.lower() not in _CANONICAL_REFERENCE_ALLOWED_SCHEMES:
            raise ValueError(
                "canonical_reference must use the http or https scheme "
                f"(one of {sorted(_CANONICAL_REFERENCE_ALLOWED_SCHEMES)})"
            )
        return value


# --- §5.3 deep verification ---------------------------------------------


class VerificationRequest(BaseModel):
    """A request to fetch and inspect ONE artifact beyond its discovery
    summary. ``retrieval_reason`` is required and non-empty after stripping
    -- an unreasoned deep fetch is not permitted to be constructed."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    canonical_reference: str
    retrieval_reason: str = Field(min_length=1)
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, gt=0)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    allowed_content_types: frozenset[str] = Field(default_factory=lambda: ALLOWED_CONTENT_TYPES)
    max_redirects: int = Field(default=DEFAULT_MAX_REDIRECTS, ge=0)

    @field_validator("retrieval_reason")
    @classmethod
    def _reason_non_empty_after_strip(cls, retrieval_reason: str) -> str:
        if not retrieval_reason.strip():
            raise ValueError("retrieval_reason must be non-empty after stripping whitespace")
        return retrieval_reason


class VerificationUpgrade(BaseModel):
    """Typed, bounded feedback from deep verification.

    **Honest scope statement (Gate D round-2 correction, D2):** this is a
    CONTRACT only in Gate D -- nothing in this codebase applies it to an
    ``AuthoredAssessment`` before bridging; ``bridge.py`` has zero
    references to ``VerificationUpgrade``. Wiring that application is
    first-real-adapter-gate work (see the module docstring's deep
    verification section and ADR 0005's deferred list), not something this
    correction round builds. Whatever future gate wires it must never
    mutate an already-ranked topic, an already-written brief, or any
    library entry -- verification happens strictly before a topic exists in
    the M1-M7 pipeline. Reuses ``intelligence.models.EvidenceType`` so a
    verified upgrade speaks the exact vocabulary ``ranking.py`` already
    understands, rather than a second, connector-local evidence taxonomy.
    """

    model_config = ConfigDict(extra="forbid")

    confirmed_evidence_type: EvidenceType | None = None
    claim_directly_verifiable_in_artifact: bool | None = None
    independent_of_subject: bool | None = None
    upgrade_reason: str = Field(max_length=500)


class ExtractionResult(BaseModel):
    """Minimum traceable fields from a verification fetch. No body, ever.

    Frozen (Gate D round-1 correction, B3): a persisted, body-free record of
    a past extraction, never mutated after construction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_reference: str
    content_type: str
    byte_count: int = Field(ge=0)
    extracted_at: datetime
    upgrade: VerificationUpgrade | None = None
    security_flags: tuple[SecurityFlag, ...] = Field(default_factory=tuple)


class VerificationOutcome(BaseModel):
    """The full, persistable result of one verification: the body-free
    extraction, proof of disposal, the audit trail, and any flags raised
    during the audit/disposal step itself (in addition to
    ``extraction.security_flags``, which covers flags raised during
    extraction). Frozen (Gate D round-1 correction, B3): a persisted result,
    never mutated after construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    extraction: ExtractionResult
    disposal: DisposalRecord
    audit_event: ConnectorAuditEvent
    security_flags: tuple[SecurityFlag, ...] = Field(default_factory=tuple)


class ContentDisposedError(RuntimeError):
    """Raised when code reads a :class:`TemporaryContentHandle` after disposal."""


class TemporaryContentHandle:
    """Transient, in-memory-only holder for verification content.

    Never written to disk by any code path in this gate. Usable as a context
    manager (disposes on ``__exit__`` if not already disposed) -- callers
    SHOULD always use ``with`` (see ``docs/connector-security.md``'s adapter
    guide) rather than calling ``dispose()``/``minimize()`` manually, so
    disposal still runs if an exception is raised while the handle is live.
    ``minimize()`` is the ONLY way to turn transient content into something
    persistable: it disposes of the raw bytes and returns the durable,
    body-free pair (:class:`ExtractionResult`, ``retention.DisposalRecord``).
    Reading ``.content`` after disposal raises :class:`ContentDisposedError`.

    **Disposal honesty (Gate D round-1 correction, C5).** The raw content is
    held in a mutable ``bytearray``, and ``.content`` returns a
    ``memoryview`` onto it rather than a ``bytes`` copy. This buys one real
    property immutable ``bytes`` cannot: ``dispose()``/``minimize()``
    overwrite the buffer's bytes IN PLACE before releasing the handle's own
    reference, so any ``memoryview`` a caller obtained from an earlier
    ``.content`` access reflects the zeroed buffer afterwards too, not the
    original content. This is still **not** a guarantee of erasure, stated
    plainly rather than implied: (1) a caller that already converted the
    view to an independent ``bytes`` copy (e.g. ``bytes(handle.content)``)
    holds a copy this class has no reference to and cannot reach; (2) CPython
    offers no way to force garbage collection or to prove no other reference
    (a debugger, a traceback frame keeping a raised exception's locals alive,
    a C extension) exists; (3) the underlying memory page is not scrubbed at
    the OS level. Disposal here means "this handle drops its own reference
    and actively overwrites its own buffer," never "this content is
    provably unrecoverable." See ``docs/threat-model.md`` T18 and
    ``docs/adr/0005-connector-security-foundation.md`` §7 for the
    corresponding documentation-honesty fix.
    """

    def __init__(self, *, canonical_reference: str, content_type: str, content: bytes) -> None:
        self._canonical_reference = canonical_reference
        self._content_type = content_type
        self._buffer: bytearray | None = bytearray(content)
        self._disposed = False

    def __enter__(self) -> TemporaryContentHandle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        if not self._disposed:
            self.dispose()

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def content(self) -> memoryview:
        """A live view onto the underlying buffer -- not a ``bytes`` copy.

        See the class docstring's disposal-honesty note: this is what lets
        ``dispose()``/``minimize()`` reach content a caller already read.
        """
        if self._disposed or self._buffer is None:
            raise ContentDisposedError(
                "temporary content was already disposed; content may be read only once,"
                " before minimize()/dispose() is called"
            )
        return memoryview(self._buffer)

    def dispose(self, *, reason: str = "minimized") -> DisposalRecord:
        """Overwrite the raw content in place, discard the handle's own
        reference, and return proof of disposal. Safe to call more than
        once; only the first call reports a non-zero ``byte_count``. See the
        class docstring's disposal-honesty note for exactly what this does
        and does not guarantee."""
        byte_count = 0 if self._buffer is None else len(self._buffer)
        if self._buffer is not None:
            self._buffer[:] = bytes(len(self._buffer))
        self._buffer = None
        self._disposed = True
        return DisposalRecord(
            canonical_reference=self._canonical_reference,
            retention_class=RetentionClass.temporary_full_content,
            byte_count=byte_count,
            disposed=True,
            reason=reason,
        )

    def minimize(
        self,
        *,
        extracted_at: datetime,
        upgrade: VerificationUpgrade | None = None,
        security_flags: tuple[SecurityFlag, ...] = (),
    ) -> tuple[ExtractionResult, DisposalRecord]:
        """Dispose of the raw content and return the durable, body-free pair."""
        if self._disposed or self._buffer is None:
            raise ContentDisposedError(
                "temporary content was already disposed; minimize() may be called only once"
            )
        byte_count = len(self._buffer)
        extraction = ExtractionResult(
            canonical_reference=self._canonical_reference,
            content_type=self._content_type,
            byte_count=byte_count,
            extracted_at=extracted_at,
            upgrade=upgrade,
            security_flags=security_flags,
        )
        disposal = self.dispose(reason="minimized")
        return extraction, disposal


# --- §5.4 triage: the product path, deterministic ----------------------


class TriageCandidate(BaseModel):
    """One discovered item's triage outcome. Triage NEVER assigns evidence,
    tier, relevance, or any ranking field -- it only narrows N discovered
    items to K candidates worth a human's authoring time. Frozen (Gate D
    round-1 correction, B3): a result value, never mutated after
    construction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    discovery_result: DiscoveryResult
    matched_tags: tuple[str, ...] = Field(default_factory=tuple)
    triage_score: int = Field(ge=0)
    selected: bool
    triage_reason: str = Field(max_length=300)


def triage(
    results: Sequence[DiscoveryResult],
    profile_tags: Iterable[str],
    max_candidates: int,
) -> tuple[TriageCandidate, ...]:
    """Pure, deterministic narrowing of discovered items to candidates.

    Matching is a plain, case-folded, TOKENIZED match of each result's title
    and summary against the caller-supplied tag vocabulary: each tag is
    tokenized the same way the haystack is (``_tokenize``, the same
    ``[a-z0-9]+`` splitter used on ``title``/``summary_normalized``), and a
    tag is matched only when ALL of its tokens are present in the haystack --
    so a hyphenated or multi-word tag such as ``agent-cli`` or
    ``hooks guardrails`` requires both ``agent``+``cli`` (or
    ``hooks``+``guardrails``) to appear, not a literal substring match of the
    hyphen/space-joined string (Gate D round-2 correction, C1: the previous
    ``tag in haystack`` check tested a whole tag string against a token SET,
    which can never contain a hyphen or a space, so every multi-token tag was
    structurally unmatchable). ``matched_tags`` reports the ORIGINAL tag
    string (e.g. ``"agent-cli"``), never the split tokens. ``triage_score`` is
    simply the count of matched tags (integer arithmetic only -- no model, no
    prose parsing). Results are ordered ``(score desc, canonical_reference
    asc)``; the first ``max_candidates`` (after that ordering) are
    ``selected=True``. Every input result appears in the output, selected or
    not, so ``selected`` is meaningful without a second lookup.
    """
    if max_candidates < 0:
        raise ValueError("max_candidates must be >= 0")

    tag_vocab = sorted({tag.strip().lower() for tag in profile_tags if tag.strip()})
    # Each tag's own token set, computed once per call. A tag that tokenizes
    # to nothing (e.g. punctuation-only) can never match -- the `and` guard
    # below prevents an empty set (vacuously a subset of everything) from
    # matching every result.
    tag_tokens: dict[str, frozenset[str]] = {tag: _tokenize(tag) for tag in tag_vocab}

    scored: list[tuple[DiscoveryResult, tuple[str, ...], int]] = []
    for result in results:
        haystack = _tokenize(result.title) | _tokenize(result.summary_normalized)
        matched = tuple(
            tag for tag in tag_vocab if tag_tokens[tag] and tag_tokens[tag] <= haystack
        )
        scored.append((result, matched, len(matched)))

    ordered = sorted(scored, key=lambda entry: (-entry[2], entry[0].canonical_reference))

    candidates: list[TriageCandidate] = []
    total = len(ordered)
    for rank, (result, matched, score) in enumerate(ordered):
        selected = rank < max_candidates
        if matched:
            reason = f"matched {len(matched)} tag(s): {', '.join(matched)}"
        else:
            reason = "no tag match"
        if selected:
            reason = f"{reason}; selected (rank {rank + 1} of {total})"
        else:
            reason = (
                f"{reason}; not selected "
                f"(rank {rank + 1} exceeds max_candidates={max_candidates})"
            )
        candidates.append(
            TriageCandidate(
                discovery_result=result,
                matched_tags=matched,
                triage_score=score,
                selected=selected,
                triage_reason=reason,
            )
        )
    return tuple(candidates)
