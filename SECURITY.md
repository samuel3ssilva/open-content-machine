# Security Policy

Open Content Machine is local-first and privacy-first. The code is public; the
data never is.

## Reporting a vulnerability

Please open a GitHub Security Advisory (preferred) or a plain issue **without
exploit details**, and we will follow up privately. Do not include personal
data in reports.

## Core guarantees

1. **No real personal data in the repository.** Real exports live only in
   `data/private/` (git-ignored). All examples and fixtures are synthetic.
2. **No credentials in the repository.** Secrets come from `.env` (git-ignored);
   `.env.example` documents the shape with empty values.
3. **No network calls by default.** The demo and tests run fully offline with
   `MockProvider`. Only `content_machine/providers/` may perform network I/O
   — and in the current release the Anthropic/OpenAI provider stubs contain
   no network code path at all; they raise `NotImplementedError` even when a
   key is configured. The only exercised provider is the offline
   `MockProvider`.
4. **PII never crosses the model boundary.** Names, emails, and profile URLs are
   removed before any provider call (see `docs/privacy.md`, ADR 0003).
5. **No secrets or personal values in logs or error messages.** Errors reference
   row numbers and column names, not field contents.
6. **No scraping.** Only exports the user legitimately obtained are accepted.
7. **No automatic publication** of content or data anywhere.

## Secret & PII hygiene for contributors

- Never commit files matching `.env*` (except `.env.example`), `*connections*.csv`
  (except the synthetic example), or anything under `data/private/`.
- Before pushing: `git status --ignored` and check nothing private is staged.
- If a secret is ever committed: rotate it immediately, then rewrite history
  before the next push (coordinate with maintainers — no force push to `main`
  without agreement).

## Release security checklist (executable)

Run before every push to a public remote / release tag:

```bash
# 1. Private data is actually ignored
git check-ignore -v data/private/anything.csv .env

# 2. Nothing sensitive is tracked
git ls-files | grep -Ei '(^|/)\.env$|connections.*\.csv|secret|credential|\.pem$|\.key$' \
  | grep -v -E '^examples/synthetic-connections([._-]|-variants/)|\.env\.example' && echo "FAIL" || echo "OK"

# 3. No obvious secrets or personal emails in tracked content
git grep -nIE '(api[_-]?key|token|password)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{16,}' -- . && echo "FAIL" || echo "OK"
# (-P is required: the negative lookahead is PCRE, not POSIX ERE)
git grep -nIP '[A-Za-z0-9._%+-]+@(?!example\.com|example\.org|users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- . && echo "CHECK MATCHES" || echo "OK"

# 4. Quality gates
pytest -q && ruff check . && mypy src

# 5. Privacy tests specifically
pytest -q tests/test_privacy*.py
```

A release is blocked if any step fails, if synthetic data could be confused
with real data, or if any feature documented in README does not actually work.
