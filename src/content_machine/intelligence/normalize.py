"""Deterministic, idempotent normalization for intelligence signals.

Every function here is pure and idempotent (``f(f(x)) == f(x)``) so that
repeated normalization passes (e.g. re-running the loader over already
normalized fixtures) never drift. Nothing here reads a clock or the network;
date parsing is strict ISO-8601 only (``date.fromisoformat``), never
locale-dependent ``strptime``.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlsplit

# Small, generic stopword set dropped from title/summary token signatures.
# Deliberately short and topic-agnostic -- this is not a linguistic stemmer,
# just enough to keep Jaccard comparisons from being swamped by function words.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "for",
        "to",
        "with",
        "is",
        "are",
        "at",
        "by",
        "from",
        "as",
        "it",
        "its",
        "this",
        "that",
        "into",
        "about",
    }
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


def normalize_text(text: str) -> str:
    """Idempotent normalization of a title or summary string.

    Lowercases, strips punctuation, collapses whitespace, and drops the
    stopword set above. ``normalize_text(normalize_text(x)) == normalize_text(x)``
    for any ``x`` -- the output is already lowercase, punctuation-free,
    whitespace-collapsed, and stopword-free, so re-running it is a no-op.
    """
    lowered = text.lower()
    stripped = _NON_ALNUM_RE.sub(" ", lowered)
    tokens = [tok for tok in stripped.split() if tok not in _STOPWORDS]
    return " ".join(tokens)


def token_signature(text: str) -> frozenset[str]:
    """Return the deduplicated token set used for Jaccard similarity."""
    return frozenset(normalize_text(text).split())


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets. Two empty sets are defined as 1.0
    (identical, vacuously); one empty and one non-empty is 0.0."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalize_canonical_reference(reference: str) -> str:
    """Idempotent canonicalization of a ``stable_reference`` string.

    URL-shaped references (containing ``://``): lowercase the host, strip the
    scheme, strip a leading ``www.``, drop the query string and fragment, and
    strip a trailing slash from the path.

    Non-URL (opaque) references -- e.g. ``"email:<slug>"`` -- carry no URL
    structure to canonicalize, so they are normalized only by lowercasing and
    trimming surrounding whitespace. A URL-shaped input's canonical form
    contains no ``://``, so a second pass takes the opaque branch and returns
    the same (already lowercase) string unchanged -- idempotent either way.
    """
    candidate = reference.strip()
    if "://" in candidate:
        parts = urlsplit(candidate)
        host = parts.netloc.lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        path = parts.path.rstrip("/")
        return f"{host}{path}".lower()
    return candidate.lower()


def parse_iso_date(value: str) -> date | None:
    """Strict ISO-8601 date parsing. Returns ``None`` on any failure rather
    than raising -- callers decide whether a missing/invalid date is fatal for
    the record it belongs to."""
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
