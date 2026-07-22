"""CEO-mandated Sprint 1 acceptance tests.

These tests exist specifically to satisfy the Sprint 1 QA mandate and are
deliberately narrow, so they complement (never duplicate) the broader unit
suites: ``test_cli_inspect.py`` (basic dry-run structure/sentinel checks),
``test_classify.py`` (per-family classification cases), ``test_export_public.py``
(sanitization unit tests against a hand-built report), ``test_performance.py``
(pipeline perf), and ``test_privacy_guarantees.py`` (general no-PII checks).

Each test below targets one specific acceptance criterion that was not yet
covered elsewhere:

1. Every-cell-unique-sentinel dry-run leak check, with a non-``example.com``
   email domain so the "no leak" assertion cannot pass by coincidence.
2. Dry-run makes zero network calls (``socket`` is monkeypatched to explode).
3. Dry-run never copies the external source file anywhere (repo-tree and
   tmp_path snapshots, before/after).
4. Classification determinism across ~30 titles, repeated and shuffled.
5. Genuinely ambiguous titles never reach ``high`` confidence.
6. Unknown/garbage titles are never forced into a family.
7. Public export suppresses small groups end-to-end, from a report built via
   the *real* pipeline (load -> normalize -> anonymize -> analyze) rather
   than a hand-constructed report object.
8. No command's stdout/stderr ever contains a sentinel value, across both
   success and failure paths.

Note on email domains: the CI release security checklist
(``.github/workflows/ci.yml``) fails the build on any tracked email address
whose domain is not exactly ``example.com``, ``example.org``, or
``users.noreply.github.com``. To keep the "non-example.com domain" fixture
requirement meaningful (see ``docs/privacy.md`` and the Sprint 1 QA brief)
while staying green on that gate, sentinel fixtures below use the literal
``example.org`` domain -- a different reserved documentation domain
(RFC 2606) than the ``example.com`` used elsewhere in this suite, so the
no-leak assertion cannot be trivially satisfied by an unrelated ``example.com``
substring appearing in output for other reasons.
"""

from __future__ import annotations

import json
import random
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from content_machine.audience.classify import Confidence, RoleFamily, classify_role
from content_machine.audience.normalize import normalize
from content_machine.audience.public_report import SUPPRESSED_LABEL, PublicReport
from content_machine.audience.report import analyze, to_json
from content_machine.cli.main import app
from content_machine.ingestion.csv_loader import load_csv
from content_machine.privacy.anonymizer import anonymize
from tests.conftest import REPO_ROOT

runner = CliRunner()

_VOLATILE_DIR_NAMES = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _is_volatile(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(part in _VOLATILE_DIR_NAMES or part.endswith(".egg-info") for part in parts):
        return True
    return relative_path.name == ".DS_Store"


def _repo_snapshot() -> set[str]:
    """Relative paths of every tracked-ish file under the repo, minus caches."""
    snapshot: set[str] = set()
    for path in REPO_ROOT.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(REPO_ROOT)
        if _is_volatile(rel):
            continue
        snapshot.add(str(rel))
    return snapshot


# ---------------------------------------------------------------------------
# 1 & 2 & 3. Dry-run: no value leaks, no network calls, no source copy.
# ---------------------------------------------------------------------------

_UNIQUE_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On,Extra Notes"
# Every field carries a DISTINCT sentinel so a leak of any single field is
# individually detectable. The email uses example.org (see module docstring).
_UNIQUE_ROW = (
    "SENTFIRSTQWERTY,SENTLASTQWERTY,https://sentinelqwerty.example.org/profile,"
    "sentinelqwerty@example.org,SENTCOQWERTY,SENTTITLEQWERTY,01 Jan 2020,SENTNOTEQWERTY"
)
_UNIQUE_SENTINELS = [
    "SENTFIRSTQWERTY",
    "SENTLASTQWERTY",
    "SENTCOQWERTY",
    "SENTTITLEQWERTY",
    "SENTNOTEQWERTY",
    "sentinelqwerty",
    "example.org",
]


def _unique_sentinel_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "external-unique.csv"
    path.write_text(_UNIQUE_HEADER + "\n" + _UNIQUE_ROW + "\n", encoding="utf-8")
    return path


def test_dry_run_every_cell_unique_sentinel_never_leaks(tmp_path: Path) -> None:
    path = _unique_sentinel_fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    assert result.exit_code == 0
    out = result.output
    for sentinel in _UNIQUE_SENTINELS:
        assert sentinel not in out, f"leaked cell value: {sentinel}"
    assert "@" not in out
    assert "http" not in out


def test_dry_run_makes_no_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted during dry-run")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    path = _unique_sentinel_fixture(tmp_path)
    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    assert result.exit_code == 0, result.output


def test_dry_run_never_copies_source_file(tmp_path: Path) -> None:
    path = _unique_sentinel_fixture(tmp_path)
    tmp_before = {p.name for p in tmp_path.iterdir()}
    repo_before = _repo_snapshot()

    result = runner.invoke(app, ["audience", "inspect", str(path), "--dry-run"])
    assert result.exit_code == 0

    repo_after = _repo_snapshot()
    assert repo_after == repo_before, (
        "audience inspect --dry-run created/removed repo files: "
        f"{repo_after ^ repo_before}"
    )

    tmp_after = {p.name for p in tmp_path.iterdir()}
    assert tmp_after == tmp_before, (
        f"audience inspect --dry-run wrote into tmp_path beyond the input: {tmp_after - tmp_before}"
    )
    # Belt-and-braces: the generic output names it advertises must not exist.
    assert not Path("audience-report.md").exists()
    assert not Path("audience-report.json").exists()


# ---------------------------------------------------------------------------
# 4. Classification determinism across ~30 titles, twice and shuffled.
# ---------------------------------------------------------------------------

_THIRTY_TITLES = [
    "Founder",
    "CEO",
    "Software Engineer",
    "Data Scientist",
    "Product Manager",
    "UX Designer",
    "Marketing Manager",
    "Account Executive",
    "Financial Controller",
    "Professor",
    "Physician",
    "Consultant",
    "Analyst",
    "Director",
    "Manager",
    "Vice President",
    "Head of Growth",
    "Recruiter",
    "General Counsel",
    "Research Scientist",
    "Full Stack Developer",
    "Content Strategist",
    "Business Development Manager",
    "Graphic Designer",
    "Data Scientist and Product Manager",
    "Senior Software Engineer",
    "",
    "asdfgh",
    "1234",
    "   ",
]
assert len(_THIRTY_TITLES) == 30


def test_classification_deterministic_repeated_and_shuffled() -> None:
    first_pass = [classify_role(t) for t in _THIRTY_TITLES]
    second_pass = [classify_role(t) for t in _THIRTY_TITLES]
    assert first_pass == second_pass, "classify_role is not repeatable on identical input"

    indices = list(range(len(_THIRTY_TITLES)))
    random.Random(20260722).shuffle(indices)
    shuffled_titles = [_THIRTY_TITLES[i] for i in indices]
    shuffled_results = [classify_role(t) for t in shuffled_titles]

    reordered = [None] * len(_THIRTY_TITLES)
    for original_index, result in zip(indices, shuffled_results, strict=True):
        reordered[original_index] = result
    assert reordered == first_pass, "classification depends on evaluation order"


# ---------------------------------------------------------------------------
# 5. Genuinely ambiguous titles never reach high confidence.
# ---------------------------------------------------------------------------

_AMBIGUOUS_TITLES = ["Consultant", "Analyst", "Director", "Manager"]


@pytest.mark.parametrize("title", _AMBIGUOUS_TITLES)
def test_ambiguous_titles_never_classify_high(title: str) -> None:
    result = classify_role(title)
    assert result.confidence is not Confidence.high, (
        f"{title!r} classified as high confidence ({result.family}); "
        "ambiguous single-token titles must be medium/low/unknown"
    )
    assert result.confidence in {Confidence.medium, Confidence.low, Confidence.unknown}


# ---------------------------------------------------------------------------
# 6. Unknown/garbage titles are never forced into a family.
# ---------------------------------------------------------------------------

_GARBAGE_TITLES = ["", "asdfgh", "1234567890", "   ", "\t\n  \t"]


@pytest.mark.parametrize("title", _GARBAGE_TITLES)
def test_garbage_titles_never_forced_into_family(title: str) -> None:
    result = classify_role(title)
    assert result.family is RoleFamily.unknown
    assert result.confidence is Confidence.unknown
    assert result.matched_evidence == ""


# ---------------------------------------------------------------------------
# 7. Public export: real pipeline, dominant cluster kept, tiny groups gone.
# ---------------------------------------------------------------------------

# One dominant cluster (>= 10) plus nine tiny clusters (< 10 each) so every
# top-list / distribution / segment in the private report has something to
# suppress. Company and position pairs are chosen so each tiny group also
# lands in a distinct role-family/seniority cluster.
_TINY_GROUPS: list[tuple[str, str]] = [
    ("TinyOne", "CFO"),
    ("TinyTwo", "Product Manager"),
    ("TinyThree", "UX Designer"),
    ("TinyFour", "Recruiter"),
    ("TinyFive", "Professor"),
    ("TinySix", "Account Executive"),
    ("TinySeven", "Business Development Manager"),
    ("TinyEight", "General Counsel"),
    ("TinyNine", "Marketing Manager"),
]
_DOMINANT_COMPANY = "MegaCorp"
_DOMINANT_POSITION = "Software Engineer"
_DOMINANT_SIZE = 15
_TINY_GROUP_SIZE = 5


def _build_public_export_fixture_csv(tmp_path: Path) -> Path:
    lines = ["First Name,Last Name,Company,Position,Connected On"]
    for i in range(_DOMINANT_SIZE):
        lines.append(f"Legion{i},Alpha{i},{_DOMINANT_COMPANY},{_DOMINANT_POSITION},18 Apr 2024")
    for company, position in _TINY_GROUPS:
        for i in range(_TINY_GROUP_SIZE):
            lines.append(f"Minor{company}{i},Beta{company}{i},{company},{position},18 Apr 2024")
    path = tmp_path / "public-export-source.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_export_public_suppresses_small_groups_from_real_pipeline(tmp_path: Path) -> None:
    csv_path = _build_public_export_fixture_csv(tmp_path)

    # Real pipeline, not a hand-built AudienceReport.
    load_result = load_csv(csv_path)
    norm = normalize(load_result)
    anon = anonymize(norm, salt="sprint1-export-public-fixture")
    private_report = analyze(anon, load_result, norm)

    assert private_report.totals.total_rows == _DOMINANT_SIZE + len(_TINY_GROUPS) * _TINY_GROUP_SIZE
    # Sanity: the private report DOES contain the tiny groups (pre-sanitization).
    private_company_labels = {c.label for c in private_report.top_companies}
    assert _DOMINANT_COMPANY in private_company_labels
    assert any(company for company, _ in _TINY_GROUPS if company in private_company_labels)

    private_path = tmp_path / "private-report.json"
    private_path.write_text(to_json(private_report), encoding="utf-8")

    public_path = tmp_path / "public-report.json"
    public_md_path = tmp_path / "public-report.md"
    result = runner.invoke(
        app,
        [
            "audience",
            "export-public",
            str(private_path),
            "-o",
            str(public_path),
            "--md",
            str(public_md_path),
        ],
    )
    assert result.exit_code == 0, result.output

    public_blob = public_path.read_text(encoding="utf-8")
    public_data = json.loads(public_blob)
    PublicReport.model_validate(public_data)  # re-validate against the strict model

    assert public_data["privacy_label"] == "sanitized-aggregate"

    # Dominant cluster survives.
    assert any(c["label"] == _DOMINANT_COMPANY and c["count"] == _DOMINANT_SIZE
               for c in public_data["top_companies"])

    # Every tiny group's company name is entirely absent (top-list: dropped).
    for company, _position in _TINY_GROUPS:
        assert company not in public_blob, f"suppressed company {company!r} leaked"

    # The suppressed bucket shows up in at least one distribution.
    role_family_labels = {c["label"] for c in public_data["role_family_distribution"]}
    assert SUPPRESSED_LABEL in role_family_labels

    # Small-size segments (< 10) never appear; only the dominant segment can.
    for segment in public_data["segments"]:
        assert segment["size"] >= 10

    # No direct identifiers of any kind.
    assert "@" not in public_blob
    assert "http" not in public_blob
    for i in range(_DOMINANT_SIZE):
        assert f"Legion{i}" not in public_blob
        assert f"Alpha{i}" not in public_blob
    for company, _position in _TINY_GROUPS:
        assert f"Minor{company}" not in public_blob

    md_blob = public_md_path.read_text(encoding="utf-8")
    assert "sanitized-aggregate" in md_blob
    assert "@" not in md_blob
    assert "http" not in md_blob


# ---------------------------------------------------------------------------
# 8. No command's logs ever contain a sentinel value: success + failure paths.
# ---------------------------------------------------------------------------

_LOG_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"

# Direct identifiers (name/email/url) and an unparseable-date marker must NEVER
# appear in ANY command's output, on ANY path -- these are exactly the values
# anonymization strips and which validate/report/anonymize/inspect must never
# echo verbatim (docs/privacy.md rule 6).
_NEVER_LEAK_SENTINELS = ["LOGSENTFIRST", "LOGSENTLAST", "logsentinel", "LOGSENTBOGUSDATE"]

# `company`/`position` are retained aggregate fields by design (they survive
# anonymization and are meant to show up in `report`/`anonymize` output) --
# but `validate`/`inspect` are structure-only and must never print them either.
_RETAINED_FIELD_SENTINELS = ["LOGSENTCO", "LOGSENTTITLE"]
_STRUCTURE_ONLY_SENTINELS = _NEVER_LEAK_SENTINELS + _RETAINED_FIELD_SENTINELS


def _log_fixture_valid(tmp_path: Path, name: str) -> Path:
    row = (
        "LOGSENTFIRST,LOGSENTLAST,https://logsentinel.example.org/u,"
        "logsentinel@example.org,LOGSENTCO,LOGSENTTITLE,01 Jan 2020"
    )
    path = tmp_path / name
    path.write_text(_LOG_HEADER + "\n" + row + "\n", encoding="utf-8")
    return path


def _log_fixture_bad_date(tmp_path: Path, name: str) -> Path:
    # Same sentinels, but an unparseable date -> triggers a non-fatal
    # parse_error issue (the "failure" path inside an otherwise successful run).
    row = (
        "LOGSENTFIRST,LOGSENTLAST,https://logsentinel.example.org/u,"
        "logsentinel@example.org,LOGSENTCO,LOGSENTTITLE,LOGSENTBOGUSDATE"
    )
    path = tmp_path / name
    path.write_text(_LOG_HEADER + "\n" + row + "\n", encoding="utf-8")
    return path


def _log_fixture_undecodable_binary(tmp_path: Path, name: str) -> Path:
    # Triggers CsvLoadError (exit 1) at the load stage -- the hard-failure path.
    marker = b"LOGSENTBINARYLEAK"
    path = tmp_path / name
    path.write_bytes(b"\x00\x01" + marker + b"\x00\x02")
    return path


def _assert_none_leak(output: str, sentinels: list[str], *, context: str) -> None:
    for sentinel in sentinels:
        assert sentinel not in output, f"{context}: leaked sentinel {sentinel!r}"
    assert "LOGSENTBINARYLEAK" not in output, f"{context}: leaked binary marker"


def test_no_command_logs_leak_pii_success_and_failure_paths(tmp_path: Path) -> None:
    valid_csv = _log_fixture_valid(tmp_path, "valid.csv")
    bad_date_csv = _log_fixture_bad_date(tmp_path, "bad-date.csv")
    undecodable = _log_fixture_undecodable_binary(tmp_path, "garbage.dat")

    # --- success paths (valid + bad-date-but-non-fatal) ---
    for csv_path, label in [(valid_csv, "valid"), (bad_date_csv, "bad-date")]:
        validate_result = runner.invoke(app, ["audience", "validate", str(csv_path)])
        assert validate_result.exit_code == 0, validate_result.output
        _assert_none_leak(
            validate_result.output, _STRUCTURE_ONLY_SENTINELS, context=f"validate/{label}"
        )

        report_result = runner.invoke(app, ["audience", "report", str(csv_path)])
        assert report_result.exit_code == 0, report_result.output
        # company/position are retained aggregate fields and legitimately
        # appear here; only direct identifiers must never leak.
        _assert_none_leak(
            report_result.output, _NEVER_LEAK_SENTINELS, context=f"report/{label}"
        )

        anon_out = tmp_path / f"anon-{label}.json"
        anon_result = runner.invoke(
            app, ["audience", "anonymize", str(csv_path), "-o", str(anon_out)]
        )
        assert anon_result.exit_code == 0, anon_result.output
        _assert_none_leak(
            anon_result.output, _NEVER_LEAK_SENTINELS, context=f"anonymize-stdout/{label}"
        )
        _assert_none_leak(
            anon_out.read_text(encoding="utf-8"),
            _NEVER_LEAK_SENTINELS,
            context=f"anonymize-file/{label}",
        )

        inspect_result = runner.invoke(
            app, ["audience", "inspect", str(csv_path), "--dry-run"]
        )
        assert inspect_result.exit_code == 0, inspect_result.output
        _assert_none_leak(
            inspect_result.output, _STRUCTURE_ONLY_SENTINELS, context=f"inspect/{label}"
        )

    # --- failure path (undecodable binary source) ---
    for args in (
        ["audience", "validate", str(undecodable)],
        ["audience", "report", str(undecodable)],
        ["audience", "anonymize", str(undecodable), "-o", str(tmp_path / "never.json")],
        ["audience", "inspect", str(undecodable), "--dry-run"],
    ):
        failure_result = runner.invoke(app, args)
        assert failure_result.exit_code == 1, failure_result.output
        _assert_none_leak(
            failure_result.output, _STRUCTURE_ONLY_SENTINELS, context=f"failure/{args[1]}"
        )
