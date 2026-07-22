# ADR 0003 — Deterministic pseudonymization with local salt

- Status: Accepted
- Date: 2026-07-22
- Decider: fable-principal-architect (Fable), with fable-security-auditor review
- Model responsible: Fable

## Context

Audience Intelligence must track connections across re-imports (dedup, growth
over time) without ever exposing who they are. Outputs (anonymized JSON,
reports) must be safe to inspect, and nothing derived from direct identifiers
may be reversible from published artifacts.

## Decision

1. Each connection gets a pseudonym ID:
   `HMAC-SHA256(salt, casefold(first_name)|casefold(last_name)|casefold(company))`,
   truncated to 16 hex chars with an `id_` prefix. Email is included when
   present (it is the strongest identity signal) — the exact recipe is frozen in
   `schemas/` and code constants.
2. The **salt is user-local and private** (`CONTENT_MACHINE_SALT` in `.env`,
   never committed, never logged, never printed). Without the salt, pseudonyms
   cannot be brute-forced from name dictionaries by third parties reading a
   leaked report. If no salt is configured, the CLI generates an ephemeral one
   per run and warns that IDs will not be stable across runs.
3. Anonymization **removes** direct identifiers (first/last name, email,
   profile URL) rather than masking or hashing them into the output. Retained
   fields are an explicit allowlist: pseudonym id, normalized company, normalized
   title, connection date, plus derived aggregates.
4. Free-text fields not on the allowlist are dropped by default.
5. Privacy tests assert that anonymized outputs and reports contain no `@`
   emails, no `http(s)://` URLs, and none of the synthetic fixture names.

## Consequences

- Stable cross-run IDs enable dedup and longitudinal stats with no identifier
  storage; changing the salt intentionally rotates all pseudonyms.
- HMAC with a private salt (not plain SHA-256) blocks dictionary attacks on
  common names — this is pseudonymization, and we still treat outputs as
  sensitive-by-default, but the residual risk is documented in the threat model.
- If a user loses the salt, historical IDs cannot be regenerated — acceptable;
  the source CSV remains the ground truth.

## Alternatives considered

- **Random UUID per row** — no cross-import stability; dedup across re-exports
  breaks.
- **Plain SHA-256 without salt** — vulnerable to name-dictionary reversal;
  rejected.
- **Local mapping table (id ↔ name)** — stores identifiers at rest; more useful
  but higher risk. Deferred; would require its own ADR and encryption story.
