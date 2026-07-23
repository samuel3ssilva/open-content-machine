# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (creator intelligence workflow — process only, no private findings)

- Generic documentation of the creator-intelligence workflow (inventory →
  Founder triage → deterministic extraction → sanitized packages →
  individually approved qualitative synthesis → labeled private drafts →
  manual publication), in `docs/creator-intelligence-workflow.md`.
- MVP status panel tracks for Creator Intelligence and Content MVP.

### Added

- Sprint 1.2 (Phase 1, ticket SONNET-1.2b) — default exclusion patterns for
  `content-machine source inspect`. `sources/inventory.py` gains
  `DEFAULT_EXCLUDED_DIRS` (`node_modules`, `.git`, `dist`, `build`,
  `coverage`, `.next`, `.nuxt`, `.cache`, `__pycache__`, `.venv`, `venv`,
  `.turbo`, `.parcel-cache`, `out`, `.output`, `vendor`, `bower_components`,
  `.pnpm-store`, `.yarn`) and a new `excluded_dirs` parameter on
  `scan_source_folder` (`None` = use the defaults, `frozenset()` = exclude
  nothing). A directory whose name matches (casefolded, exact match, at any
  depth) is skipped entirely — never descended, never emitted as an entry —
  and counted in the new `InventoryTotals.excluded_dirs` field. The CLI gains
  an `--include-all` flag to disable the default exclusions, and stdout gains
  one line: "Excluded dependency/generated directories: N (default patterns;
  use --include-all to disable)". 13 new tests across
  `tests/test_source_inventory.py` and `tests/test_cli_source_inspect.py`
  cover: `node_modules` and other generated directories (`dist`, `coverage`,
  `__pycache__`) not walked and their inner sentinel content absent from
  entries/artifacts/stdout; nested-depth exclusion; case-insensitive
  matching; `excluded_dirs=frozenset()` and `--include-all` walking
  everything; the scan still never modifies the source tree or makes network
  calls with exclusions active; and no absolute path or sentinel body leaks
  into stdout or artifacts. `source_inventory.schema.json` regenerated to
  include the new `excluded_dirs` totals field.
- Sprint 1.2 (Phase 1, ticket SONNET-1.2) — `content-machine source inspect
  FOLDER --dry-run --output-dir DIR` CLI command
  (`src/content_machine/cli/main.py`): wires the metadata-safe source
  inventory module up to a Typer sub-app. Requires `--dry-run` (refuses
  otherwise, exit 1) and a required `--output-dir`; both `FOLDER` and
  `--output-dir` are rejected (exit 1) if they resolve inside the repository
  tree. Writes `source-inventory-private.md`, `source-inventory-private.json`,
  and `source-review-private.csv` to `--output-dir` (dir mode `0700`, file
  mode `0600`) using the fixed sanitized `root_label="<private-source>"` —
  never the real path. Stdout prints AGGREGATE counts only (totals,
  by-category, by-status, duplicates, human-readable bytes) plus explicit
  "no network", "not copied/modified", and Founder-approval-gate reminders;
  no individual file name or ref is ever printed. `SourceScanError` is
  reported as a friendly message with no traceback. 18 new tests
  (`tests/test_cli_source_inspect.py`) cover: source folder never
  copied/modified (before/after snapshot), no network calls, no absolute
  path or sentinel file-body content leaking into any of the three
  artifacts or stdout, symlink-escape/archive/hidden/encrypted-suspected
  files reported only as counts, missing-flag and inside-repo rejections,
  and review-CSV approval columns empty for every row (including category-C
  rows, which are never auto-approved).
- Sprint 1.2 (Phase 1, ticket OPUS-1.2) — new private source-folder
  inventory module (`src/content_machine/sources/inventory.py`,
  `tests/test_source_inventory.py`): a metadata-only scanner for a
  creator's private biography folder. Never reads a file's body beyond a
  bounded 512-byte magic-number sniff and a streaming SHA-256 for exact-
  duplicate detection; symlinks are never followed (including path-
  traversal escapes), archives are never extracted, hidden files/dirs are
  recorded but never descended, and unreadable files degrade to a status
  code with no path or errno leaking. Assigns a provisional, explainable
  A/B/C/D/unknown privacy category (most-restrictive-wins lattice,
  PT/EN-aware) via `categorize()`, and renders three deterministic outputs
  (`to_markdown`, `to_json`, `to_review_csv`) — the review CSV's
  `approved_for_analysis`, `intended_use`, and `founder_notes` columns are
  intentionally empty on every row; no inventory model has an approval
  field. Also adds the frozen Phase-2 provenance draft contracts
  (`src/content_machine/sources/contracts.py`) and their JSON schemas. See
  [`docs/source-approval-gate.md`](docs/source-approval-gate.md) for the
  binding approval rules this module exists to feed.
- Sprint 1.1 — classifier rebuilt as a seven-tier precedence engine
  (`src/content_machine/audience/classify.py`, ticket OPUS-1.1): ownership
  overrides, exact/phrase functional matches, strong domain keywords,
  recognized professions, general executive terms, weak/ambiguous tokens,
  unknown — each tier documented with its precedence and confidence policy.
  Role *family* (function) and *seniority* (level) are now derived
  independently from the same normalized title
  (`content_machine.audience.normalize.infer_seniority`); a seniority word
  alone (e.g. "Director") never assigns a family, and a functional
  director/head/VP title (e.g. "Director of Engineering") always keeps its
  function rather than falling into `founder_executive`.
- Sprint 1.1 — new evaluation harness (`src/content_machine/audience/evaluate.py`,
  `tests/test_evaluate.py`): scores the classifier against a hand-labeled
  synthetic CSV fixture, reporting `high_confidence_precision`,
  `overall_classified_precision`, `unknown_rate`, and family/seniority
  confusion matrices. `unknown` predictions are excluded from every
  precision denominator by design, so forcing an ambiguous title into a
  family can never inflate precision (metric-integrity rule, audited by
  Fable). Reports are aggregate-only and never carry a raw title.
- Sprint 1.1 — substantially broadened PT/EN vocabulary across all tier
  tables (engineering/data/AI, product, marketing, sales/BD, design/UX,
  operations/people/finance/legal, education/research, and recognized
  professions) — 372 total keyword/phrase rules, up from 276. Covers common
  Brazilian/international LinkedIn title patterns (e.g. "Engenheira de
  Dados", "Desenvolvedor Full Stack", "Analista de Qualidade", "SDR"/"BDR",
  "Scrum Master", C-level functional acronyms CTO/CIO/CISO/CDO/CPO/CHRO/CRO)
  plus a small set of deliberately *undocumented-as-mapped* ambiguous tokens
  (bare "Cientista", "Especialista", "Fiscal", "BI") that are left `unknown`
  rather than forced — every non-obvious decision is recorded in the new
  [`docs/classification.md`](docs/classification.md) decision table with a
  dedicated regression test.
- Sprint 1.1 — labeled evaluation fixture
  (`tests/fixtures/labeled_titles_synthetic.csv`) grown from 126 to 259
  synthetic rows, covering PT, EN, mixed-language, compound, company-suffix,
  ambiguous, and deliberately non-conventional titles (expected `unknown`).
  Measured on the grown fixture: `high_confidence_precision` 1.0,
  `overall_classified_precision` 1.0, `unknown_rate` 0.0695 (well under the
  0.25 ceiling), zero functional-leadership→`founder_executive` confusions.
- Sprint 1.1 — `docs/classification.md`: the seven-tier precedence model,
  family/seniority independence, confidence semantics, the metric-integrity
  rule, how to run the evaluation harness, and the full documented
  edge-case decision table.
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
