# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
