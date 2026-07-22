# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this is

Open Content Machine: an open-source, local-first, privacy-first platform
that turns a creator's professional network, notes, and experiences into
audience intelligence, positioning, and content in the creator's own voice.
The current build focus is the Audience Intelligence module: validate,
anonymize, and report over a LinkedIn-style connections CSV, fully offline.

## Key commands

```bash
# install (editable, with dev extras)
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# tests
pytest -q

# lint
ruff check .

# types
mypy src
```

All three (pytest, ruff, mypy) must pass before work is considered done. The
demo and all tests must run fully offline, with no API key.

## Architecture

Start with [`docs/architecture.md`](docs/architecture.md) for the module map,
dependency rules, trust boundaries, and the Audience Intelligence data flow.
Any change to module boundaries or trust boundaries needs a new ADR under
`docs/adr/` — that is architecture work, not implementation work (see Model
routing below).

## Hard privacy rules

These are non-negotiable for every agent and every change:

1. **Never read or open `data/private/`** in any tool, editor, or model
   context. It holds real personal data and is git-ignored on purpose.
2. **Synthetic data only** in code, fixtures, tests, and examples. Use
   invented people and `example.com`-style domains.
3. **No PII in code, logs, fixtures, or error messages.** Errors reference
   row numbers and column names, never field values.
4. **No network calls in core.** `content_machine/providers/` is the only
   module allowed to perform network I/O, and only via the `ModelProvider`
   abstraction with vendor SDKs imported lazily inside their own module. The
   default and only provider exercised this sprint is the offline
   `MockProvider`.
5. Data may only reach a model boundary through `privacy.strip_for_model()`
   with its field allowlist — never names, emails, or profile URLs. See
   [`docs/privacy.md`](docs/privacy.md) and ADR 0002/0003.

Full detail: [`docs/privacy.md`](docs/privacy.md), [`docs/threat-model.md`](docs/threat-model.md),
and [`SECURITY.md`](SECURITY.md).

## Model routing

Engineering work on this project is split across three Claude models by
task type (data sensitivity, architectural impact, reversibility). See
[`docs/model-routing.md`](docs/model-routing.md) for the full routing table
and escalation paths, and [`.claude/agents/`](.claude/agents/) for the agent
definitions:

- **Fable** — systemic architecture, threat modeling, privacy governance,
  trust boundaries, release audits.
- **Opus** — bounded-but-complex design: module/interface design, provider
  layer, pipelines, schemas, reviewing Sonnet's work.
- **Sonnet** — daily execution: well-specified tickets, CLI commands,
  validators, transforms, fixtures, tests, docs, changelog.

Every issue, PR, and execution report records the responsible model
(`Model responsible: Fable | Opus | Sonnet`).

## Commit conventions

[Conventional Commits](https://www.conventionalcommits.org/) (`feat:`,
`fix:`, `docs:`, `chore:`, `test:`, `refactor:`, ...) and
[SemVer](https://semver.org/) for releases, per ADR 0001. See
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow and
quality gates.
