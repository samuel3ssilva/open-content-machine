# Product Requirements

Product requirements for Open Content Machine are owned by the CEO role
(GPT), not by the engineering models. This document is a stub: detailed,
sprint-level requirements will be filled in over time via repo handoffs from
the CEO to the engineering team, and this file (or successors under
`docs/`) will be updated to reflect them as they are approved.

Until that handoff process produces more detail, [`docs/vision.md`](vision.md)
and [`ROADMAP.md`](../ROADMAP.md) are the authoritative statements of product
direction, and [`docs/architecture.md`](architecture.md) is the authoritative
statement of what is actually being built this sprint.

## Bootstrap sprint acceptance criteria

The current sprint (Phase 1 of the roadmap: Foundation & security, plus the
Audience Intelligence MVP) is considered done when all of the following
hold:

- The package installs cleanly: `pip install -e ".[dev]"` succeeds on
  Python 3.12.
- `content-machine demo` runs the full validate → anonymize → report flow
  offline, with no API key, against the synthetic example CSV.
- `content-machine audience validate`, `audience anonymize`, and
  `audience report` all work correctly against a synthetic connections CSV.
- CI is green: pytest, Ruff, and mypy all pass.
- No personal data (real names, emails, profile URLs, or anything
  resembling real data) appears anywhere in the repository.
- No secrets (API keys, salts, credentials) appear anywhere in the
  repository; `.env.example` documents required configuration with empty
  values only.
