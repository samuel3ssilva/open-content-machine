"""Phase-2 draft contracts derived from approved private sources (stubs only).

These models are SCHEMA STUBS. No analysis code populates them this sprint --
they exist to freeze the shape and, above all, the provenance and approval
invariants that Phase 2 must honour. Enforcement (rejecting any draft built from
an unapproved or wrongly-categorized source) lands in Phase 2; here it is
documented and structurally scaffolded.

Data-zone placement (docs/architecture.md data zones)
-----------------------------------------------------
Every model in this module lives in the PRIVATE zone. They are derived from the
Founder's biography material and MUST remain separated from public content
outputs. A draft may only be turned into a public artifact after passing the
same export-public style sanitization gate used elsewhere in the pipeline; it is
never rendered into a public artifact directly.

Core invariant (Phase 2, documented here)
------------------------------------------
Each draft carries ``sources: list[SourceReference]`` provenance and a
``derived_only_from_approved: bool`` flag. The flag asserts that EVERY referenced
source had ``approved=True`` at build time. Approval never comes from the
inventory or from these models -- it originates solely in the Founder's private
review CSV (see :mod:`content_machine.sources.inventory`), whose approval column
starts empty by design. In Phase 2, construction must fail rather than silently
set the flag false.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from content_machine.sources.inventory import IntendedUse, PrivacyCategory


class SourceReference(BaseModel):
    """Provenance pointer from a derived draft back to one inventoried source.

    Carries only metadata-safe identifiers -- ``file_id`` and the sanitized
    ``root_label`` -- never a path or file content. ``approved`` mirrors the
    Founder's private review decision at build time; ``intended_use`` records the
    destination the Founder assigned. A reference with ``approved=False`` must
    never appear in a draft whose ``derived_only_from_approved`` is ``True``
    (Phase-2 enforcement).
    """

    model_config = ConfigDict(extra="forbid")

    file_id: str
    root_label: str
    category: PrivacyCategory = PrivacyCategory.unknown
    approved: bool = False
    intended_use: IntendedUse | None = None


class CreatorProfileDraft(BaseModel):
    """Draft of the creator's professional profile, assembled from approved sources.

    Stub only. ``derived_only_from_approved`` must be provably ``True`` -- every
    entry in ``sources`` had ``approved=True`` -- before this draft may feed any
    downstream step (Phase-2 enforcement). PRIVATE zone; never rendered public
    without the sanitization gate.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceReference] = Field(default_factory=list)
    derived_only_from_approved: bool = False


class AuthorityMapDraft(BaseModel):
    """Draft map of the creator's domains of demonstrated authority.

    Stub only. Same provenance/approval invariant as
    :class:`CreatorProfileDraft`. PRIVATE zone.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceReference] = Field(default_factory=list)
    derived_only_from_approved: bool = False


class StoryIndexEntry(BaseModel):
    """One indexed personal story/experience, traced to its approved sources.

    Stub only. Same provenance/approval invariant. PRIVATE zone: personal stories
    must not surface in public artifacts without the sanitization gate.
    """

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceReference] = Field(default_factory=list)
    derived_only_from_approved: bool = False
