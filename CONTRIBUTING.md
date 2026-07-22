# Contributing

Thanks for your interest in Open Content Machine. This is a local-first,
privacy-first project — please read [`docs/privacy.md`](docs/privacy.md) and
[`SECURITY.md`](SECURITY.md) before contributing.

## Dev setup

Requires Python 3.12 or later.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quality gates

All of the following must pass before a PR can be merged. They must also all
pass in CI.

```bash
pytest -q
ruff check .
mypy src
```

If you touch anonymization, ingestion, or CLI error handling, also run the
privacy-focused tests explicitly:

```bash
pytest -q tests/test_privacy*.py
```

## Commit conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`, ...) and
[SemVer](https://semver.org/) for releases. Keep commits focused; write the
"why" in the body when it isn't obvious from the diff.

## Privacy rules for contributors

1. **Synthetic data only.** Never commit real personal data of any kind.
   Fixtures, examples, and tests must use invented people and
   `example.com`-style domains.
2. **Never commit anything from `data/private/` or `.env`.** Both are
   git-ignored on purpose. Before pushing, run
   `git status --ignored` and confirm nothing private is staged.
3. **New dependencies need review.** Open an issue or discuss in the PR
   before adding a dependency — this includes dev dependencies. Any new
   dependency, and especially anything that adds network I/O outside
   `content_machine/providers/`, is a review blocker (see
   [`docs/threat-model.md`](docs/threat-model.md), T7/T8).
4. **No PII in code, logs, or error messages.** Errors reference row numbers
   and column names, never field values.
5. If you're unsure whether something is safe to commit, ask first rather
   than pushing and fixing later.

## Pull request expectations

- Describe what changed and how you tested it.
- If your PR was authored (fully or partially) by an AI agent, state the
  responsible model in the PR description (`Model responsible: Fable | Opus |
  Sonnet | human`), consistent with [`docs/model-routing.md`](docs/model-routing.md).
- Use the PR template in `.github/PULL_REQUEST_TEMPLATE.md`.
- Keep PRs scoped to one change; large, mixed-purpose PRs are harder to review
  and slower to merge.
