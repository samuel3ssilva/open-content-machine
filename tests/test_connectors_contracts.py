"""Tests for content_machine.connectors.models (Gate D commit 1): pinned
constants, the DiscoveryRequest/DiscoveryResult/verification contracts, the
TemporaryContentHandle disposal lifecycle, and triage().

Also carries the no-network static check for every module that exists in
this commit (models/registry/permissions/retention/sanitize/failures) --
runner.py, bridge.py, and synthetic/ ship in a later commit and are not
scanned here."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from content_machine.connectors.models import (
    ALLOWED_CONTENT_TYPES,
    CANONICAL_REFERENCE_MAX_CHARS,
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
    VerificationRequest,
    VerificationUpgrade,
    triage,
)

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _provenance(run_id: str = "run_1") -> ProvenanceMetadata:
    return ProvenanceMetadata(
        adapter_name="synthetic_success", discovery_run_id=run_id, discovered_at=_NOW
    )


def _permission_ref(source_id: str = "src_vendor_alpha") -> PermissionRef:
    return PermissionRef(
        source_id=source_id, approved_mode="discovery", status_at_discovery="approved"
    )


def _discovery_result(
    *,
    source_id: str = "src_vendor_alpha",
    canonical_reference: str = "https://example.com/vendor-alpha/post-1",
    title: str = "Vendor Alpha ships a new capability",
    summary_normalized: str = "A short, human-normalized summary.",
    publication_date: date | None = date(2026, 7, 18),
    content_type: str = "text/html",
) -> DiscoveryResult:
    return DiscoveryResult(
        source_id=source_id,
        source_group="grp_vendor_alpha",
        title=title,
        publication_date=publication_date,
        canonical_reference=canonical_reference,
        summary_normalized=summary_normalized,
        summary_provenance=SummaryProvenance.system_derived,
        content_type=content_type,
        retrieved_at=_NOW,
        provenance=_provenance(),
        permission_ref=_permission_ref(source_id=source_id),
    )


# --- §2 pinned literal constants (Gate C precedent: assert STALE_WEEKS == 8) -


def test_scalar_constants_are_pinned_to_spec_literals() -> None:
    """Each assertion is against the LITERAL value from spec §2, not derived
    from the constant itself -- a mutation of any of these would fail here,
    unlike a self-referential test."""
    assert DEFAULT_MAX_BYTES == 2_000_000
    assert DEFAULT_TIMEOUT_SECONDS == 20
    assert DEFAULT_MAX_REDIRECTS == 3
    assert DEFAULT_MAX_ITEMS_PER_SOURCE == 50
    assert DEFAULT_MAX_REQUESTS_PER_RUN == 200
    assert SUMMARY_MAX_CHARS == 2000
    assert TITLE_MAX_CHARS == 300


def test_canonical_reference_max_chars_is_pinned_to_its_literal() -> None:
    """Gate D round-1 correction (C3): the new constant, pinned like every
    other one in this section rather than derived from itself."""
    assert CANONICAL_REFERENCE_MAX_CHARS == 2_048


def test_allowed_content_types_is_pinned_to_spec_literal() -> None:
    assert ALLOWED_CONTENT_TYPES == frozenset(
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


# --- DiscoveryRequest ---------------------------------------------------


def test_discovery_request_applies_bounded_defaults() -> None:
    request = DiscoveryRequest(
        source_id="src_vendor_alpha", window_start=date(2026, 7, 13), window_end=date(2026, 7, 20)
    )
    assert request.max_items_per_source == DEFAULT_MAX_ITEMS_PER_SOURCE
    assert request.max_requests == DEFAULT_MAX_REQUESTS_PER_RUN


def test_discovery_request_rejects_non_positive_window() -> None:
    with pytest.raises(ValidationError):
        DiscoveryRequest(
            source_id="src_vendor_alpha",
            window_start=date(2026, 7, 20),
            window_end=date(2026, 7, 20),
        )
    with pytest.raises(ValidationError):
        DiscoveryRequest(
            source_id="src_vendor_alpha",
            window_start=date(2026, 7, 20),
            window_end=date(2026, 7, 13),
        )


def test_discovery_request_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        DiscoveryRequest(
            source_id="src_vendor_alpha",
            window_start=date(2026, 7, 13),
            window_end=date(2026, 7, 20),
            unexpected_field="nope",  # type: ignore[call-arg]
        )


# --- DiscoveryResult: allowlist, no body field --------------------------


def test_discovery_result_has_no_body_or_raw_content_field() -> None:
    """Full-body persistence in discovery must be structurally impossible --
    i.e. the field must not exist on the model at all, not merely be unused."""
    field_names = set(DiscoveryResult.model_fields)
    for forbidden in ("body", "raw", "raw_content", "content", "html", "text"):
        assert forbidden not in field_names


def test_discovery_result_has_no_publisher_classification_field() -> None:
    """publisher_classification is a property of the curated source
    (registry.SourceRegistryEntry), decided before any retrieval -- never a
    per-item discovery field."""
    assert "publisher_classification" not in DiscoveryResult.model_fields


def test_discovery_result_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        DiscoveryResult(
            **_discovery_result().model_dump(),
            unexpected_field="nope",  # type: ignore[call-arg]
        )


def test_discovery_result_rejects_content_type_outside_allowlist() -> None:
    with pytest.raises(ValidationError):
        _discovery_result(content_type="application/octet-stream")


def test_discovery_result_accepts_every_allowed_content_type() -> None:
    for content_type in ALLOWED_CONTENT_TYPES:
        result = _discovery_result(content_type=content_type)
        assert result.content_type == content_type


def test_discovery_result_enforces_title_and_summary_length_caps() -> None:
    with pytest.raises(ValidationError):
        _discovery_result(title="x" * (TITLE_MAX_CHARS + 1))
    with pytest.raises(ValidationError):
        _discovery_result(summary_normalized="x" * (SUMMARY_MAX_CHARS + 1))


def test_discovery_result_requires_non_empty_title_and_canonical_reference() -> None:
    with pytest.raises(ValidationError):
        _discovery_result(title="")
    with pytest.raises(ValidationError):
        _discovery_result(canonical_reference="")


def test_discovery_result_publication_date_is_optional() -> None:
    result = _discovery_result(publication_date=None)
    assert result.publication_date is None


# --- C3 (Gate D round-1 correction): canonical_reference bounds ------------


def test_canonical_reference_rejects_value_over_the_length_cap() -> None:
    with pytest.raises(ValidationError):
        _discovery_result(
            canonical_reference="https://example.com/" + ("a" * CANONICAL_REFERENCE_MAX_CHARS)
        )


def test_canonical_reference_rejects_non_http_scheme() -> None:
    bad_references = (
        "ftp://example.com/a",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "example.com/a",
    )
    for bad in bad_references:
        with pytest.raises(ValidationError):
            _discovery_result(canonical_reference=bad)


def test_canonical_reference_accepts_http_and_https() -> None:
    for scheme in ("http", "https"):
        result = _discovery_result(canonical_reference=f"{scheme}://example.com/a")
        assert result.canonical_reference.startswith(scheme)


def test_canonical_reference_rejects_embedded_control_or_whitespace_characters() -> None:
    for bad in (
        "https://example.com/a\nb",
        "https://example.com/a\tb",
        "https://example.com/a b",
        "https://example.com/a\x00b",
    ):
        with pytest.raises(ValidationError):
            _discovery_result(canonical_reference=bad)


def test_provenance_metadata_and_permission_ref_are_closed_and_scalar_only() -> None:
    for field in ProvenanceMetadata.model_fields.values():
        assert field.annotation in (str, int, float, bool, datetime, date) or "datetime" in str(
            field.annotation
        )
    with pytest.raises(ValidationError):
        ProvenanceMetadata(
            adapter_name="a", discovery_run_id="b", discovered_at=_NOW, extra="nope"
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        PermissionRef(
            source_id="s", approved_mode="discovery", status_at_discovery="approved", extra="nope"
        )  # type: ignore[call-arg]


def test_discovery_result_serialization_is_deterministic() -> None:
    """Same input twice -> byte-identical serialization."""
    first = _discovery_result().model_dump_json()
    second = _discovery_result().model_dump_json()
    assert first == second


# --- B3 (Gate D round-1 correction): frozen, so post-construction mutation
# -- including via model_copy(update=...), which bypasses validation
# entirely -- must raise rather than silently succeed ----------------------


def test_discovery_result_is_frozen_direct_attribute_mutation_raises() -> None:
    result = _discovery_result()
    with pytest.raises(ValidationError):
        result.content_type = "application/x-evil"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        result.summary_normalized = "x" * 100_000  # type: ignore[misc]


def test_discovery_result_model_copy_update_still_bypasses_frozen() -> None:
    """Documents WHY ``frozen=True`` alone is not the fix: Pydantic's
    ``model_copy(update=...)`` writes directly to internal state and skips
    both validation AND the frozen check, so it silently succeeds even here.
    This is exactly the proven B3 probe -- and exactly why runner.py's own
    ``model_copy(update=...)`` call (the in-tree idiom a future engineer
    would otherwise copy) was replaced with re-validating construction
    instead of relying on ``frozen`` to catch it. See
    ``test_connectors_runner.py`` for the re-validating-construction
    behavior this pushed the runner toward."""
    result = _discovery_result()
    mutated = result.model_copy(update={"content_type": "application/x-evil"})
    assert mutated.content_type == "application/x-evil"  # bypass succeeds -- frozen does not help
    # The correct way to change a field on a frozen model re-validates:
    with pytest.raises(ValidationError):
        DiscoveryResult.model_validate(
            {**result.model_dump(), "content_type": "application/x-evil"}
        )


def test_connector_audit_event_is_frozen() -> None:
    event = ConnectorAuditEvent(
        source_id="src_a",
        event_kind=AuditEventKind.permission,
        reason_code="approved",
        occurred_at=_NOW,
    )
    with pytest.raises(ValidationError):
        event.detail = "mutated"  # type: ignore[misc]


def test_provenance_metadata_and_permission_ref_are_frozen() -> None:
    provenance = _provenance()
    with pytest.raises(ValidationError):
        provenance.adapter_name = "mutated"  # type: ignore[misc]
    permission_ref = _permission_ref()
    with pytest.raises(ValidationError):
        permission_ref.status_at_discovery = "revoked"  # type: ignore[misc]


# --- Deep verification ----------------------------------------------------


def test_verification_request_requires_non_empty_retrieval_reason() -> None:
    with pytest.raises(ValidationError):
        VerificationRequest(
            source_id="src_vendor_alpha", canonical_reference="https://example.com/a",
            retrieval_reason="",
        )
    with pytest.raises(ValidationError):
        VerificationRequest(
            source_id="src_vendor_alpha", canonical_reference="https://example.com/a",
            retrieval_reason="   ",
        )


def test_verification_request_applies_bounded_defaults() -> None:
    request = VerificationRequest(
        source_id="src_vendor_alpha",
        canonical_reference="https://example.com/a",
        retrieval_reason="human requested a deep check of a claimed benchmark",
    )
    assert request.max_bytes == DEFAULT_MAX_BYTES
    assert request.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert request.max_redirects == DEFAULT_MAX_REDIRECTS
    assert request.allowed_content_types == ALLOWED_CONTENT_TYPES


def test_extraction_result_has_no_body_field() -> None:
    field_names = set(ExtractionResult.model_fields)
    for forbidden in ("body", "raw", "raw_content", "content", "html", "text"):
        assert forbidden not in field_names


def test_extraction_result_is_frozen() -> None:
    extraction = ExtractionResult(
        canonical_reference="https://example.com/a",
        content_type="text/html",
        byte_count=10,
        extracted_at=_NOW,
    )
    with pytest.raises(ValidationError):
        extraction.byte_count = 999999  # type: ignore[misc]


def test_verification_upgrade_reuses_intelligence_evidence_type() -> None:
    upgrade = VerificationUpgrade(
        confirmed_evidence_type="benchmark_with_methodology",
        claim_directly_verifiable_in_artifact=True,
        independent_of_subject=True,
        upgrade_reason="artifact states a reproducible benchmark methodology",
    )
    assert upgrade.confirmed_evidence_type == "benchmark_with_methodology"
    with pytest.raises(ValidationError):
        VerificationUpgrade.model_validate(
            {"confirmed_evidence_type": "not_a_real_evidence_type", "upgrade_reason": "x"}
        )


def test_verification_upgrade_fields_default_to_none() -> None:
    upgrade = VerificationUpgrade(upgrade_reason="no upgrade determined")
    assert upgrade.confirmed_evidence_type is None
    assert upgrade.claim_directly_verifiable_in_artifact is None
    assert upgrade.independent_of_subject is None


def test_connector_audit_event_is_closed_and_body_free() -> None:
    field_names = set(ConnectorAuditEvent.model_fields)
    for forbidden in ("body", "raw", "content"):
        assert forbidden not in field_names
    with pytest.raises(ValidationError):
        ConnectorAuditEvent(
            source_id="s",
            event_kind=AuditEventKind.permission,
            reason_code="status_revoked",
            occurred_at=_NOW,
            extra="nope",
        )  # type: ignore[call-arg]


# --- TemporaryContentHandle: transient, in-memory only, disposal-tracked ---


def test_temporary_content_handle_minimize_returns_body_free_extraction() -> None:
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/vendor-alpha/post-1",
        content_type="text/html",
        content=b"the full raw article body would go here",
    )
    extraction, disposal = handle.minimize(extracted_at=_NOW)
    assert extraction.byte_count == len(b"the full raw article body would go here")
    assert disposal.disposed is True
    assert disposal.byte_count == extraction.byte_count
    assert "body" not in set(type(extraction).model_fields)


def test_temporary_content_handle_raises_after_disposal() -> None:
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a", content_type="text/plain", content=b"hello"
    )
    handle.dispose()
    with pytest.raises(ContentDisposedError):
        _ = handle.content
    with pytest.raises(ContentDisposedError):
        handle.minimize(extracted_at=_NOW)


def test_temporary_content_handle_context_manager_disposes_on_exit() -> None:
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a", content_type="text/plain", content=b"hello"
    )
    with handle as h:
        assert h.content == b"hello"
    assert handle.disposed is True
    with pytest.raises(ContentDisposedError):
        _ = handle.content


def test_temporary_content_handle_dispose_is_idempotent() -> None:
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a", content_type="text/plain", content=b"hello"
    )
    first = handle.dispose()
    second = handle.dispose()
    assert first.byte_count == len(b"hello")
    assert second.byte_count == 0  # already-disposed second call reports nothing left to dispose


def test_temporary_content_handle_error_never_names_the_reference() -> None:
    """House style: error messages never contain field values."""
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/secret-path-shaped-value",
        content_type="text/plain",
        content=b"hello",
    )
    handle.dispose()
    with pytest.raises(ContentDisposedError) as excinfo:
        _ = handle.content
    assert "secret-path-shaped-value" not in str(excinfo.value)


# --- C5 (Gate D round-1 correction): disposal honesty -----------------------


def test_temporary_content_handle_content_is_a_memoryview_equal_to_the_bytes() -> None:
    """`.content` returns a memoryview, not a bytes copy -- it still compares
    equal to the original bytes, so this is not observable as a behavior
    change for a caller that only compares/reads it."""
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a", content_type="text/plain", content=b"hello"
    )
    assert isinstance(handle.content, memoryview)
    assert handle.content == b"hello"


def test_dispose_scrubs_a_memoryview_a_caller_already_obtained() -> None:
    """Proven probe (C5): a caller that read `.content` before minimize()/
    dispose() keeps ONE reference alive past disposal -- but because the
    underlying buffer is a mutable bytearray, dispose() overwrites it in
    place, so that caller-held memoryview now reflects the scrubbed buffer,
    not the original secret content. This is the one honest improvement
    over plain immutable bytes; it is not a guarantee of erasure (see the
    class docstring) -- a caller that already copied the view out via
    bytes(...) before disposal keeps that copy regardless."""
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a",
        content_type="text/plain",
        content=b"top secret body",
    )
    caller_held_view = handle.content
    assert bytes(caller_held_view) == b"top secret body"
    handle.dispose()
    assert bytes(caller_held_view) == b"\x00" * len(b"top secret body")


def test_minimize_also_scrubs_a_memoryview_a_caller_already_obtained() -> None:
    handle = TemporaryContentHandle(
        canonical_reference="https://example.com/a",
        content_type="text/plain",
        content=b"top secret body",
    )
    caller_held_view = handle.content
    handle.minimize(extracted_at=_NOW)
    assert bytes(caller_held_view) == b"\x00" * len(b"top secret body")


# --- triage(): deterministic, pure, never a second ranking system ---------


def test_triage_is_pure_and_deterministic() -> None:
    results = [
        _discovery_result(
            source_id="src_a", canonical_reference="https://example.com/a", title="Agents update"
        ),
        _discovery_result(
            source_id="src_b", canonical_reference="https://example.com/b", title="Unrelated news"
        ),
    ]
    first = triage(results, ["agents", "mcp"], max_candidates=1)
    second = triage(results, ["agents", "mcp"], max_candidates=1)
    assert tuple(c.model_dump_json() for c in first) == tuple(c.model_dump_json() for c in second)


def test_triage_orders_by_score_desc_then_canonical_reference_asc() -> None:
    low = _discovery_result(
        source_id="src_a", canonical_reference="https://example.com/z-low", title="no match here"
    )
    high = _discovery_result(
        source_id="src_b",
        canonical_reference="https://example.com/a-high",
        title="Agents and MCP roundup",
    )
    tie_a = _discovery_result(
        source_id="src_c", canonical_reference="https://example.com/tie-a", title="agents piece"
    )
    tie_b = _discovery_result(
        source_id="src_d", canonical_reference="https://example.com/tie-b", title="agents piece"
    )
    candidates = triage([low, high, tie_b, tie_a], ["agents", "mcp"], max_candidates=2)
    ordering = [c.discovery_result.canonical_reference for c in candidates]
    assert ordering[0] == "https://example.com/a-high"  # highest score (2 tags matched)
    assert ordering[1] == "https://example.com/tie-a"  # tie broken by canonical_reference asc
    assert ordering[2] == "https://example.com/tie-b"
    assert ordering[3] == "https://example.com/z-low"


def test_triage_selected_flag_matches_max_candidates() -> None:
    results = [
        _discovery_result(
            source_id=f"src_{i}",
            canonical_reference=f"https://example.com/{i}",
            title="agents piece",
        )
        for i in range(5)
    ]
    candidates = triage(results, ["agents"], max_candidates=2)
    assert sum(1 for c in candidates if c.selected) == 2
    assert len(candidates) == 5


def test_triage_case_folds_tag_matching() -> None:
    result = _discovery_result(title="MCP And Agents Roundup")
    candidates = triage([result], ["mcp", "AGENTS"], max_candidates=1)
    assert set(candidates[0].matched_tags) == {"mcp", "agents"}


def test_triage_rejects_negative_max_candidates() -> None:
    with pytest.raises(ValueError, match="max_candidates"):
        triage([_discovery_result()], ["agents"], max_candidates=-1)


def test_triage_candidate_never_carries_a_ranking_field() -> None:
    """Triage NEVER assigns evidence, tier, relevance, or any ranking field."""
    field_names = set(TriageCandidate.model_fields)
    for forbidden in ("evidence_type", "tier", "relevance", "evidence_level", "rank"):
        assert forbidden not in field_names


def test_triage_candidate_is_frozen() -> None:
    candidates = triage([_discovery_result()], ["agents"], 1)
    with pytest.raises(ValidationError):
        candidates[0].triage_score = 999  # type: ignore[misc]


# --- Gate D round-2 correction (C1): hyphenated/multi-word tag matching ----


def test_triage_matches_hyphenated_tag() -> None:
    result = _discovery_result(title="New agent-cli release with hooks-guardrails support")
    candidates = triage([result], ["agent-cli", "hooks-guardrails"], max_candidates=1)
    assert set(candidates[0].matched_tags) == {"agent-cli", "hooks-guardrails"}
    assert candidates[0].triage_score == 2


def test_triage_matches_multi_word_tag() -> None:
    result = _discovery_result(title="Multi agent workflows are here")
    candidates = triage([result], ["multi agent"], max_candidates=1)
    assert candidates[0].matched_tags == ("multi agent",)


def test_triage_hyphenated_tag_requires_all_tokens_present() -> None:
    """A hyphenated tag only matches when EVERY one of its tokens is present
    -- a partial token overlap is not a match."""
    result = _discovery_result(title="agent release notes")  # no "cli" token
    candidates = triage([result], ["agent-cli"], max_candidates=1)
    assert candidates[0].matched_tags == ()
    assert candidates[0].triage_score == 0


def test_triage_hyphenated_tag_outranks_alphabetical_fallthrough() -> None:
    """Orchestrator-confirmed regression probe (C1, severity 1): with the old
    ``tag in haystack`` substring-only check, an item titled "New agent-cli
    release with hooks-guardrails support" scored 0 -- identical to a
    genuinely irrelevant item -- and the tiebreak (canonical_reference
    ascending) put whichever URL sorted first ahead of it, regardless of
    relevance. The relevant, hyphenated-tag item must now win on score,
    proving the alphabetical fallthrough is gone."""
    irrelevant = _discovery_result(
        source_id="src_a",
        canonical_reference="https://example.com/a-alphabetically-first",
        title="Completely unrelated announcement",
    )
    relevant = _discovery_result(
        source_id="src_b",
        canonical_reference="https://example.com/z-alphabetically-last",
        title="New agent-cli release with hooks-guardrails support",
    )
    candidates = triage(
        [irrelevant, relevant], ["agent-cli", "hooks-guardrails"], max_candidates=1
    )
    assert candidates[0].discovery_result.canonical_reference == relevant.canonical_reference
    assert candidates[0].selected is True
    assert candidates[0].triage_score == 2


# --- no-network static check (this commit's modules only) -----------------

_CONNECTORS_DIR = Path(__file__).resolve().parents[1] / "src" / "content_machine" / "connectors"
_COMMIT_1_MODULES = (
    "models.py",
    "registry.py",
    "permissions.py",
    "retention.py",
    "sanitize.py",
    "failures.py",
    "__init__.py",
)
_FORBIDDEN_IMPORTS = {
    "socket",
    "requests",
    "httpx",
    "urllib.request",
    "http.client",
    "aiohttp",
    "ssl",
    "smtplib",
    "ftplib",
    "websockets",
}


def test_no_network_import_in_any_commit_1_connectors_module() -> None:
    """Static AST scan denylisting known network-capable imports, across the
    FULL tree (not just module-level ``tree.body``) of every module shipped
    in this commit. Known false-negative gaps: transitive imports and
    ``importlib``/``__import__`` dynamic imports are not caught here -- a
    runtime no-network proof (patching ``socket.socket``) is deferred to the
    integration tests that ship with ``runner.py``/``synthetic/`` in a later
    commit, and remains the primary control; this test is a cheap, fast
    belt-and-suspenders check, not an exhaustive guarantee."""
    for filename in _COMMIT_1_MODULES:
        path = _CONNECTORS_DIR / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in {
                        f.split(".")[0] for f in _FORBIDDEN_IMPORTS
                    }, f"forbidden import in {filename}: {alias.name!r}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module.split(".")[0] not in {
                    f.split(".")[0] for f in _FORBIDDEN_IMPORTS
                }, f"forbidden import in {filename}: {module!r}"
