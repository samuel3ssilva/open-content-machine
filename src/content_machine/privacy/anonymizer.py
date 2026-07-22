"""Deterministic pseudonymization and identifier removal (ADR 0003).

The pseudonym recipe is frozen here and in ``schemas/``:

    id = "id_" + HMAC-SHA256(
            key   = salt,
            msg   = casefold(first)|casefold(last)|casefold(company)|casefold(email or ""),
         ).hexdigest()[:16]

Anonymization *removes* direct identifiers (names, emails, URLs, and the raw
company string) rather than masking them. The output model uses an explicit
allowlist and forbids extra fields, so a leaked field cannot silently ride
along.
"""

from __future__ import annotations

import hmac
import secrets

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.normalize import NormalizationResult, NormalizedConnection

# Frozen constants of the pseudonym recipe (mirrored in schemas/).
_HASH_ALGO = "sha256"
_ID_PREFIX = "id_"
_ID_HEX_LEN = 16
_FIELD_SEPARATOR = "|"


class AnonymizedConnection(BaseModel):
    """Safe-zone connection record: the ONLY input allowed into stats/reports.

    Contains no direct identifiers by construction. ``extra="forbid"`` makes any
    accidental additional field a hard error rather than a silent leak.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    company: str | None = None
    position: str | None = None
    seniority_bucket: str = "unknown"
    seniority_inferred: bool = True
    connected_year: int | None = None
    is_duplicate: bool = False


class AnonymizationResult(BaseModel):
    """Anonymized rows plus provenance about the salt used."""

    connections: list[AnonymizedConnection] = Field(default_factory=list)
    ephemeral_salt: bool = False


def pseudonym(
    salt: str,
    *,
    first_name: str | None,
    last_name: str | None,
    company: str | None,
    email: str | None,
) -> str:
    """Return the deterministic pseudonym id for one identity.

    Uses HMAC-SHA256 keyed by ``salt`` over the casefolded identity fields
    joined by ``|``. The same inputs and salt always yield the same id; a
    different salt yields a different id. Email is included because it is the
    strongest identity signal; an absent email contributes an empty segment.
    """
    parts = [
        (first_name or "").casefold(),
        (last_name or "").casefold(),
        (company or "").casefold(),
        (email or "").casefold(),
    ]
    message = _FIELD_SEPARATOR.join(parts).encode("utf-8")
    digest = hmac.new(salt.encode("utf-8"), message, _HASH_ALGO).hexdigest()
    return f"{_ID_PREFIX}{digest[:_ID_HEX_LEN]}"


def anonymize(normalized: NormalizationResult, salt: str | None) -> AnonymizationResult:
    """Anonymize normalized connections into the safe zone.

    If ``salt`` is ``None``, a cryptographically random ephemeral salt is
    generated for this run (ids will not be stable across runs) and
    ``ephemeral_salt=True`` is set so callers can warn the user. Direct
    identifiers are dropped, not carried forward.
    """
    ephemeral = salt is None
    effective_salt = salt if salt is not None else secrets.token_hex(32)

    connections = [
        _anonymize_one(conn, effective_salt) for conn in normalized.connections
    ]
    return AnonymizationResult(connections=connections, ephemeral_salt=ephemeral)


def _anonymize_one(conn: NormalizedConnection, salt: str) -> AnonymizedConnection:
    return AnonymizedConnection(
        id=pseudonym(
            salt,
            first_name=conn.first_name,
            last_name=conn.last_name,
            company=conn.company,
            email=conn.email,
        ),
        company=conn.company,
        position=conn.position,
        seniority_bucket=conn.seniority_bucket,
        seniority_inferred=conn.seniority_inferred,
        connected_year=conn.connected_year,
        is_duplicate=conn.is_duplicate,
    )


def strip_for_model(anonymized_rows: list[AnonymizedConnection]) -> list[dict[str, str]]:
    """Reduce anonymized rows to the minimal field set allowed to cross TB-2.

    This is the future choke point for model calls (docs/architecture.md §3,
    trust boundary TB-2): the ONLY fields that may ever reach a model provider
    are the normalized ``company`` and ``position`` strings. Names, emails,
    URLs, ids, dates, and duplicate flags are never included. Rows where both
    fields are empty are dropped. Nothing in this sprint's pipeline calls a real
    provider; this function exists so the boundary is testable today.
    """
    stripped: list[dict[str, str]] = []
    for row in anonymized_rows:
        company = row.company or ""
        position = row.position or ""
        if not company and not position:
            continue
        stripped.append({"company": company, "position": position})
    return stripped
