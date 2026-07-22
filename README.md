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

Bootstrap sprint — Audience Intelligence MVP. See [`ROADMAP.md`](ROADMAP.md)
for the full build order and [`CHANGELOG.md`](CHANGELOG.md) for what has
landed so far.

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
content-machine audience anonymize <file.csv> [-o out.json]
content-machine audience report <file.csv> [-o report.md] [--json report.json]
```

All commands run fully offline and require no API key. `content-machine demo`
runs the full validate → anonymize → report flow against a synthetic example
CSV so you can see the pipeline without touching any real data. To use your
own export, place it in `data/private/` (git-ignored, never committed — see
[`data/README.md`](data/README.md)) and point the commands at it.

## Project structure

```
src/content_machine/
├── config/      # typed settings: paths, salt, provider choice
├── ingestion/   # reading external exports (CSV) safely
├── privacy/     # PII detection/stripping, deterministic pseudonymization
├── audience/    # normalization, statistics, report generation
├── providers/   # ModelProvider interface + Mock/Anthropic/OpenAI implementations
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
da máquina. Os módulos seguintes (posicionamento, voz, rascunhos, revisão,
repropósito de conteúdo) seguem o mesmo roteiro em [`ROADMAP.md`](ROADMAP.md).
Consulte [`docs/privacy.md`](docs/privacy.md) e [`SECURITY.md`](SECURITY.md)
para os detalhes de privacidade e segurança.
