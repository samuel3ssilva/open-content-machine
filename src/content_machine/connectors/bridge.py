"""The ONLY ``DiscoveryResult`` -> ``intelligence.SourceItem`` path (Gate D
§5.5, commit 2).

Nothing else in this codebase may construct a
:class:`~content_machine.intelligence.models.SourceItem` from connector
output -- every future adapter, every future gate, funnels through
:func:`to_source_item`. That makes this module the single choke point
(trust boundary TB-4, retrieval -> pipeline, recorded in ADR 0005 in a later
Gate D commit) where connector output either earns its way into M1-M7 or is
rejected outright.

**Fail closed on provenance.** Only ``AssessmentProvenance.human_authored``
may cross this bridge in Gate D. ``derived_deterministic`` and
``model_proposed`` are fully representable on :class:`AuthoredAssessment` --
future gates may legitimately want them -- but :func:`to_source_item` raises
:class:`AssessmentProvenanceNotPermitted` for either. Admitting them is an
admission-POLICY change and belongs to Fable plus a future gate, never to an
implementer quietly relaxing a check here.

**Fail closed on empty authored fields.** ``topic_tags``/``subject_entity_ids``
Pydantic-default to an empty tuple, and an unfilled default is actively
harmful here, not merely incomplete: empty ``topic_tags`` zero the 40% of the
ranking score that is a JOIN against the profile's territories
(``ranking.py``'s relevance dimension), and empty ``subject_entity_ids`` make
every publisher look independent of every subject (``cluster.py``'s
independence check is a subject-membership test). Both are rejected before
construction, never silently accepted as "no tags"/"no subjects".

**Fail closed on un-reviewed SecurityFlags (Gate D round-1 correction, B2).**
Every ``SecurityFlag`` on the incoming ``DiscoveryResult`` was previously
destroyed at this bridge -- ``SourceItem`` carries no ``security_flags``
field, so a hostile item flagged ``instruction_shaped_text`` crossed into
the pipeline with the injection text intact and the marker gone. This
bridge now REJECTS (:class:`UnreviewedSecurityFlagsError`) any result
carrying a member of :data:`BLOCKING_SECURITY_FLAGS` that a caller has not
named via ``human_reviewed_flags``. This is a temporary, fail-closed
substitute for the durable fix -- propagating ``security_flags`` onto
``SourceItem`` itself and surfacing them in the published brief -- which is
explicitly DEFERRED to the gate that ships the first real adapter, where
extending the intelligence model is in scope under Fable review; the
Founder's constraint against touching ``intelligence/`` in this gate rules
that fix out here.

**Deterministic identity.** ``item_id`` is derived from the NORMALIZED
canonical reference (:func:`content_machine.intelligence.normalize.normalize_canonical_reference`,
then sha256 -- never Python's ``hash()`` on a string, which is salted per
process by ``PYTHONHASHSEED`` and therefore not reproducible across runs),
never from ``retrieved_at`` -- the same artifact re-discovered next week
canonicalizes to the same reference and therefore the same ``item_id``, so it
is recognized as the SAME item rather than minted twice.

``detection_date`` is an explicit, caller-supplied parameter with FIRST-SEEN
semantics (never ``date.today()`` -- this module contains no clock read
anywhere): the caller is responsible for knowing whether this
``canonical_reference`` has been seen before and, if so, passing the date it
was FIRST seen, not today's date.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from content_machine.connectors.models import AuditEventKind, ConnectorAuditEvent, DiscoveryResult
from content_machine.connectors.registry import SourceRegistryEntry
from content_machine.connectors.sanitize import SecurityFlag
from content_machine.intelligence.models import (
    ActionRequired,
    ChangeClass,
    EvidenceType,
    ExperimentAffordance,
    SourceItem,
)
from content_machine.intelligence.normalize import normalize_canonical_reference

#: SecurityFlag members that FAIL CLOSED at this bridge in Gate D round-1
#: (B2). This is an implementer judgment call, not a spec-mandated list --
#: recorded here, reviewably, rather than silently chosen:
#:
#: * ``instruction_shaped_text`` -- REQUIRED by the round-1 fix. By design
#:   (see ``sanitize.py``'s docstring), the matched phrase is left in the
#:   text UNMODIFIED, "kept as inert data" for a human to see alongside the
#:   flag. Every other SecurityFlag that ``sanitize_text`` raises actively
#:   redacts or truncates the hazardous substring OUT of the field itself
#:   (credential/email/path replaced with a placeholder, markup stripped,
#:   oversized truncated) -- ``instruction_shaped_text`` is the one case
#:   where the flag is the ONLY thing standing between a reviewer and text
#:   that still contains the hostile phrase verbatim.
#: * ``malformed_encoding`` -- added by this implementer's judgment. After
#:   the round-1 C2 fix, this flag also covers bidi-override/invisible
#:   Unicode characters used specifically to make rendered text differ from
#:   logical text (deceiving a human reviewer) -- the same class of harm
#:   ``instruction_shaped_text`` protects against, even though the
#:   characters themselves are stripped before this field is populated.
#:
#: Every OTHER flag (credential/email/path/markup/oversized/duplicate-
#: reference/conflicting-date) already had its hazardous substring redacted
#: or truncated by ``sanitize_text``/the runner before reaching this bridge,
#: so admitting an item carrying only those flags carries no equivalent
#: residual risk in this gate.
BLOCKING_SECURITY_FLAGS: frozenset[SecurityFlag] = frozenset(
    {SecurityFlag.instruction_shaped_text, SecurityFlag.malformed_encoding}
)


class AssessmentProvenance(StrEnum):
    """Who/what produced an :class:`AuthoredAssessment`. Only
    ``human_authored`` may cross :func:`to_source_item` in this gate -- see
    the module docstring."""

    human_authored = "human_authored"
    derived_deterministic = "derived_deterministic"
    model_proposed = "model_proposed"


class AuthoredAssessment(BaseModel):
    """The fields :class:`~content_machine.intelligence.models.SourceItem`
    requires that a :class:`~content_machine.connectors.models.DiscoveryResult`
    structurally cannot supply (evidence/change/action/experiment judgment
    calls, topic/subject assignment) -- always paired with exactly one
    ``DiscoveryResult`` by the caller of :func:`to_source_item`, never
    embedded inside it."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: EvidenceType
    change_class: ChangeClass
    change_class_rationale: str
    action_required: ActionRequired
    experiment_affordance: ExperimentAffordance
    contains_benefit_or_performance_claim: bool
    claim_directly_verifiable_in_artifact: bool
    topic_tags: tuple[str, ...] = Field(default_factory=tuple)
    subject_entity_ids: tuple[str, ...] = Field(default_factory=tuple)
    provenance: AssessmentProvenance


class AssessmentProvenanceNotPermitted(PermissionError):
    """Raised by :func:`to_source_item` when ``assessment.provenance`` is
    anything other than ``human_authored`` -- see the module docstring."""


class EmptyTopicTagsError(ValueError):
    """Raised by :func:`to_source_item` when ``assessment.topic_tags`` is
    empty -- see the module docstring's fail-closed rule."""


class EmptySubjectEntityIdsError(ValueError):
    """Raised by :func:`to_source_item` when ``assessment.subject_entity_ids``
    is empty -- see the module docstring's fail-closed rule."""


class RegistryEntryMismatchError(ValueError):
    """Raised by :func:`to_source_item` when ``registry_entry.source_id``
    does not match ``result.source_id`` -- a defensive check against wiring
    the wrong registry entry to a result, never expected in correct caller
    code."""


class UnreviewedSecurityFlagsError(PermissionError):
    """Raised by :func:`to_source_item` when ``result.security_flags``
    carries a flag in :data:`BLOCKING_SECURITY_FLAGS` that ``human_reviewed_flags``
    does not explicitly name.

    **Gate D round-1 security correction (B2).** Fable's review proved that
    every ``SecurityFlag`` a :class:`DiscoveryResult` carries is destroyed at
    this bridge: ``SourceItem`` has no ``security_flags`` field, so a
    hostile item flagged ``instruction_shaped_text`` crossed into the
    pipeline with the injection text intact and the marker gone -- the
    threat model's whole prompt-injection posture rests on flags being
    "traceable for a human reviewer," which downstream they were not.

    Two fixes were offered: (a) add ``security_flags`` to ``SourceItem``, or
    (b) fail closed here instead. The Founder's constraint against touching
    ``intelligence/`` in this gate makes (a) out of scope, so (b) is what is
    implemented: this is a **temporary, fail-closed substitute** for
    propagating the flag downstream, not the durable fix. The durable fix --
    adding ``security_flags`` to ``SourceItem`` and surfacing them in the
    brief for a human reviewer -- is DEFERRED to the gate that ships the
    first real adapter, where extending the intelligence model is in scope
    under Fable review (see ADR 0005's round-1 findings section).
    """


class MissingReviewerNoteError(ValueError):
    """Raised by :func:`to_source_item` when ``human_reviewed_flags`` is
    passed (non-empty) but ``reviewer_note`` is ``None`` or empty/whitespace
    after stripping.

    **Gate D round-2 correction (C2).** Round 1's ``human_reviewed_flags``
    override correctly required every blocking flag to be named and never
    admitted on ``None``/empty -- but once overridden, nothing recorded that
    a human actually reviewed and waved the item through: no reviewer
    identity, no note, no audit event. An overridden item was therefore
    indistinguishable from a clean one after the fact. This exception closes
    half of that gap (a human must write down WHY); see
    :func:`to_source_item_with_audit` for the other half (a
    :class:`ConnectorAuditEvent` recording the override, since ``SourceItem``
    itself has no field to carry the note).
    """


def derive_item_id(canonical_reference: str) -> str:
    """Deterministic ``item_id`` from a (not-yet-normalized) canonical
    reference: normalize it (idempotent -- see ``normalize.py``), then
    sha256 it. Never Python's ``hash()`` on a string (unstable across
    ``PYTHONHASHSEED``); sha256 is stable across processes, interpreters,
    and ``PYTHONHASHSEED`` values by construction."""
    normalized = normalize_canonical_reference(canonical_reference)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def to_source_item(
    result: DiscoveryResult,
    assessment: AuthoredAssessment,
    registry_entry: SourceRegistryEntry,
    *,
    detection_date: date,
    human_reviewed_flags: frozenset[SecurityFlag] | None = None,
    reviewer_note: str | None = None,
) -> SourceItem:
    """The ONLY code path from connector output into M1-M7 (see the module
    docstring). Fails closed on provenance, on the two authored fields
    whose empty default is actively harmful, and (Gate D round-1 correction,
    B2) on any :data:`BLOCKING_SECURITY_FLAGS` member ``result`` carries that
    a human has not explicitly reviewed and accepted; ``publisher_id``/
    ``source_category``/``source_type`` always come from ``registry_entry``
    (the curated, pre-retrieval source registry), never from the discovery
    payload itself, exactly as spec §5.5 requires.

    ``human_reviewed_flags`` is the explicit human-override this bridge
    requires before admitting a blocked item: a caller must name the EXACT
    flag(s) a human has seen and accepted on ``result``. ``None`` (the
    default) or an empty set never admits a flagged item -- naming the wrong
    flag (one not actually present, or missing one that IS present) also
    still rejects, since every blocking flag on the result must be covered.
    See :class:`UnreviewedSecurityFlagsError` for why this is fail-closed
    rather than propagating the flag itself, which is the durable fix
    deferred to a future gate.

    ``reviewer_note`` (Gate D round-2 correction, C2) is REQUIRED, non-empty
    (after stripping), whenever ``human_reviewed_flags`` is passed as a
    non-empty set -- i.e. whenever an override actually takes effect. Round 1
    correctly required naming every blocking flag but left no trace of WHO
    waved an item through or WHY; an overridden item was indistinguishable
    from a clean one afterward. A flags-without-note call raises
    :class:`MissingReviewerNoteError`. This check runs only once the flags
    themselves have already cleared :class:`UnreviewedSecurityFlagsError`
    above, so naming the wrong flag (or too few flags) still raises that
    error first, exactly as before -- ``reviewer_note`` is never required on
    a call that was going to be rejected anyway. See
    :func:`to_source_item_with_audit` for the companion function that also
    returns a :class:`ConnectorAuditEvent` recording the override; this
    function's own return type (``SourceItem`` alone) is unchanged so no
    existing caller breaks.
    """
    if registry_entry.source_id != result.source_id:
        raise RegistryEntryMismatchError(
            "registry_entry.source_id does not match result.source_id"
        )

    unreviewed_blocking_flags = (set(result.security_flags) & BLOCKING_SECURITY_FLAGS) - set(
        human_reviewed_flags or ()
    )
    if unreviewed_blocking_flags:
        flag_names = ", ".join(sorted(flag.value for flag in unreviewed_blocking_flags))
        raise UnreviewedSecurityFlagsError(
            f"DiscoveryResult carries blocking SecurityFlag(s) [{flag_names}] that a human "
            "has not explicitly reviewed and accepted via human_reviewed_flags; this is a "
            "Gate D round-1 fail-closed control because SecurityFlag cannot yet be carried "
            "downstream on SourceItem itself -- see UnreviewedSecurityFlagsError"
        )

    if human_reviewed_flags and (reviewer_note is None or not reviewer_note.strip()):
        raise MissingReviewerNoteError(
            "human_reviewed_flags was passed but reviewer_note is empty; a human override of "
            "a blocking SecurityFlag must be accompanied by a non-empty note explaining why "
            "-- see MissingReviewerNoteError (Gate D round-2 correction, C2)"
        )

    if assessment.provenance != AssessmentProvenance.human_authored:
        raise AssessmentProvenanceNotPermitted(
            "only AssessmentProvenance.human_authored may cross to_source_item in Gate D; "
            f"got {assessment.provenance!r} -- admitting derived/model provenance is deferred "
            "to Fable review and a future gate, not this bridge"
        )

    if not assessment.topic_tags:
        raise EmptyTopicTagsError(
            "assessment.topic_tags must be non-empty: an empty default silently zeroes the "
            "relevance dimension's profile join rather than genuinely scoring as untagged"
        )

    if not assessment.subject_entity_ids:
        raise EmptySubjectEntityIdsError(
            "assessment.subject_entity_ids must be non-empty: an empty default silently makes "
            "every publisher look independent of every subject"
        )

    return SourceItem(
        item_id=derive_item_id(result.canonical_reference),
        source_type=registry_entry.source_type,
        source_category=registry_entry.source_category,
        publisher_id=registry_entry.publisher_id,
        subject_entity_ids=list(assessment.subject_entity_ids),
        title=result.title,
        summary_normalized=result.summary_normalized,
        publication_date=result.publication_date,
        detection_date=detection_date,
        stable_reference=result.canonical_reference,
        evidence_type=assessment.evidence_type,
        change_class=assessment.change_class,
        change_class_rationale=assessment.change_class_rationale,
        action_required=assessment.action_required,
        experiment_affordance=assessment.experiment_affordance,
        topic_tags=list(assessment.topic_tags),
        contains_benefit_or_performance_claim=assessment.contains_benefit_or_performance_claim,
        claim_directly_verifiable_in_artifact=assessment.claim_directly_verifiable_in_artifact,
    )


def to_source_item_with_audit(
    result: DiscoveryResult,
    assessment: AuthoredAssessment,
    registry_entry: SourceRegistryEntry,
    *,
    detection_date: date,
    occurred_at: datetime,
    human_reviewed_flags: frozenset[SecurityFlag] | None = None,
    reviewer_note: str | None = None,
) -> tuple[SourceItem, ConnectorAuditEvent | None]:
    """Companion to :func:`to_source_item` (Gate D round-2 correction, C2)
    that also returns the audit trail of a human-reviewed-flags override.

    **Design choice, stated explicitly.** ``to_source_item``'s return type
    (``SourceItem`` alone) is a contract every existing caller and test in
    this codebase already depends on, and this gate does not change it --
    changing a widely-called function's return shape to thread an
    occasionally-``None`` audit event through every call site is a bigger,
    riskier diff than the bug being fixed warrants. Instead, this is a
    SEPARATE, opt-in function: it does the identical validation
    ``to_source_item`` does (by calling it directly, so the two can never
    disagree), and additionally returns a :class:`ConnectorAuditEvent` when
    -- and only when -- ``human_reviewed_flags`` actually overrides a
    blocking flag. On the ordinary, non-override path the second element is
    ``None``: no security event is fabricated for a clean admission.

    The returned event's ``event_kind`` is ``AuditEventKind.security``, its
    ``reason_code`` is ``"human_reviewed_security_flags_override"``, and its
    ``detail`` records the accepted flags and the reviewer's note (capped at
    280 chars, matching every other audit ``detail`` in this package).
    ``occurred_at`` is a required, caller-supplied timestamp -- this module
    contains no clock read anywhere (rule F), so the caller (typically the
    same code that already supplies ``detection_date``) must provide it.
    """
    item = to_source_item(
        result,
        assessment,
        registry_entry,
        detection_date=detection_date,
        human_reviewed_flags=human_reviewed_flags,
        reviewer_note=reviewer_note,
    )
    if not human_reviewed_flags:
        return item, None

    accepted_flags = ", ".join(sorted(flag.value for flag in human_reviewed_flags))
    event = ConnectorAuditEvent(
        source_id=result.source_id,
        event_kind=AuditEventKind.security,
        reason_code="human_reviewed_security_flags_override",
        occurred_at=occurred_at,
        detail=(
            f"human override admitted item_id={item.item_id} despite blocking flag(s) "
            f"[{accepted_flags}]; reviewer note: {reviewer_note}"
        )[:280],
    )
    return item, event
