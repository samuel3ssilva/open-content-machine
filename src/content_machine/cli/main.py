"""Typer CLI for Open Content Machine (docs/architecture.md §5).

All commands run offline with no API key. User errors are reported as friendly
messages with non-zero exit codes, never tracebacks (§6). Error text references
rows by index and columns by name, never personal field values.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

import typer

from content_machine import __version__
from content_machine.audience.normalize import normalize
from content_machine.audience.public_report import (
    PublicReport,
    sanitize,
)
from content_machine.audience.public_report import to_json as public_to_json
from content_machine.audience.public_report import to_markdown as public_to_markdown
from content_machine.audience.report import AudienceReport, analyze, to_json, to_markdown
from content_machine.config.settings import get_settings
from content_machine.ingestion.csv_loader import CsvLoadError, LoadResult, load_csv
from content_machine.privacy.anonymizer import anonymize

app = typer.Typer(
    help="Open Content Machine: local-first, privacy-by-design audience intelligence.",
    no_args_is_help=True,
    add_completion=False,
)

audience_app = typer.Typer(
    help="Audience intelligence commands (validate, anonymize, report).",
    no_args_is_help=True,
)
app.add_typer(audience_app, name="audience")

# Path to the shipped synthetic example, resolved relative to the repo root.
_EXAMPLE_CSV = Path(__file__).resolve().parents[3] / "examples" / "synthetic-connections.csv"


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


def _load_or_exit(file: Path) -> LoadResult:
    """Load a CSV, converting user-level load errors into a clean exit."""
    try:
        return load_csv(file)
    except CsvLoadError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


@audience_app.command("validate")
def audience_validate(
    file: Annotated[Path, typer.Argument(help="Path to a connections CSV export.")],
) -> None:
    """Validate a CSV and print a quality summary. Exit 1 if unreadable."""
    result = _load_or_exit(file)
    norm = normalize(result)

    all_columns = ["first_name", "last_name", "url", "email", "company", "position", "connected_on"]
    present = set(result.columns_present)
    missing = [c for c in all_columns if c not in present]

    issue_counts: Counter[str] = Counter(issue.kind for issue in result.issues)

    typer.echo(f"File: {file}")
    typer.echo(f"Encoding: {result.encoding_used}")
    typer.echo(f"Skipped preamble lines: {result.skipped_preamble_lines}")
    typer.echo(f"Rows parsed: {len(result.rows)}")
    typer.echo(f"Columns present: {', '.join(result.columns_present) or '(none)'}")
    typer.echo(f"Columns missing: {', '.join(missing) or '(none)'}")
    typer.echo(f"Duplicates detected: {len(norm.duplicate_pairs)}")
    typer.echo("Issues by kind:")
    for kind in ("missing_value", "empty_row", "parse_error"):
        typer.echo(f"  {kind}: {issue_counts.get(kind, 0)}")
    raise typer.Exit(code=0)


@audience_app.command("anonymize")
def audience_anonymize(
    file: Annotated[Path, typer.Argument(help="Path to a connections CSV export.")],
    output: Annotated[
        Path, typer.Option("-o", "--output", help="Where to write the anonymized JSON list.")
    ],
) -> None:
    """Anonymize a CSV and write the safe-zone JSON list."""
    result = _load_or_exit(file)
    norm = normalize(result)
    settings = get_settings()
    anon = anonymize(norm, settings.salt)

    if anon.ephemeral_salt:
        typer.secho(
            "Warning: no CONTENT_MACHINE_SALT set; using an ephemeral salt. "
            "Pseudonym IDs will NOT be stable across runs.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    payload = "[\n" + ",\n".join(
        "  " + c.model_dump_json() for c in anon.connections
    ) + "\n]\n" if anon.connections else "[]\n"
    output.write_text(payload, encoding="utf-8")
    typer.echo(f"Wrote {len(anon.connections)} anonymized records to {output}")
    raise typer.Exit(code=0)


@audience_app.command("report")
def audience_report(
    file: Annotated[Path, typer.Argument(help="Path to a connections CSV export.")],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Write the Markdown report to this path."),
    ] = None,
    json_output: Annotated[
        Path | None, typer.Option("--json", help="Write the JSON report to this path.")
    ] = None,
) -> None:
    """Run the full pipeline and render a Markdown (+ optional JSON) report."""
    markdown, json_text, ephemeral = _run_report(file)

    if ephemeral:
        typer.secho(
            "Warning: no CONTENT_MACHINE_SALT set; using an ephemeral salt. "
            "Pseudonym IDs will NOT be stable across runs.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    wrote_any = False
    if output is not None:
        output.write_text(markdown, encoding="utf-8")
        typer.echo(f"Wrote Markdown report to {output}")
        wrote_any = True
    if json_output is not None:
        json_output.write_text(json_text + "\n", encoding="utf-8")
        typer.echo(f"Wrote JSON report to {json_output}")
        wrote_any = True

    if not wrote_any:
        typer.echo(markdown)
    raise typer.Exit(code=0)


# Direct identifiers that anonymization always removes (never masks).
_DIRECT_IDENTIFIERS = ("first_name", "last_name", "email", "url")
# The fixed set of pipeline transformations, in order.
_TRANSFORMATIONS = ("normalize", "dedup", "pseudonymize", "classify", "aggregate")
# Generic output names -- the input path is NEVER echoed into a persisted name.
_WOULD_CREATE = ("./audience-report.md", "./audience-report.json")


def _human_size(num_bytes: int) -> str:
    """Human-readable byte size, e.g. ``12.3 KB``."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"  # pragma: no cover


@audience_app.command("inspect")
def audience_inspect(
    file: Annotated[
        Path, typer.Argument(help="Path to an EXTERNAL connections CSV to inspect.")
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Required: perform a read-only, privacy-safe inspection."),
    ] = False,
) -> None:
    """Privacy-safe, read-only inspection of an external CSV (dry-run only).

    Parses the file in place (never copies it, never persists anything) and
    prints STRUCTURE ONLY: types, sizes, column names, transformations, and the
    files that *would* be created. It never prints a single cell value.
    """
    if not dry_run:
        typer.secho(
            "Error: 'audience inspect' only supports the read-only --dry-run mode "
            "in this version. Re-run with --dry-run to inspect the file safely.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    result = _load_or_exit(file)

    try:
        size_bytes = file.stat().st_size
    except OSError:
        size_bytes = 0

    accepted = sorted(result.columns_present)
    ignored = list(result.ignored_headers)
    empty_rows = sum(1 for i in result.issues if i.kind == "empty_row")
    unparseable = 0  # dates are parsed downstream; inspect stays load-only.

    identifiers_present = [c for c in _DIRECT_IDENTIFIERS if c in result.columns_present]

    lines: list[str] = []
    lines.append("Open Content Machine — audience inspect (dry run)")
    lines.append("")
    lines.append(f"File: {file}")
    lines.append("File type: CSV (recognized)")
    lines.append(f"File size: {_human_size(size_bytes)} ({size_bytes} bytes)")
    lines.append(f"Encoding detected: {result.encoding_used}")
    lines.append(f"Skipped preamble lines: {result.skipped_preamble_lines}")
    lines.append(
        f"Data rows: {len(result.rows)} (count only; values parsed in memory, never displayed)"
    )
    lines.append("")
    lines.append(
        "Column names found: " + (", ".join(result.header_fields) or "(none)")
    )
    lines.append(
        "Columns accepted by the pipeline: " + (", ".join(accepted) or "(none)")
    )
    lines.append("Columns ignored (unmapped): " + (", ".join(ignored) or "(none)"))
    lines.append("")
    lines.append(
        "Direct identifiers that will be REMOVED at anonymization: "
        + (", ".join(identifiers_present) or "(none present)")
    )
    lines.append("Transformations that would be applied: " + ", ".join(_TRANSFORMATIONS))
    lines.append("")
    lines.append("Output files that WOULD be created (nothing written now):")
    for name in _WOULD_CREATE:
        lines.append(f"  - {name}")
    lines.append("")
    lines.append("Network access: none (offline by design)")
    lines.append("Source file copied: no")
    lines.append("")

    warnings: list[str] = []
    if ignored:
        warnings.append(f"{len(ignored)} unmapped column(s) will be ignored.")
    if empty_rows:
        warnings.append(f"{empty_rows} empty row(s) present; they will be skipped.")
    missing_core = [
        c for c in ("first_name", "last_name", "company", "position")
        if c not in result.columns_present
    ]
    if missing_core:
        warnings.append(
            "Expected column(s) not found: " + ", ".join(missing_core) + "."
        )
    if unparseable:  # pragma: no cover - reserved for future date pre-scan.
        warnings.append(f"{unparseable} date value(s) may not parse.")

    lines.append("Warnings:")
    if warnings:
        for w in warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("  - (none)")

    typer.echo("\n".join(lines))
    raise typer.Exit(code=0)


@audience_app.command("export-public")
def audience_export_public(
    private_report: Annotated[
        Path, typer.Argument(help="Path to a previously generated private report JSON.")
    ],
    output: Annotated[
        Path | None,
        typer.Option("-o", "--output", help="Where to write the sanitized public JSON."),
    ] = None,
    md_output: Annotated[
        Path | None,
        typer.Option("--md", help="Also write a sanitized Markdown artifact here."),
    ] = None,
) -> None:
    """Sanitize a private report JSON into a shareable public artifact.

    Suppresses every group under 10. Never runs automatically; requires an
    explicit ``-o`` output path.
    """
    if output is None:
        typer.secho(
            "Error: an output path is required. Pass -o/--output to choose where "
            "the sanitized public report is written.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        raw = private_report.read_text(encoding="utf-8")
    except OSError as exc:
        typer.secho(f"Error: could not read {private_report}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    try:
        report = AudienceReport.model_validate_json(raw)
    except ValueError:
        typer.secho(
            f"Error: {private_report} is not a valid audience report JSON.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None

    public: PublicReport = sanitize(report)
    output.write_text(public_to_json(public) + "\n", encoding="utf-8")
    typer.echo(f"Wrote sanitized public report to {output}")

    if md_output is not None:
        md_output.write_text(public_to_markdown(public), encoding="utf-8")
        typer.echo(f"Wrote sanitized Markdown report to {md_output}")

    raise typer.Exit(code=0)


@app.command()
def demo() -> None:
    """Run the full report pipeline on the shipped synthetic example (stdout)."""
    if not _EXAMPLE_CSV.exists():
        typer.secho(
            f"Error: example file not found at {_EXAMPLE_CSV}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)
    markdown, _json_text, _ephemeral = _run_report(_EXAMPLE_CSV)
    typer.echo(markdown)
    raise typer.Exit(code=0)


def _run_report(file: Path) -> tuple[str, str, bool]:
    """Shared pipeline: load -> normalize -> anonymize -> analyze -> render."""
    result = _load_or_exit(file)
    norm = normalize(result)
    settings = get_settings()
    anon = anonymize(norm, settings.salt)
    report = analyze(anon, result, norm)
    return to_markdown(report), to_json(report), anon.ephemeral_salt


if __name__ == "__main__":  # pragma: no cover
    app()
