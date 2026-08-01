"""Typer CLI for Open Content Machine (docs/architecture.md §5).

All commands run offline with no API key. User errors are reported as friendly
messages with non-zero exit codes, never tracebacks (§6). Error text references
rows by index and columns by name, never personal field values.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from content_machine.intelligence.brief import render_markdown
from content_machine.intelligence.library import load_topics
from content_machine.intelligence.limitations_overlay import (
    OVERLAY_FILENAME,
    LimitationsOverlayError,
    load_limitations_overlay,
    rendered_topic_ids,
)
from content_machine.intelligence.loader import (
    DEFAULT_PROFILE_PATH,
    ProfileLoadError,
    SignalLoadError,
    load_profile,
    load_signals,
)
from content_machine.intelligence.weekly import (
    DEFAULT_CADENCE_DESCRIPTION,
    DEFAULT_TIMEZONE,
    LimitationsOverlayManifest,
    derive_week_label,
    resolve_window,
    run_weekly,
    write_weekly_run_outputs,
)
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

intelligence_app = typer.Typer(
    help="Weekly Intelligence Brief engine commands (offline, synthetic-fixture friendly).",
    no_args_is_help=True,
)
app.add_typer(intelligence_app, name="intelligence")

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
    include_all: Annotated[
        bool,
        typer.Option(
            "--include-all",
            help=(
                "Disable the default dependency/generated-directory exclusions "
                "(node_modules, .git, dist, build, __pycache__, ...) and scan "
                "everything."
            ),
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

    By default, common dependency/generated directories (``node_modules``,
    ``.git``, ``dist``, ``__pycache__``, ...) are excluded from the scan
    entirely -- pass ``--include-all`` to disable that and scan everything.
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
    excluded_dirs: frozenset[str] | None = frozenset() if include_all else None
    try:
        inventory = scan_source_folder(
            folder,
            root_label="<private-source>",
            scanned_at=scanned_at,
            excluded_dirs=excluded_dirs,
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
    lines.append(
        "Excluded dependency/generated directories: "
        f"{totals.excluded_dirs} (default patterns; use --include-all to disable)"
    )
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


_WEEKLY_RUN_EPILOG = (
    "Documented default cadence: Saturday 18:00 America/Sao_Paulo -- this is "
    "DOCUMENTATION ONLY. Neither this command nor content_machine installs, waits "
    "for, or activates any scheduler; you decide when to invoke it.\n\n"
    "Example OS cron line to run it at that documented cadence (DOCUMENTATION "
    "ONLY -- nothing here installs this):\n\n"
    "  0 18 * * 6 cd /path/to/repo && content-machine intelligence weekly-run "
    "--signals /path/to/signals.json --library /path/to/library.jsonl "
    '--reference-date "$(date +\\%F)" --output-dir /path/to/output'
)


@intelligence_app.command("weekly-run", epilog=_WEEKLY_RUN_EPILOG)
def intelligence_weekly_run(
    signals: Annotated[
        Path,
        typer.Option(
            "--signals", help="Path to a JSON array of signal items (SourceItem records)."
        ),
    ],
    reference_date: Annotated[
        str,
        typer.Option(
            "--reference-date",
            help=(
                "ISO date or datetime the 7-day analysis window is computed from. "
                "Window = [reference_date-7d 00:00, reference_date 00:00), inclusive/"
                "exclusive, at local midnight in --timezone."
            ),
        ),
    ],
    profile: Annotated[
        Path,
        typer.Option(
            "--profile",
            help=(
                "Path to a RelevanceProfile JSON. Defaults to the shipped synthetic "
                "example -- the real Founder profile is private and must never enter "
                "this repo."
            ),
        ),
    ] = DEFAULT_PROFILE_PATH,
    library: Annotated[
        Path | None,
        typer.Option(
            "--library",
            help=(
                "Path to an existing topic library JSONL. Optional: an empty prior "
                "library is used if absent (e.g. the very first run)."
            ),
        ),
    ] = None,
    timezone: Annotated[
        str,
        typer.Option(
            "--timezone",
            help=(
                "IANA timezone applied to the window boundaries (e.g. "
                "America/Sao_Paulo). "
                f"Documented default cadence: {DEFAULT_CADENCE_DESCRIPTION} -- this is "
                "DOCUMENTATION ONLY; the command does not wait for Saturday."
            ),
        ),
    ] = DEFAULT_TIMEZONE,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help="Directory to write brief/library/manifest outputs. Required unless --dry-run.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Compute and print a summary + run_id only; write nothing. Exits 0."
        ),
    ] = False,
    regenerate: Annotated[
        bool,
        typer.Option(
            "--regenerate",
            help=(
                "Redo an already-completed run (same run_id) safely -- never "
                "duplicates append-only library/score-history/audit rows."
            ),
        ),
    ] = False,
) -> None:
    """Run the weekly Intelligence Brief engine end-to-end, fully offline.

    Composes load -> cluster -> rank -> tier -> brief -> library over a
    deterministic 7-day window and writes brief.md, brief.json,
    topics.jsonl, score-history.jsonl, audit.jsonl, and run-manifest.json to
    --output-dir. Re-running the same week is idempotent by default (no
    duplicated library/score/audit rows); pass --regenerate to force a
    redo. See the epilog for the documented default cadence and an example
    cron line (documentation only -- nothing here schedules itself).
    """
    if output_dir is None and not dry_run:
        typer.secho(
            "Error: --output-dir is required unless --dry-run is set.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        load_result = load_signals(signals)
    except SignalLoadError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    try:
        relevance_profile = load_profile(profile)
    except ProfileLoadError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    prior_entries = load_topics(library) if library is not None else []

    try:
        resolve_window(reference_date, timezone)  # validate before deriving week_label
        week_label = derive_week_label(reference_date, timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    execution_timestamp = datetime.now(ZoneInfo(timezone)).isoformat()

    result = run_weekly(
        signals=load_result.items,
        profile=relevance_profile,
        prior_library=prior_entries,
        week_label=week_label,
        reference_date=reference_date,
        timezone=timezone,
        execution_timestamp=execution_timestamp,
    )

    manifest = result.manifest
    summary_lines = [
        "Open Content Machine -- intelligence weekly-run",
        "",
        f"week_label: {manifest.week_label}",
        f"reference_date: {manifest.reference_date}",
        f"timezone: {manifest.timezone}",
        f"window: [{manifest.window_start}, {manifest.window_end})",
        f"run_id: {manifest.run_id}",
        f"signal_count (in window): {manifest.signal_count}",
        f"topic_count: {manifest.topic_count}",
        f"tier1_count: {manifest.tier1_count}",
        f"review_status: {manifest.review_status}",
    ]

    if dry_run:
        typer.echo("\n".join(summary_lines))
        typer.echo("")
        typer.echo("(--dry-run: nothing written)")
        raise typer.Exit(code=0)

    assert output_dir is not None  # guarded above

    # Fable ruling 2026-08-01 (Part C; rekeyed by item_id under Part A of the
    # 2026-08-01 follow-up ruling): the Founder-approved, per-item
    # limitations overlay -- a private, run-specific, human-authored JSON
    # sidecar a human places at <output_dir>/limitations-overlay.json BEFORE
    # this command runs, keyed by SourceItem.item_id (never topic_id -- a
    # topic_id is run-scoped and would not exist yet at authoring time).
    # Absent file is NOT a failure (the run proceeds with no limitations
    # composed); any other validation failure aborts the ENTIRE render --
    # nothing is written -- so this must run BEFORE write_weekly_run_outputs
    # is ever called. load_limitations_overlay validates each item_id
    # against result.item_topic_map and resolves it to its containing
    # topic_id BEFORE render_markdown is ever called with it -- composed
    # ONLY into the rendered brief.md string (never into result.brief /
    # brief.json, the pipeline, or a model boundary).
    overlay_path = output_dir / OVERLAY_FILENAME
    try:
        overlay_result = load_limitations_overlay(
            overlay_path,
            run_id=result.manifest.run_id,
            item_topic_map=result.item_topic_map,
            rendered_topic_ids=rendered_topic_ids(result.brief),
        )
    except LimitationsOverlayError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    if overlay_result is not None:
        result = result.model_copy(
            update={
                "brief_markdown": render_markdown(
                    result.brief, limitations_overlay=overlay_result.limitations
                ),
                "manifest": result.manifest.model_copy(
                    update={
                        "limitations_overlay": LimitationsOverlayManifest(
                            present=True,
                            item_count=overlay_result.item_count,
                            overlay_sha256=overlay_result.overlay_sha256,
                            provenance=overlay_result.provenance,
                        )
                    }
                ),
            }
        )

    outcome = write_weekly_run_outputs(result, output_dir, regenerate=regenerate)

    typer.echo("\n".join(summary_lines))
    typer.echo("")
    if outcome.wrote:
        typer.echo(f"Wrote {len(outcome.files_written)} output file(s) to {output_dir}:")
        for name in outcome.files_written:
            typer.echo(f"  - {name}")
    else:
        typer.echo(f"Skipped write: {outcome.skipped_reason}")
        # Fable ruling 2026-08-01 (Part B, follow-up: "idempotency skip
        # gap"): a validated overlay this invocation, combined with a skip
        # (a completed run with a matching run_id already on disk), can mean
        # the Founder believes the overlay was applied when it was not --
        # write_weekly_run_outputs never re-writes brief.md on a skip, so an
        # overlay validated THIS run never reaches a prior run's on-disk
        # brief.md. Detect the divergence by reading the ON-DISK manifest's
        # own limitations_overlay.present (never assumed from this
        # invocation's overlay_result, which describes the FILE, not what
        # was actually written to brief.md) and warn loudly -- a WARNING,
        # not an error, since a prior run that already applied the same
        # overlay is a legitimate, non-divergent idempotent skip.
        if overlay_result is not None:
            on_disk_overlay_present = False
            manifest_path = output_dir / "run-manifest.json"
            try:
                on_disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                on_disk_overlay_present = bool(
                    on_disk_manifest.get("limitations_overlay", {}).get("present", False)
                )
            except (OSError, json.JSONDecodeError, AttributeError):
                # Unreadable/malformed manifest: treat as "not confirmed
                # applied" -- the conservative, warn-worthy assumption below.
                on_disk_overlay_present = False
            if not on_disk_overlay_present:
                typer.secho(
                    "WARNING: a valid limitations overlay was supplied for this run, but "
                    f"the existing run already completed on disk at {output_dir} "
                    f"(run_id={outcome.run_id}) does NOT carry it -- its brief.md has no "
                    "limitations text and run-manifest.json records "
                    "limitations_overlay.present=false. The overlay was NOT applied. Pass "
                    "--regenerate to redo this run's outputs with the overlay applied.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
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
