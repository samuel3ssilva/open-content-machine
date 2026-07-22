"""Tests for the Founder-review consumption path (ticket OPUS-1.1b §1).

The review CSV is a stand-in for a PRIVATE local file; every fixture here is
synthetic. The suite pins the aggregation math, the yes/no + validation parsing,
and the two privacy guarantees: no title/note value ever reaches the output, and
the input file is never modified.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from content_machine.audience.evaluate import (
    ReviewAggregateReport,
    evaluate_review,
    evaluate_review_csv,
    load_review_csv,
    render_review_report,
)

_COLUMNS = [
    "position",
    "role_family",
    "seniority",
    "confidence",
    "rule_evidence",
    "family_correct",
    "expected_family",
    "seniority_correct",
    "expected_seniority",
    "reviewer_notes",
]


def _write_review(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in _COLUMNS})
    return path


# A deterministic 6-row set with hand-computable aggregates.
_SAMPLE_ROWS: list[dict[str, str]] = [
    # 1: high, family yes, seniority yes
    {"position": "p1", "role_family": "engineering_data_ai", "confidence": "high",
     "family_correct": "yes", "seniority_correct": "yes"},
    # 2: high, family NO (ops->marketing), seniority NO
    {"position": "p2", "role_family": "operations_people_finance_legal",
     "confidence": "high", "family_correct": "no",
     "expected_family": "marketing_growth_content", "seniority_correct": "no",
     "expected_seniority": "manager_lead"},
    # 3: medium, family yes, seniority yes
    {"position": "p3", "role_family": "product", "confidence": "medium",
     "family_correct": "yes", "seniority_correct": "yes"},
    # 4: low, family NO (product->design), seniority blank
    {"position": "p4", "role_family": "product", "confidence": "low",
     "family_correct": "no", "expected_family": "design_ux"},
    # 5: unknown confidence, family yes, seniority yes
    {"position": "p5", "role_family": "sales_bd_partnerships", "confidence": "unknown",
     "family_correct": "yes", "seniority_correct": "yes"},
    # 6: fully unanswered
    {"position": "p6", "role_family": "product", "confidence": "high"},
]


def test_aggregates_are_exact(tmp_path: Path) -> None:
    path = _write_review(tmp_path / "review.csv", _SAMPLE_ROWS)
    report = evaluate_review_csv(path)

    assert isinstance(report, ReviewAggregateReport)
    assert report.records_total == 6
    assert report.records_reviewed == 5
    assert report.records_unanswered == 1
    assert report.family_answered == 5
    assert report.family_unanswered == 1
    assert report.seniority_answered == 4
    assert report.seniority_unanswered == 2
    assert report.family_accuracy == 0.6  # 3 yes / 5 answered
    assert report.seniority_accuracy == 0.75  # 3 yes / 4 answered


def test_confidence_precision_and_unknown_rate(tmp_path: Path) -> None:
    path = _write_review(tmp_path / "review.csv", _SAMPLE_ROWS)
    report = evaluate_review_csv(path)

    by_level = {cp.confidence: cp for cp in report.confidence_precision}
    assert by_level["high"].denom == 2 and by_level["high"].correct == 1
    assert by_level["high"].precision == 0.5
    assert by_level["medium"].precision == 1.0 and by_level["medium"].denom == 1
    assert by_level["low"].precision == 0.0 and by_level["low"].denom == 1
    # The unknown-confidence row is EXCLUDED from every precision denominator.
    assert sum(cp.denom for cp in report.confidence_precision) == 4
    assert report.unknown_confidence_rate == 0.2  # 1 of 5 answered


def test_confusion_and_errors_per_family(tmp_path: Path) -> None:
    path = _write_review(tmp_path / "review.csv", _SAMPLE_ROWS)
    report = evaluate_review_csv(path)

    cells = {(c.expected, c.predicted): c.count for c in report.family_confusion}
    assert cells == {
        ("marketing_growth_content", "operations_people_finance_legal"): 1,
        ("design_ux", "product"): 1,
    }
    errors = {e.category: e.count for e in report.errors_per_family}
    assert errors == {"operations_people_finance_legal": 1, "product": 1}


def test_portuguese_yes_no_variants(tmp_path: Path) -> None:
    rows = [
        {"position": "a", "role_family": "product", "confidence": "high",
         "family_correct": "SIM", "seniority_correct": "não"},
        {"position": "b", "role_family": "product", "confidence": "high",
         "family_correct": "nao", "seniority_correct": "Y"},
    ]
    path = _write_review(tmp_path / "pt.csv", rows)
    report = evaluate_review_csv(path)
    # family: one yes (sim), one no (nao) -> accuracy 0.5
    assert report.family_answered == 2
    assert report.family_accuracy == 0.5
    # seniority: one no (não), one yes (Y) -> accuracy 0.5
    assert report.seniority_answered == 2
    assert report.seniority_accuracy == 0.5


def test_unanswered_rows_are_counted_not_scored(tmp_path: Path) -> None:
    rows = [
        {"position": "a", "role_family": "product", "confidence": "high"},
        {"position": "b", "role_family": "product", "confidence": "high",
         "family_correct": "", "seniority_correct": ""},
        {"position": "c", "role_family": "product", "confidence": "high",
         "family_correct": "yes"},
    ]
    path = _write_review(tmp_path / "u.csv", rows)
    report = evaluate_review_csv(path)
    assert report.records_total == 3
    assert report.records_unanswered == 2
    assert report.records_reviewed == 1
    assert report.family_answered == 1
    # Empty precision denominators are defined as 1.0 (mirrors evaluate()).
    assert report.seniority_accuracy == 1.0


def test_invalid_expected_family_reports_row_without_value(tmp_path: Path) -> None:
    rows = [
        {"position": "a", "role_family": "product", "confidence": "high",
         "family_correct": "no", "expected_family": "bananacraft"},
    ]
    path = _write_review(tmp_path / "bad.csv", rows)
    with pytest.raises(ValueError) as excinfo:
        load_review_csv(path)
    message = str(excinfo.value)
    assert "Row 2" in message
    assert "expected_family" in message
    assert "bananacraft" not in message  # the offending value is never echoed


def test_invalid_expected_seniority_reports_row(tmp_path: Path) -> None:
    rows = [
        {"position": "a", "role_family": "product", "confidence": "high",
         "seniority_correct": "no", "expected_seniority": "archduke"},
    ]
    path = _write_review(tmp_path / "bad.csv", rows)
    with pytest.raises(ValueError) as excinfo:
        load_review_csv(path)
    message = str(excinfo.value)
    assert "Row 2" in message
    assert "expected_seniority" in message
    assert "archduke" not in message


def test_unrecognized_answer_reports_row_without_value(tmp_path: Path) -> None:
    rows = [
        {"position": "a", "role_family": "product", "confidence": "high",
         "family_correct": "perhapsish"},
    ]
    path = _write_review(tmp_path / "bad.csv", rows)
    with pytest.raises(ValueError) as excinfo:
        load_review_csv(path)
    message = str(excinfo.value)
    assert "Row 2" in message
    assert "family_correct" in message
    assert "perhapsish" not in message


def test_missing_required_column_errors(tmp_path: Path) -> None:
    path = tmp_path / "missing.csv"
    path.write_text(
        "position,role_family,confidence,family_correct\n"
        "p,product,high,yes\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_review_csv(path)
    assert "expected_family" in str(excinfo.value)


def test_no_position_or_note_value_reaches_output(tmp_path: Path) -> None:
    # Distinctive sentinels in the columns that must never surface.
    rows = [
        {"position": "ZZZSECRETTITLE", "role_family": "product", "confidence": "high",
         "rule_evidence": "ZZZEVIDENCE", "family_correct": "no",
         "expected_family": "design_ux", "seniority_correct": "yes",
         "reviewer_notes": "ZZZPRIVATENOTE"},
    ]
    path = _write_review(tmp_path / "leak.csv", rows)
    report = evaluate_review_csv(path)
    rendered = render_review_report(report)
    blob = report.model_dump_json()
    for sentinel in ("ZZZSECRETTITLE", "ZZZEVIDENCE", "ZZZPRIVATENOTE"):
        assert sentinel not in rendered
        assert sentinel not in blob


def test_input_file_is_never_modified(tmp_path: Path) -> None:
    path = _write_review(tmp_path / "immutable.csv", _SAMPLE_ROWS)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    evaluate_review(load_review_csv(path))
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = _write_review(tmp_path / "blanks.csv", _SAMPLE_ROWS)
    # Append a fully blank data line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(",,,,,,,,,\n")
    report = evaluate_review_csv(path)
    assert report.records_total == 6  # the blank line is not a record
