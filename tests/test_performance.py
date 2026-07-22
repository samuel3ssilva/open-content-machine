"""Performance smoke test: the full deterministic pipeline must handle a
realistic 8,000-row dataset well under 10 seconds. Kept generous so it flags a
real regression (e.g. an accidental O(n^2)) without being flaky on slow CI.
"""

from __future__ import annotations

import time

from content_machine.audience.normalize import normalize
from content_machine.audience.report import analyze
from content_machine.ingestion.csv_loader import LoadResult, RawConnection
from content_machine.privacy.anonymizer import anonymize

_TITLES = [
    "Software Engineer",
    "CFO",
    "Product Manager",
    "UX Designer",
    "Marketing Manager",
    "Data Scientist",
    "Consultant",
    "Head of Data",
    "Student",
    "Vice President of Sales",
]
_COMPANIES = ["Acme Analytics Inc", "Umbrella Robotics", "DadosCorp Ltda", "Nimbus Cloud LLC"]


def _synthetic_load(n: int) -> LoadResult:
    rows = [
        RawConnection(
            row_index=i,
            first_name=f"First{i}",
            last_name=f"Last{i}",
            email=f"u{i}@example.com",
            url=f"https://example/{i}",
            company=_COMPANIES[i % len(_COMPANIES)],
            position=_TITLES[i % len(_TITLES)],
            connected_on="18 Apr 2024",
        )
        for i in range(n)
    ]
    return LoadResult(
        rows=rows,
        columns_present=[
            "first_name", "last_name", "url", "email", "company", "position", "connected_on"
        ],
        issues=[],
        encoding_used="utf-8",
        skipped_preamble_lines=0,
    )


def test_8000_rows_under_10s() -> None:
    load = _synthetic_load(8_000)
    start = time.perf_counter()
    norm = normalize(load)
    anon = anonymize(norm, salt="perf")
    report = analyze(anon, load, norm)
    elapsed = time.perf_counter() - start
    assert report.totals.total_rows == 8_000
    assert elapsed < 10.0, f"pipeline took {elapsed:.2f}s (>10s)"
