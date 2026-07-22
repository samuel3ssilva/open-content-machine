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
- `.github` issue and pull request templates.
- `prompts/README.md` for future versioned prompt templates.
- `CLAUDE.md` repository guidance for Claude Code sessions.

### Planned for this sprint (tracked, not yet in `[Unreleased]` as shipped)

- Installable `content-machine` package (Typer CLI) with `--help`, `version`,
  and `demo` commands.
- Audience Intelligence MVP: `audience validate`, `audience anonymize`, and
  `audience report` commands over a synthetic connections CSV, fully
  offline via `MockProvider`.
- CI (pytest, Ruff, mypy) on Python 3.12.
