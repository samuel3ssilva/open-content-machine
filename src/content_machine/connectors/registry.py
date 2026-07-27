"""Curated source registry (Gate D §3) -- closes a fail-open hole.

Without a registry, "is this publisher independent?" would have to be
answered ad hoc, per item, by whoever authors a :class:`SourceItem` --
exactly the kind of silent, unauditable judgment call Gate A's evidence rubric
was built to avoid. :class:`SourceRegistryEntry` makes
``publisher_classification`` a property of the CURATED SOURCE, decided once,
before any retrieval, and :class:`SourceRegistry` is the single place that
answers "what do we know about this source_id" for the rest of the package.

Reuses :data:`content_machine.intelligence.models.SourceType` rather than
duplicating that taxonomy -- a read-only import of a plain type alias, not a
dependency on any intelligence *behavior*. Nothing in ``intelligence`` imports
``connectors``.

The repository ships exactly ONE invented example registry entry (in the test
suite, ``tests/test_connectors_registry.py``) for shape verification. The
real ~20-source registry lives in the Founder's private workspace and is
never committed here.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from content_machine.intelligence.models import SourceType


class PublisherClassification(StrEnum):
    """Where a source sits relative to the subjects it covers.

    Decided once, when the source is curated -- never inferred from a single
    item, and never present on ``models.DiscoveryResult`` (see that model's
    docstring): it is a property of the source, known before any retrieval.
    """

    vendor_first_party = "vendor_first_party"
    independent = "independent"
    community = "community"
    unclassified = "unclassified"


class SourceRegistryEntry(BaseModel):
    """One curated source, known and classified before any retrieval happens."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_group: str
    publisher_id: str
    source_category: str
    source_type: SourceType
    publisher_classification: PublisherClassification
    # An opaque label for humans (e.g. "VendorA blog"), NEVER a URL to fetch --
    # this gate has no fetch path, and nothing here should look fetchable.
    endpoint_label: str = Field(max_length=200)

    @property
    def may_supply_independence(self) -> bool:
        """True only for ``independent``/``community`` sources.

        Fail-closed: ``unclassified`` -- and, just as importantly,
        ``vendor_first_party`` -- can never supply independence. An
        unclassified source is treated exactly as conservatively as a known
        vendor source, never more favorably, until a human curates it.
        """
        return self.publisher_classification in (
            PublisherClassification.independent,
            PublisherClassification.community,
        )


class UnknownSourceError(KeyError):
    """Raised when a ``source_id`` has no :class:`SourceRegistryEntry`."""


class DuplicateSourceIdError(ValueError):
    """Raised when two registry entries share a ``source_id`` at construction."""


class SourceRegistry:
    """In-memory registry keyed by ``source_id``.

    Built from an explicit, caller-supplied list of entries -- no filesystem
    or environment I/O in this gate. Iteration order matches construction
    order (a plain ``dict``, never a ``set``), so any serialization built on
    top of it is deterministic.
    """

    def __init__(self, entries: list[SourceRegistryEntry]) -> None:
        by_id: dict[str, SourceRegistryEntry] = {}
        for entry in entries:
            if entry.source_id in by_id:
                raise DuplicateSourceIdError(
                    f"duplicate source_id in registry construction: {entry.source_id!r}"
                )
            by_id[entry.source_id] = entry
        self._by_id = by_id

    def get(self, source_id: str) -> SourceRegistryEntry | None:
        """Return the entry for ``source_id``, or ``None`` if unregistered."""
        return self._by_id.get(source_id)

    def require(self, source_id: str) -> SourceRegistryEntry:
        """Return the entry for ``source_id``, or raise :class:`UnknownSourceError`."""
        entry = self._by_id.get(source_id)
        if entry is None:
            raise UnknownSourceError("source_id is not registered")
        return entry

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[SourceRegistryEntry]:
        return iter(self._by_id.values())

    def __contains__(self, source_id: str) -> bool:
        return source_id in self._by_id
