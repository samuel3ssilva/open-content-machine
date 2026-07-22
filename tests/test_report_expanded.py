"""Tests for the expanded private report: new aggregates and candidate segments."""

from __future__ import annotations

from content_machine.audience.normalize import normalize
from content_machine.audience.report import analyze, to_json, to_markdown
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import anonymize
from tests.conftest import SYNTHETIC_CSV, SYNTHETIC_NAMES


def _report():  # type: ignore[no-untyped-def]
    load = load_csv(SYNTHETIC_CSV)
    norm = normalize(load)
    anon = anonymize(norm, salt="fixed")
    return analyze(anon, load, norm)


def test_new_aggregate_fields_present() -> None:
    report = _report()
    assert report.valid_rows == 30
    assert report.invalid_rows == 1  # the trailing empty row
    assert report.role_family_distribution
    assert report.confidence_distribution
    assert 0.0 <= report.unknown_share <= 1.0
    # Top roles are casefolded.
    labels = [c.label for c in report.top_roles]
    assert labels == [label.casefold() for label in labels]


def test_seniority_uses_new_seven_buckets() -> None:
    report = _report()
    allowed = {
        "founder_owner",
        "c_level",
        "vp_head_director",
        "manager_lead",
        "individual_contributor",
        "entry_student",
        "unknown",
    }
    for item in report.seniority_distribution:
        assert item.label in allowed
    # The old senior_ic/ic buckets must be gone.
    assert not any(i.label in {"senior_ic", "ic"} for i in report.seniority_distribution)


def test_candidate_segments_deterministic_and_bounded() -> None:
    report_a = _report()
    report_b = _report()
    assert [s.model_dump() for s in report_a.candidate_segments] == [
        s.model_dump() for s in report_b.candidate_segments
    ]
    assert 3 <= len(report_a.candidate_segments) <= 5
    for seg in report_a.candidate_segments:
        assert seg.size > 0
        assert seg.evidence  # deterministic evidence present
        assert 0.0 <= seg.share <= 1.0


def test_limitations_include_required_statements() -> None:
    report = _report()
    joined = " ".join(report.limitations).lower()
    assert "not evidence" in joined
    assert "heuristic" in joined
    assert "stale" in joined


def test_expanded_report_has_no_identifiers() -> None:
    report = _report()
    for blob in (to_markdown(report), to_json(report)):
        assert "@" not in blob
        assert "http://" not in blob
        assert "https://" not in blob
        for name in SYNTHETIC_NAMES:
            assert name not in blob
