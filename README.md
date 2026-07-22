# Open Content Machine

Open-source, local-first, privacy-first platform that turns a creator's
professional network, notes, and experiences into audience intelligence,
positioning, and content in the creator's own voice.

Everything runs on the creator's own machine. Personal data is processed as
little as possible, as locally as possible, and stripped before any model
boundary. Deterministic local code is always preferred over a model call, and
the product core never imports a vendor SDK directly — it talks to a
`ModelProvider` interface, with a fully offline `MockProvider` as the default
for development and demos.

The first module being built is **Audience Intelligence**: validate,
anonymize, and report over a LinkedIn-style connections export, entirely
offline, with no API key and no personal data ever leaving the machine. Later
modules (positioning, voice, drafting, review, repurposing — see
[`ROADMAP.md`](ROADMAP.md)) build on the same principles.

## Privacy principles

- No personal data leaves the machine. No cloud infrastructure, no telemetry.
- Direct identifiers (names, emails, profile URLs) are removed, not masked,
  at anonymization — see [`docs/privacy.md`](docs/privacy.md).
- The only network-capable code lives in `providers/`, and this sprint ships
  only the offline `MockProvider`.
- Inference is always labeled: a connection's existence is never presented as
  evidence of interest in the creator's content.

Full details: [`SECURITY.md`](SECURITY.md) (guarantees, secret hygiene,
release checklist) and [`docs/privacy.md`](docs/privacy.md) (data
classification and handling rules).

## Status

- **v0.0.1** is tagged and released: the bootstrap synthetic pipeline
  (`validate` → `anonymize` → `report`, fully offline, against the shipped
  synthetic dataset).
- Sprint 1 additions — `audience inspect --dry-run`, deterministic role
  classification, the expanded private report, and `audience export-public`
  — are merged on `main` but **not yet in a tagged release**.
- **`v0.1.0` will be tagged only after the pipeline validates a private, real
  connections export locally** (dry-run first, then explicit Founder
  authorization for a real run — see
  [`docs/real-data-runbook.md`](docs/real-data-runbook.md)). No real personal
  data has been processed by this project yet.

Live one-page dashboard: [MVP Status](docs/MVP_STATUS.md). See
[`ROADMAP.md`](ROADMAP.md) for the full build order and
[`CHANGELOG.md`](CHANGELOG.md) for what has landed so far.

## Install

Requires Python 3.12 or later.

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

## Quickstart

```bash
content-machine --help
content-machine version
content-machine demo

content-machine audience validate <file.csv>
content-machine audience anonymize <file.csv> -o out.json
content-machine audience report <file.csv> [-o report.md] [--json report.json]

# Sprint 1: read-only structural inspection of an external file (no cell
# value, ever — see docs/privacy.md). Always required to pass --dry-run.
content-machine audience inspect <file.csv> --dry-run

# Sprint 1: sanitize a private report.json into a shareable artifact
# (suppresses any group under 10; adds privacy_label="sanitized-aggregate").
content-machine audience export-public <report.json> -o public.json [--md public.md]

# Sprint 1.2: metadata-safe inventory of a private source folder (Phase 1 —
# triage only, no content is read). --output-dir must be OUTSIDE the repo.
content-machine source inspect ~/private/biography-material --dry-run \
  --output-dir ~/private/biography-material/_inventory
```

All commands run fully offline and require no API key. `content-machine demo`
runs the full validate → anonymize → report flow against a synthetic example
CSV so you can see the pipeline without touching any real data. To use your
own export, keep it outside the repository (or in the git-ignored
`data/private/` — see [`data/README.md`](data/README.md)) and point the
commands at its path; nothing is ever copied into the project. Details in
[`docs/private-workspace.md`](docs/private-workspace.md).

### Example: `content-machine demo`

Real output (truncated) from running the offline demo against the shipped
synthetic dataset:

```
# Audience Report

## Totals

- Total rows: 30
- Unique connections: 28
- Duplicates: 2
- Valid rows: 30
- Invalid rows (empty): 1

## Data completeness

| Column | Complete |
| --- | --- |
| company | 96.7% |
| connected_on | 100.0% |
...
```

## Architecture at a glance

```
data/private/ (git-ignored, never committed)
        │
        ▼  TB-1: read-only, never copied
   Connections.csv
        │
        ▼
   [ validate ]  → row/column issues, no values
        │
        ▼
   [ normalize ] → whitespace, company suffixes, seniority, year
        │
        ▼  TB-2: names / emails / URLs REMOVED here
   [ anonymize ] → HMAC-SHA256 pseudonym id
        │
        ▼
   [ classify ]  → deterministic role-family + confidence
        │
        ▼
   [ aggregate ] → counts, distributions, candidate segments
        │
        ├──▶ report.md / report.json      (private; may have small groups)
        └──▶ export-public → public.json  (TB-3: groups <10 suppressed)
```

Full trust-boundary details: [`docs/architecture.md`](docs/architecture.md).

## Features today vs. roadmap

**Implemented today (`main`):**

- `content-machine --help` / `version` / `demo`
- `content-machine audience validate FILE`
- `content-machine audience anonymize FILE -o OUT.json`
- `content-machine audience report FILE [-o OUT.md] [--json OUT.json]`
- `content-machine audience inspect FILE --dry-run` — privacy-safe structural
  inspection of an external file; never prints a cell value, makes no network
  calls, never copies the source
- `content-machine audience export-public REPORT.json -o OUT.json [--md OUT.md]`
  — sanitizes a private report into a shareable artifact (suppresses groups
  under 10)
- `content-machine source inspect FOLDER --dry-run --output-dir DIR` —
  metadata-safe inventory of a private source folder (Phase 1: file bodies
  are never read); writes three private outputs (Markdown, JSON, review CSV)
  whose approval fields start empty — see
  [`docs/source-approval-gate.md`](docs/source-approval-gate.md)
- Deterministic, explainable role-family classification
  (`content_machine/audience/classify.py`): a seven-tier precedence engine
  with independent family (function) and seniority (level) inference, broad
  PT/EN vocabulary coverage, an explicit confidence level per title, and an
  evaluation harness (`audience/evaluate.py`) that scores it against a
  labeled synthetic fixture — see [`docs/classification.md`](docs/classification.md)
- Localized (Portuguese/Spanish) header and connection-date aliases
- Expanded private report: role/seniority/confidence distributions,
  candidate segments, mandatory limitations

**Planned:** positioning & creator profile, voice vault, oracle, interview
panel, draft-in-voice, evidence check, writer's council, revision,
repurpose, analytics — see the full build order in
[`ROADMAP.md`](ROADMAP.md).

## Engineering trade-offs

- Role and seniority classification are keyword-table heuristics, not ML —
  explainable and auditable, but will miss or misclassify titles outside the
  tables (always shipped with an explicit confidence level, never presented
  as ground truth).
- Fully offline by design: no network I/O anywhere in this sprint, including
  the two model-provider stubs (`providers/anthropic_provider.py`,
  `providers/openai_provider.py`).
- Flat-file storage only (CSV in, JSON/Markdown out) — no database; fine for
  a single export, not built for large-scale longitudinal history.
- Single-user, single-machine execution model; no multi-tenant or server
  deployment story.
- The public-export suppression threshold (k=10) is a fixed constant, not a
  configurable privacy budget.

## Project structure

```
src/content_machine/
├── config/      # typed settings: paths, salt, provider choice
├── ingestion/   # reading external exports (CSV) safely
├── privacy/     # PII detection/stripping, deterministic pseudonymization
├── audience/    # normalization, statistics, report generation
├── providers/   # ModelProvider interface + offline Mock (Anthropic/OpenAI are inert stubs)
└── cli/         # Typer app: `content-machine` and its subcommands

docs/            # architecture, privacy, threat model, ADRs, vision
schemas/         # public JSON Schemas for data contracts
examples/        # synthetic example data and expected outputs
data/private/    # your real, git-ignored local data (never committed)
```

See [`docs/architecture.md`](docs/architecture.md) for the full module map,
trust boundaries, and dependency rules.

## Model workforce

This project is built by a team of Claude models with a fixed division of
labor: **Fable** handles architecture and security, **Opus** handles
engineering design, and **Sonnet** handles implementation. See
[`docs/model-routing.md`](docs/model-routing.md) for the full routing rules
and accountability model.

## Contributing and license

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, quality gates, and
privacy rules for contributors, and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
for community expectations. Licensed under [Apache-2.0](LICENSE).

## Em português

O Open Content Machine é uma plataforma open-source, local-first e
privacy-first que transforma a rede profissional, as notas e as experiências
de um criador em inteligência de audiência, posicionamento e conteúdo na voz
do próprio criador. Tudo roda na máquina do criador: nenhum dado pessoal sai
do computador, código determinístico local é sempre preferido a chamadas de
modelo, e o único módulo capaz de acessar a rede é o `providers/` — que,
nesta sprint, expõe apenas um provider simulado (`MockProvider`), totalmente
offline.

O primeiro módulo em construção é o de Inteligência de Audiência: validar,
anonimizar e gerar relatórios a partir de uma exportação de conexões no
estilo LinkedIn, sem exigir chave de API e sem que nenhum dado pessoal saia
da máquina. A Sprint 1 adicionou uma inspeção estrutural somente-leitura
(`audience inspect --dry-run`) e uma classificação determinística de papéis
profissionais (`audience/classify.py`), ambas já em `main`, mas ainda sem uma
release marcada. Os módulos seguintes (posicionamento, voz, rascunhos,
revisão, repropósito de conteúdo) seguem o mesmo roteiro em
[`ROADMAP.md`](ROADMAP.md). Consulte [`docs/privacy.md`](docs/privacy.md) e
[`SECURITY.md`](SECURITY.md) para os detalhes de privacidade e segurança.
