# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Sprint 1 — `content-machine audience inspect FILE --dry-run`: privacy-safe,
  read-only structural inspection of an external connections file. Prints
  file/column metadata, row counts, the transformations that would run, and
  the direct identifiers that would be removed — never a single cell value,
  never a network call, and never a copy of the source file. `--dry-run` is
  mandatory; the command refuses to run without it.
- Sprint 1 — `content-machine audience export-public REPORT.json -o OUT.json
  [--md OUT.md]`: sanitizes a private `AudienceReport` into a shareable
  `PublicReport` (`src/content_machine/audience/public_report.py`). Suppresses
  every group under 10 (`SUPPRESSION_THRESHOLD`) — top-lists are dropped,
  distributions are merged into a `(suppressed, <10)` bucket, and small
  candidate segments are dropped entirely. Output carries
  `privacy_label="sanitized-aggregate"` and a review banner; sanitization is
  always an explicit, human-invoked step, never automatic.
- Sprint 1 — deterministic, explainable role-family classification
  (`src/content_machine/audience/classify.py`): maps a normalized job title to
  one of 9 coarse `RoleFamily` values with an explicit `high`/`medium`/`low`/
  `unknown` confidence and a `matched_evidence` string naming the exact
  rule/keyword that fired (never a person's data). Ambiguous single-token
  titles never reach `high`; unclassifiable titles are left `unknown` rather
  than forced into a family. Every anonymized connection now carries
  `role_family`, `role_confidence`, and `role_evidence`.
- Sprint 1 — localized (Portuguese/Spanish) header aliases and connection-date
  parsing in the CSV loader, plus fixtures for column-order independence and
  localized headers (`examples/synthetic-connections-variants/`); seniority
  inference refactored onto 7 explicit buckets.
- Sprint 1 — expanded private report (`audience/report.py`): role-family,
  seniority, and confidence distributions; an `unknown_share` metric;
  deterministic candidate segments (role family × seniority) with aggregate
  evidence and a rationale; mandatory limitations always included in the
  render.
- Sprint 1 — regenerated JSON Schemas for the new/changed contracts
  (`schemas/role_classification.schema.json`,
  `schemas/public_report.schema.json`, and updates to
  `schemas/audience_report.schema.json` /
  `schemas/anonymized_connection.schema.json`).
- Sprint 1 — test suite: `tests/test_classify.py`,
  `tests/test_cli_inspect.py`, `tests/test_export_public.py`,
  `tests/test_loader_variants.py`, `tests/test_report_expanded.py`, an
  8,000-row performance test (`tests/test_performance.py`), and a dedicated
  CEO-mandated acceptance suite (`tests/test_sprint1_requirements.py`)
  covering dry-run leak/network/copy guarantees, classification determinism,
  end-to-end public-export suppression via the real pipeline, and no-PII-in-logs
  across success and failure paths — 158 tests total, all offline.
- `docs/real-data-runbook.md`: the only approved procedure for running the
  pipeline against a real connections export, gated on a mandatory dry-run
  and explicit Founder authorization.
- `docs/MVP_STATUS.md`: a single live dashboard tracking Sprint 1 progress,
  linked from the README.
- Project bootstrap: repository scaffolding, `LICENSE` (Apache-2.0),
  `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `ROADMAP.md`.
- Governance and security documentation: `docs/architecture.md`,
  `docs/privacy.md`, `docs/threat-model.md`, `SECURITY.md`,
  `docs/model-routing.md`, and ADRs 0001–0003 (stack and local-first
  operation, model provider abstraction, deterministic pseudonymization).
- Product direction documentation: `docs/vision.md`,
  `docs/build-in-public.md`, `docs/product-requirements.md`.
- `data/README.md` describing the `data/private/` workflow and how to
  delete local data.
- `docs/private-workspace.md` formalizing the public repository vs. private
  local workspace boundary.
- `.github` issue and pull request templates.
- `prompts/README.md` for future versioned prompt templates.
- `CLAUDE.md` repository guidance for Claude Code sessions.
- Installable `content-machine` package (`pip install -e ".[dev]"`) with
  typed `Settings` (`config/`) reading `CONTENT_MACHINE_*` environment
  variables and an optional `.env`.
- Tolerant CSV ingestion (`ingestion/csv_loader.py`): encoding fallback
  chain (utf-8-sig → utf-8 → latin-1), LinkedIn preamble skipping, header
  alias matching, and row-level issue collection that never carries field
  values.
- Deterministic normalization (`audience/normalize.py`): whitespace
  collapsing, company legal-suffix stripping, heuristic seniority
  inference, connection-date parsing, and exact-duplicate detection.
- Deterministic anonymization per ADR 0003 (`privacy/anonymizer.py`):
  HMAC-SHA256 pseudonym ids, an allowlist-only `AnonymizedConnection` model
  (`extra="forbid"`), and the `strip_for_model()` choke point that limits
  any future model-provider input to `company`/`position` only.
- Aggregate audience analytics and Markdown/JSON report rendering
  (`audience/report.py`), always including the mandatory
  no-interest-inference caveat and labeling seniority as inferred.
- Provider abstraction (`providers/`) with an offline `MockProvider` as the
  default; the `anthropic`/`openai` providers ship as non-networking stubs
  per ADR 0002 — no network I/O occurs in this sprint.
- `content-machine` CLI (Typer): `--help`, `version`, `demo`, and
  `audience validate|anonymize|report`, all offline and requiring no API
  key.
- Synthetic example dataset (`examples/synthetic-connections.csv`),
  checked-in expected pipeline output (`examples/expected-output/`), and
  exported public JSON Schemas (`schemas/`).
- Test suite covering the loader, normalization, anonymization, report
  rendering, and CLI, plus dedicated privacy-guarantee tests
  (`tests/test_privacy_guarantees.py`), loader edge cases
  (`tests/test_loader_edge_cases.py`), and a golden-output regression test
  against `examples/expected-output/` (`tests/test_golden_outputs.py`).
- CI (`.github/workflows/ci.yml`): ruff, mypy, and pytest on Python 3.12,
  plus a release security checklist step that fails the build on tracked
  private-data-shaped filenames, secret-shaped literals, or non-example
  email addresses in tracked content (per `SECURITY.md`).

### Security

- Hardened `.gitignore` against full platform data exports (any
  `*DataExport*` folder and the standard LinkedIn export filenames), after
  a real export folder briefly appeared in a working tree.
- Sanitized containment report for the data-export incident
  (`docs/security/linkedin-export-incident.md`).
