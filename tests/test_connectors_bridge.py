"""Tests for content_machine.connectors.bridge (Gate D commit 2, spec §5.5):
the fail-closed provenance gate, the empty-field rejections, deterministic
item_id derivation, and detection_date passthrough.

Also covers the Gate D round-1 security correction (B2): every
SecurityFlag was previously destroyed at this bridge -- SourceItem has no
security_flags field -- so a hostile item flagged instruction_shaped_text
crossed into the pipeline with the flag gone. The bridge now fails closed
on BLOCKING_SECURITY_FLAGS unless a caller explicitly names the exact
flag(s) a human has reviewed via human_reviewed_flags.

And the Gate D round-2 security correction (C2): round 1's override left no
trace of WHO waved a flagged item through or WHY. A reviewer note is now
required whenever the override actually takes effect, and
to_source_item_with_audit additionally returns a ConnectorAuditEvent
recording it."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from content_machine.connectors.bridge import (
    BLOCKING_SECURITY_FLAGS,
    AssessmentProvenance,
    AssessmentProvenanceNotPermitted,
    AuthoredAssessment,
    EmptySubjectEntityIdsError,
    EmptyTopicTagsError,
    MissingReviewerNoteError,
    RegistryEntryMismatchError,
    UnreviewedSecurityFlagsError,
    derive_item_id,
    to_source_item,
    to_source_item_with_audit,
)
from content_machine.connectors.models import (
    AuditEventKind,
    DiscoveryResult,
    PermissionRef,
    ProvenanceMetadata,
    SummaryProvenance,
)
from content_machine.connectors.registry import PublisherClassification, SourceRegistryEntry
from content_machine.connectors.sanitize import SecurityFlag
from content_machine.intelligence.normalize import normalize_canonical_reference

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _registry_entry(source_id: str = "src_vendor_alpha") -> SourceRegistryEntry:
    return SourceRegistryEntry(
        source_id=source_id,
        source_group="grp_vendor_alpha",
        publisher_id="vendor-alpha",
        source_category="vendor_blog",
        source_type="feed",
        publisher_classification=PublisherClassification.vendor_first_party,
        endpoint_label="VendorAlpha blog",
    )


def _discovery_result(
    *,
    source_id: str = "src_vendor_alpha",
    canonical_reference: str = "https://example.com/vendor-alpha/post-1?utm_source=x",
    publication_date: date | None = date(2026, 7, 18),
    security_flags: tuple[SecurityFlag, ...] = (),
) -> DiscoveryResult:
    return DiscoveryResult(
        source_id=source_id,
        source_group="grp_vendor_alpha",
        title="Vendor Alpha ships a new capability",
        publication_date=publication_date,
        canonical_reference=canonical_reference,
        summary_normalized="A short, human-normalized summary.",
        summary_provenance=SummaryProvenance.system_derived,
        content_type="text/html",
        retrieved_at=_NOW,
        provenance=ProvenanceMetadata(
            adapter_name="synthetic_success", discovery_run_id="run_1", discovered_at=_NOW
        ),
        permission_ref=PermissionRef(
            source_id=source_id, approved_mode="discovery", status_at_discovery="approved"
        ),
        security_flags=security_flags,
    )


def _assessment(**overrides: object) -> AuthoredAssessment:
    base: dict[str, object] = {
        "evidence_type": "announcement",
        "change_class": "material_change",
        "change_class_rationale": "n/a",
        "action_required": "none",
        "experiment_affordance": "not_testable",
        "contains_benefit_or_performance_claim": False,
        "claim_directly_verifiable_in_artifact": False,
        "topic_tags": ("agents",),
        "subject_entity_ids": ("vendor-alpha",),
        "provenance": AssessmentProvenance.human_authored,
    }
    base.update(overrides)
    return AuthoredAssessment.model_validate(base)


# --- B2 (Gate D round-1 correction): fail closed on unreviewed SecurityFlags


def test_blocking_flags_pin_the_implementer_choice() -> None:
    """Pinned per Gate C precedent: instruction_shaped_text is REQUIRED by
    the round-1 spec; malformed_encoding is this implementer's documented
    judgment call (see bridge.py's BLOCKING_SECURITY_FLAGS docstring)."""
    assert BLOCKING_SECURITY_FLAGS == frozenset(
        {SecurityFlag.instruction_shaped_text, SecurityFlag.malformed_encoding}
    )


def test_flagged_item_is_rejected_by_default() -> None:
    """Proven probe: a hostile item flagged instruction_shaped_text must be
    rejected, not admitted with the flag silently dropped."""
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result, _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
        )


def test_flagged_item_is_admitted_with_the_exact_matching_override() -> None:
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    item = to_source_item(
        result,
        _assessment(),
        _registry_entry(),
        detection_date=date(2026, 7, 18),
        human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
        reviewer_note="Reviewed: phrase is quoted marketing copy, not an injection attempt.",
    )
    assert item.evidence_type == "announcement"


def test_override_naming_the_wrong_flag_still_rejects() -> None:
    """Naming a DIFFERENT flag than the one actually present must not admit
    the item -- the override must name the ACTUAL blocking flag(s)."""
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset({SecurityFlag.malformed_encoding}),
        )


def test_override_naming_only_one_of_two_blocking_flags_still_rejects() -> None:
    result = _discovery_result(
        security_flags=(SecurityFlag.instruction_shaped_text, SecurityFlag.malformed_encoding)
    )
    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
        )
    # naming BOTH admits it
    item = to_source_item(
        result,
        _assessment(),
        _registry_entry(),
        detection_date=date(2026, 7, 18),
        human_reviewed_flags=frozenset(
            {SecurityFlag.instruction_shaped_text, SecurityFlag.malformed_encoding}
        ),
        reviewer_note="Reviewed both flags: benign, no injection or deceptive encoding found.",
    )
    assert item.evidence_type == "announcement"


def test_non_blocking_flags_never_require_an_override() -> None:
    """Flags whose hazardous substring was already redacted/truncated by
    sanitize_text (credential/email/path/markup/oversized) are not in
    BLOCKING_SECURITY_FLAGS and must not require human_reviewed_flags."""
    result = _discovery_result(
        security_flags=(
            SecurityFlag.credential_shaped_text,
            SecurityFlag.email_shaped_text,
            SecurityFlag.filesystem_path_shaped_text,
            SecurityFlag.active_markup_neutralized,
            SecurityFlag.oversized_truncated,
        )
    )
    item = to_source_item(
        result, _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
    )
    assert item.evidence_type == "announcement"


def test_empty_human_reviewed_flags_never_admits_a_flagged_item() -> None:
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset(),
        )


# --- C2 (Gate D round-2 correction): reviewer note + audit trail ----------


def test_override_without_a_note_raises() -> None:
    """A flags-named-but-no-note call must raise -- naming the right flags
    is no longer sufficient by itself."""
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(MissingReviewerNoteError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
        )


def test_override_with_whitespace_only_note_raises() -> None:
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(MissingReviewerNoteError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
            reviewer_note="   ",
        )


def test_override_with_wrong_flag_and_no_note_raises_unreviewed_not_missing_note() -> None:
    """The flag-coverage check runs FIRST: naming the wrong flag (with no
    note either) must still surface UnreviewedSecurityFlagsError, not
    MissingReviewerNoteError -- a caller who was always going to be
    rejected for the flags themselves should see that reason, not a
    confusing secondary one."""
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(UnreviewedSecurityFlagsError):
        to_source_item(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            human_reviewed_flags=frozenset({SecurityFlag.malformed_encoding}),
        )


def test_override_with_note_admits_and_yields_audit_event() -> None:
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    item, event = to_source_item_with_audit(
        result,
        _assessment(),
        _registry_entry(),
        detection_date=date(2026, 7, 18),
        occurred_at=_NOW,
        human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
        reviewer_note="Reviewed: quoted marketing copy, not an injection attempt.",
    )
    assert item.evidence_type == "announcement"
    assert event is not None
    assert event.event_kind == AuditEventKind.security
    assert event.reason_code == "human_reviewed_security_flags_override"
    assert event.source_id == result.source_id
    assert "instruction_shaped_text" in event.detail
    assert "Reviewed: quoted marketing copy" in event.detail
    assert item.item_id in event.detail


def test_override_without_note_via_with_audit_still_raises() -> None:
    """to_source_item_with_audit delegates to to_source_item for validation
    -- it must not admit an override that lacks a note either."""
    result = _discovery_result(security_flags=(SecurityFlag.instruction_shaped_text,))
    with pytest.raises(MissingReviewerNoteError):
        to_source_item_with_audit(
            result,
            _assessment(),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
            occurred_at=_NOW,
            human_reviewed_flags=frozenset({SecurityFlag.instruction_shaped_text}),
        )


def test_non_override_path_emits_no_security_event() -> None:
    """A clean admission (no human_reviewed_flags at all) must not fabricate
    a security audit event."""
    result = _discovery_result()  # no security_flags
    item, event = to_source_item_with_audit(
        result,
        _assessment(),
        _registry_entry(),
        detection_date=date(2026, 7, 18),
        occurred_at=_NOW,
    )
    assert item.evidence_type == "announcement"
    assert event is None


# --- fail-closed provenance --------------------------------------------


def test_human_authored_provenance_is_accepted() -> None:
    item = to_source_item(
        _discovery_result(), _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
    )
    assert item.evidence_type == "announcement"


@pytest.mark.parametrize(
    "provenance", [AssessmentProvenance.derived_deterministic, AssessmentProvenance.model_proposed]
)
def test_non_human_provenance_is_rejected(provenance: AssessmentProvenance) -> None:
    with pytest.raises(AssessmentProvenanceNotPermitted):
        to_source_item(
            _discovery_result(),
            _assessment(provenance=provenance),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
        )


# --- fail-closed empty fields --------------------------------------------


def test_empty_topic_tags_is_rejected() -> None:
    with pytest.raises(EmptyTopicTagsError):
        to_source_item(
            _discovery_result(),
            _assessment(topic_tags=()),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
        )


def test_empty_subject_entity_ids_is_rejected() -> None:
    with pytest.raises(EmptySubjectEntityIdsError):
        to_source_item(
            _discovery_result(),
            _assessment(subject_entity_ids=()),
            _registry_entry(),
            detection_date=date(2026, 7, 18),
        )


def test_authored_assessment_defaults_are_empty_tuples_not_silently_accepted() -> None:
    """Pydantic defaults topic_tags/subject_entity_ids to (); confirm the
    bridge actually rejects that default rather than only rejecting an
    explicit empty value."""
    minimal = AuthoredAssessment.model_validate(
        {
            "evidence_type": "announcement",
            "change_class": "material_change",
            "change_class_rationale": "n/a",
            "action_required": "none",
            "experiment_affordance": "not_testable",
            "contains_benefit_or_performance_claim": False,
            "claim_directly_verifiable_in_artifact": False,
            "provenance": AssessmentProvenance.human_authored,
        }
    )
    assert minimal.topic_tags == ()
    assert minimal.subject_entity_ids == ()
    with pytest.raises(EmptyTopicTagsError):
        to_source_item(
            _discovery_result(), minimal, _registry_entry(), detection_date=date(2026, 7, 18)
        )


# --- registry entry mismatch -------------------------------------------


def test_registry_entry_source_id_mismatch_is_rejected() -> None:
    with pytest.raises(RegistryEntryMismatchError):
        to_source_item(
            _discovery_result(source_id="src_vendor_alpha"),
            _assessment(),
            _registry_entry(source_id="src_vendor_other"),
            detection_date=date(2026, 7, 18),
        )


# --- registry fields, never the discovery payload ------------------------


def test_publisher_fields_come_from_registry_entry_not_discovery_payload() -> None:
    entry = _registry_entry()
    item = to_source_item(
        _discovery_result(), _assessment(), entry, detection_date=date(2026, 7, 18)
    )
    assert item.publisher_id == entry.publisher_id
    assert item.source_category == entry.source_category
    assert item.source_type == entry.source_type


# --- deterministic item_id -----------------------------------------------


def test_item_id_is_deterministic_sha256_of_normalized_reference() -> None:
    reference = "https://example.com/vendor-alpha/post-1?utm_source=x"
    expected = __import__("hashlib").sha256(
        normalize_canonical_reference(reference).encode("utf-8")
    ).hexdigest()
    assert derive_item_id(reference) == expected


def test_item_id_is_stable_across_re_discovery_with_different_query_string() -> None:
    """Re-discovering the same artifact next week, with a different tracking
    query string, must canonicalize to the SAME item_id -- never derived
    from retrieved_at."""
    first = _discovery_result(
        canonical_reference="https://example.com/vendor-alpha/post-1?utm_source=feedreader"
    )
    second = _discovery_result(
        canonical_reference="https://example.com/vendor-alpha/post-1?utm_source=newsletter"
    )
    item_first = to_source_item(
        first, _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
    )
    item_second = to_source_item(
        second, _assessment(), _registry_entry(), detection_date=date(2026, 7, 25)
    )
    assert item_first.item_id == item_second.item_id


def test_item_id_never_derived_from_retrieved_at() -> None:
    """Two DiscoveryResults with the SAME canonical_reference but DIFFERENT
    retrieved_at timestamps must produce the SAME item_id."""
    same_reference = "https://example.com/vendor-alpha/stable-post"
    early = _discovery_result(canonical_reference=same_reference)
    late = DiscoveryResult.model_validate(
        {**early.model_dump(), "retrieved_at": datetime(2026, 8, 1, 0, 0, tzinfo=UTC)}
    )
    item_early = to_source_item(
        early, _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
    )
    item_late = to_source_item(
        late, _assessment(), _registry_entry(), detection_date=date(2026, 7, 18)
    )
    assert item_early.item_id == item_late.item_id


# --- detection_date is first-seen semantics, never "today" ----------------


def test_detection_date_is_exactly_the_caller_supplied_value() -> None:
    item = to_source_item(
        _discovery_result(), _assessment(), _registry_entry(), detection_date=date(2020, 1, 1)
    )
    assert item.detection_date == date(2020, 1, 1)


def test_bridge_module_never_reads_the_wall_clock() -> None:
    """Static AST proof accompanying the behavioral one above: derive_item_id
    and to_source_item accept every date/time value as an explicit parameter
    and the bridge module's CODE (not its prose docstrings, which discuss the
    rule in English) contains no ``date.today()``/``datetime.now()``/
    ``datetime.utcnow()`` call anywhere."""
    import ast
    import inspect

    import content_machine.connectors.bridge as bridge_module

    tree = ast.parse(inspect.getsource(bridge_module))
    forbidden_attrs = {"today", "now", "utcnow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_attrs, (
                f"forbidden wall-clock call: {node.func.attr}()"
            )
