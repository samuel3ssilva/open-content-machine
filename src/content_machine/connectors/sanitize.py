"""Untrusted-content neutralization and the security-flag vocabulary (Gate D §7).

Every source is hostile data. This module is the ONLY place in ``connectors``
that touches raw retrieved text, and it has exactly one job: turn arbitrary
bytes-as-text into a bounded, markup-free, secret-scrubbed string plus a set
of flags a human reviewer can act on. It never executes, interprets, or
forwards anything it sanitizes to a model -- there is no execution path in
this gate, and retrieved content never crosses a model boundary at all.

**Honesty requirement.** This is NOT semantic prompt-injection detection and
must never be described as such (see also the ADR and public
``docs/connector-security.md``, both landing in a later Gate D commit). Text
that merely *looks like* an instruction is flagged
``instruction_shaped_text`` and left as inert data in the output -- flagging
is a heuristic marker for a human, not a security boundary. The real control
is architectural: sanitized content is data, has no instruction channel, does
not reach a model boundary in this gate, and can only reach the M1-M7
pipeline through an authored, human-provenance bridge (``bridge.py``, a later
commit). No claim of perfect (or even reliable) semantic detection is made or
implied anywhere in this module.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REPLACEMENT_CHAR = "�"
_WHITESPACE_RE = re.compile(r"\s+")
_MARKUP_TAG_RE = re.compile(r"<[^>]*>")

# Zero-width/bidi-control/invisible characters (Gate D round-1 correction,
# C2): a zero-width space defeats the instruction-shaped regex below by
# splitting a word ("Ign" + ZWSP + "ore all previous instructions"), and a
# bidi override (U+202E etc.) makes the RENDERED text differ from the
# LOGICAL text a downstream reviewer or system sees -- deceiving a human
# even though the underlying characters are unchanged. Ranges, in order:
#   U+200B-U+200F  zero-width space/non-joiner/joiner, LTR/RTL marks
#   U+202A-U+202E  LTR/RTL embedding, pop directional formatting, LTR/RTL override
#   U+2066-U+2069  LTR/RTL/first-strong isolate, pop directional isolate
#   U+FEFF         zero-width no-break space / byte-order mark
#
# Written as explicit \u escapes rather than literal glyphs so the invisible
# characters this regex targets are never themselves silently present,
# unreadable, in this file's source.
_BIDI_AND_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"
)

# Local part may vary; anchored on the domain plus a couple of common TLDs so
# it does not over-match ordinary prose that happens to contain an "@".
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Credential-shaped: a known secret-prefix token, or a `key = "<16+ chars>"`
# / `key: <16+ chars>` assignment shape. Matches inside prose, not only at the
# start of a line -- see sanitize_text's docstring for why fixtures embed
# these mid-sentence rather than as a bare assignment.
_CREDENTIAL_PREFIX_RE = re.compile(
    r"\b(?:sk|pk|api|token|secret|bearer)[_-][A-Za-z0-9_\-]{12,}\b", re.IGNORECASE
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"\b(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}['\"]?",
    re.IGNORECASE,
)

# Home- or root-anchored filesystem path only (Gate D round-1 correction,
# C1): the original pattern matched ANY two-or-more-segment absolute path,
# which meant an ordinary multi-segment URL path -- "https://example.com/
# vendor/blog/post-42" -- was misdetected as a filesystem path, redacted, and
# falsely flagged on essentially every real-world RSS/blog item. Anchoring
# on ``~/``, ``/Users/``, ``/home/``, ``/var/``, or a Windows drive letter
# (per this correction's spec) plus a negative lookbehind for an immediately
# preceding ``://`` closes that false-positive without losing the genuine
# local-path case. Deliberately conservative (a couple of false negatives are
# fine; the architectural control, not this heuristic, is what matters).
_FS_PATH_RE = re.compile(
    r"(?<!://)(?:~(?:/[\w.\-]+)*|/(?:Users|home|var)(?:/[\w.\-]+)+"
    r"|[A-Za-z]:\\(?:[\w.\-]+\\)*[\w.\-]+)"
)

# Heuristic instruction-shaped phrases. Intentionally small and generic --
# widening it is a tuning exercise for a later gate, not a detection promise.
_INSTRUCTION_PATTERNS = (
    re.compile(r"\bignore (?:all |the )?(?:previous|prior|above) instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard (?:the )?(?:previous|prior|above)\b", re.IGNORECASE),
    re.compile(r"\byou are now\b", re.IGNORECASE),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*assistant\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bact as (?:a|an)\b", re.IGNORECASE),
)

_REDACTED_EMAIL = "[redacted-email]"
_REDACTED_CREDENTIAL = "[redacted-credential]"
_REDACTED_PATH = "[redacted-path]"


class SecurityFlag(StrEnum):
    """Heuristic markers raised while sanitizing untrusted content.

    Every flag is a marker for a human reviewer, never a claim of complete
    detection (see the module docstring's honesty requirement).
    """

    active_markup_neutralized = "active_markup_neutralized"
    instruction_shaped_text = "instruction_shaped_text"
    credential_shaped_text = "credential_shaped_text"
    email_shaped_text = "email_shaped_text"
    filesystem_path_shaped_text = "filesystem_path_shaped_text"
    oversized_truncated = "oversized_truncated"
    malformed_encoding = "malformed_encoding"
    unsupported_content_type = "unsupported_content_type"
    redirect_chain_exceeded = "redirect_chain_exceeded"
    duplicate_canonical_reference = "duplicate_canonical_reference"
    conflicting_publication_date = "conflicting_publication_date"


class SanitizedText(BaseModel):
    """The result of :func:`sanitize_text`: cleaned text plus the flags raised.

    ``flags`` is a tuple (not a set) so serialization order is deterministic
    and never depends on set-iteration order.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    flags: tuple[SecurityFlag, ...] = Field(default_factory=tuple)
    truncated: bool = False


def sanitize_text(raw: str, *, max_chars: int) -> SanitizedText:
    """Neutralize untrusted text and cap its length.

    Order of operations (each idempotent on its own output):

    1. Detect and drop the Unicode replacement character (a signal of
       malformed upstream decoding), flagging ``malformed_encoding``.
    2. Strip C0/DEL control characters.
    3. Detect and drop zero-width/bidi-control/invisible characters (Gate D
       round-1 correction, C2), also flagging ``malformed_encoding``. This
       MUST happen before the instruction-shaped check (step 6): a
       zero-width space embedded mid-word (``"Ign" + ZWSP + "ore all
       previous instructions"``) would otherwise defeat that heuristic, and
       a bidi override left in place would make the rendered text a human
       reviewer sees differ from the logical text this function returns.
    4. Neutralize markup: strip well-formed ``<...>`` tags, then strip any
       stray/unmatched angle brackets left over (mirrors
       ``intelligence.library.build_normalized_summary``'s belt-and-suspenders
       approach rather than inventing a second style), flagging
       ``active_markup_neutralized`` when anything was removed.
    5. Collapse whitespace.
    6. Detect instruction-shaped phrases and flag them -- the matched text is
       left in place, unmodified: it is data, not something to strip.
    7. Redact credential-shaped, email-shaped, and filesystem-path-shaped
       substrings from the result, flagging each kind found.
    8. Truncate to ``max_chars``, flagging ``oversized_truncated`` if the
       text was longer.

    (Gate D round-2 correction, D3: this list previously named step 6
    "redact" and step 7 "detect instruction-shaped text" -- the reverse of
    what the code below actually does. The order above now matches
    execution: instruction-shaped detection runs BEFORE credential/email/
    path redaction, so a phrase like "ignore all previous instructions"
    is still intact and detectable at the point that check runs, exactly
    as steps 3's ordering note already explained for the bidi/zero-width
    check relative to this same detection step.)
    """
    flags: list[SecurityFlag] = []

    def _flag_once(flag: SecurityFlag) -> None:
        if flag not in flags:
            flags.append(flag)

    working = raw
    if _REPLACEMENT_CHAR in working:
        _flag_once(SecurityFlag.malformed_encoding)
        working = working.replace(_REPLACEMENT_CHAR, "")
    working = unicodedata.normalize("NFC", working)
    working = _CONTROL_CHAR_RE.sub("", working)
    if _BIDI_AND_INVISIBLE_RE.search(working):
        _flag_once(SecurityFlag.malformed_encoding)
        working = _BIDI_AND_INVISIBLE_RE.sub("", working)

    without_tags = _MARKUP_TAG_RE.sub("", working)
    without_brackets = without_tags.replace("<", "").replace(">", "")
    if without_brackets != working:
        flags.append(SecurityFlag.active_markup_neutralized)
    working = without_brackets

    working = _WHITESPACE_RE.sub(" ", working).strip()

    if _has_instruction_shaped_text(working):
        flags.append(SecurityFlag.instruction_shaped_text)

    if _CREDENTIAL_PREFIX_RE.search(working) or _CREDENTIAL_ASSIGNMENT_RE.search(working):
        flags.append(SecurityFlag.credential_shaped_text)
        working = _CREDENTIAL_ASSIGNMENT_RE.sub(_REDACTED_CREDENTIAL, working)
        working = _CREDENTIAL_PREFIX_RE.sub(_REDACTED_CREDENTIAL, working)

    if _EMAIL_RE.search(working):
        flags.append(SecurityFlag.email_shaped_text)
        working = _EMAIL_RE.sub(_REDACTED_EMAIL, working)

    if _FS_PATH_RE.search(working):
        flags.append(SecurityFlag.filesystem_path_shaped_text)
        working = _FS_PATH_RE.sub(_REDACTED_PATH, working)

    truncated = len(working) > max_chars
    if truncated:
        flags.append(SecurityFlag.oversized_truncated)
        working = working[:max_chars]

    return SanitizedText(text=working, flags=tuple(flags), truncated=truncated)


def _has_instruction_shaped_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INSTRUCTION_PATTERNS)


def sanitize_error(exc_or_message: BaseException | str) -> str:
    """Return an error string safe to log, persist, or surface to a human.

    For a :class:`BaseException`, ONLY its type name is returned -- the
    exception's own message is discarded entirely rather than partially
    scrubbed. An exception message is untrusted and there is no reliable way
    to distinguish a safe message from one carrying a body fragment, a
    credential, or a path, so the safe default is to drop it, exactly as
    ``sanitize_error``'s contract requires ("never a body fragment, never a
    credential, never a path").

    For a ``str`` (a caller-supplied short message, e.g. a result-code-shaped
    string), the same redaction and length cap used by :func:`sanitize_text`
    is applied before it is returned.
    """
    if isinstance(exc_or_message, BaseException):
        return type(exc_or_message).__name__
    return sanitize_text(exc_or_message, max_chars=200).text
