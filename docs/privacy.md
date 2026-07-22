# Privacy Policy (engineering)

This document defines how Open Content Machine handles personal data. It is
binding for all code and all agents. Changes require review by
fable-principal-architect or fable-security-auditor.

## What the system processes

The first data source is a professional-network connections export (e.g. the
CSV LinkedIn provides through its official data-export feature). Typical fields:
first name, last name, company, position, email (often empty), profile URL,
connected-on date.

**Only exports legitimately obtained by the user are accepted. Scraping is not
supported and will not be built.**

## Data classification

| Class | Examples | Handling |
|---|---|---|
| Direct identifiers | names, emails, profile URLs | Stay in `data/private/` and process memory only. Removed at anonymization. Never in git, logs, reports, or model calls. |
| Indirect attributes | company, job title, connection date | Normalized, then allowed into anonymized outputs and aggregates. |
| Derived data | pseudonym IDs, statistics, inferred segments | Safe zone; still treated as sensitive-by-default (aggregates can identify in small populations). |
| Secrets | API keys, salt | `.env` only. Never committed, logged, or echoed. |

## Rules

1. **Local-first**: no personal data leaves the machine.
2. **Data minimization**: each processing step receives only the fields it
   needs; model calls receive only an explicit allowlist (see ADR 0002, TB-2).
3. **Deterministic before generative**: validation, normalization, dedup, and
   statistics are plain local code. LLMs are used only where deterministic code
   cannot do the job, and never on direct identifiers.
4. **Pseudonymization** is HMAC-SHA256 with a private, user-local salt
   (ADR 0003). Anonymized outputs contain no direct identifiers at all.
5. **Inference labeling**: every inferred field is marked `inferred: true` with
   a confidence level. The existence of a connection is never presented as
   interest in the creator's content.
6. **Logs and errors** reference rows/columns, never field values.
7. **The real CSV** lives in `data/private/` which is git-ignored; the repo
   ships only `examples/synthetic-connections.csv` with invented people at
   `example.com`-style domains.
8. **Right to delete**: all state is local files; deleting `data/private/` and
   generated outputs removes everything. There is no hidden copy.

## Consent points (human-in-the-loop)

- Importing a file into `data/private/` is a user action.
- Any future feature that sends anonymized fields to an external model will be
  opt-in, will show exactly which fields cross the boundary, and requires
  Founder approval before implementation.
- Publication of any content is always manual.
