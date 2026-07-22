"""Tests for public-report sanitization and `audience export-public`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from content_machine.audience.public_report import (
    SUPPRESSED_LABEL,
    PublicReport,
    sanitize,
)
from content_machine.audience.report import (
    AudienceReport,
    CountItem,
    ReportTotals,
    Segment,
)
from content_machine.cli.main import app

runner = CliRunner()


def _private_report() -> AudienceReport:
    return AudienceReport(
        totals=ReportTotals(total_rows=24, unique_connections=22, duplicates=2),
        valid_rows=24,
        invalid_rows=1,
        completeness_pct={"company": 100.0},
        top_companies=[
            CountItem(label="BigCo", count=12),
            CountItem(label="SmallCo", count=3),
        ],
        top_roles=[
            CountItem(label="engineer", count=15),
            CountItem(label="ceo", count=4),
        ],
        seniority_distribution=[
            CountItem(label="individual_contributor", count=20),
            CountItem(label="c_level", count=4),
        ],
        role_family_distribution=[
            CountItem(label="engineering_data_ai", count=15),
            CountItem(label="unknown", count=5),
            CountItem(label="product", count=4),
        ],
        confidence_distribution=[
            CountItem(label="high", count=18),
            CountItem(label="low", count=6),
        ],
        unknown_share=0.2083,
        connections_per_year=[
            CountItem(label="2020", count=12),
            CountItem(label="2021", count=3),
        ],
        candidate_segments=[
            Segment(name="Big segment", size=12, share=0.5, evidence=["x"], rationale="r"),
            Segment(name="Tiny segment", size=4, share=0.17, evidence=["y"], rationale="r"),
        ],
        limitations=["Connections are not evidence of interest in your content."],
        generated_notes=["note"],
    )


def test_sanitize_suppresses_small_groups() -> None:
    public = sanitize(_private_report())
    assert public.privacy_label == "sanitized-aggregate"

    # Top-lists: small groups dropped entirely.
    assert [c.label for c in public.top_companies] == ["BigCo"]
    assert [c.label for c in public.top_roles] == ["engineer"]

    # Distributions: small groups merged into one suppressed bucket.
    fam = {c.label: c.count for c in public.role_family_distribution}
    assert fam == {"engineering_data_ai": 15, SUPPRESSED_LABEL: 9}

    years = {c.label: c.count for c in public.connections_per_year}
    assert years == {"2020": 12, SUPPRESSED_LABEL: 3}

    # Segments under 10 are dropped entirely.
    assert [s.name for s in public.segments] == ["Big segment"]


def test_public_report_forbids_extra_and_has_no_evidence_field() -> None:
    public = sanitize(_private_report())
    # Public segments expose only name/size/share.
    assert set(public.segments[0].model_dump().keys()) == {"name", "size", "share"}


def test_cli_export_public_writes_sanitized_json(tmp_path: Path) -> None:
    src = tmp_path / "private.json"
    src.write_text(_private_report().model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "public.json"

    result = runner.invoke(
        app, ["audience", "export-public", str(src), "-o", str(out)]
    )
    assert result.exit_code == 0
    assert out.exists()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["privacy_label"] == "sanitized-aggregate"
    # Re-validates against the strict model.
    PublicReport.model_validate(data)

    blob = out.read_text(encoding="utf-8")
    assert "@" not in blob
    assert "http" not in blob
    assert "SmallCo" not in blob  # suppressed group did not leak
    assert "Tiny segment" not in blob  # suppressed segment did not leak


def test_cli_export_public_markdown_banner(tmp_path: Path) -> None:
    src = tmp_path / "private.json"
    src.write_text(_private_report().model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "public.json"
    md = tmp_path / "public.md"

    result = runner.invoke(
        app, ["audience", "export-public", str(src), "-o", str(out), "--md", str(md)]
    )
    assert result.exit_code == 0
    text = md.read_text(encoding="utf-8")
    assert "groups under 10 suppressed" in text
    assert "review before sharing" in text


def test_cli_export_public_requires_output(tmp_path: Path) -> None:
    src = tmp_path / "private.json"
    src.write_text(_private_report().model_dump_json(indent=2), encoding="utf-8")
    result = runner.invoke(app, ["audience", "export-public", str(src)])
    assert result.exit_code == 1
    assert "output path is required" in result.output


def test_cli_export_public_rejects_invalid_json(tmp_path: Path) -> None:
    src = tmp_path / "bad.json"
    src.write_text("{not valid audience report}", encoding="utf-8")
    out = tmp_path / "public.json"
    result = runner.invoke(
        app, ["audience", "export-public", str(src), "-o", str(out)]
    )
    assert result.exit_code == 1
