"""Tests for content_machine.intelligence.models and .loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from content_machine.intelligence.loader import (
    ProfileLoadError,
    SignalLoadError,
    load_profile,
    load_signals,
)
from content_machine.intelligence.models import TOPIC_TAXONOMY, RelevanceProfile, SourceItem

REPO_ROOT = Path(__file__).resolve().parents[1]
VALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-synthetic.json"
INVALID_FIXTURE = REPO_ROOT / "examples" / "intelligence-signals-invalid.json"
PROFILE_FIXTURE = REPO_ROOT / "examples" / "intelligence-profile-synthetic.json"


# --- valid fixture loads whole, no issues -----------------------------------


def test_valid_fixture_loads_whole_with_no_issues() -> None:
    result = load_signals(VALID_FIXTURE)
    assert result.issues == []
    assert len(result.items) == 40
    assert all(isinstance(item, SourceItem) for item in result.items)


def test_valid_fixture_topic_tags_are_subset_of_taxonomy() -> None:
    result = load_signals(VALID_FIXTURE)
    for item in result.items:
        assert set(item.topic_tags) <= TOPIC_TAXONOMY


# --- invalid fixture: every malformed case is handled without crashing -----


def test_invalid_fixture_accepts_only_the_one_recoverable_item() -> None:
    result = load_signals(INVALID_FIXTURE)
    # Only bad004 (unparseable publication_date) is recoverable; every other
    # malformed item is skipped.
    assert len(result.items) == 1
    assert result.items[0].item_id == "bad004"
    assert result.items[0].publication_date is None


def test_unknown_fields_are_skipped_and_issue_names_field_only() -> None:
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.kind == "unknown_fields")
    assert issue.fields == ["internal_priority_score"]
    assert issue.item_index == 0


def test_missing_required_field_is_skipped_with_issue() -> None:
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.kind == "missing_field")
    assert "stable_reference" in issue.fields
    assert "summary_normalized" in issue.fields
    assert "change_class_rationale" in issue.fields


def test_invalid_detection_date_skips_the_item() -> None:
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.item_index == 2)
    assert issue.kind == "invalid_date"
    assert issue.fields == ["detection_date"]
    assert not any(item.item_id == "bad003" for item in result.items)


def test_invalid_publication_date_keeps_item_with_none() -> None:
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.item_index == 3)
    assert issue.kind == "invalid_date"
    assert issue.fields == ["publication_date"]
    kept = next(item for item in result.items if item.item_id == "bad004")
    assert kept.publication_date is None


def test_unknown_topic_tag_is_rejected() -> None:
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.kind == "unknown_topic_tag")
    assert issue.fields == ["quantum-computing"]
    assert not any(item.item_id == "bad005" for item in result.items)


def test_invalid_enum_literal_is_invalid_value_with_field_name_only() -> None:
    """F3: a well-shaped item with an invalid enum literal (e.g.
    evidence_type='bogus') must hit the generic ValidationError catch and be
    reported as kind='invalid_value' naming the field only, never the bad
    value."""
    result = load_signals(INVALID_FIXTURE)
    issue = next(i for i in result.issues if i.kind == "invalid_value")
    assert issue.fields == ["evidence_type"]
    assert "bogus" not in issue.message
    assert not any(item.item_id == "bad006" for item in result.items)


def test_unknown_topic_tag_taxonomy_shaped_tag_is_echoed_verbatim(tmp_path: Path) -> None:
    """S8/F1: a taxonomy-SHAPED unknown tag (lowercase, hyphens, digits only)
    is safe to echo verbatim -- it's plausibly just a new/misspelled tag."""
    path = tmp_path / "signals.json"
    path.write_text(
        json.dumps(
            [
                {
                    "item_id": "shape-ok",
                    "source_type": "feed",
                    "source_category": "vendor_blog",
                    "publisher_id": "vendor-shape",
                    "subject_entity_ids": ["vendor-shape"],
                    "title": "t",
                    "summary_normalized": "s",
                    "publication_date": "2026-06-01",
                    "detection_date": "2026-06-01",
                    "stable_reference": "https://example.com/shape",
                    "evidence_type": "announcement",
                    "change_class": "material_change",
                    "change_class_rationale": "n/a",
                    "action_required": "none",
                    "experiment_affordance": "not_testable",
                    "topic_tags": ["quantum-computing"],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = load_signals(path)
    issue = next(i for i in result.issues if i.kind == "unknown_topic_tag")
    assert issue.fields == ["quantum-computing"]


def test_unknown_topic_tag_non_conforming_tag_is_never_leaked(tmp_path: Path) -> None:
    """S8/F1: a tag that is NOT taxonomy-shaped (here, email-shaped) must
    never appear verbatim in a LoadIssue -- only the literal placeholder,
    with the non-conforming count visible in the message."""
    path = tmp_path / "signals.json"
    path.write_text(
        json.dumps(
            [
                {
                    "item_id": "shape-bad",
                    "source_type": "feed",
                    "source_category": "vendor_blog",
                    "publisher_id": "vendor-shape",
                    "subject_entity_ids": ["vendor-shape"],
                    "title": "t",
                    "summary_normalized": "s",
                    "publication_date": "2026-06-01",
                    "detection_date": "2026-06-01",
                    "stable_reference": "https://example.com/shape-bad",
                    "evidence_type": "announcement",
                    "change_class": "material_change",
                    "change_class_rationale": "n/a",
                    "action_required": "none",
                    "experiment_affordance": "not_testable",
                    "topic_tags": ["not.a-real-tag@example.com"],
                }
            ]
        ),
        encoding="utf-8",
    )
    result = load_signals(path)
    issue = next(i for i in result.issues if i.kind == "unknown_topic_tag")
    assert issue.fields == ["<non-conforming>"]
    assert "@example.com" not in issue.message
    assert "1 non-conforming" in issue.message


def test_issues_never_contain_free_text_field_values() -> None:
    """Titles/summaries/publisher ids from the malformed items must never leak
    into an issue's message or fields list -- only field names, tag names, or
    counts."""
    result = load_signals(INVALID_FIXTURE)
    raw_items = json.loads(INVALID_FIXTURE.read_text(encoding="utf-8"))
    free_text_values = [
        raw_items[0]["title"],
        raw_items[1]["title"],
        raw_items[2]["title"],
        raw_items[2]["summary_normalized"],
        raw_items[3]["title"],
        raw_items[4]["title"],
        raw_items[4]["summary_normalized"],
    ]
    for issue in result.issues:
        blob = issue.message + " ".join(issue.fields)
        for value in free_text_values:
            assert value not in blob


# --- file-level failures -----------------------------------------------------


def test_load_signals_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SignalLoadError):
        load_signals(tmp_path / "does-not-exist.json")


def test_load_signals_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SignalLoadError):
        load_signals(path)


def test_load_signals_non_array_top_level_raises(tmp_path: Path) -> None:
    path = tmp_path / "object.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(SignalLoadError):
        load_signals(path)


def test_load_signals_non_object_entry_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(["just a string", 42]), encoding="utf-8")
    result = load_signals(path)
    assert result.items == []
    assert len(result.issues) == 2
    assert all(i.kind == "invalid_value" for i in result.issues)


# --- RelevanceProfile / load_profile -----------------------------------------


def test_synthetic_profile_loads_and_validates() -> None:
    profile = load_profile(PROFILE_FIXTURE)
    assert isinstance(profile, RelevanceProfile)
    assert profile.profile_version
    assert profile.territories
    assert profile.experiment_budget in {"low", "medium", "high"}


def test_load_profile_default_points_at_synthetic_fixture() -> None:
    # The documented default must be the synthetic fixture, never a private path.
    profile = load_profile()
    assert profile.profile_version == "synthetic-1"


def test_load_profile_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileLoadError):
        load_profile(tmp_path / "missing-profile.json")


def test_load_profile_invalid_shape_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad-profile.json"
    path.write_text(json.dumps({"profile_version": "x"}), encoding="utf-8")
    with pytest.raises(ProfileLoadError):
        load_profile(path)


# --- SourceItem / RelevanceProfile model contracts --------------------------


def test_source_item_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SourceItem.model_validate(
            {
                "item_id": "x",
                "source_type": "feed",
                "source_category": "vendor_blog",
                "publisher_id": "vendor-x",
                "subject_entity_ids": [],
                "title": "t",
                "summary_normalized": "s",
                "detection_date": "2026-01-01",
                "stable_reference": "https://example.com/x",
                "evidence_type": "announcement",
                "change_class": "material_change",
                "change_class_rationale": "n/a",
                "action_required": "none",
                "experiment_affordance": "not_testable",
                "topic_tags": [],
                "extra_field": "not allowed",
            }
        )


def test_relevance_profile_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RelevanceProfile.model_validate(
            {
                "profile_version": "v1",
                "territories": [],
                "live_questions": [],
                "current_tooling": [],
                "experiment_budget": "low",
                "extra_field": "nope",
            }
        )
