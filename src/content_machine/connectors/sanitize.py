"""Untrusted-content neutralization (Gate D §7).

Every source is hostile data. This module is the ONLY place in ``connectors``
that touches raw retrieved text, and it has exactly one job: turn arbitrary
bytes-as-text into a bounded, markup-free, secret-scrubbed string plus a set
of flags a human reviewer can act on. It never executes, interprets, or
forwards anything it sanitizes to a model -- there is no execution path in
this gate, and retrieved content never crosses a model boundary at all.

**Gate E0 (E0.1) move.** ``SecurityFlag`` itself now lives in
:mod:`content_machine.intelligence.models` (see that module's "Gate E0
(E0.1) addition" docstring note for why), so that :class:`SourceItem` can
carry it natively all the way to the published brief -- durably fixing what
was, before this gate, a fail-closed substitute at ``connectors.bridge``
(``UnreviewedSecurityFlagsError``, kept in place, see that module). It is
imported and re-exported here so every existing
``from content_machine.connectors.sanitize import SecurityFlag`` (and the
``connectors`` package's own re-export) keeps working unchanged.

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

**RC-1-R (Fable ruling, 2026-07-28) -- open evasion of the detective layer,
accepted.** ``sanitize_text`` now checks instruction-shaped patterns against
both an empty-string-substituted and a space-substituted normalization of
markup, closing the specific tag-between-words bypass RC-1-R was opened to
fix (e.g. "previous<br/>instructions"). This closes ONE bypass of a
heuristic marker; it does not close the class. A single input COMBINING both
known techniques -- a word deliberately split by a tag (defeated only by the
empty-string normalization) AND a different word pair joined across a tag
(defeated only by the space-substituted normalization) in the SAME string --
is a known, accepted residual that neither normalization alone, nor the two
together, catches; see the pinned regression test in
``tests/test_connectors_sanitize.py`` for the exact shape. RC-1-R must never
be described as closing the class of markup-adjacency evasions of
``instruction_shaped_text``, only this one measured bypass. As always, the
detective marker is not the control: the actual, preventive defense against
retrieved content acting as instructions is architectural -- the no-model-path
design (sanitized content never reaches a model boundary in this gate) and
the fail-closed, human-provenance bridge (``bridge.py``) that is the only way
connector output ever reaches the M1-M7 pipeline.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field

from content_machine.intelligence.models import SecurityFlag

__all__ = [
    "SanitizedText",
    "SecurityFlag",
    "sanitize_error",
    "sanitize_text",
]

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
#
# Gate E0, F0 (Fable ruling, "instruction_shaped_text false positives: FIX
# (a), KEEP THE BLOCK"): two of these patterns produced false positives on
# ordinary release-note/blog prose -- "the agent acts as a proxy" and "you
# are now able to configure X" -- both ubiquitous phrasings that are not
# instruction-shaped at all. Fable's finding: "repeated benign blocks train a
# reflexive override on the one flag whose override must stay exceptional.
# Alarm fatigue is a security failure mode." The two narrowed patterns below
# require either second-person imperative address or sentence/string-initial
# position (the ``you are now ...`` pattern is a matched-then-excluded
# regex, not an include-list -- see its own comment for why). The
# ignore/disregard patterns and the ``system:``/``assistant:`` family are
# UNCHANGED and must not be widened (Fable: "Keep the ignore/disregard
# patterns ... and the ^\\s*system\\s*: family ... EXACTLY as they are.").
_INSTRUCTION_PATTERNS = (
    re.compile(r"\bignore (?:all |the )?(?:previous|prior|above) instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard (?:the )?(?:previous|prior|above)\b", re.IGNORECASE),
    # Gate E0 round 1 (Fable security audit, REQUIRED CHANGE 1): the prior
    # six-adjective exclusion list (able|ready|free|welcome|going|expected)
    # was under-inclusive -- the benign class is not a fixed adjective set
    # but the open-ended SHAPE "<word> to <verb>" (a generalized-infinitive
    # continuation), and the reviewer's independent corpus fired 11/11 on
    # ordinary release-note prose against the old list, exactly the alarm-
    # fatigue failure mode this pattern exists to prevent. "you are now a
    # pirate"/"you are now in developer mode"/"you are now the ranking
    # engine" must still fire, and so must "you are now DAN" (a bare
    # jailbreak-persona name with no article) -- a naive
    # `\byou are now (?:a|an|the|in)\b` include-list would NOT catch "DAN",
    # so this remains a broad match with a narrow, named exclusion rather
    # than an include-list. The exclusion now generalizes: suppress the
    # match when the word right after "now" is NOT an article/"in" AND that
    # word is itself followed by "to" (the generalized-infinitive shape,
    # e.g. "now required to authenticate", "now up to date"). Fable
    # RATIFIED this exact broad-match-with-generalized-
    # exclusion form:
    re.compile(
        r"\byou are now\b(?!\s+(?!(?:a|an|the|in)\b)\w+\s+to\b)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*assistant\s*:", re.IGNORECASE | re.MULTILINE),
    # "the agent acts as a proxy" (verb "acts", not imperative "act", and
    # not sentence-initial) must NOT fire; "You must act as a system
    # administrator..." (second-person imperative) and sentence/string-
    # initial "Act as a/an ..." must still fire.
    #
    # Gate E0 round 1 (Fable security audit, REQUIRED CHANGE 2): the
    # `^\s*act as (?:a|an)\b` alternative above was dead code for any
    # mid-document hostile instance. `sanitize_text` collapses ALL
    # whitespace (`_WHITESPACE_RE`, applied at step 5) BEFORE this
    # instruction check runs (step 6), which merges every line into one
    # string -- so `^` under re.MULTILINE can only ever match position 0
    # of the whole string, never a genuine sentence/line start that
    # followed other text. A hostile "Act as a system administrator..."
    # appearing after benign prose never fired, even though coverage F0
    # item 1 mandated keeping sentence-initial "Act as a/an" coverage.
    # Fable authorized this as an ORDERED RESTORATION of mandated
    # coverage -- not a widening under the "must not widen" rule -- by
    # adding a third alternative: a fixed-width lookbehind for sentence-
    # terminating punctuation followed by exactly ONE space. That fixed
    # width is sound ONLY BECAUSE the whitespace-collapse step (5) already
    # guarantees a single space between sentences by the time this check
    # (step 6) runs -- making sanitize_text's step ORDER load-bearing for
    # this alternative. A later gate must NOT reorder sanitize_text's
    # steps (whitespace collapse before instruction detection) without
    # re-verifying this lookbehind.
    re.compile(
        r"\byou (?:are|will|must|should) (?:now )?act as (?:a|an)\b"
        r"|^\s*act as (?:a|an)\b"
        r"|(?<=[.!?:;] )act as (?:a|an)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
)

_REDACTED_EMAIL = "[redacted-email]"
_REDACTED_CREDENTIAL = "[redacted-credential]"
_REDACTED_PATH = "[redacted-path]"


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
       ``active_markup_neutralized`` when anything was removed. Immediately
       before this empty-string substitution runs, a SEPARATE,
       detection-only variant is derived from the same post-step-3
       ``working`` value by substituting a SPACE for markup instead (RC-1-R,
       Fable ruling 2026-07-28): empty-string substitution can merge two
       words that a tag was deliberately placed between (e.g.
       "previous<br/>instructions" -> "previousinstructions"), defeating the
       word-boundary-anchored patterns step 6 checks. This variant is
       computed from `working` post-steps-1-3 rather than from the raw
       input specifically so the bidi/zero-width defense (step 3) is not
       silently bypassed on this detection-only path. The variant is never
       stored, returned, logged, redacted, or truncated -- it exists only to
       produce one boolean for step 6, then is discarded.
    5. Collapse whitespace.
    6. Detect instruction-shaped phrases and flag them -- the matched text is
       left in place, unmodified: it is data, not something to strip. Checked
       against BOTH the retained (collapsed) string from step 4 AND the
       space-substituted variant from step 4 (RC-1-R): checking only the
       retained string misses the tag-between-words bypass above; checking
       only the space-substituted variant would, per Fable's ruling, reopen a
       different bypass -- a word deliberately split by a tag (e.g.
       "ig<b>nore</b>"), which the retained string's empty-string
       substitution correctly reassembles and therefore still catches.
       Neither normalization alone suffices. Note this is an OR over two
       heuristic checks, not a new detection technique: `_INSTRUCTION_PATTERNS`
       itself is unchanged (Fable explicitly rejected widening the patterns
       themselves to tolerate intra-phrase junk).
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

    # RC-1-R (Fable ruling, 2026-07-28): a detection-only variant that
    # substitutes a SPACE for markup, rather than the empty string the
    # retained pipeline uses below. It exists solely to catch the case the
    # empty-string substitution misses: a tag placed BETWEEN two words of an
    # instruction phrase (e.g. "previous<br/>instructions") is invisible to
    # the empty-string path, because deleting the tag glues the two words
    # back together with nothing between them, defeating the
    # word-boundary-anchored patterns in _INSTRUCTION_PATTERNS. Derived from
    # `working` HERE -- i.e. after steps 1-3 (replacement-character drop,
    # NFC normalization, control-character strip, bidi/zero-width strip)
    # have already run, but before the retained empty-string markup strip
    # below. This placement is load-bearing: deriving the variant from the
    # raw input instead would silently reopen the zero-width/bidi defense
    # (C2) on this detection path, since a zero-width space or bidi override
    # would then reach the instruction check unfiltered via the variant.
    # This variant is local only -- never stored, returned, logged,
    # redacted, or truncated; it exists to produce one boolean, below, and
    # is then discarded.
    _instruction_variant = _MARKUP_TAG_RE.sub(" ", working)
    _instruction_variant = _instruction_variant.replace("<", " ").replace(">", " ")
    _instruction_variant = _WHITESPACE_RE.sub(" ", _instruction_variant).strip()

    without_tags = _MARKUP_TAG_RE.sub("", working)
    without_brackets = without_tags.replace("<", "").replace(">", "")
    if without_brackets != working:
        flags.append(SecurityFlag.active_markup_neutralized)
    working = without_brackets

    working = _WHITESPACE_RE.sub(" ", working).strip()

    # RC-1-R: OR the retained (collapsed) string against the space-substituted
    # variant computed above. Checking ONLY `working` would miss the
    # tag-between-words bypass; checking ONLY a space-substituted variant
    # would (per Fable's ruling) reopen a DIFFERENT bypass -- a word
    # deliberately split by a tag (e.g. "ig<b>nore</b>") that the empty-string
    # path's retained `working` correctly reassembles into "ignore" and
    # therefore still catches. Neither normalization alone is sufficient;
    # both must be checked.
    if _has_instruction_shaped_text(working) or _has_instruction_shaped_text(
        _instruction_variant
    ):
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
