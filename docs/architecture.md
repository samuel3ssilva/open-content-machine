# Architecture

Status: approved by Principal Architect (Fable) — Bootstrap Sprint, 2026-07-22.
Owner: fable-principal-architect. Changes affecting module boundaries or trust
boundaries require a new ADR.

## 1. Principles

1. **Local-first.** Everything runs on the creator's machine. No cloud
   infrastructure, no remote database, no telemetry.
2. **Privacy by design / data minimization.** Personal data is processed as
   little as possible, as locally as possible, and stripped before any model
   boundary. Deterministic local code is always preferred over an LLM call.
3. **Simple, modular, replaceable.** Plain Python packages with explicit
   interfaces. Any module can be rewritten without touching the others.
4. **Provider-agnostic core.** The product core never imports a vendor SDK
   directly; it talks to a `ModelProvider` interface. The demo works fully
   offline via `MockProvider`.
5. **Inference is labeled.** Any field produced by heuristics or models carries
   an explicit confidence level. A connection's existence is never treated as
   evidence of interest in content.

## 2. Module map

```
src/content_machine/
├── config/      # typed settings (Pydantic BaseSettings): paths, salt, provider choice
├── ingestion/   # reading external exports (CSV) safely: encoding, columns, dedup
├── privacy/     # PII detection/stripping, deterministic pseudonymization
├── audience/    # normalization, statistics, report generation (MD + JSON)
├── providers/   # ModelProvider interface + Anthropic/OpenAI/Mock implementations
└── cli/         # Typer app: `content-machine` root, `audience` subcommands
```

Planned but NOT yet created (added only when their sprint starts): positioning,
voice, oracle, interview, drafting, evidence, council, revision, repurpose,
analytics. They will follow the same pattern: pure module + CLI subcommand +
schemas in `schemas/`.

### Dependency rules

- `cli` may import everything; nothing imports `cli`.
- `audience` may import `ingestion`, `privacy`, `config`.
- `ingestion` and `privacy` may import only `config` (and stdlib/Pydantic).
- `providers` may import only `config`. Vendor SDKs are imported **only inside**
  the corresponding provider module, lazily, so the core installs and runs
  without them.
- No module reads environment variables directly; only `config` does.

## 3. Data zones and trust boundaries

| Zone | Location | Contents | May leave the machine? |
|---|---|---|---|
| **Private** | `data/private/` (git-ignored) | Real exports (e.g. LinkedIn connections CSV), raw personal data | Never |
| **Working** | memory / local temp | Parsed + normalized records incl. names/emails/URLs | Never |
| **Anonymized** | local output files | Pseudonymized IDs, aggregated fields, no direct identifiers | Only fields on the allowlist, only with user consent |
| **Public** | git repository | Code, docs, schemas, synthetic examples | Yes (reviewed) |

**Trust boundary TB-1 (disk → repo):** enforced by `.gitignore` + privacy tests
+ pre-push audit. Nothing from `data/private/` is ever committed.

**Trust boundary TB-2 (local → model provider):** enforced in code. The only
path to a provider call is through `privacy.strip_for_model()`, which passes an
explicit **allowlist** of fields (never names, emails, URLs, free-text notes).
In this sprint no real provider call exists at all; `MockProvider` is the only
implementation exercised.

**Trust boundary TB-3 (report → publication):** reports are generated from the
anonymized zone only, and publication is always a human action. The system never
posts anywhere automatically.

## 4. Audience Intelligence flow (MVP)

```
<export.csv>
   │  ingestion.load_csv()        — encoding detection (utf-8/utf-8-sig/latin-1),
   │                                header mapping tolerant to export variants,
   │                                row-level issues collected, never silently dropped
   ▼
RawConnection[]                   — optional fields only; absent column ≠ empty value
   │  audience.normalize()        — trim/casefold, company & title normalization,
   │                                duplicate detection (same person, same row)
   ▼
NormalizedConnection[]
   │  privacy.anonymize()         — HMAC-SHA256(salt, stable identity fields) → pseudonym id;
   │                                names/emails/URLs REMOVED, not masked;
   │                                remaining fields pass an allowlist
   ▼
AnonymizedConnection[]            — safe zone; this is the ONLY input to stats/reports
   │  audience.analyze()          — deterministic aggregates: totals, duplicates,
   │                                completeness, top companies/titles/seniority buckets,
   │                                connection growth by year (when dates exist)
   ▼
AudienceReport (Pydantic)
   │  audience.render()
   ▼
report.md + report.json           — human-readable + machine-readable
```

Future (next sprints, design only): batch classification of anonymized
title/company pairs into audience segments via `ModelProvider`, with structured
outputs validated against `schemas/`, confidence levels, and an explicit
"inferred" marker on every derived field.

## 5. CLI contract (current)

```
content-machine --help
content-machine version
content-machine demo                            # runs the full flow on examples/synthetic-connections.csv
content-machine audience validate <file.csv>    # schema + quality report, exit 0/1
content-machine audience anonymize <file.csv> [-o out.json]
content-machine audience report <file.csv> [-o report.md] [--json report.json]

# Sprint 1.x additions
content-machine audience inspect <file.csv> --dry-run              # privacy-safe, read-only structural inspection
content-machine audience export-public <report.json> -o <out.json> [--md <out.md>]  # sanitize a private report
content-machine audience evaluate-review <review.csv>               # aggregate a private Founder review CSV
content-machine audience compare-classifiers <fixture.csv> --baseline <snapshot.json>  # diff current classifier against a baseline
content-machine source inspect <folder> --dry-run --output-dir <dir>  # metadata-safe private source-folder inventory
```

All commands work offline, with no API key, and print actionable errors
(file, row, column, what to fix).

## 6. Error handling

- User errors (bad path, wrong encoding, missing required columns) → friendly
  message + non-zero exit, never a traceback.
- Row-level problems (missing fields, duplicates) → collected into the
  validation report, counted, and shown; processing continues.
- Bugs → normal traceback (do not swallow), because we want honest failures.
- Log lines and error messages must never include personal field values;
  reference rows by index and columns by name.

## 7. What we deliberately do not build now

Kubernetes, microservices, remote databases, distributed queues, web dashboard,
cloud infra, agent frameworks, real provider calls, scraping of any kind. Each
requires a future ADR with demonstrated need.
