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

**RC-1-R2 (Fable ruling, 2026-07-28, superseding RC-1-R) -- open evasion of
the detective layer, accepted, described as a CLASS.** The underlying defect
is general, not markup-specific: every empty-string deletion in
``sanitize_text`` merges whatever flanks the deleted span, and a deleted
character placed BETWEEN two words of an instruction phrase can defeat the
word-boundary-anchored ``_INSTRUCTION_PATTERNS`` regardless of which
character class was deleted. RC-1-R (2026-07-28, same day, superseded)
closed only the markup member of this class. Fable's follow-up adversarial
probe found the same defeat via the replacement character, zero-width/bidi
characters, and bare C0/DEL control characters. The members differ in how
they were contained before RC-1-R2, not in whether they were real:

- Replacement character, zero-width/bidi characters: each already raised
  ``malformed_encoding`` on deletion -- a member of
  ``bridge.BLOCKING_SECURITY_FLAGS`` -- so these were already fail-closed at
  the bridge choke point even before any detection fix, though
  ``instruction_shaped_text`` itself did not yet fire on them.
- C0/DEL control characters: raised NO flag at all on deletion -- neither
  blocking nor advisory -- the one true gap in the taxonomy, now closed by
  this same commit's addition of ``malformed_encoding`` to that deletion
  (see ``sanitize_text``'s step 2 docstring note).
- Markup tags: raise only ``active_markup_neutralized``, which is NOT in
  ``BLOCKING_SECURITY_FLAGS`` -- the sole defense against this member is
  detection (``instruction_shaped_text`` firing), which is what RC-1-R
  added and RC-1-R2 generalizes.

``sanitize_text`` now maintains a second, space-substituted, detection-only
string in parallel with the retained one, from the same deletion sites
across all of steps 1-4, and checks instruction-shaped patterns against
both (see that function's docstring for the exact mechanics). This closes
the MEASURED bypasses above; it does not close the class in general, and
must never be described as doing so. A single input combining a
character-deleted-inside-a-word split (defeated only by the retained,
empty-string string) with a different, separator-deleted-between-words join
(defeated only by the space-substituted variant) in the SAME string remains
a known, accepted residual that neither normalization alone, nor their OR,
catches; see the pinned regression test in
``tests/test_connectors_sanitize.py`` for the exact shape. As always, the
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

    Order of operations (each idempotent on its own output). Every step below
    that deletes a character is applied to TWO strings in parallel, ``working``
    (retained, unchanged from pre-RC-1-R2 behavior) and ``variant``
    (detection-only, local, never stored/returned/logged/redacted/truncated):
    ``working`` substitutes the empty string as it always has; ``variant``
    substitutes a single space at the exact same site. This is RC-1-R2
    (Fable ruling 2026-07-28), which SUPERSEDES RC-1-R below: RC-1-R
    introduced this retained/variant split for markup only, deriving the
    variant after steps 1-3 had already run on ``working``. Fable's follow-up
    adversarial probe found the same class of bypass -- an empty-string
    deletion merging the two words on either side of it -- via the
    replacement character, zero-width/bidi characters, and bare C0/DEL
    control characters, none of which RC-1-R's markup-only variant covered.
    RC-1-R2 generalizes the variant to cover every deletion site in steps
    1-4, from the start, superseding RC-1-R's narrower placement rather than
    violating it.

    1. Detect and drop the Unicode replacement character (a signal of
       malformed upstream decoding) in ``working``, flagging
       ``malformed_encoding``; substitute a space at the same site in
       ``variant``.
    2. Strip C0/DEL control characters from ``working``; substitute a space
       at the same site in ``variant``. RC-1-R2 item 4: this step now ALSO
       flags ``malformed_encoding`` (checked against ``working`` before the
       substitution) -- previously this deletion raised no flag at all, the
       one gap in an otherwise-consistent "flag every deleted-character
       class" taxonomy (steps 1 and 3 already flagged their own deletions).
    3. Detect and drop zero-width/bidi-control/invisible characters (Gate D
       round-1 correction, C2) from ``working``, also flagging
       ``malformed_encoding``; substitute a space at the same site in
       ``variant``. This MUST happen before the instruction-shaped check
       (step 6): a zero-width space embedded mid-word (``"Ign" + ZWSP +
       "ore all previous instructions"``) would otherwise defeat that
       heuristic on ``working``, and a bidi override left in place would
       make the rendered text a human reviewer sees differ from the logical
       text this function returns.
    4. Neutralize markup in ``working``: strip well-formed ``<...>`` tags,
       then strip any stray/unmatched angle brackets left over (mirrors
       ``intelligence.library.build_normalized_summary``'s belt-and-suspenders
       approach rather than inventing a second style), flagging
       ``active_markup_neutralized`` when anything was removed -- the ONE
       flagging site for that flag, unchanged since Gate D. Substitute a
       space at the same sites in ``variant`` (RC-1-R's original motivating
       case: a tag between two words, e.g. "previous<br/>instructions" ->
       "previousinstructions" in ``working``, which defeats the
       word-boundary-anchored patterns step 6 checks unless ``variant``
       also catches it).
    5. Collapse whitespace -- applied identically to both ``working`` and
       ``variant``.
    6. Detect instruction-shaped phrases and flag them -- the matched text is
       left in place, unmodified: it is data, not something to strip. Checked
       against BOTH ``working`` and ``variant`` (an OR, one
       ``flags.append``): checking only ``working`` misses every
       empty-string-merge bypass above; checking only ``variant`` would, per
       Fable's ruling, reopen a DIFFERENT bypass -- a single word
       deliberately split by a deleted character (e.g. "ig<b>nore</b>", or
       "ig" + ZWSP + "nore"), which ``working``'s own empty-string
       substitutions correctly reassemble and therefore still catch.
       Neither normalization alone suffices; both must be checked.
       `_INSTRUCTION_PATTERNS` itself is unchanged -- Fable explicitly
       rejected widening the patterns themselves to tolerate intra-phrase
       junk, in both the RC-1-R and RC-1-R2 rulings.
    7. Redact credential-shaped, email-shaped, and filesystem-path-shaped
       substrings from ``working`` (the retained string only), flagging each
       kind found.
    8. Truncate ``working`` to ``max_chars``, flagging ``oversized_truncated``
       if the text was longer.

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

    # RC-1-R2 (Fable ruling, 2026-07-28, superseding RC-1-R): the underlying
    # class is that EVERY empty-string deletion in this function merges
    # whatever character-run flanks the deleted span, which can glue two
    # words of an instruction phrase together and defeat the word-boundary-
    # anchored patterns in _INSTRUCTION_PATTERNS. RC-1-R closed only the
    # markup member of that class (a tag between two words). Fable's
    # follow-up adversarial probe found the same defeat via a zero-width
    # space, a bidi override, the replacement character, and -- with no
    # flag raised at all -- a bare C0/DEL control character (vertical tab,
    # bell, DEL) between two words.
    #
    # `variant` is maintained in PARALLEL with the retained `working` string,
    # through the exact same steps in the exact same order, substituting a
    # single SPACE at every site `working` substitutes the empty string.
    # `variant` is local only -- never stored, returned, logged, redacted,
    # or truncated. It exists solely to produce one boolean at step 6, then
    # is discarded. `working` itself, and every flag derived from searches
    # against it, is completely unchanged from pre-RC-1-R2 behavior (see
    # step 2's note below for the one narrow addition: a flag that was
    # already true of `working`'s own contents, just never previously
    # checked for).
    working = raw
    variant = raw

    # Step 1: replacement character (a signal of malformed upstream decoding).
    if _REPLACEMENT_CHAR in working:
        _flag_once(SecurityFlag.malformed_encoding)
        working = working.replace(_REPLACEMENT_CHAR, "")
    variant = variant.replace(_REPLACEMENT_CHAR, " ")

    working = unicodedata.normalize("NFC", working)
    variant = unicodedata.normalize("NFC", variant)

    # Step 2: strip C0/DEL control characters.
    #
    # RC-1-R2 item 4: this deletion previously raised NO flag at all -- the
    # one member of the empty-string-deletion class with zero downstream
    # signal, not even a non-blocking one (contrast step 1's
    # malformed_encoding and step 3's malformed_encoding: both already
    # flagged their own deletions). C0/DEL characters are the same
    # "must not exist in well-formed text" class as those two, so the
    # blocking taxonomy was inconsistent, not intentionally lenient here.
    # Checked against `working` before the substitution below removes them;
    # `variant` has not diverged from `working` in any way that touches
    # this pattern's matches (step 1 only ever touched the replacement
    # character, disjoint from the C0/DEL range), so checking `working`
    # alone is equivalent to checking either string at this point.
    if _CONTROL_CHAR_RE.search(working):
        _flag_once(SecurityFlag.malformed_encoding)
    working = _CONTROL_CHAR_RE.sub("", working)
    variant = _CONTROL_CHAR_RE.sub(" ", variant)

    # Step 3: zero-width/bidi-control/invisible characters (Gate D round-1
    # correction, C2). This MUST happen before the instruction-shaped check
    # (step 6): a zero-width space embedded mid-word (``"Ign" + ZWSP + "ore
    # all previous instructions"``) would otherwise defeat that heuristic on
    # the retained path, and a bidi override left in place would make the
    # rendered text a human reviewer sees differ from the logical text this
    # function returns.
    if _BIDI_AND_INVISIBLE_RE.search(working):
        _flag_once(SecurityFlag.malformed_encoding)
        working = _BIDI_AND_INVISIBLE_RE.sub("", working)
    variant = _BIDI_AND_INVISIBLE_RE.sub(" ", variant)

    # Step 4: neutralize markup. Strip well-formed ``<...>`` tags, then strip
    # any stray/unmatched angle brackets left over (mirrors
    # ``intelligence.library.build_normalized_summary``'s belt-and-suspenders
    # approach rather than inventing a second style), flagging
    # ``active_markup_neutralized`` when anything was removed. This is the
    # ONE flagging site for that flag, and the retained substitution below
    # is byte-for-byte what RC-1-R (and Gate D before it) shipped.
    without_tags = _MARKUP_TAG_RE.sub("", working)
    without_brackets = without_tags.replace("<", "").replace(">", "")
    if without_brackets != working:
        flags.append(SecurityFlag.active_markup_neutralized)
    working = without_brackets

    variant = _MARKUP_TAG_RE.sub(" ", variant)
    variant = variant.replace("<", " ").replace(">", " ")

    # Step 5: collapse whitespace -- applied to BOTH strings identically.
    working = _WHITESPACE_RE.sub(" ", working).strip()
    variant = _WHITESPACE_RE.sub(" ", variant).strip()

    # Step 6: detect instruction-shaped phrases and flag them -- the matched
    # text is left in place, unmodified: it is data, not something to strip.
    # Checked against BOTH `working` and `variant` (an OR, one
    # `flags.append`): checking only `working` misses every empty-string-
    # merge bypass in the class above; checking only `variant` would, per
    # Fable's ruling, reopen a DIFFERENT bypass -- a single word deliberately
    # split by a deleted character (e.g. "ig<b>nore</b>", or "ig" + ZWSP +
    # "nore"), which `working`'s own empty-string substitutions correctly
    # reassemble and therefore still catch. Neither normalization alone
    # suffices; both must be checked. `_INSTRUCTION_PATTERNS` itself is
    # unchanged -- Fable explicitly rejected widening the patterns to
    # tolerate intra-phrase junk.
    if _has_instruction_shaped_text(working) or _has_instruction_shaped_text(variant):
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
