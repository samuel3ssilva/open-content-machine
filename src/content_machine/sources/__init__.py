"""Private source-folder inventory and Phase-2 draft contracts.

This package inventories the Founder's private biography material at metadata
granularity (:mod:`content_machine.sources.inventory`) and declares the frozen,
provenance-carrying draft contracts that Phase 2 will populate
(:mod:`content_machine.sources.contracts`). Everything here lives in the PRIVATE
data zone (docs/architecture.md): it never reads file content and never renders
into a public artifact without passing the export-public sanitization gate.
"""

from __future__ import annotations

from content_machine.sources.contracts import (
    AuthorityMapDraft,
    CreatorProfileDraft,
    SourceReference,
    StoryIndexEntry,
)
from content_machine.sources.inventory import (
    FileStatus,
    IntendedUse,
    InventoryEntry,
    InventoryTotals,
    PrivacyCategory,
    SourceInventory,
    SourceScanError,
    categorize,
    scan_source_folder,
    to_json,
    to_markdown,
    to_review_csv,
)

__all__ = [
    "AuthorityMapDraft",
    "CreatorProfileDraft",
    "FileStatus",
    "IntendedUse",
    "InventoryEntry",
    "InventoryTotals",
    "PrivacyCategory",
    "SourceInventory",
    "SourceReference",
    "SourceScanError",
    "StoryIndexEntry",
    "categorize",
    "scan_source_folder",
    "to_json",
    "to_markdown",
    "to_review_csv",
]
