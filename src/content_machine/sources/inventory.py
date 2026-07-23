"""Phase-1 metadata-safe inventory of a private local source folder.

This module inventories the Founder's private biography material (docs/privacy.md
data zones) so files can be *triaged* before any content analysis. The purpose is
inventory, NOT ingestion.

Content is sacred here. File *bodies* are NEVER read, summarized, decoded, or
surfaced. The only byte-level access this module performs is:

  (a) a bounded magic-byte sniff of at most ``_SNIFF_MAX_BYTES`` (512) bytes,
      used solely to refine the guessed MIME type and to detect a clearly
      encrypted container; and
  (b) a streaming SHA-256, used solely to detect exact duplicates.

Neither the sniffed bytes nor the hashed content ever appear in any output, log,
or error message. Errors reference the sanitized ``root_label`` and paths that
are *relative to the scanned root* only -- never an absolute path, and never a
file's contents (docs/privacy.md rules 3 and 6).

Security posture (each requirement is covered by a dedicated test):

  * Symlinks (file or dir) are NEVER followed -- recorded ``symlink_skipped``,
    never hashed or sniffed, never descended into. Any path that resolves
    outside the scanned root (symlink escape or otherwise) is likewise recorded
    ``symlink_skipped`` and skipped -- defence in depth against path traversal.
  * Archives are never extracted or listed inside (``archive_not_extracted``).
  * Hidden files/dirs (dotfiles, ``~$`` lock files) are recorded ``hidden`` and
    never hashed; hidden directories are not descended into.
  * Unreadable files (permission errors) are recorded ``unreadable`` with the
    underlying error swallowed -- no path or errno escapes in an exception.
  * The scan opens files strictly read-only and writes nothing inside the root.
  * Ordering is deterministic (entries sorted by relative path) so repeated runs
    over an unchanged tree are byte-for-byte identical.
  * By default, common dependency/generated directories (``node_modules``,
    ``.git``, ``dist``, ``__pycache__``, ...) are skipped entirely -- not
    descended, not emitted as entries -- see ``DEFAULT_EXCLUDED_DIRS`` and the
    ``excluded_dirs`` parameter on :func:`scan_source_folder`.

Nothing here approves a file for use. ``provisional_category`` is triage only;
there is deliberately no "approved" field on any model in this module. Approval
lives exclusively in the Founder's private review CSV, whose
``approved_for_analysis`` column starts empty by design (Sonnet builds that
workflow).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from collections.abc import Set as AbstractSet
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.normalize import strip_accents

# --- Byte-access budget (metadata only) ------------------------------------
_SNIFF_MAX_BYTES = 512  # hard cap on the magic-byte sniff; never exceeded.
_HASH_CHUNK = 65_536  # streaming SHA-256 chunk size.

_FILE_ID_PREFIX = "src_"
_FILE_ID_HEX_LEN = 12

# Extensions handled without opening the file at all.
_ARCHIVE_EXTS: frozenset[str] = frozenset(
    {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
)
# Clearly encrypted containers detectable by extension. Note the limitation
# (documented on ``_looks_encrypted``): an encrypted PDF or an AES blob without a
# recognized extension CANNOT be detected without parsing, which we refuse to do.
_ENCRYPTED_EXTS: frozenset[str] = frozenset({".gpg", ".age", ".enc"})

# Directory names excluded from a scan by default (dependency/build/cache
# directories that are never source material). Matched by exact NAME
# (casefolded), at any depth -- see ``scan_source_folder``. Passing an empty
# frozenset() as ``excluded_dirs`` disables this default and walks everything.
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        ".nuxt",
        ".cache",
        "__pycache__",
        ".venv",
        "venv",
        ".turbo",
        ".parcel-cache",
        "out",
        ".output",
        "vendor",
        "bower_components",
        ".pnpm-store",
        ".yarn",
    }
)


class PrivacyCategory(StrEnum):
    """Provisional privacy lattice for a source file. String-valued.

    Ordered by restrictiveness (see :func:`categorize`):
    ``restricted`` (D) > ``third_party_confidential`` (C) >
    ``creator_private`` (B) > ``creator_public`` (A) > ``unknown``.
    """

    creator_public = "creator_public"  # A
    creator_private = "creator_private"  # B
    third_party_confidential = "third_party_confidential"  # C
    restricted = "restricted"  # D
    unknown = "unknown"  # UNKNOWN


class IntendedUse(StrEnum):
    """Downstream destination the Founder may (later) assign to a source.

    Never assigned automatically in Phase 1 -- it exists so the review workflow
    and the future draft contracts share one vocabulary.
    """

    creator_profile = "creator_profile"
    authority_map = "authority_map"
    experience_vault = "experience_vault"
    timeline = "timeline"
    story_index = "story_index"
    voice_vault = "voice_vault"
    positioning = "positioning"
    do_not_use = "do_not_use"


class FileStatus(StrEnum):
    """How a discovered filesystem object was handled by the scan."""

    ok = "ok"
    symlink_skipped = "symlink_skipped"
    archive_not_extracted = "archive_not_extracted"
    encrypted_suspected = "encrypted_suspected"
    unsupported = "unsupported"
    unreadable = "unreadable"
    hidden = "hidden"


class InventoryEntry(BaseModel):
    """One discovered filesystem object, described by metadata only.

    ``relative_ref`` is always relative to the scanned root (never absolute).
    ``category_evidence`` names the rule that fired (e.g. ``"ext:.pem -> ...")``
    and NEVER contains file content. ``sha256`` is populated only for ``ok``
    files; every other status leaves it ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    file_id: str
    relative_ref: str
    extension: str
    mime_guess: str | None = None
    size_bytes: int = 0
    modified_at: str | None = None
    subfolder: str = ""
    sha256: str | None = None
    status: FileStatus
    provisional_category: PrivacyCategory = PrivacyCategory.unknown
    category_evidence: str = ""
    is_duplicate_of: str | None = None


class InventoryTotals(BaseModel):
    """Aggregate counters over a scan.

    ``files`` counts file-like entries (regular files and skipped symlinks);
    ``dirs`` counts directories encountered (hidden directories included, symlink
    targets never, excluded directories never -- see ``excluded_dirs`` below).
    ``total_bytes`` sums only the bytes of ``ok`` files -- the content actually
    read for hashing. Grouping maps are keyed by the enum value (or extension
    string) and are sorted for deterministic output.

    ``excluded_dirs`` counts directories skipped by name (default patterns such
    as ``node_modules`` or ``.git``, or a caller-supplied set) -- see
    ``scan_source_folder``. These directories are never descended into, so
    files beneath them are never counted anywhere else in these totals.
    """

    model_config = ConfigDict(extra="forbid")

    files: int = 0
    dirs: int = 0
    by_extension: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    duplicate_count: int = 0
    total_bytes: int = 0
    excluded_dirs: int = 0


class SourceInventory(BaseModel):
    """A complete, private, metadata-only inventory of one source folder.

    ``root_label`` is a SANITIZED label supplied by the caller, never the real
    filesystem path. ``scanned_at`` is a caller-supplied ISO timestamp (kept
    caller-supplied so the model stays pure and reproducible in tests).
    """

    model_config = ConfigDict(extra="forbid")

    root_label: str
    scanned_at: str
    entries: list[InventoryEntry] = Field(default_factory=list)
    totals: InventoryTotals = Field(default_factory=InventoryTotals)


class SourceScanError(Exception):
    """Raised when the scan root is missing or is not a directory.

    The message references the sanitized ``root_label`` only -- never the real
    filesystem path -- so it is safe to surface at the CLI without a traceback.
    """


# ---------------------------------------------------------------------------
# Provisional categorization (data-driven, conservative, explainable).
#
# The lattice, from most to least restrictive:
#
#   restricted (D) > third_party_confidential (C) > creator_private (B)
#       > creator_public (A) > unknown
#
# Rules are evaluated in THAT order and the first hit wins, so the MOST
# restrictive applicable category is always chosen. Between the two "creator"
# categories, B is checked before A: a file is private until proven public.
#
# Matching is done on accent-stripped, casefolded path components (reusing
# ``normalize.strip_accents``). Short tokens (e.g. "rg", "cpf") match only as
# whole word-ish tokens, never as substrings, so "target" never trips "rg".
# Multi-word phrases match as substrings. No file is ever auto-approved: this is
# triage, not a grant.
# ---------------------------------------------------------------------------

_RESTRICTED_EXTS: frozenset[str] = frozenset({".pem", ".key", ".p12", ".kdbx"})
# Single-token restricted keywords (whole-token match, accent-free).
_RESTRICTED_TOKENS: frozenset[str] = frozenset(
    {
        "senha",
        "password",
        "credential",
        "credentials",
        "credencial",
        "credenciais",
        "banco",
        "extrato",
        "imposto",
        "declaracao",
        "rg",
        "cpf",
        "passaporte",
        "contrato",
        "holerite",
        "exame",
    }
)
# Multi-word restricted phrases (substring match). "receita" alone is NOT
# restricted (recipe / revenue ambiguity); only the medical phrase is.
_RESTRICTED_PHRASES: tuple[str, ...] = ("receita medica",)

_THIRD_PARTY_TOKENS: frozenset[str] = frozenset(
    {
        "mensagens",
        "messages",
        "whatsapp",
        "conversa",
        "conversas",
        "feedback",
        "depoimento",
        "depoimentos",
        "familia",
        "family",
        "cliente",
        "clientes",
        "recomendacao",
        "recomendacoes",
        "recommendation",
        "recommendations",
    }
)

_PUBLIC_EXTS: frozenset[str] = frozenset({".md", ".docx", ".txt", ".pdf"})
_PUBLIC_TOKENS: frozenset[str] = frozenset(
    {
        "publicado",
        "publicada",
        "published",
        "post",
        "posts",
        "artigo",
        "artigos",
        "newsletter",
    }
)

_PRIVATE_WRITING_EXTS: frozenset[str] = frozenset(
    {".md", ".docx", ".txt", ".pages", ".rtf"}
)
_PRIVATE_TOKENS: frozenset[str] = frozenset(
    {
        "biografia",
        "historia",
        "memoria",
        "memorias",
        "reflexao",
        "reflexoes",
        "rascunho",
        "draft",
        "drafts",
        "capitulo",
        "capitulos",
        "licao",
        "licoes",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _norm(value: str) -> str:
    """Casefold and strip accents so PT/ES variants match accent-free tokens."""
    return strip_accents(value.casefold())


def _first(matches: AbstractSet[str]) -> str:
    """Deterministically pick the lexicographically-first matched token."""
    return sorted(matches)[0]


def categorize(relative_ref: str, extension: str) -> tuple[PrivacyCategory, str]:
    """Assign a provisional privacy category and a short evidence string.

    Conservative and explainable. Considers the file extension, every ancestor
    folder name, and the filename (all casefolded and accent-stripped). Returns
    ``(unknown, "")`` when no rule fires. The returned evidence names the rule
    (extension, token, phrase, or folder) that decided the category and never
    echoes file content.

    Lattice (first hit wins, most restrictive first):
    ``restricted`` > ``third_party_confidential`` > ``creator_private`` >
    ``creator_public``. ``creator_private`` beats ``creator_public`` so a file is
    private until proven public.
    """
    ext = extension.lower()
    rel = PurePosixPath(relative_ref)
    name_norm = _norm(rel.name)
    folder_norm = " ".join(_norm(part) for part in rel.parts[:-1])
    haystack = f"{folder_norm} {name_norm}".strip()
    all_tokens = set(_TOKEN_RE.findall(haystack))
    folder_tokens = set(_TOKEN_RE.findall(folder_norm))
    name_tokens = set(_TOKEN_RE.findall(name_norm))

    # --- D: restricted -----------------------------------------------------
    if ext in _RESTRICTED_EXTS:
        return PrivacyCategory.restricted, f"ext:{ext} -> restricted"
    if name_norm == ".env" or name_norm.startswith(".env"):
        return PrivacyCategory.restricted, "name:.env -> restricted"
    for phrase in _RESTRICTED_PHRASES:
        if phrase in haystack:
            return PrivacyCategory.restricted, f"phrase:{phrase} -> restricted"
    restricted_hits = _RESTRICTED_TOKENS & all_tokens
    if restricted_hits:
        return PrivacyCategory.restricted, f"token:{_first(restricted_hits)} -> restricted"

    # --- C: third-party confidential --------------------------------------
    third_party_hits = _THIRD_PARTY_TOKENS & all_tokens
    if third_party_hits:
        return (
            PrivacyCategory.third_party_confidential,
            f"token:{_first(third_party_hits)} -> third_party_confidential",
        )

    # --- B: creator private (checked before A: private until proven public) -
    folder_private = _PRIVATE_TOKENS & folder_tokens
    if folder_private:
        return (
            PrivacyCategory.creator_private,
            f"folder:{_first(folder_private)} -> creator_private",
        )
    name_private = _PRIVATE_TOKENS & name_tokens
    if ext in _PRIVATE_WRITING_EXTS and name_private:
        return (
            PrivacyCategory.creator_private,
            f"token:{_first(name_private)} -> creator_private",
        )

    # --- A: creator public -------------------------------------------------
    public_hits = _PUBLIC_TOKENS & all_tokens
    if ext in _PUBLIC_EXTS and public_hits:
        return (
            PrivacyCategory.creator_public,
            f"token:{_first(public_hits)} -> creator_public",
        )

    return PrivacyCategory.unknown, ""


# ---------------------------------------------------------------------------
# Byte-level helpers (metadata only).
# ---------------------------------------------------------------------------

# Magic signatures used ONLY to refine an unknown MIME guess. The matched bytes
# are a fixed file-type marker, not user content.
_MAGIC_MIME: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"%!PS", "application/postscript"),
    (b"{\\rtf", "application/rtf"),
)


def _mime_from_magic(head: bytes) -> str | None:
    """Return a MIME type from a leading magic signature, or ``None``."""
    for signature, mime in _MAGIC_MIME:
        if head.startswith(signature):
            return mime
    return None


def _looks_encrypted(head: bytes) -> bool:
    """Heuristic: does the head look like a clearly encrypted container?

    Only the unambiguous ``age`` textual header is detected here. PGP/AES and
    encrypted PDFs cannot be recognized from a bounded sniff without parsing the
    container, which this module deliberately refuses to do -- those are caught
    by extension (``_ENCRYPTED_EXTS``) instead, or not at all. Documented
    limitation, by design.
    """
    return head.startswith(b"age-encryption.org")


def _sniff_and_hash(path: Path) -> tuple[FileStatus, str | None, bytes]:
    """Open ``path`` read-only ONCE; sniff <=512 bytes, then stream a SHA-256.

    Returns ``(status, sha256_or_None, head_bytes)``. If the head looks like an
    encrypted container, hashing is abandoned and ``encrypted_suspected`` is
    returned with no hash. Any permission/OS error is swallowed into
    ``unreadable`` -- the path and errno never escape in an exception.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_SNIFF_MAX_BYTES)
            if _looks_encrypted(head):
                return FileStatus.encrypted_suspected, None, head
            digest = hashlib.sha256()
            digest.update(head)
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
            return FileStatus.ok, digest.hexdigest(), head
    except OSError:
        return FileStatus.unreadable, None, b""


def _file_id(relative_ref: str) -> str:
    """Stable id: ``src_`` + first 12 hex chars of sha256(relative path)."""
    digest = hashlib.sha256(relative_ref.encode("utf-8")).hexdigest()
    return f"{_FILE_ID_PREFIX}{digest[:_FILE_ID_HEX_LEN]}"


def _is_hidden(name: str) -> bool:
    """Hidden if a dotfile or an Office/LibreOffice ``~$`` lock file."""
    return name.startswith(".") or name.startswith("~$")


def _stat_metadata(dir_entry: os.DirEntry[str]) -> tuple[int, str | None]:
    """Return ``(size_bytes, modified_iso_date_or_None)`` for the entry itself.

    ``follow_symlinks=False`` so a symlink is stat-ed as itself, never its
    target. Errors degrade to ``(0, None)`` rather than raising.
    """
    try:
        stat = dir_entry.stat(follow_symlinks=False)
        modified = date.fromtimestamp(stat.st_mtime).isoformat()
        return stat.st_size, modified
    except OSError:
        return 0, None


def _build_entry(
    resolved_root: Path,
    path: Path,
    *,
    status: FileStatus,
    size_bytes: int,
    modified_at: str | None,
    sha256: str | None,
    head: bytes | None,
) -> InventoryEntry:
    """Assemble one :class:`InventoryEntry` from already-gathered metadata."""
    relative_ref = path.relative_to(resolved_root).as_posix()
    rel_parts = PurePosixPath(relative_ref).parts
    subfolder = rel_parts[0] if len(rel_parts) > 1 else ""
    extension = path.suffix.lower()

    mime_guess = mimetypes.guess_type(path.name)[0]
    if mime_guess is None and head:
        mime_guess = _mime_from_magic(head)

    category, evidence = categorize(relative_ref, extension)

    return InventoryEntry(
        file_id=_file_id(relative_ref),
        relative_ref=relative_ref,
        extension=extension,
        mime_guess=mime_guess,
        size_bytes=size_bytes,
        modified_at=modified_at,
        subfolder=subfolder,
        sha256=sha256,
        status=status,
        provisional_category=category,
        category_evidence=evidence,
        is_duplicate_of=None,
    )


def scan_source_folder(
    root: Path,
    *,
    root_label: str,
    scanned_at: str,
    excluded_dirs: frozenset[str] | None = None,
) -> SourceInventory:
    """Inventory ``root`` at metadata granularity, safely and deterministically.

    ``root_label`` is a sanitized display label (never the real path) and
    ``scanned_at`` is a caller-supplied ISO timestamp. Symlinks are never
    followed, archives never extracted, hidden directories never descended, and
    files are opened strictly read-only. See the module docstring for the full
    security contract. Raises :class:`SourceScanError` (referencing
    ``root_label`` only) if ``root`` is missing or not a directory.

    ``excluded_dirs`` controls dependency/generated-directory exclusion:

    - ``None`` (the default) uses :data:`DEFAULT_EXCLUDED_DIRS`.
    - ``frozenset()`` (empty) excludes nothing -- everything is walked.
    - Any other frozenset replaces the default set entirely.

    A directory whose NAME matches an excluded pattern (casefolded, exact
    match, at any depth) is neither descended into nor emitted as an entry --
    it, and everything beneath it, is skipped outright and only counted in
    ``InventoryTotals.excluded_dirs``. This is checked before the hidden-
    directory check, so an excluded name (e.g. ``.git``) is accounted for as
    excluded, not as ``hidden``.
    """
    excluded_cf = frozenset(
        name.casefold()
        for name in (DEFAULT_EXCLUDED_DIRS if excluded_dirs is None else excluded_dirs)
    )

    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SourceScanError(f"Source root {root_label!r} does not exist.") from None
    if not resolved_root.is_dir():
        raise SourceScanError(
            f"Source root {root_label!r} is not a directory."
        ) from None

    entries: list[InventoryEntry] = []
    dir_count = 0
    file_count = 0
    excluded_dir_count = 0

    # Iterative DFS. Sorting each directory's children keeps traversal stable;
    # the final entry list is sorted by relative path regardless.
    stack: list[Path] = [resolved_root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            # An unreadable directory: skip it. We never surface its path.
            continue

        for child in children:
            path = Path(child.path)

            # 1. Symlinks are never followed -- record and move on.
            if child.is_symlink():
                size_bytes, modified_at = _stat_metadata(child)
                entries.append(
                    _build_entry(
                        resolved_root,
                        path,
                        status=FileStatus.symlink_skipped,
                        size_bytes=size_bytes,
                        modified_at=modified_at,
                        sha256=None,
                        head=None,
                    )
                )
                file_count += 1
                continue

            # 2. Path-traversal defence: anything resolving outside the root is
            #    treated exactly like a symlink escape -- recorded, never touched.
            try:
                real = path.resolve()
            except OSError:
                real = path
            if not real.is_relative_to(resolved_root):
                size_bytes, modified_at = _stat_metadata(child)
                entries.append(
                    _build_entry(
                        resolved_root,
                        path,
                        status=FileStatus.symlink_skipped,
                        size_bytes=size_bytes,
                        modified_at=modified_at,
                        sha256=None,
                        head=None,
                    )
                )
                file_count += 1
                continue

            hidden = _is_hidden(child.name)

            # 3. Directories.
            if child.is_dir(follow_symlinks=False):
                # 3a. Excluded (dependency/generated) directories: skipped
                # outright -- not descended, not emitted as an entry, not
                # counted in ``dirs``. Checked before the hidden check so an
                # excluded name (e.g. ".git") is counted as excluded.
                if child.name.casefold() in excluded_cf:
                    excluded_dir_count += 1
                    continue

                dir_count += 1
                if hidden:
                    # Recorded but NOT descended -- we never inventory the
                    # internals of a hidden directory (e.g. a ``.git`` tree).
                    size_bytes, modified_at = _stat_metadata(child)
                    entries.append(
                        _build_entry(
                            resolved_root,
                            path,
                            status=FileStatus.hidden,
                            size_bytes=size_bytes,
                            modified_at=modified_at,
                            sha256=None,
                            head=None,
                        )
                    )
                else:
                    stack.append(path)
                continue

            # 4. Files (and special files).
            size_bytes, modified_at = _stat_metadata(child)
            if not child.is_file(follow_symlinks=False):
                status, sha256, head = FileStatus.unsupported, None, None
            elif hidden:
                status, sha256, head = FileStatus.hidden, None, None
            else:
                ext = path.suffix.lower()
                if ext in _ARCHIVE_EXTS:
                    status, sha256, head = FileStatus.archive_not_extracted, None, None
                elif ext in _ENCRYPTED_EXTS:
                    status, sha256, head = FileStatus.encrypted_suspected, None, None
                else:
                    status, sha256, sniff_head = _sniff_and_hash(path)
                    head = sniff_head

            entries.append(
                _build_entry(
                    resolved_root,
                    path,
                    status=status,
                    size_bytes=size_bytes,
                    modified_at=modified_at,
                    sha256=sha256,
                    head=head,
                )
            )
            file_count += 1

    entries.sort(key=lambda entry: entry.relative_ref)
    duplicate_count = _mark_duplicates(entries)
    totals = _compute_totals(entries, files=file_count, dirs=dir_count)
    totals.duplicate_count = duplicate_count
    totals.excluded_dirs = excluded_dir_count

    return SourceInventory(
        root_label=root_label,
        scanned_at=scanned_at,
        entries=entries,
        totals=totals,
    )


def _mark_duplicates(entries: list[InventoryEntry]) -> int:
    """Flag exact-content duplicates among ``ok`` entries.

    ``entries`` must already be sorted by ``relative_ref``. The first entry (in
    that order) sharing a given SHA-256 is canonical; every later one gets
    ``is_duplicate_of`` set to the canonical ``file_id``. Returns the number of
    entries marked as duplicates.
    """
    canonical_by_hash: dict[str, str] = {}
    duplicate_count = 0
    for entry in entries:
        if entry.status is not FileStatus.ok or entry.sha256 is None:
            continue
        canonical = canonical_by_hash.get(entry.sha256)
        if canonical is None:
            canonical_by_hash[entry.sha256] = entry.file_id
        else:
            entry.is_duplicate_of = canonical
            duplicate_count += 1
    return duplicate_count


def _compute_totals(
    entries: list[InventoryEntry], *, files: int, dirs: int
) -> InventoryTotals:
    """Aggregate deterministic counters over the entry list."""
    by_extension: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total_bytes = 0
    for entry in entries:
        by_extension[entry.extension] = by_extension.get(entry.extension, 0) + 1
        cat = entry.provisional_category.value
        by_category[cat] = by_category.get(cat, 0) + 1
        stat = entry.status.value
        by_status[stat] = by_status.get(stat, 0) + 1
        if entry.status is FileStatus.ok:
            total_bytes += entry.size_bytes
    return InventoryTotals(
        files=files,
        dirs=dirs,
        by_extension=dict(sorted(by_extension.items())),
        by_category=dict(sorted(by_category.items())),
        by_status=dict(sorted(by_status.items())),
        duplicate_count=0,
        total_bytes=total_bytes,
    )


# ---------------------------------------------------------------------------
# Renderers. All three consume only the metadata-safe inventory. None emit an
# absolute path or any file content.
# ---------------------------------------------------------------------------


def to_json(inv: SourceInventory) -> str:
    """Return the inventory as pretty-printed, deterministic JSON."""
    return inv.model_dump_json(indent=2)


def to_markdown(inv: SourceInventory) -> str:
    """Render a private human summary: aggregate tables, then a per-entry table.

    PRIVATE-only by contract -- this output describes the Founder's private
    biography folder and must never be published. That is stated in the header.
    Contains no absolute paths and no file content.
    """
    lines: list[str] = []
    lines.append(f"# Source inventory: {inv.root_label}")
    lines.append("")
    lines.append(
        "> PRIVATE. Metadata-only triage of a private source folder. "
        "Do not publish or share. No file contents are included."
    )
    lines.append("")
    lines.append(f"Scanned at: {inv.scanned_at}")
    lines.append("")

    totals = inv.totals
    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Files: {totals.files}")
    lines.append(f"- Directories: {totals.dirs}")
    lines.append(f"- Duplicates: {totals.duplicate_count}")
    lines.append(f"- Total bytes (ok files): {totals.total_bytes}")
    lines.append("")

    lines.extend(_md_count_table("By category", totals.by_category))
    lines.extend(_md_count_table("By status", totals.by_status))
    lines.extend(_md_count_table("By extension", totals.by_extension))

    lines.append("## Entries")
    lines.append("")
    lines.append("| file_id | relative_ref | category | status | size_bytes |")
    lines.append("| --- | --- | --- | --- | --- |")
    for entry in inv.entries:
        lines.append(
            f"| {entry.file_id} | {entry.relative_ref} "
            f"| {entry.provisional_category.value} | {entry.status.value} "
            f"| {entry.size_bytes} |"
        )
    lines.append("")
    return "\n".join(lines)


def _md_count_table(heading: str, counts: dict[str, int]) -> list[str]:
    """Render one ``key | count`` markdown table (empty note if no rows)."""
    rows: list[str] = [f"## {heading}", ""]
    if not counts:
        rows.append("_none_")
        rows.append("")
        return rows
    rows.append("| key | count |")
    rows.append("| --- | --- |")
    for key, count in counts.items():
        label = key if key != "" else "(none)"
        rows.append(f"| {label} | {count} |")
    rows.append("")
    return rows


def to_review_csv(inv: SourceInventory) -> str:
    """Render the Founder's private review CSV.

    Columns, in this exact order:
    ``file_id, relative_ref, provisional_category, likely_content_type,
    approved_for_analysis, intended_use, founder_notes``. The last three are
    intentionally empty -- approval, intended use, and notes are the Founder's to
    fill in during review. ``likely_content_type`` is the MIME guess, or the
    extension when the MIME type is unknown. Rows are sorted by ``relative_ref``.
    """
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "file_id",
            "relative_ref",
            "provisional_category",
            "likely_content_type",
            "approved_for_analysis",
            "intended_use",
            "founder_notes",
        ]
    )
    for entry in inv.entries:
        likely = entry.mime_guess or entry.extension
        writer.writerow(
            [
                entry.file_id,
                entry.relative_ref,
                entry.provisional_category.value,
                likely,
                "",  # approved_for_analysis -- Founder-owned, empty by design.
                "",  # intended_use -- Founder-owned.
                "",  # founder_notes -- Founder-owned.
            ]
        )
    return buffer.getvalue()
