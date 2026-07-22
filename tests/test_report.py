"""Tests for report analytics and rendering."""

from __future__ import annotations

from content_machine.audience.normalize import normalize
from content_machine.audience.report import MANDATORY_CAVEAT, analyze, to_json, to_markdown
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import anonymize
from tests.conftest import SYNTHETIC_CSV, SYNTHETIC_NAMES


def _report():  # type: ignore[no-untyped-def]
    load = load_csv(SYNTHETIC_CSV)
    norm = normalize(load)
    anon = anonymize(norm, salt="fixed")
    return analyze(anon, load, norm), load, norm


def test_totals_counts() -> None:
    report, _load, _norm = _report()
    assert report.totals.total_rows == 30
    assert report.totals.duplicates == 2
    assert report.totals.unique_connections == 28


def test_top_companies_normalized() -> None:
    report, _load, _norm = _report()
    labels = {c.label for c in report.top_companies}
    assert "Acme Analytics" in labels
    # Legal suffixes must have been stripped before counting.
    assert "Umbrella Robotics" in labels
    assert "Umbrella Robotics Inc" not in labels


def test_connections_per_year_present() -> None:
    report, _load, _norm = _report()
    years = {c.label for c in report.connections_per_year}
    assert "2019" in years
    assert "2026" in years


def test_completeness_only_present_columns() -> None:
    report, _load, _norm = _report()
    # Email is sparse in the fixture but the column is present.
    assert "email" in report.completeness_pct
    assert report.completeness_pct["email"] < 100.0


def test_markdown_contains_caveat() -> None:
    report, _load, _norm = _report()
    md = to_markdown(report)
    assert MANDATORY_CAVEAT in md


def test_markdown_has_no_identifiers() -> None:
    report, _load, _norm = _report()
    md = to_markdown(report)
    assert "@" not in md
    assert "http://" not in md
    assert "https://" not in md
    for name in SYNTHETIC_NAMES:
        assert name not in md


def test_json_round_trips() -> None:
    report, _load, _norm = _report()
    text = to_json(report)
    assert '"total_rows": 30' in text
    assert MANDATORY_CAVEAT in text
