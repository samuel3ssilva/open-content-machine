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

**RC-1-R3 (Fable ruling, 2026-07-28, superseding RC-1-R2 and RC-1-R, final
iteration of this loop) -- open evasion of the detective layer, accepted,
described as a CLASS.** The underlying defect is general, not
markup-specific: every empty-string deletion in ``sanitize_text`` merges
whatever flanks the deleted span, and a deleted character placed BETWEEN two
words of an instruction phrase can defeat the word-boundary-anchored
``_INSTRUCTION_PATTERNS`` regardless of which character class was deleted.
RC-1-R (2026-07-28, superseded) closed only the markup member. RC-1-R2
(2026-07-28, superseded) generalized to a single space-substituted variant
covering every deletion site, closing six of ten hostile rows in Fable's
adversarial probe with zero new false positives -- but that single variant
gave up one row RC-1-R happened to catch (see below). RC-1-R3 replaces the
one variant with the full set of independent normalizations and is the last
ruling in this series. The members differ in how they were contained, not
in whether they were real:

- Replacement character, zero-width/bidi characters: each already raised
  ``malformed_encoding`` on deletion -- a member of
  ``bridge.BLOCKING_SECURITY_FLAGS`` -- so these were already fail-closed at
  the bridge choke point even before any detection fix, though
  ``instruction_shaped_text`` itself did not always fire on them.
- C0/DEL control characters: raised NO flag at all on deletion before
  RC-1-R2 -- neither blocking nor advisory -- the one true gap in the
  taxonomy, closed by RC-1-R2's addition of ``malformed_encoding`` to that
  deletion (see ``sanitize_text``'s step 2 docstring note), unchanged here.
- Markup tags: raise only ``active_markup_neutralized``, which is NOT in
  ``BLOCKING_SECURITY_FLAGS`` -- the sole defense against this member is
  detection (``instruction_shaped_text`` firing).

There are exactly four normalizations available: ``{invisible-class
deletions -> "" | " "} x {markup deletions -> "" | " "}``. The retained
string (``working``, N0) already IS the (empty, empty) cell;
``sanitize_text`` now also checks three further DETECTION-ONLY variants
(N1/N2/N3, one per remaining cell, built by
:func:`_instruction_detection_variant` -- see that function and
``sanitize_text``'s own docstring for the exact mechanics) and ORs all four
into a single ``instruction_shaped_text`` flag append. Fable explicitly
REJECTED splitting the invisible axis into its three constituent character
families (replacement/control/bidi) for a 16-variant refinement: the extra
cells it would add are all cross-family instances of the SAME ceiling named
below, already bridge-blocked, for zero containment gain against a
quadrupled reasoning surface.

**The accepted ceiling, unchanged in kind from RC-1-R/RC-1-R2 but now
precisely stated:** one substitution axis required in contradictory roles
(word-split and word-separator) -- whether by one character family or by two
families sharing the axis -- defeats all four normalizations at once, since
no single cell of the 2x2 can hold two different values on the same axis
simultaneously. Two pinned shapes in ``tests/test_connectors_sanitize.py``:

- **Pin (i), the PRIORITY residual:** the markup axis required in both
  roles (e.g. ``ig<b>nore</b> all previous<br/>instructions``). This is the
  one vector that is neither detected nor blocked -- it raises only
  ``active_markup_neutralized``, which carries no blocking weight. For this
  vector specifically, the preventive controls are the entire boundary:
  the no-model-path architecture (sanitized content never reaches a model
  boundary in this gate) and the fail-closed, human-provenance bridge
  (``bridge.py``) that is the only way connector output ever reaches the
  M1-M7 pipeline.
- **Pin (ii):** the invisible axis required in both roles, same-family
  (e.g. two zero-width spaces) or cross-family (e.g. an RTL override split
  plus a control-character separator) -- both cases already raise
  ``malformed_encoding`` and are bridge-blocked regardless of what
  ``instruction_shaped_text`` does, so these two pins document a
  **labeling** ceiling (correct adversarial labeling on an already-blocked
  item, which protects against reviewer override-fatigue), not a
  containment gap.

RC-1-R3 must never be described as closing the class of
empty-string-deletion-adjacency evasions of ``instruction_shaped_text`` in
general -- only these measured bypasses, with the two ceiling shapes above
named and pinned. As always, the detective marker is not the control.
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

    Order of operations (each idempotent on its own output) on the RETAINED
    string, ``working`` -- N0 in RC-1-R3's (Fable ruling 2026-07-28,
    superseding RC-1-R2) terms. ``working`` is never recomputed for
    detection purposes; every flag below comes from it, exactly once, as
    always:

    1. Detect and drop the Unicode replacement character (a signal of
       malformed upstream decoding), flagging ``malformed_encoding``.
    2. Strip C0/DEL control characters, also flagging ``malformed_encoding``
       (RC-1-R2 item 4 -- previously this deletion raised no flag at all,
       the one gap in an otherwise-consistent "flag every deleted-character
       class" taxonomy; steps 1 and 3 already flagged their own deletions).
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
       ``active_markup_neutralized`` when anything was removed -- the ONE
       flagging site for that flag, unchanged since Gate D.
    5. Collapse whitespace.
    6. Detect instruction-shaped phrases and flag them -- the matched text is
       left in place, unmodified: it is data, not something to strip.

       RC-1-R3 (Fable ruling 2026-07-28, SUPERSEDES RC-1-R2): checked
       against ``working`` (N0) AND three further DETECTION-ONLY variants,
       N1/N2/N3, built by :func:`_instruction_detection_variant` directly
       from ``raw`` (never chained through ``working``, since each variant
       needs its own independent substitution choice at every deletion
       site). There are exactly four normalizations available -- the full
       2x2 of {invisible-class deletions -> "" | " "} x {markup deletions ->
       "" | " "} -- and ``working``/N0 above already IS the (empty, empty)
       cell:

       - N1 = (" ", " ") -- space for both axes (RC-1-R2's variant).
       - N2 = ("", " ") -- empty for invisible, space for markup.
       - N3 = (" ", "") -- space for invisible, empty for markup.

       RC-1-R2's single variant (N1 alone) closed six of ten measured
       bypasses in Fable's adversarial probe but gave up a row RC-1-R
       happened to catch: a word split by an invisible character COMBINED
       with a different word pair joined by a deleted markup separator
       (e.g. ``"ig" + ZWSP + "nore all previous<br/>instructions"``) --
       N1 spaces every deletion site, so "ignore" becomes "ig nore" and the
       pattern no longer matches. N2 catches that row (empty reassembles
       the ZWSP-split word; space still separates the markup-joined
       words); N3 catches the mirror row (a tag-split word joined by an
       invisible-character separator). The check is a single
       ``flags.append``, one OR over all four strings.

       The remaining, accepted ceiling: **one substitution axis required in
       contradictory roles** (word-split and word-separator) -- whether by
       one character family or by two families sharing the axis -- still
       defeats all four normalizations at once. See
       ``tests/test_connectors_sanitize.py`` for both pinned shapes (markup
       axis in both roles -- the PRIORITY residual, since it carries no
       blocking flag; and invisible axis in both roles, same- and
       cross-family, both bridge-blocked via ``malformed_encoding``
       regardless of detection).

       `_INSTRUCTION_PATTERNS` itself is unchanged -- Fable explicitly
       rejected widening the patterns themselves to tolerate intra-phrase
       junk, in every ruling in this series.
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

    # N0: the retained pipeline. RC-1-R3 (Fable ruling, 2026-07-28,
    # superseding RC-1-R2) requires this to stay byte-for-byte identical to
    # what shipped at RC-1-R2 -- `working` is never recomputed or touched by
    # anything below; every flag it raises (malformed_encoding at three
    # sites, active_markup_neutralized at one) is exactly as it was.
    working = raw
    if _REPLACEMENT_CHAR in working:
        _flag_once(SecurityFlag.malformed_encoding)
        working = working.replace(_REPLACEMENT_CHAR, "")
    working = unicodedata.normalize("NFC", working)

    if _CONTROL_CHAR_RE.search(working):
        _flag_once(SecurityFlag.malformed_encoding)
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

    # RC-1-R3 (Fable ruling, 2026-07-28, SUPERSEDES RC-1-R2): RC-1-R2's
    # single space-substituted variant closed six of the ten measured
    # bypasses in Fable's adversarial probe, but gave up one row RC-1-R
    # happened to catch: a word split by an invisible character COMBINED
    # with a different word pair joined by a deleted markup separator (e.g.
    # "ig" + ZWSP + "nore all previous<br/>instructions") -- RC-1-R2's
    # variant spaces every deletion site, so "ignore" becomes "ig nore" and
    # the pattern no longer matches. There are exactly four normalizations
    # available -- the full 2x2 of {invisible-class deletions -> "" | " "}
    # x {markup deletions -> "" | " "} -- and `working`/N0 above already IS
    # the (empty, empty) cell. N1/N2/N3 below are the other three cells,
    # built by `_instruction_detection_variant` from `raw` directly (NOT
    # chained through `working`, since each cell needs its own independent
    # substitution choice at every deletion site, not a fixed choice
    # inherited from the previous cell). None of N1/N2/N3 raises any flag of
    # its own -- every flag in this function comes from `working`/N0 above,
    # exactly once, as always. Fable explicitly considered and REJECTED
    # splitting the invisible axis into its three constituent character
    # families (replacement/control/bidi) for a 16-variant refinement: the
    # extra cells it would add are all cross-family instances of the same
    # already-accepted, already-bridge-blocked ceiling (see this function's
    # module docstring and docs/threat-model.md), for zero containment gain
    # against a quadrupled reasoning surface.
    n1 = _instruction_detection_variant(raw, invisible_sub=" ", markup_sub=" ")
    n2 = _instruction_detection_variant(raw, invisible_sub="", markup_sub=" ")
    n3 = _instruction_detection_variant(raw, invisible_sub=" ", markup_sub="")

    # Step 6: detect instruction-shaped phrases and flag them -- the matched
    # text is left in place, unmodified: it is data, not something to strip.
    # Checked against `working` (N0) and all three variants (N1/N2/N3): an
    # OR over the full 2x2, one `flags.append`. `_INSTRUCTION_PATTERNS`
    # itself is unchanged -- Fable explicitly rejected widening the patterns
    # to tolerate intra-phrase junk, in every ruling in this series.
    if any(_has_instruction_shaped_text(s) for s in (working, n1, n2, n3)):
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


def _instruction_detection_variant(raw: str, *, invisible_sub: str, markup_sub: str) -> str:
    """Build one DETECTION-ONLY variant of ``raw`` for the OR in
    :func:`sanitize_text` step 6 (RC-1-R3, Fable ruling 2026-07-28,
    superseding RC-1-R2).

    Mirrors :func:`sanitize_text`'s retained pipeline (steps 1-5) exactly, in
    the same order -- replacement-char substitution, NFC normalization,
    control-char substitution, bidi/invisible substitution, markup-tag
    substitution, stray-bracket substitution, whitespace collapse -- but
    substitutes ``invisible_sub`` at every one of the replacement-character,
    C0/DEL-control-character, and zero-width/bidi/invisible-character
    deletion sites (ONE axis covering all three families: Fable explicitly
    REJECTED splitting this into three separately-parametrized families --
    a 16-variant refinement whose extra cells are all cross-family instances
    of an already-accepted, already-bridge-blocked ceiling, for zero
    containment gain against a quadrupled reasoning surface), and
    ``markup_sub`` at the markup-tag and stray-angle-bracket deletion sites
    (the other axis).

    Raises no flags of its own. Every flag :func:`sanitize_text` raises comes
    from its retained ``working`` string exactly once, as always; this
    function exists purely to produce comparison strings for the
    ``instruction_shaped_text`` OR at step 6, and its result is never stored,
    returned, logged, redacted, or truncated.
    """
    text = raw
    text = text.replace(_REPLACEMENT_CHAR, invisible_sub)
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL_CHAR_RE.sub(invisible_sub, text)
    text = _BIDI_AND_INVISIBLE_RE.sub(invisible_sub, text)
    text = _MARKUP_TAG_RE.sub(markup_sub, text)
    text = text.replace("<", markup_sub).replace(">", markup_sub)
    return _WHITESPACE_RE.sub(" ", text).strip()


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
