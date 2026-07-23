"""Tests for `content-machine source inspect FOLDER --dry-run --output-dir DIR`.

The command must:

* never copy or modify the scanned source folder;
* make no network calls;
* print AGGREGATE counts only to stdout (never a filename, ever a
  file body);
* write three PRIVATE artifacts (Markdown, JSON, review CSV) that never
  contain the real (tmp_path) filesystem path, a symlink-escaped file's
  name/content, an archive's inner-member name, or any sentinel file body;
* refuse to run without --dry-run and --output-dir, and refuse a source
  folder or output dir that lives inside the repository tree.

Every fixture lives under pytest ``tmp_path``; nothing here touches real user
data (CLAUDE.md hard privacy rules).
"""

from __future__ import annotations

import csv
import io
import os
import socket as socket_module
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from content_machine.cli.main import _REPO_ROOT, app

runner = CliRunner()

# Sentinel content: if any of these ever leak into an artifact or stdout, the
# module has a privacy bug. None of these strings appear in any code path
# other than the synthetic fixture bodies below.
_SENTINEL_PUBLIC_BODY = "SENTINEL_BODY_1_conteudo_publicado"
_SENTINEL_PRIVATE_BODY = "SENTINEL_BODY_2_memorias_pessoais"
_SENTINEL_THIRDPARTY_BODY = "SENTINEL_BODY_3_conversa_privada"
_SENTINEL_RESTRICTED_BODY = "SENTINEL_BODY_4_chave_privada"
_SENTINEL_HIDDEN_BODY = "SENTINEL_BODY_5_conteudo_oculto"
_SENTINEL_DUP_BODY = "SENTINEL_BODY_DUP_conteudo_identico"
_SENTINEL_OUTSIDE_BODY = "SENTINEL_BODY_OUTSIDE_nunca_deve_aparecer"
_SENTINEL_GPG_BODY = "SENTINEL_BODY_GPG_criptografado"
_SENTINEL_ARCHIVE_MEMBER = "INNER_MEMBER_SENTINEL_nome_interno"

_ALL_SENTINEL_BODIES = [
    _SENTINEL_PUBLIC_BODY,
    _SENTINEL_PRIVATE_BODY,
    _SENTINEL_THIRDPARTY_BODY,
    _SENTINEL_RESTRICTED_BODY,
    _SENTINEL_HIDDEN_BODY,
    _SENTINEL_DUP_BODY,
    _SENTINEL_OUTSIDE_BODY,
    _SENTINEL_GPG_BODY,
    _SENTINEL_ARCHIVE_MEMBER,
]

# Individual fixture filenames -- must never appear on stdout (aggregates
# only), though they legitimately appear inside the private artifacts.
_FIXTURE_FILENAMES = [
    "artigo-publicado.md",
    "reflexão-sobre-carreira.md",
    "whatsapp-export.txt",
    "server.pem",
    ".secret-notes",
    "arquivo-backup.zip",
    "dup1.txt",
    "dup2.txt",
    "escape-link",
    "vault.gpg",
]

_OUTSIDE_DIRNAME = "outside-secret"
_OUTSIDE_FILENAME = "outside-secret-file.md"


def _build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Build a synthetic source folder + a sibling output dir under tmp_path."""
    source_root = tmp_path / "src-root"
    source_root.mkdir()

    # A: creator_public.
    (source_root / "artigo-publicado.md").write_text(
        f"{_SENTINEL_PUBLIC_BODY} artigo publicado no linkedin", encoding="utf-8"
    )

    # B: creator_private. PT-accented filename.
    (source_root / "reflexão-sobre-carreira.md").write_text(
        f"{_SENTINEL_PRIVATE_BODY} reflexao sobre a carreira", encoding="utf-8"
    )

    # C: third_party_confidential.
    mensagens_dir = source_root / "mensagens"
    mensagens_dir.mkdir()
    (mensagens_dir / "whatsapp-export.txt").write_text(
        f"{_SENTINEL_THIRDPARTY_BODY} conversa de whatsapp", encoding="utf-8"
    )

    # D: restricted.
    (source_root / "server.pem").write_bytes(
        f"-----BEGIN PRIVATE KEY-----{_SENTINEL_RESTRICTED_BODY}".encode()
    )

    # Hidden dotfile.
    (source_root / ".secret-notes").write_text(
        f"{_SENTINEL_HIDDEN_BODY} notas ocultas", encoding="utf-8"
    )

    # Archive (never extracted -- inner member name must never surface).
    (source_root / "arquivo-backup.zip").write_bytes(
        b"PK\x03\x04" + _SENTINEL_ARCHIVE_MEMBER.encode() + b"rest-of-archive-bytes"
    )

    # Encrypted-by-extension (the "suspicious" status).
    (source_root / "vault.gpg").write_bytes(_SENTINEL_GPG_BODY.encode())

    # Exact-content duplicate pair.
    (source_root / "dup1.txt").write_text(_SENTINEL_DUP_BODY, encoding="utf-8")
    (source_root / "dup2.txt").write_text(_SENTINEL_DUP_BODY, encoding="utf-8")

    # A symlink that escapes the scanned root.
    outside_dir = tmp_path / _OUTSIDE_DIRNAME
    outside_dir.mkdir()
    (outside_dir / _OUTSIDE_FILENAME).write_text(
        _SENTINEL_OUTSIDE_BODY, encoding="utf-8"
    )
    (source_root / "escape-link").symlink_to(outside_dir, target_is_directory=True)

    output_dir = tmp_path / "private-outputs"
    return source_root, output_dir


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Snapshot every entry's (size, mtime_ns) WITHOUT following symlinks."""
    snap: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            st = path.lstat()
            snap[str(path)] = (st.st_size, st.st_mtime_ns)
    return snap


def _run(source_root: Path, output_dir: Path):
    return runner.invoke(
        app,
        [
            "source",
            "inspect",
            str(source_root),
            "--dry-run",
            "--output-dir",
            str(output_dir),
        ],
    )


def _artifact_texts(output_dir: Path) -> list[str]:
    md = (output_dir / "source-inventory-private.md").read_text(encoding="utf-8")
    js = (output_dir / "source-inventory-private.json").read_text(encoding="utf-8")
    csv_text = (output_dir / "source-review-private.csv").read_text(encoding="utf-8")
    return [md, js, csv_text]


# ---------------------------------------------------------------------------
# Happy path: exit code, source untouched, no leaks.
# ---------------------------------------------------------------------------


def test_dry_run_exits_zero_and_writes_three_artifacts(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    assert (output_dir / "source-inventory-private.md").exists()
    assert (output_dir / "source-inventory-private.json").exists()
    assert (output_dir / "source-review-private.csv").exists()


def test_source_folder_never_copied_or_modified(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    before = _snapshot(source_root)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    after = _snapshot(source_root)
    assert before == after
    # And nothing was copied into the output dir either.
    copied_names = {p.name for p in output_dir.rglob("*")} if output_dir.exists() else set()
    for fixture_name in _FIXTURE_FILENAMES:
        assert fixture_name not in copied_names


def test_no_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root, output_dir = _build_fixture(tmp_path)

    class _NoSocket:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("network access attempted during source inspect")

    monkeypatch.setattr(socket_module, "socket", _NoSocket)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output


def test_no_full_personal_path_in_written_artifacts(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    for text in _artifact_texts(output_dir):
        assert str(tmp_path) not in text
        assert str(source_root) not in text


def test_no_sentinel_body_content_anywhere(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    haystacks = [*_artifact_texts(output_dir), result.output]
    for sentinel in _ALL_SENTINEL_BODIES:
        for text in haystacks:
            assert sentinel not in text, f"leaked sentinel body: {sentinel}"


def test_stdout_contains_no_individual_filename_aggregates_only(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    for fixture_name in _FIXTURE_FILENAMES:
        assert fixture_name not in result.output, f"leaked filename on stdout: {fixture_name}"
    # But the aggregate facts required by the ticket ARE present.
    assert "Total files:" in result.output
    assert "Total directories:" in result.output
    assert "By category:" in result.output
    assert "By status:" in result.output
    assert "Duplicate files:" in result.output
    assert "Network access: none (offline by design)" in result.output
    assert "Source files copied or modified: no" in result.output
    assert f"Wrote 3 private outputs to {output_dir}" in result.output
    assert "approved_for_analysis" in result.output or "approval" in result.output.lower()


def test_root_label_is_fixed_sanitized_string(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    json_text = (output_dir / "source-inventory-private.json").read_text(encoding="utf-8")
    assert '"root_label": "<private-source>"' in json_text
    assert str(source_root) not in json_text


# ---------------------------------------------------------------------------
# Symlink escape, archive, hidden, encrypted/suspicious -- counted, not leaked.
# ---------------------------------------------------------------------------


def test_symlink_escape_appears_only_as_skipped_count(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    assert "symlink_skipped: 1" in result.output
    for text in [*_artifact_texts(output_dir), result.output]:
        assert _OUTSIDE_DIRNAME not in text
        assert _OUTSIDE_FILENAME not in text
        assert _SENTINEL_OUTSIDE_BODY not in text


def test_archive_not_extracted_inner_member_absent(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    assert "archive_not_extracted: 1" in result.output
    for text in [*_artifact_texts(output_dir), result.output]:
        assert _SENTINEL_ARCHIVE_MEMBER not in text


def test_hidden_file_reported_as_count(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    assert "hidden: 1" in result.output


def test_unsupported_and_suspicious_reported_as_counts(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    # The .gpg fixture is flagged as encrypted_suspected -- an explicit count.
    assert "encrypted_suspected: 1" in result.output
    # Every status (including zero-count ones like "unsupported") is reported
    # as a count line, never omitted or replaced by a filename.
    assert "unsupported: " in result.output


# ---------------------------------------------------------------------------
# Required flags and repo-boundary validation.
# ---------------------------------------------------------------------------


def test_missing_dry_run_exits_one_with_helpful_message(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = runner.invoke(
        app, ["source", "inspect", str(source_root), "--output-dir", str(output_dir)]
    )
    assert result.exit_code == 1
    assert "--dry-run" in result.output
    assert not output_dir.exists()


def test_missing_output_dir_exits_nonzero(tmp_path: Path) -> None:
    source_root, _output_dir = _build_fixture(tmp_path)
    result = runner.invoke(app, ["source", "inspect", str(source_root), "--dry-run"])
    assert result.exit_code != 0


def test_output_dir_inside_repo_exits_one(tmp_path: Path) -> None:
    source_root, _output_dir = _build_fixture(tmp_path)
    inside_repo_output = _REPO_ROOT / ".tmp-source-inspect-test-output-dir"
    result = _run(source_root, inside_repo_output)
    assert result.exit_code == 1
    assert "repository" in result.output.lower()
    assert not inside_repo_output.exists()


def test_folder_inside_repo_exits_one(tmp_path: Path) -> None:
    _source_root, output_dir = _build_fixture(tmp_path)
    result = _run(_REPO_ROOT, output_dir)
    assert result.exit_code == 1
    assert "repository" in result.output.lower()
    assert not output_dir.exists()


# ---------------------------------------------------------------------------
# Review CSV shape.
# ---------------------------------------------------------------------------


def test_review_csv_header_and_all_approval_columns_empty(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output

    csv_text = (output_dir / "source-review-private.csv").read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == [
        "file_id",
        "relative_ref",
        "provisional_category",
        "likely_content_type",
        "approved_for_analysis",
        "intended_use",
        "founder_notes",
    ]
    assert len(rows) > 1
    for data_row in rows[1:]:
        assert data_row[4] == ""  # approved_for_analysis
        assert data_row[5] == ""  # intended_use
        assert data_row[6] == ""  # founder_notes


def test_third_party_rows_never_auto_approved(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output

    csv_text = (output_dir / "source-review-private.csv").read_text(encoding="utf-8")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    third_party_rows = [r for r in rows if r["provisional_category"] == "third_party_confidential"]
    assert third_party_rows, "fixture must contain at least one category-C row"
    for row in third_party_rows:
        assert row["approved_for_analysis"] == ""


# ---------------------------------------------------------------------------
# File/dir permissions of the private outputs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Default directory exclusions (SONNET-1.2b).
# ---------------------------------------------------------------------------

_SENTINEL_NODE_MODULES_BODY = "SENTINEL_BODY_NM_pacote_dependencia_interna"
_SENTINEL_DIST_BODY = "SENTINEL_BODY_DIST_arquivo_gerado"
_SENTINEL_COVERAGE_BODY = "SENTINEL_BODY_COVERAGE_relatorio_gerado"
_SENTINEL_PYCACHE_BODY = "SENTINEL_BODY_PYCACHE_bytecode_gerado"

_EXCLUDED_DIR_SENTINELS = [
    _SENTINEL_NODE_MODULES_BODY,
    _SENTINEL_DIST_BODY,
    _SENTINEL_COVERAGE_BODY,
    _SENTINEL_PYCACHE_BODY,
]
_EXCLUDED_DIR_NAMES = ["node_modules", "dist", "coverage", "__pycache__"]


def _add_excluded_dirs(source_root: Path) -> None:
    """Add one default-excluded directory per name, each with a sentinel file."""
    nm = source_root / "node_modules" / "some-pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text(_SENTINEL_NODE_MODULES_BODY, encoding="utf-8")

    dist = source_root / "dist"
    dist.mkdir()
    (dist / "bundle.js").write_text(_SENTINEL_DIST_BODY, encoding="utf-8")

    coverage = source_root / "coverage"
    coverage.mkdir()
    (coverage / "lcov.info").write_text(_SENTINEL_COVERAGE_BODY, encoding="utf-8")

    pycache = source_root / "__pycache__"
    pycache.mkdir()
    (pycache / "mod.cpython-312.pyc").write_bytes(_SENTINEL_PYCACHE_BODY.encode())


def _run_include_all(source_root: Path, output_dir: Path):
    return runner.invoke(
        app,
        [
            "source",
            "inspect",
            str(source_root),
            "--dry-run",
            "--output-dir",
            str(output_dir),
            "--include-all",
        ],
    )


def test_dependency_directory_excluded_by_default(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output

    # Check the artifacts (which never contain the scanned folder's own path)
    # for the excluded directory name and its sentinel body; stdout is
    # aggregate-only by design and is checked separately below.
    for text in _artifact_texts(output_dir):
        assert "node_modules" not in text
        assert _SENTINEL_NODE_MODULES_BODY not in text
    assert "node_modules" not in result.output
    assert _SENTINEL_NODE_MODULES_BODY not in result.output
    assert f"Excluded dependency/generated directories: {len(_EXCLUDED_DIR_NAMES)}" in result.output


def test_other_generated_dirs_excluded_by_default(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output

    haystacks = [*_artifact_texts(output_dir), result.output]
    for name, sentinel in zip(
        ("dist", "coverage", "__pycache__"),
        (_SENTINEL_DIST_BODY, _SENTINEL_COVERAGE_BODY, _SENTINEL_PYCACHE_BODY),
        strict=True,
    ):
        for text in haystacks:
            assert name not in text
            assert sentinel not in text


def test_include_all_flag_walks_excluded_dirs(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)
    result = _run_include_all(source_root, output_dir)
    assert result.exit_code == 0, result.output

    assert "Excluded dependency/generated directories: 0" in result.output
    json_text = (output_dir / "source-inventory-private.json").read_text(encoding="utf-8")
    for name in _EXCLUDED_DIR_NAMES:
        assert f'"relative_ref": "{name}' in json_text or f'/{name}/' in json_text
    for sentinel in _EXCLUDED_DIR_SENTINELS:
        # Sentinel BODIES never appear even with --include-all -- only metadata
        # (names/refs) is ever written, never file content.
        assert sentinel not in json_text


def test_excluded_dirs_scan_does_not_modify_source_tree(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)
    before = _snapshot(source_root)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    after = _snapshot(source_root)
    assert before == after


def test_no_network_calls_with_include_all_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)

    class _NoSocket:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("network access attempted during source inspect")

    monkeypatch.setattr(socket_module, "socket", _NoSocket)
    result = _run_include_all(source_root, output_dir)
    assert result.exit_code == 0, result.output


def test_no_absolute_path_leaks_with_exclusions_active(tmp_path: Path) -> None:
    # Matches the pattern of test_no_full_personal_path_in_written_artifacts:
    # the three PRIVATE artifacts must never contain the real filesystem path
    # (stdout's "Scanning private source folder:" line intentionally shows an
    # abbreviated path by design and is covered by other tests).
    source_root, output_dir = _build_fixture(tmp_path)
    _add_excluded_dirs(source_root)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output
    for text in _artifact_texts(output_dir):
        assert str(tmp_path) not in text
        assert str(source_root) not in text


def test_output_dir_and_files_are_locked_down(tmp_path: Path) -> None:
    source_root, output_dir = _build_fixture(tmp_path)
    result = _run(source_root, output_dir)
    assert result.exit_code == 0, result.output

    dir_mode = stat.S_IMODE(output_dir.stat().st_mode)
    assert dir_mode == 0o700
    for name in (
        "source-inventory-private.md",
        "source-inventory-private.json",
        "source-review-private.csv",
    ):
        file_mode = stat.S_IMODE((output_dir / name).stat().st_mode)
        assert file_mode == 0o600
