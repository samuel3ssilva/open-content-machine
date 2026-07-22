# Threat Model

Owner: fable-security-auditor. Revisit at every new module, integration, or
trust-boundary change. Method: lightweight STRIDE over the data-flow in
`docs/architecture.md` §3–4.

## Assets

- **A1** Real connections export(s) in `data/private/` (names, emails, URLs).
- **A2** The private salt and any API keys in `.env`.
- **A3** Anonymized outputs and reports (sensitive-by-default aggregates).
- **A4** The public repository's integrity and reputation.
- **A5** The Founder's accounts (GitHub, future platform accounts).

## Actors

- **Founder/user** (trusted, may make mistakes).
- **Contributors / PR authors** (semi-trusted; code review gate).
- **Public readers** of the repo (untrusted).
- **Model vendors** (honest-but-curious; must never receive identifiers).
- **AI agents (Fable/Opus/Sonnet)** — treated as fallible operators: they get
  least privilege, never see `data/private/`, and their output is reviewed.

## Threats and controls

| ID | Threat | Asset | Controls (implemented this sprint unless noted) |
|----|--------|-------|--------------------------------------------------|
| T1 | Real CSV accidentally committed and pushed | A1, A4 | `.gitignore` (`data/private/*`, `*connections*.csv`); privacy test that paths are ignored; pre-push checklist in SECURITY.md; audit before first push |
| T2 | Secrets committed (`.env`, keys) | A2 | `.gitignore`; `.env.example` with empty values; secret-scan step in checklist; CI needs no secrets |
| T3 | PII leaks into anonymized output or report | A1, A3 | Allowlist-based anonymizer (removal, not masking); privacy tests grep outputs for emails/URLs/fixture names |
| T4 | PII sent to a model vendor | A1 | No real provider implemented this sprint; TB-2 choke point `strip_for_model()`; providers module is the only network-capable code |
| T5 | Pseudonyms reversed from a leaked report | A3 | HMAC with private salt (ADR 0003), not plain hashes; direct identifiers absent entirely; reports aggregate ≥ top-N only |
| T6 | PII or secrets in logs/tracebacks | A1, A2 | Error-message policy (row/column refs only); tests assert no personal values in CLI output on failure paths |
| T7 | Malicious or typosquatted dependency | A4, A2 | Minimal dependency set (typer, pydantic, pytest, ruff, mypy); new deps require review per agent rules |
| T8 | Malicious PR (backdoor, exfil in providers/) | A4 | Human + Opus review; CI runs offline; any network code outside `providers/` is a review blocker |
| T9 | Prompt injection via data files (a CSV cell containing instructions to an agent/model) | A1, A3 | Data is treated as data: deterministic pipeline this sprint; future model calls receive only normalized short fields (company/title), never free text, with structured outputs |
| T10 | Agent overreach (an AI agent reads private data or pushes) | A1, A4 | Agent definitions forbid `data/private/` and force-push; no bypassPermissions; integration only through reviewed commits |
| T11 | Loss of salt → broken longitudinal IDs | A3 | Documented recovery stance in ADR 0003 (source CSV is ground truth); warning when running with ephemeral salt |
| T12 | Small-population re-identification in published aggregates | A3 | Reports are for the user, not auto-published; build-in-public guidance forbids publishing raw aggregates; future: k-anonymity floor before any sharing feature |

## Non-threats (out of scope, by design)

- Multi-user access control — single-user local tool.
- Server hardening — there is no server.
- DoS — nothing is exposed.

## Standing rules for agents and contributors

1. Never open `data/private/` contents in any tool or model context.
2. Never paste tokens/keys into chats, code, or issues.
3. Anything that adds network I/O, a dependency, or a new data field crossing
   TB-2 is security-relevant → Fable review required.
