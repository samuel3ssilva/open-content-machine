"""Authoritative performance test: the full deterministic pipeline, driven
through the real CSV loader (not hand-built in-memory objects), must handle a
realistic 8,000-row dataset well under 10 seconds. Kept generous so it flags a
real regression (e.g. an accidental O(n^2)) without being flaky on slow CI.

Row content is fully deterministic: it cycles fixed title/company lists by
index (no ``random`` module, seeded or otherwise), so the dataset -- and this
test's outcome -- is identical on every run and every machine.
"""

from __future__ import annotations

import time
from pathlib import Path

from content_machine.audience.normalize import normalize
from content_machine.audience.report import analyze
from content_machine.ingestion.csv_loader import load_csv
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

_ROW_COUNT = 8_000


def _write_synthetic_csv(path: Path, n: int) -> None:
    header = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
    lines = [header]
    for i in range(n):
        lines.append(
            f"First{i},Last{i},https://example.com/{i},u{i}@example.com,"
            f"{_COMPANIES[i % len(_COMPANIES)]},{_TITLES[i % len(_TITLES)]},18 Apr 2024"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_8000_rows_load_normalize_anonymize_report_under_10s(tmp_path: Path) -> None:
    csv_path = tmp_path / "synthetic-8000.csv"
    _write_synthetic_csv(csv_path, _ROW_COUNT)

    start = time.perf_counter()
    load = load_csv(csv_path)
    norm = normalize(load)
    anon = anonymize(norm, salt="perf")
    report = analyze(anon, load, norm)
    elapsed = time.perf_counter() - start

    assert len(load.rows) == _ROW_COUNT
    assert report.totals.total_rows == _ROW_COUNT
    assert report.totals.unique_connections == _ROW_COUNT  # every row has a unique name
    assert report.valid_rows == _ROW_COUNT
    assert elapsed < 10.0, f"pipeline took {elapsed:.2f}s (>10s)"
