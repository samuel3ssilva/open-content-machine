"""Tests for content_machine.connectors.registry (Gate D §3).

Also carries the ONE invented example registry the spec asks the repo to
ship (a shape demonstration for tests only; the real ~20-source registry
lives in the Founder's private workspace and is never committed here)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from content_machine.connectors.registry import (
    DuplicateSourceIdError,
    PublisherClassification,
    SourceRegistry,
    SourceRegistryEntry,
    UnknownSourceError,
)

# The one invented example registry (spec §3): entirely synthetic vendor
# names, never a real source.
_EXAMPLE_ENTRIES = [
    SourceRegistryEntry(
        source_id="src_vendor_alpha_blog",
        source_group="grp_vendor_alpha",
        publisher_id="pub_vendor_alpha",
        source_category="vendor_blog",
        source_type="feed",
        publisher_classification=PublisherClassification.vendor_first_party,
        endpoint_label="Vendor Alpha Engineering Blog",
    ),
    SourceRegistryEntry(
        source_id="src_indie_analyst_newsletter",
        source_group="grp_indie_analyst",
        publisher_id="pub_indie_analyst",
        source_category="newsletter",
        source_type="email",
        publisher_classification=PublisherClassification.independent,
        endpoint_label="Indie Analyst Weekly Newsletter",
    ),
    SourceRegistryEntry(
        source_id="src_community_forum_digest",
        source_group="grp_community_forum",
        publisher_id="pub_community_forum",
        source_category="community_digest",
        source_type="doc",
        publisher_classification=PublisherClassification.community,
        endpoint_label="Community Forum Weekly Digest",
    ),
    SourceRegistryEntry(
        source_id="src_uncurated_feed",
        source_group="grp_uncurated",
        publisher_id="pub_uncurated",
        source_category="misc_feed",
        source_type="feed",
        publisher_classification=PublisherClassification.unclassified,
        endpoint_label="Not Yet Curated Feed",
    ),
]


def test_example_registry_is_synthetic_example_domain_only() -> None:
    for entry in _EXAMPLE_ENTRIES:
        assert not entry.endpoint_label.startswith("http")  # never a fetchable URL


# --- fail-closed publisher classification ----------------------------------


def test_may_supply_independence_true_only_for_independent_and_community() -> None:
    by_id = {e.source_id: e for e in _EXAMPLE_ENTRIES}
    assert by_id["src_indie_analyst_newsletter"].may_supply_independence is True
    assert by_id["src_community_forum_digest"].may_supply_independence is True


def test_may_supply_independence_false_for_vendor_first_party() -> None:
    by_id = {e.source_id: e for e in _EXAMPLE_ENTRIES}
    assert by_id["src_vendor_alpha_blog"].may_supply_independence is False


def test_may_supply_independence_false_for_unclassified_fail_closed() -> None:
    """The core fail-closed rule: an unclassified source can never supply
    independence -- treated at least as conservatively as a known vendor."""
    by_id = {e.source_id: e for e in _EXAMPLE_ENTRIES}
    assert by_id["src_uncurated_feed"].may_supply_independence is False


# --- SourceRegistryEntry shape -------------------------------------------


def test_source_registry_entry_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SourceRegistryEntry(
            **_EXAMPLE_ENTRIES[0].model_dump(),
            unexpected_field="nope",  # type: ignore[call-arg]
        )


def test_source_registry_entry_endpoint_label_is_not_url_required() -> None:
    """endpoint_label is documented as opaque, NOT a URL -- any plain string
    is accepted, since there is no fetch path in this gate to validate
    against."""
    entry = SourceRegistryEntry(
        source_id="src_x",
        source_group="grp_x",
        publisher_id="pub_x",
        source_category="cat_x",
        source_type="feed",
        publisher_classification=PublisherClassification.unclassified,
        endpoint_label="just a label, not a URL",
    )
    assert entry.endpoint_label == "just a label, not a URL"


# --- SourceRegistry --------------------------------------------------------


def test_source_registry_get_and_require() -> None:
    registry = SourceRegistry(_EXAMPLE_ENTRIES)
    assert registry.get("src_vendor_alpha_blog") is not None
    assert registry.get("src_does_not_exist") is None
    assert registry.require("src_vendor_alpha_blog").publisher_id == "pub_vendor_alpha"
    with pytest.raises(UnknownSourceError):
        registry.require("src_does_not_exist")


def test_source_registry_rejects_duplicate_source_id() -> None:
    duplicate = _EXAMPLE_ENTRIES[0]
    with pytest.raises(DuplicateSourceIdError):
        SourceRegistry([duplicate, duplicate])


def test_source_registry_iteration_order_matches_construction_order() -> None:
    registry = SourceRegistry(_EXAMPLE_ENTRIES)
    assert [e.source_id for e in registry] == [e.source_id for e in _EXAMPLE_ENTRIES]


def test_source_registry_len_and_contains() -> None:
    registry = SourceRegistry(_EXAMPLE_ENTRIES)
    assert len(registry) == len(_EXAMPLE_ENTRIES)
    assert "src_vendor_alpha_blog" in registry
    assert "src_does_not_exist" not in registry
