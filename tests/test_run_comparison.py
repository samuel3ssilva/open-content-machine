"""Tests for before/after run comparison math (ticket OPUS-1.1b §3)."""

from __future__ import annotations

from content_machine.audience.report import (
    AudienceReport,
    CountItem,
    ReportTotals,
    Segment,
)
from content_machine.audience.run_comparison import (
    ReclassificationStats,
    RunComparison,
    build_before_after,
    count_reclassified,
)
from content_machine.privacy.anonymizer import AnonymizedConnection


def _report(
    *,
    unknown_share: float,
    families: dict[str, int],
    seniorities: dict[str, int] | None = None,
    confidences: dict[str, int] | None = None,
    segments: list[tuple[str, int, float]] | None = None,
) -> AudienceReport:
    return AudienceReport(
        totals=ReportTotals(total_rows=0, unique_connections=0, duplicates=0),
        valid_rows=0,
        invalid_rows=0,
        role_family_distribution=[
            CountItem(label=k, count=v) for k, v in families.items()
        ],
        seniority_distribution=[
            CountItem(label=k, count=v) for k, v in (seniorities or {}).items()
        ],
        confidence_distribution=[
            CountItem(label=k, count=v) for k, v in (confidences or {}).items()
        ],
        unknown_share=unknown_share,
        candidate_segments=[
            Segment(name=n, size=s, share=sh, evidence=[], rationale="")
            for (n, s, sh) in (segments or [])
        ],
    )


def _conn(
    id_: str,
    *,
    family: str = "product",
    confidence: str = "high",
    seniority: str = "manager_lead",
) -> AnonymizedConnection:
    return AnonymizedConnection(
        id=id_,
        role_family=family,
        role_confidence=confidence,
        seniority_bucket=seniority,
    )


def test_build_before_after_unknown_reduction_and_deltas() -> None:
    before = _report(
        unknown_share=0.30,
        families={"unknown": 30, "product": 40, "engineering_data_ai": 30},
        segments=[("Product — managers & leads", 40, 0.4)],
    )
    after = _report(
        unknown_share=0.10,
        families={"unknown": 10, "product": 45, "engineering_data_ai": 45},
        segments=[
            ("Engineering, Data & AI — individual contributors", 45, 0.45),
        ],
    )
    comp = build_before_after(before, after)

    assert isinstance(comp, RunComparison)
    assert comp.before_unknown_rate == 0.30
    assert comp.after_unknown_rate == 0.10
    assert comp.unknown_reduction == 0.20

    deltas = {d.label: d.delta for d in comp.family_deltas}
    assert deltas == {"unknown": -20, "product": 5, "engineering_data_ai": 15}
    # Ordered by largest absolute delta first.
    assert comp.family_deltas[0].label == "unknown"

    assert comp.segments_added == [
        "Engineering, Data & AI — individual contributors"
    ]
    assert comp.segments_removed == ["Product — managers & leads"]


def test_build_before_after_handles_new_family_label() -> None:
    before = _report(unknown_share=0.0, families={"product": 10})
    after = _report(unknown_share=0.0, families={"product": 8, "design_ux": 2})
    comp = build_before_after(before, after)
    deltas = {d.label: (d.before, d.after, d.delta) for d in comp.family_deltas}
    assert deltas["design_ux"] == (0, 2, 2)
    assert deltas["product"] == (10, 8, -2)


def test_count_reclassified_matches_by_id() -> None:
    old = [
        _conn("id_a", family="unknown", confidence="unknown", seniority="unknown"),
        _conn("id_b", family="product", confidence="high", seniority="manager_lead"),
        _conn("id_c", family="engineering_data_ai", confidence="high"),
        _conn("id_only_old", family="product"),
    ]
    new = [
        # a: unknown -> classified (family + confidence + seniority all change)
        _conn("id_a", family="product", confidence="medium", seniority="manager_lead"),
        # b: unchanged
        _conn("id_b", family="product", confidence="high", seniority="manager_lead"),
        # c: classified -> unknown
        _conn("id_c", family="unknown", confidence="unknown", seniority="manager_lead"),
        # present only in new -> ignored
        _conn("id_only_new", family="product"),
    ]
    stats = count_reclassified(old, new)
    assert isinstance(stats, ReclassificationStats)
    assert stats.n_matched == 3  # a, b, c (only-old / only-new ignored)
    assert stats.n_family_changed == 2  # a and c
    assert stats.unknown_to_classified == 1  # a
    assert stats.classified_to_unknown == 1  # c
    assert stats.n_confidence_changed == 2  # a and c
    assert stats.n_seniority_changed == 1  # a: unknown -> manager_lead


def test_count_reclassified_duplicate_id_first_wins() -> None:
    old = [
        _conn("id_dup", family="product"),
        _conn("id_dup", family="engineering_data_ai"),  # ignored (dup)
    ]
    new = [
        _conn("id_dup", family="product"),
        _conn("id_dup", family="design_ux"),  # ignored (dup)
    ]
    stats = count_reclassified(old, new)
    assert stats.n_matched == 1
    assert stats.n_family_changed == 0  # first old vs first new: product == product


def test_count_reclassified_empty() -> None:
    stats = count_reclassified([], [])
    assert stats.n_matched == 0
    assert stats.n_family_changed == 0
