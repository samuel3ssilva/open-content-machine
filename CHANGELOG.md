# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (AI & Claude Intelligence Brief v0.1 — weekly, synthetic signals only)

The Intelligence Brief turns many authorized signals into a small number of
ranked, evidence-checked, actionable weekly topics. Everything below runs
**fully offline against synthetic fixtures**: no connector, no scheduler, no
network path, and no real source is implemented. Every brief terminates in
`awaiting_founder_review` — nothing is ever published automatically.

- Gate C (M7) — `content-machine intelligence weekly-run`: the end-to-end
  weekly engine, composing load → cluster → rank → tier → brief → library in
  one command (`--signals`, `--reference-date`, `--timezone`, `--profile`,
  `--library`, `--output-dir`, `--dry-run`, `--regenerate`). Documented
  default cadence is Saturday 18:00 `America/Sao_Paulo` with an example cron
  line — **documentation only; nothing in the package schedules itself**.
- Gate C — timezone-aware seven-day window: `[reference_date − 7d 00:00,
  reference_date 00:00)` resolved at local midnight via `zoneinfo`, with the
  inclusive/exclusive convention documented and tested.
- Gate C — deterministic run identity: `run_id` is a SHA-256 over
  (week label, input fingerprint, code version, profile version, window start,
  window end), so two reference dates inside the same ISO week no longer
  collide; the input fingerprint is order-independent, and the wall-clock
  execution timestamp is recorded in the manifest but kept out of the run
  identity.
- Gate C — idempotent re-runs and explicit regeneration: repeating a completed
  week is skipped without duplicating library, score-history, or audit rows;
  `--regenerate` redoes the week without duplication.
- Gate C — atomic outputs with rollback: the weekly output set is staged in
  temporary files and swapped into place, and a failure at any point —
  including mid-rename — restores the output directory byte-for-byte.
- Gate C — topic library v0.2 (`intelligence/library.py`): topic merge
  (shared subject entity plus normalized-title Jaccard ≥ 0.7, surviving entry
  keeps the earliest `first_seen`, absorbed entry becomes `merged` with
  `merged_into`, titles preserved as aliases, histories unioned, audit event
  emitted, no double counting; `merged` is the eleventh lifecycle status),
  deterministic decay of an effective rank score by weeks since last evidence
  (20/10/2 per week for urgent/time-sensitive/evergreen, never mutating the
  stored score), staleness after eight weeks (dropped from "current", kept in
  history), structured relevance reasons carrying a profile version, and a
  markup-neutralized `normalized_summary` capped at 280 characters that never
  retains a raw body.
- Gate C — weekly deltas: per-topic score, rank, and tier movement plus new
  evidence and an `is_new` flag; a topic absent from the previous week is
  reported as new with an undefined score delta, never as a drop.
- Gate C — eight artifacts per weekly run: `brief.md`, `brief.json`,
  `topics.jsonl`, `score-history.jsonl`, `audit.jsonl`, `run-manifest.json`,
  `movements.md`, `discarded.jsonl`.
- Gate C — validated by a synthetic three-week demonstration (seeding, Founder
  decisions, deferred return on a score trigger, rejected topics never
  returning automatically, staleness at eight weeks, evergreen versus
  time-sensitive decay, byte-identical idempotent re-run, and duplication-free
  regeneration). The demonstration outputs live in the private local
  workspace, never in this repository.
- Gates A/B (M1–M6, merged earlier in this cycle) — the pipeline the weekly
  engine composes: intelligence schemas and synthetic fixtures; deterministic
  clustering and deduplication; explainable ranking on fixed 30/20/15/15/10/10
  weights with integer arithmetic and an inspectable breakdown; evidence
  levels, tiering (Must-Understand / Should-Know / Radar) over a Top 10 with
  no backfill; the Markdown + JSON brief (lean Tier 1 plus full appendix);
  and the persistent topic library with lifecycle, append-only score history,
  audit trail, and reconsideration rules.
- ADR 0004 — records decisions D1–D8 for evidence, ranking, and library
  behavior, including the first-party-authoritative Tier-1 exception (fires at
  evidence ≥ 3 under a six-part predicate, never for benefit, performance,
  self-benchmark, institutional opinion, or promotional claims, and always
  rendering a visible "no independent corroboration" marker), plus an explicit
  "Deferred to v0.3" section.
- New JSON Schemas: `run_manifest`, `weekly_delta`, `movements_document`, and
  updates to `topic_library_entry` and `weekly_brief`.
- Suite grew to 592 tests, all offline; `ruff` and `mypy` clean.

Not implemented and explicitly out of scope for v0.1: real connectors of any
kind (Gmail, RSS, HTML), an active scheduler or daemon, ranking calibration
against real signals, and the editorial/drafting layer.

### Added (Gate D — connector security foundation, contracts + synthetic harness only)

A new `content_machine.connectors` package: the permission, retention,
sanitization, and failure-taxonomy foundation future real source connectors
(RSS, vendor changelogs, a Gmail digest, etc.) will be built on. **This is
contracts and a synthetic harness only: nothing fetches, there is no network
code anywhere in this package, no credential, no scheduler, and no real
source.** `git diff` confirms zero lines changed in `intelligence`,
`audience`, `privacy`, `ingestion`, `providers`, or `cli` — ranking and
rendering behavior are completely untouched by this gate.

- Bounded discovery (`DiscoveryResult`, no body field — full-body persistence
  is structurally impossible, not merely discouraged) and deep verification
  (`VerificationRequest` with a required, non-empty `retrieval_reason`;
  content lives only in an in-memory `TemporaryContentHandle` that must be
  disposed via `minimize()`) as two separately-permissioned modes.
- `SourceRegistry`: the curated, pre-retrieval, authoritative source of
  `publisher_id`/`source_category`/`publisher_classification`; an
  `unclassified` source fails exactly as closed as a known vendor source and
  can never supply evidence independence.
- `PermissionRegistry`: a fail-closed `proposed`/`approved`/`suspended`/
  `revoked` lifecycle × mode × `permitted_fields` allowlist, with a distinct
  reason code per denial invariant; a result that populates a field outside
  its source's `permitted_fields` is rejected and audited, never silently
  stripped.
- `sanitize.sanitize_text`/`sanitize_error`: neutralizes markup and control
  characters, redacts credential-/email-/filesystem-path-shaped substrings,
  and flags (never claims to reliably detect) instruction-shaped text — the
  real control against retrieved hostile content is architectural, not
  semantic detection.
- `RetentionPolicy`: every value a connector produces belongs to exactly one
  retention class; raw retrieved content is the one class that may never be
  persisted and must be disposed with a `DisposalRecord` as proof.
- `run_discovery`: batch orchestration with per-source isolation (one
  adapter's failure never aborts the batch), a deterministic
  `SourceCoverageReport`, and a closed `FailureKind` taxonomy with
  retry-eligibility as advisory metadata only — no automatic retry exists
  anywhere in this gate.
- `bridge.to_source_item`: the single choke point (trust boundary TB-4) from
  connector output into the M1–M7 pipeline. Fails closed on provenance (only
  `AssessmentProvenance.human_authored` may cross in this gate — admitting a
  derived or model-proposed assessment is a policy change reserved for a
  future Fable review and gate) and on empty `topic_tags`/
  `subject_entity_ids`, whose empty default would otherwise silently corrupt
  the relevance and independence dimensions of ranking.
- Seven deterministic, network-free synthetic adapters exercising success,
  timeout, rate-limit, malicious-content, oversized/malformed/unsupported-
  content, revoked-permission, and partial-batch scenarios.
- ADR 0005 — records the placement, TB-4, fail-closed provenance, the two-mode
  split, the registry's fail-closed classification, the permission/
  `permitted_fields` model, retention/disposal, the failure/isolation model,
  and the pinned constants (`DEFAULT_MAX_BYTES` 2,000,000,
  `DEFAULT_TIMEOUT_SECONDS` 20, `DEFAULT_MAX_REDIRECTS` 3,
  `DEFAULT_MAX_ITEMS_PER_SOURCE` 50, `DEFAULT_MAX_REQUESTS_PER_RUN` 200,
  `SUMMARY_MAX_CHARS` 280, `TITLE_MAX_CHARS` 300, plus a closed
  `ALLOWED_CONTENT_TYPES` allowlist).
- New `docs/connector-security.md`: the contributor-facing guide to the two
  modes, the permission lifecycle, what a future adapter author must and must
  not do, retention defaults, credential rules, and the checklist a real
  adapter must pass before activation.
- Threat model: nine new connector threats (T13–T21) and a Prevented/
  Detected/Mitigated/Accepted/Deferred honesty subsection stating plainly
  that there is no claim of semantic prompt-injection detection.
- Suite grew to 800 tests (208 new), all offline; `ruff` and `mypy` clean.

### Gate E0 — connector security prerequisites (PR #6)

- `security_flags` now survive from `DiscoveryResult` through the bridge,
  cluster, tiered topic, brief, and audit row — carrying flag names only,
  never the hostile text, and bypassing `ranking.py` entirely (0-line diff).
- Live pre-retrieval permission gate (`authorize_retrieval`), separate from
  `authorize` and exposing no caller-suppliable date: the enforcement clock is
  read at the enforcement point, so a stale date cannot dodge expiry. New
  `expires_at` field fails closed; `review_due`'s allowed-but-flagged
  semantics are unchanged.
- `may_supply_independence` consumed as a tri-state AND-conjunct that can only
  ever remove independence, never grant it; the bridge never emits `None`.
- Bridge permission re-verification at TB-4 is **required**, not optional,
  with a runtime guard — a control that defaults off is not a control.
- New `connectors/network.py`, stdlib only, one public entry point: HTTPS
  only; credential-bearing URLs rejected; a **per-source** hostname allowlist
  (never a global pool); loopback, private, link-local and IP-literal hosts
  blocked with no special case; bounded redirects revalidated at every hop;
  DNS-rebinding defence that resolves once and pins the connection to the
  vetted IP while verifying TLS against the original hostname; connect/read
  timeouts; a byte cap that aborts mid-stream; MIME allowlist; per-source rate
  limiting; errors sanitized so the endpoint never appears.
- Fail-closed private endpoint config loader: path resolved through `config/`
  only, endpoint and hostname as `SecretStr`, validation errors re-raised
  sanitized because pydantic embeds the offending input value.
- Static AST scan enforcing that only `config/` reads environment variables —
  a rule the architecture stated but nothing checked.
- Narrowed the `instruction_shaped_text` heuristics after a security review
  found the shipped form fired on 11 of 11 ordinary release-note sentences,
  each raising a blocking flag. Repeated benign blocks train a reflexive
  override on the one flag whose override must stay exceptional. Three
  residual false positives are pinned as expected-to-fire so any future
  narrowing is a visible decision.
- Suite grew to 965 tests, all offline; `ruff` and `mypy` clean.

Known-open and deliberately not fixed here: stripped HTML tags are replaced
with the empty string, so a hostile instruction immediately after a closing
tag produces no sentence boundary for the heuristic to anchor on; the rate
limiter runs before address validation, so beyond its window an SSRF attempt
is recorded as `rate_limited` rather than its true reason. Both need their own
review — the first moves golden hashes, and that is a deliberate decision
rather than an incidental fix.

**No adapter is wired to the fetcher.** This gate ships an enforced, tested
boundary with no caller: zero external network calls, zero real sources, zero
credentials, no scheduler, no publication.

Not implemented and explicitly out of scope for this gate: any real adapter,
any network call, any credential, any scheduler, coverage reporting wired
into the published brief, and any change to ranking calibration or
admission-policy for non-human-authored assessments.

**Known test-coverage gaps (not runtime holes).** Independent QA mutation
testing found two branches of `sanitize.py`'s patterns that the suite does
not regression-lock: removing `password`/`passwd` from
`_CREDENTIAL_ASSIGNMENT_RE`, and removing `\x7f` (DEL) from
`_CONTROL_CHAR_RE`, each survive the test suite unnoticed. Both tokens are
still present in the shipped patterns — nothing is missing at runtime — but
a future edit could silently drop either without a failing test catching it.
Recorded here for a follow-up test addition.

### Documentation

- Portfolio readiness pass: public status alignment across README,
  `docs/MVP_STATUS.md`, `ROADMAP.md`, `SECURITY.md`, and
  `docs/architecture.md`; rebuilt README top and navigation; added
  `docs/PORTFOLIO_CASE_STUDY.md`; added synthetic architecture/CLI/output
  visuals under `docs/assets/`. Current suite: 329 tests, all passing.

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
