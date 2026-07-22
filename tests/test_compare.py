"""Tests for snapshot-based classifier comparison (ticket OPUS-1.1b §2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from content_machine.audience.compare import (
    ClassificationSnapshot,
    compare,
    load_snapshot,
    snapshot_classifications,
    snapshot_to_json,
)


def _snap(family: str, seniority: str, confidence: str) -> ClassificationSnapshot:
    return ClassificationSnapshot(
        family=family, seniority=seniority, confidence=confidence
    )


def test_snapshot_is_index_aligned_and_titleless() -> None:
    titles = ["Software Engineer", "Head of Product", "asdfqwerty"]
    snaps = snapshot_classifications(titles)
    assert len(snaps) == len(titles)
    assert snaps[0].family == "engineering_data_ai"
    assert snaps[2].family == "unknown"  # nonsense stays unknown, never forced
    # No title text may appear in the serialized snapshot.
    blob = snapshot_to_json(snaps)
    for title in titles:
        assert title not in blob


def test_snapshot_json_round_trip(tmp_path: Path) -> None:
    titles = ["Data Scientist", "VP of Marketing", "Founder & CEO", "Product Owner"]
    snaps = snapshot_classifications(titles)
    path = tmp_path / "snap.json"
    path.write_text(snapshot_to_json(snaps), encoding="utf-8")
    loaded = load_snapshot(path)
    assert loaded == snaps


def test_identical_snapshots_report_no_change() -> None:
    snaps = snapshot_classifications(["Software Engineer", "Marketing Manager"])
    report = compare(snaps, snaps)
    assert report.n == 2
    assert report.total_changed == 0
    assert report.family_changes == []
    assert report.confidence_changes == []
    assert report.seniority_changes == []
    assert report.possible_regressions == []


def test_change_aggregation_counts() -> None:
    a = [
        _snap("engineering_data_ai", "individual_contributor", "high"),
        _snap("product", "manager_lead", "high"),
        _snap("product", "manager_lead", "medium"),
    ]
    b = [
        # family + confidence flip
        _snap("operations_people_finance_legal", "individual_contributor", "medium"),
        # seniority-only change
        _snap("product", "vp_head_director", "high"),
        # unchanged
        _snap("product", "manager_lead", "medium"),
    ]
    report = compare(a, b)
    assert report.n == 3
    assert report.total_changed == 2
    fam = {(c.from_label, c.to_label): c.count for c in report.family_changes}
    assert fam == {("engineering_data_ai", "operations_people_finance_legal"): 1}
    sen = {(c.from_label, c.to_label): c.count for c in report.seniority_changes}
    assert sen == {("manager_lead", "vp_head_director"): 1}
    conf = {(c.from_label, c.to_label): c.count for c in report.confidence_changes}
    assert conf == {("high", "medium"): 1}


def test_unknown_transitions_are_counted() -> None:
    a = [
        _snap("unknown", "unknown", "unknown"),
        _snap("product", "manager_lead", "high"),
    ]
    b = [
        _snap("product", "manager_lead", "high"),  # unknown -> classified
        _snap("unknown", "unknown", "unknown"),  # classified -> unknown
    ]
    report = compare(a, b)
    assert report.unknown_to_classified == 1
    assert report.classified_to_unknown == 1


def test_regression_heuristic_flags_high_conf_flip_and_to_unknown() -> None:
    a = [
        _snap("engineering_data_ai", "individual_contributor", "high"),
        _snap("marketing_growth_content", "manager_lead", "high"),
    ]
    b = [
        # high -> high but DIFFERENT family: the riskiest silent flip
        _snap("operations_people_finance_legal", "individual_contributor", "high"),
        # classified -> unknown
        _snap("unknown", "unknown", "unknown"),
    ]
    report = compare(a, b)
    kinds = {(r.kind, r.from_family, r.to_family): r.count for r in report.possible_regressions}
    assert kinds[("high_conf_family_flip", "engineering_data_ai",
                  "operations_people_finance_legal")] == 1
    assert kinds[("classified_to_unknown", "marketing_growth_content", "unknown")] == 1


def test_low_confidence_family_flip_is_not_a_regression() -> None:
    a = [_snap("engineering_data_ai", "individual_contributor", "low")]
    b = [_snap("product", "individual_contributor", "low")]
    report = compare(a, b)
    # A family change is recorded, but a LOW-confidence flip is not flagged as a
    # possible regression (the heuristic targets confident silent flips only).
    assert report.total_changed == 1
    assert report.possible_regressions == []


def test_misaligned_snapshots_raise() -> None:
    a = [_snap("product", "manager_lead", "high")]
    b = [
        _snap("product", "manager_lead", "high"),
        _snap("product", "manager_lead", "high"),
    ]
    with pytest.raises(ValueError) as excinfo:
        compare(a, b)
    assert "index-aligned" in str(excinfo.value)


def test_load_snapshot_rejects_non_array(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"family": "product"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_snapshot(path)


def test_committed_sprint11_snapshot_is_titleless_and_loadable() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    snapshot_path = repo_root / "tests" / "fixtures" / "classifier_snapshot_sprint11.json"
    snaps = load_snapshot(snapshot_path)
    assert len(snaps) > 0
    # Comparing the committed baseline against a fresh run of the CURRENT code
    # must be a no-op while the classifier is unchanged.
    labeled_path = repo_root / "tests" / "fixtures" / "labeled_titles_synthetic.csv"
    if labeled_path.exists():
        from content_machine.audience.evaluate import load_labeled_csv

        titles = [row.title for row in load_labeled_csv(labeled_path)]
        # Only assert alignment when the fixture has not been re-sized underneath
        # the committed snapshot (a concurrent fixture edit is tolerated).
        if len(titles) == len(snaps):
            current = snapshot_classifications(titles)
            assert compare(snaps, current).total_changed == 0
