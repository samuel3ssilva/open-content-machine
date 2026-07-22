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
from content_machine.audience.report import analyze, to_json, to_markdown
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
