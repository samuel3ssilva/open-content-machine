# ADR 0001 — Python CLI-first stack, local-first operation

- Status: Accepted
- Date: 2026-07-22
- Decider: fable-principal-architect (Fable)
- Model responsible: Fable

## Context

Open Content Machine processes a creator's professional network and personal
notes — sensitive personal data. The first user is the Founder; the code is
public, the data is not. We need a stack that a single maintainer can audit,
that runs entirely on a laptop, and that contributors can install in minutes.

## Decision

1. **Python ≥ 3.12**, `src/` layout, packaged with `pyproject.toml` (setuptools).
2. **CLI-first** with **Typer**; no web UI, no daemon.
3. **Pydantic v2** for every contract: settings, input/normalized/anonymized
   records, reports. JSON Schemas exported to `schemas/` for public reference.
4. **pytest** + **Ruff** (lint & format) + **mypy** as quality gates, wired into
   GitHub Actions on Python 3.12.
5. **Local-first**: all storage is local files (Markdown/JSON/CSV). Real data
   lives only in `data/private/` (git-ignored). No network calls in the default
   path; the demo runs with zero credentials.
6. **Apache-2.0**, Conventional Commits, SemVer, Keep-a-Changelog.

## Consequences

- Anyone can run the full demo offline; audits are trivial (`grep` for network
  usage finds only the providers module).
- No multi-user or hosted scenario is supported — acceptable: the product is
  personal by design. Revisit via ADR if that changes.
- Python 3.12 as the floor lets us use modern typing without excluding current
  LTS-ish environments. Local dev on 3.14 is fine; CI pins the floor.

## Alternatives considered

- **TypeScript/Node** — fine ecosystem, but the data/ML tooling and the
  Founder's workflow favor Python.
- **Web dashboard first** — rejected: larger attack surface and effort before
  any user value; the CLI proves the pipeline.
- **SQLite storage now** — premature; flat files are inspectable and sufficient
  at this scale. Revisit when analytics needs querying.
