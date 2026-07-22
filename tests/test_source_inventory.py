"""Tests for the metadata-safe private source-folder inventory (OPUS-1.2).

Every folder here is built under a pytest ``tmp_path``; no real user data is
touched. The scan must never read file bodies, never follow symlinks, never
extract archives, never descend hidden directories, never modify anything, and
never leak an absolute path into any rendered output.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from content_machine.sources.inventory import (
    FileStatus,
    PrivacyCategory,
    SourceScanError,
    categorize,
    scan_source_folder,
    to_json,
    to_markdown,
    to_review_csv,
)

_SCANNED_AT = "2026-07-22T00:00:00Z"
_LABEL = "founder-bio"


def _scan(root: Path):
    return scan_source_folder(root, root_label=_LABEL, scanned_at=_SCANNED_AT)


def _by_ref(inv) -> dict[str, object]:
    return {entry.relative_ref: entry for entry in inv.entries}


# ---------------------------------------------------------------------------
# Root validation.
# ---------------------------------------------------------------------------


def test_missing_root_raises_clean_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SourceScanError) as exc:
        _scan(missing)
    # Message references the sanitized label, never the real path.
    assert _LABEL in str(exc.value)
    assert str(missing) not in str(exc.value)


def test_root_that_is_a_file_raises(tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(SourceScanError):
        _scan(a_file)


# ---------------------------------------------------------------------------
# Symlink safety and path traversal.
# ---------------------------------------------------------------------------


def test_symlink_to_outside_dir_is_skipped_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.md").write_text("biografia rascunho", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("should never be inventoried", encoding="utf-8")

    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    inv = _scan(root)
    by_ref = _by_ref(inv)
    assert by_ref["escape"].status is FileStatus.symlink_skipped
    assert by_ref["escape"].sha256 is None
    # The outside file must not appear under any name.
    assert not any("secret" in ref for ref in by_ref)


def test_symlink_to_inside_file_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.txt"
    target.write_text("hello", encoding="utf-8")
    (root / "alias.txt").symlink_to(target)

    inv = _scan(root)
    by_ref = _by_ref(inv)
    assert by_ref["alias.txt"].status is FileStatus.symlink_skipped
    assert by_ref["alias.txt"].sha256 is None
    # The real file is still inventoried normally.
    assert by_ref["target.txt"].status is FileStatus.ok


# ---------------------------------------------------------------------------
# Hidden files/dirs, archives, encryption.
# ---------------------------------------------------------------------------


def test_hidden_file_recorded_not_hashed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / ".secret").write_text("x", encoding="utf-8")

    by_ref = _by_ref(_scan(root))
    assert by_ref[".secret"].status is FileStatus.hidden
    assert by_ref[".secret"].sha256 is None


def test_office_lock_file_is_hidden(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "~$memoir.docx").write_text("x", encoding="utf-8")

    by_ref = _by_ref(_scan(root))
    assert by_ref["~$memoir.docx"].status is FileStatus.hidden


def test_hidden_directory_not_descended(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    hidden_dir = root / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "config").write_text("should not be inventoried", encoding="utf-8")

    by_ref = _by_ref(_scan(root))
    assert by_ref[".git"].status is FileStatus.hidden
    # Contents of the hidden directory are never inventoried.
    assert not any(ref.startswith(".git/") for ref in by_ref)


def test_archive_not_extracted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    # A ZIP-looking file; magic bytes present but never opened as an archive.
    (root / "bundle.zip").write_bytes(b"PK\x03\x04rest-of-archive")

    by_ref = _by_ref(_scan(root))
    entry = by_ref["bundle.zip"]
    assert entry.status is FileStatus.archive_not_extracted
    assert entry.sha256 is None


def test_encrypted_by_extension_flagged(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "vault.gpg").write_bytes(b"\x00\x01\x02")

    by_ref = _by_ref(_scan(root))
    entry = by_ref["vault.gpg"]
    assert entry.status is FileStatus.encrypted_suspected
    assert entry.sha256 is None


def test_encrypted_by_age_magic_flagged(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    # ``.txt`` extension but an age header in the first bytes -> detected by sniff.
    (root / "notes.txt").write_bytes(b"age-encryption.org/v1\n" + b"\x00" * 40)

    by_ref = _by_ref(_scan(root))
    entry = by_ref["notes.txt"]
    assert entry.status is FileStatus.encrypted_suspected
    assert entry.sha256 is None


# ---------------------------------------------------------------------------
# Unreadable files (permission errors).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions, so chmod 000 is not unreadable",
)
def test_unreadable_file_recorded_without_leaking_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    locked = root / "locked.txt"
    locked.write_text("secret", encoding="utf-8")
    os.chmod(locked, 0o000)
    try:
        by_ref = _by_ref(_scan(root))
        entry = by_ref["locked.txt"]
        assert entry.status is FileStatus.unreadable
        assert entry.sha256 is None
    finally:
        os.chmod(locked, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# Duplicate detection.
# ---------------------------------------------------------------------------


def test_duplicate_detection_marks_second_occurrence(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("identical bytes", encoding="utf-8")
    (root / "b.txt").write_text("identical bytes", encoding="utf-8")
    (root / "c.txt").write_text("different bytes", encoding="utf-8")

    inv = _scan(root)
    by_ref = _by_ref(inv)
    # Canonical is the lexicographically-first path (a.txt); b.txt points at it.
    assert by_ref["a.txt"].is_duplicate_of is None
    assert by_ref["b.txt"].is_duplicate_of == by_ref["a.txt"].file_id
    assert by_ref["c.txt"].is_duplicate_of is None
    assert inv.totals.duplicate_count == 1


# ---------------------------------------------------------------------------
# The scan never modifies anything.
# ---------------------------------------------------------------------------


def test_scan_does_not_modify_the_tree(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "one.md").write_text("biografia", encoding="utf-8")
    (root / "sub" / "two.txt").write_text("rascunho", encoding="utf-8")

    def snapshot() -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        for path in sorted(root.rglob("*")):
            st = path.stat()
            snap[str(path)] = (st.st_size, st.st_mtime_ns)
        return snap

    before = snapshot()
    _scan(root)
    after = snapshot()
    assert before == after


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------


def test_two_scans_are_identical(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "memorias").mkdir()
    (root / "memorias" / "reflexão-sobre-carreira.md").write_text(
        "conteudo", encoding="utf-8"
    )
    (root / "server.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----")
    (root / "dup1.txt").write_text("same", encoding="utf-8")
    (root / "dup2.txt").write_text("same", encoding="utf-8")

    first = to_json(_scan(root))
    second = to_json(_scan(root))
    assert first == second


# ---------------------------------------------------------------------------
# Categorization lattice.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative_ref", "extension", "expected"),
    [
        # D: restricted.
        ("server.pem", ".pem", PrivacyCategory.restricted),
        ("keys/id_rsa.key", ".key", PrivacyCategory.restricted),
        (".env", "", PrivacyCategory.restricted),
        (".env.local", "", PrivacyCategory.restricted),
        ("financas/extrato-banco.pdf", ".pdf", PrivacyCategory.restricted),
        ("docs/declaracao-imposto.pdf", ".pdf", PrivacyCategory.restricted),
        ("saude/receita medica.pdf", ".pdf", PrivacyCategory.restricted),
        # C: third-party confidential.
        ("mensagens/whatsapp-export.txt", ".txt", PrivacyCategory.third_party_confidential),
        ("familia/album.md", ".md", PrivacyCategory.third_party_confidential),
        ("feedback-de-clientes.docx", ".docx", PrivacyCategory.third_party_confidential),
        # B: creator private.
        ("memorias/reflexao-sobre-carreira.md", ".md", PrivacyCategory.creator_private),
        ("biografia-rascunho.docx", ".docx", PrivacyCategory.creator_private),
        # A: creator public.
        ("publicados/artigo-linkedin.md", ".md", PrivacyCategory.creator_public),
        ("newsletter-edicao-3.txt", ".txt", PrivacyCategory.creator_public),
        # Unknown.
        ("random/photo.jpg", ".jpg", PrivacyCategory.unknown),
    ],
)
def test_categorize_lattice(
    relative_ref: str, extension: str, expected: PrivacyCategory
) -> None:
    category, evidence = categorize(relative_ref, extension)
    assert category is expected
    if expected is PrivacyCategory.unknown:
        assert evidence == ""
    else:
        assert evidence != ""


def test_most_restrictive_wins_on_conflict(tmp_path: Path) -> None:
    # A "published" token (would be A) plus a "contrato" token (D). D must win.
    category, evidence = categorize("publicados/contrato-publicado.md", ".md")
    assert category is PrivacyCategory.restricted
    assert "contrato" in evidence


def test_private_beats_public_when_both_present(tmp_path: Path) -> None:
    # Folder is a private memoir folder; filename carries a public token. Private
    # (B) is checked before public (A): private until proven public.
    category, _ = categorize("memorias/post-publicado.md", ".md")
    assert category is PrivacyCategory.creator_private


def test_receita_alone_is_not_restricted(tmp_path: Path) -> None:
    # "receita" without "medica" is ambiguous (recipe/revenue) -> not restricted.
    category, _ = categorize("cozinha/receita-de-bolo.md", ".md")
    assert category is not PrivacyCategory.restricted


def test_short_token_does_not_match_as_substring(tmp_path: Path) -> None:
    # "rg" is a restricted token but must not fire inside "target".
    category, _ = categorize("projects/target-audience.md", ".md")
    assert category is not PrivacyCategory.restricted


# ---------------------------------------------------------------------------
# No absolute path leaks into any rendered output.
# ---------------------------------------------------------------------------


def test_no_absolute_path_in_any_output(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "memorias").mkdir()
    (root / "memorias" / "capítulo-1.md").write_text("historia", encoding="utf-8")
    (root / "server.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----")
    (root / "bundle.zip").write_bytes(b"PK\x03\x04")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    inv = _scan(root)
    needle = str(tmp_path)
    for rendered in (to_markdown(inv), to_json(inv), to_review_csv(inv)):
        assert needle not in rendered
    # And no rendered relative_ref is absolute.
    for entry in inv.entries:
        assert not Path(entry.relative_ref).is_absolute()


# ---------------------------------------------------------------------------
# Review CSV shape and empty approval fields.
# ---------------------------------------------------------------------------


def test_review_csv_header_and_empty_approval_fields(tmp_path: Path) -> None:
    import csv
    import io

    root = tmp_path / "root"
    root.mkdir()
    (root / "biografia.md").write_text("rascunho", encoding="utf-8")
    (root / "server.pem").write_bytes(b"key")

    reader = csv.reader(io.StringIO(to_review_csv(_scan(root))))
    rows = list(reader)
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
        # approved_for_analysis, intended_use, founder_notes are empty by design.
        assert data_row[4] == ""
        assert data_row[5] == ""
        assert data_row[6] == ""


# ---------------------------------------------------------------------------
# ok files are hashed; totals are coherent.
# ---------------------------------------------------------------------------


def test_ok_file_is_hashed_and_totals_coherent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    inv = _scan(root)
    by_ref = _by_ref(inv)
    assert by_ref["a.txt"].sha256 is not None
    assert by_ref["a.txt"].subfolder == ""
    assert by_ref["sub/b.txt"].subfolder == "sub"
    assert inv.totals.files == 2
    assert inv.totals.dirs == 1
    assert inv.totals.total_bytes == len("alpha") + len("beta")
