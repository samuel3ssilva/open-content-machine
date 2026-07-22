"""Tests for the classifier evaluation harness and the synthetic labeled set.

Covers the metric-integrity contract (ticket OPUS-1.1 §7): unknown predictions
never enter a precision denominator, and no committed evaluation artifact may
carry a raw title.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from content_machine.audience.classify import RoleFamily, classify_role
from content_machine.audience.evaluate import (
    EvaluationReport,
    LabeledTitle,
    evaluate,
    evaluate_csv,
    load_labeled_csv,
)
from tests.conftest import REPO_ROOT

LABELED_CSV = REPO_ROOT / "tests" / "fixtures" / "labeled_titles_synthetic.csv"


def test_labeled_fixture_exists_and_is_reasonably_sized() -> None:
    # Sprint 1.1 grew the labeled fixture to >= 250 rows (ticket §2) for
    # broader PT/EN vocabulary regression coverage.
    labeled = load_labeled_csv(LABELED_CSV)
    assert len(labeled) >= 250


def test_synthetic_set_meets_quality_bar() -> None:
    report = evaluate_csv(LABELED_CSV)
    assert report.high_confidence_precision >= 0.90
    assert report.overall_classified_precision >= 0.90
    # unknown must stay reasonable on a labeled set (mostly well-formed titles).
    assert report.unknown_rate < 0.25


def test_no_functional_leadership_predicted_as_founder_executive() -> None:
    # The canonical cross-domain error must occur zero times: any labeled title
    # that is a functional director/head/VP (expected != founder_executive) must
    # not be predicted founder_executive.
    labeled = load_labeled_csv(LABELED_CSV)
    leadership_markers = ("director", "diretor", "head of", "vp of", "vice president")
    offenders: list[str] = []
    for row in labeled:
        lowered = row.title.casefold()
        is_leadership = any(m in lowered for m in leadership_markers)
        if not is_leadership or row.expected_family == RoleFamily.founder_executive.value:
            continue
        if classify_role(row.title).family is RoleFamily.founder_executive:
            offenders.append(row.title)
    assert not offenders, f"functional leadership mis-routed to founder_executive: {offenders}"


def test_report_contains_no_raw_titles() -> None:
    # A committed EvaluationReport must be aggregate-only: no fixture title may
    # appear anywhere in its serialized form (docs/privacy.md rule 6).
    labeled = load_labeled_csv(LABELED_CSV)
    report = evaluate_csv(LABELED_CSV)
    blob = report.model_dump_json()
    for row in labeled:
        assert row.title not in blob, f"title leaked into report: {row.title!r}"


# --- Metric-integrity unit tests -------------------------------------------


def test_unknown_predictions_excluded_from_precision_denominators() -> None:
    # "asdfqwerty" predicts unknown; it must not count against classified
    # precision, only raise unknown_rate.
    labeled = [
        LabeledTitle(
            title="Software Engineer",
            expected_family="engineering_data_ai",
            expected_seniority="individual_contributor",
        ),
        LabeledTitle(
            title="asdfqwerty",
            expected_family="engineering_data_ai",
            expected_seniority="unknown",
        ),
    ]
    report = evaluate(labeled)
    assert report.n == 2
    # One classified prediction (the engineer), correct -> precision 1.0.
    assert report.overall_classified_precision == 1.0
    assert report.high_confidence_precision == 1.0
    assert report.unknown_rate == 0.5


def test_wrong_classified_prediction_lowers_precision() -> None:
    labeled = [
        LabeledTitle(
            title="Marketing Manager",
            expected_family="engineering_data_ai",  # deliberately wrong label
            expected_seniority="manager_lead",
        ),
    ]
    report = evaluate(labeled)
    assert report.overall_classified_precision == 0.0
    assert report.unknown_rate == 0.0
    assert report.top_error_patterns
    pattern = report.top_error_patterns[0]
    assert pattern.expected == "engineering_data_ai"
    assert pattern.predicted == "marketing_growth_content"
    assert pattern.count == 1


def test_empty_labeled_set_is_defined() -> None:
    report = evaluate([])
    assert report.n == 0
    assert report.high_confidence_precision == 1.0
    assert report.overall_classified_precision == 1.0
    assert report.unknown_rate == 0.0


def test_confusion_cells_are_deterministic() -> None:
    labeled = load_labeled_csv(LABELED_CSV)
    first = evaluate(labeled)
    second = evaluate(labeled)
    assert first.model_dump() == second.model_dump()
    assert isinstance(first, EvaluationReport)


def test_load_labeled_csv_missing_column_errors_without_values(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("title,expected_family\nCEO,founder_executive\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        load_labeled_csv(bad)
    message = str(excinfo.value)
    assert "expected_seniority" in message
    # Error references the column, never a field value.
    assert "CEO" not in message


def test_load_labeled_csv_empty_required_cell_errors_by_row(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "title,expected_family,expected_seniority\n"
        "Software Engineer,,individual_contributor\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_labeled_csv(bad)
    message = str(excinfo.value)
    assert "Row 2" in message
    assert "expected_family" in message
    assert "Software Engineer" not in message
