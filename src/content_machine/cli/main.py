"""Typer CLI for Open Content Machine (docs/architecture.md §5).

All commands run offline with no API key. User errors are reported as friendly
messages with non-zero exit codes, never tracebacks (§6). Error text references
rows by index and columns by name, never personal field values.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from content_machine import __version__
from content_machine.audience.compare import (
    compare,
    load_snapshot,
    render_comparison,
    snapshot_classifications,
    snapshot_to_json,
)
from content_machine.audience.evaluate import (
    evaluate_review,
    load_labeled_csv,
    load_review_csv,
    render_review_report,
)
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
from content_machine.sources.inventory import (
    FileStatus,
    PrivacyCategory,
    SourceScanError,
    scan_source_folder,
)
from content_machine.sources.inventory import to_json as source_to_json
from content_machine.sources.inventory import to_markdown as source_to_markdown
from content_machine.sources.inventory import to_review_csv as source_to_review_csv

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

source_app = typer.Typer(
    help="Private source folder commands (Phase 1: metadata-safe inventory).",
    no_args_is_help=True,
)
app.add_typer(source_app, name="source")

# The repo root, used both to locate the shipped example and to warn when a
# private review file is (mis)placed inside the version-controlled tree.
_REPO_ROOT = Path(__file__).resolve().parents[3]
# Path to the shipped synthetic example, resolved relative to the repo root.
_EXAMPLE_CSV = _REPO_ROOT / "examples" / "synthetic-connections.csv"


def _warn_if_in_repo(file: Path) -> None:
    """Warn (never fail) if a private input lives inside the repo tree.

    Real review exports must stay in ``data/private/`` (git-ignored) or fully
    outside the checkout; a file under the repo root risks being committed. The
    path itself is user-supplied, not a data value, so echoing it is safe.
    """
    try:
        file.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        return
    typer.secho(
        "Warning: this file is inside the repository tree. Private review files "
        "must never be committed — keep them in data/private/ or outside the repo.",
        fg=typer.colors.YELLOW,
        err=True,
    )


def _reject_if_in_repo(path: Path, *, what: str) -> None:
    """Hard-fail (exit 1) if ``path`` resolves inside the repository tree.

    Unlike :func:`_warn_if_in_repo`, this is used where a private source
    folder or its outputs must NEVER live inside the version-controlled
    checkout — a warning is not enough. The path is echoed back because it is
    user-supplied CLI input, not a data value.
    """
    try:
        path.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        return
    typer.secho(
        f"Error: {what} ({path}) is inside the repository tree. Private source "
        "material and its outputs must stay outside the repo — choose a path "
        f"outside {_REPO_ROOT}.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


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


def _abbreviate_home(path: Path) -> str:
    """Render a path with the user's home directory abbreviated to ``~``.

    Terminal echoes of user-supplied paths must not expose the account name
    (Phase-1 rule: no complete personal paths in output).
    """
    try:
        return "~/" + str(path.resolve().relative_to(Path.home()))
    except ValueError:
        return str(path)


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


@audience_app.command("evaluate-review")
def audience_evaluate_review(
    review_file: Annotated[
        Path,
        typer.Argument(
            help="Path to a PRIVATE Founder review CSV (kept out of the repo)."
        ),
    ],
) -> None:
    """Aggregate a private Founder review CSV and print AGGREGATES ONLY.

    Reads the file read-only, trusts its recorded predictions (never re-runs the
    classifier), and prints counts, accuracies, precision-by-confidence, and a
    family confusion matrix. It NEVER prints a title/note value and never writes
    a file. Validation errors reference row numbers only.
    """
    _warn_if_in_repo(review_file)
    try:
        records = load_review_csv(review_file)
    except OSError:
        typer.secho(
            f"Error: could not read {review_file}.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    report = evaluate_review(records)
    typer.echo(render_review_report(report))
    raise typer.Exit(code=0)


@audience_app.command("compare-classifiers")
def audience_compare_classifiers(
    fixture: Annotated[
        Path,
        typer.Argument(help="Path to a PUBLIC labeled fixture CSV (title column)."),
    ],
    baseline: Annotated[
        Path,
        typer.Option("--baseline", help="Baseline snapshot JSON to compare against."),
    ],
    save_snapshot: Annotated[
        Path | None,
        typer.Option(
            "--save-snapshot",
            help="Also write the current run's snapshot (no titles) here.",
        ),
    ] = None,
) -> None:
    """Classify a fixture with the CURRENT code and diff it against a baseline.

    The fixture must be a public synthetic labeled CSV. Snapshots hold only
    family/seniority/confidence labels (never titles). Prints an aggregate diff.
    """
    try:
        titles = [row.title for row in load_labeled_csv(fixture)]
    except OSError:
        typer.secho(
            f"Error: could not read {fixture}.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    current = snapshot_classifications(titles)

    if save_snapshot is not None:
        save_snapshot.write_text(snapshot_to_json(current), encoding="utf-8")
        typer.echo(f"Wrote current snapshot ({len(current)} rows) to {save_snapshot}")

    try:
        baseline_snapshot = load_snapshot(baseline)
    except OSError:
        typer.secho(
            f"Error: could not read baseline {baseline}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    try:
        report = compare(baseline_snapshot, current)
    except ValueError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    typer.echo(render_comparison(report))
    raise typer.Exit(code=0)


# Category letters shown alongside each PrivacyCategory enum name in the
# aggregate stdout summary (docs/source-approval-gate.md lattice).
_CATEGORY_LABELS: dict[PrivacyCategory, str] = {
    PrivacyCategory.creator_public: "creator_public (A)",
    PrivacyCategory.creator_private: "creator_private (B)",
    PrivacyCategory.third_party_confidential: "third_party_confidential (C)",
    PrivacyCategory.restricted: "restricted (D)",
    PrivacyCategory.unknown: "unknown",
}


def _write_private(path: Path, content: str) -> None:
    """Write a private artifact and lock it down to owner-only (mode 0o600)."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


@source_app.command("inspect")
def source_inspect(
    folder: Annotated[
        Path, typer.Argument(help="Path to a PRIVATE source folder to inventory.")
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Where to write the three private outputs (must be outside the repo).",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Required: perform a read-only, metadata-safe inventory."
        ),
    ] = False,
) -> None:
    """Phase-1 metadata-safe inventory of a private source folder (dry-run only).

    Never reads a file's body. Writes three PRIVATE outputs (Markdown, JSON,
    review CSV) to ``--output-dir``, which -- like ``folder`` -- must be
    outside the repository tree. Prints AGGREGATE counts only; individual
    file names/refs never reach stdout. See docs/source-approval-gate.md:
    approval fields in the review CSV start empty and analysis of any file
    requires the Founder's explicit, per-file approval.
    """
    if not dry_run:
        typer.secho(
            "Error: 'source inspect' only supports the read-only --dry-run mode "
            "in this version (metadata-safe inventory only). Re-run with "
            "--dry-run to scan the folder safely.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    _reject_if_in_repo(folder, what="the source folder")
    _reject_if_in_repo(output_dir, what="--output-dir")

    scanned_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        inventory = scan_source_folder(
            folder, root_label="<private-source>", scanned_at=scanned_at
        )
    except SourceScanError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    md_path = output_dir / "source-inventory-private.md"
    json_path = output_dir / "source-inventory-private.json"
    csv_path = output_dir / "source-review-private.csv"
    _write_private(md_path, source_to_markdown(inventory))
    _write_private(json_path, source_to_json(inventory))
    _write_private(csv_path, source_to_review_csv(inventory))

    totals = inventory.totals
    lines: list[str] = []
    lines.append(
        "Open Content Machine — source inspect (dry run, Phase 1: "
        "metadata-safe inventory)"
    )
    lines.append("")
    lines.append(f"Scanning private source folder: {_abbreviate_home(folder)}")
    lines.append("")
    lines.append(f"Total files: {totals.files}")
    lines.append(f"Total directories: {totals.dirs}")
    lines.append("")
    lines.append("By category:")
    for category in PrivacyCategory:
        count = totals.by_category.get(category.value, 0)
        lines.append(f"  {_CATEGORY_LABELS[category]}: {count}")
    lines.append("")
    lines.append("By status:")
    for status in FileStatus:
        count = totals.by_status.get(status.value, 0)
        lines.append(f"  {status.name}: {count}")
    lines.append("")
    lines.append(f"Duplicate files: {totals.duplicate_count}")
    lines.append(
        f"Total bytes (ok files): {_human_size(totals.total_bytes)} "
        f"({totals.total_bytes} bytes)"
    )
    lines.append("")
    lines.append("Network access: none (offline by design)")
    lines.append("Source files copied or modified: no")
    lines.append(f"Wrote 3 private outputs to {_abbreviate_home(output_dir)}")
    lines.append("")
    lines.append(
        "Reminder: approval fields in the review CSV start EMPTY. No file may "
        "be analyzed until the Founder sets approved_for_analysis per file — "
        "see docs/source-approval-gate.md."
    )

    typer.echo("\n".join(lines))
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
