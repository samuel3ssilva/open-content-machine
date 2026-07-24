# Open Content Machine — Portfolio Case Study

## Problem

Professionals and creators build up real context over time — a network, notes,
lived experience — that could become genuinely useful content and editorial
positioning. Turning that context into content usually forces a choice between
two bad options: hand personal data to cloud tools, or do everything by hand.
There is also a second, more personal motivation behind this project: the
builder wanted a real laboratory for AI-assisted engineering — a project
small enough to fully understand end to end, but real enough to force honest
answers about privacy, scope, and quality, not a toy.

## Constraints

- **Privacy first.** Personal data is processed as little as possible, as
  locally as possible, and stripped before any model boundary.
- **Local processing.** No cloud infrastructure, no telemetry, no remote
  database.
- **Explicit source authorization.** Only exports and folders the creator
  legitimately owns and explicitly authorizes are ever touched — no scraping,
  no implicit access.
- **Traceability.** Every non-trivial decision is recorded (ADRs, changelog,
  a documented model-routing policy) so the reasoning behind the system is
  auditable, not just its output.
- **Human publication approval.** Nothing is ever posted or published
  automatically — approval is always an explicit human action.
- **Strict public/private separation.** Real data and editorial outputs never
  enter the repository; the public repo is code, docs, schemas, and synthetic
  examples only.

## What I built

I (the Founder) built this solo, using AI-assisted engineering: Claude models
supported implementation, review, analysis, tests, and documentation under a
documented routing policy ([`docs/model-routing.md`](model-routing.md),
[`.claude/agents/`](../.claude/agents/)). I retained every product, privacy,
and publication decision myself — there is no large human team behind this,
and no claim of one. The current shipped scope is the Audience Intelligence
module: a CLI, a privacy layer, a provider abstraction, and CI, all working
together end to end on a LinkedIn-style connections export.

## Architecture

An authorized local CSV is validated (structural checks; errors cite rows and
columns, never values), then anonymized (names, emails, and profile URLs are
dropped — not masked — and each connection gets a stable pseudonymous id),
then classified by a deterministic role/seniority engine, then aggregated into
a report (Markdown and JSON). A separate export-public step sanitizes that
private report into a shareable artifact (small groups suppressed), and
publication is always a human review step from there. `providers/` is the
only network-capable module in the codebase; `MockProvider` is the default
and the only one exercised this release; `privacy.strip_for_model()` enforces
a field allowlist at the model boundary so no direct identifier can ever
reach a provider call.

![Pipeline architecture: authorized local input → validation → privacy controls → structured evidence → human review → approved output, with an offline model boundary](assets/architecture-pipeline.svg)

## Key engineering decisions

**1. Local-first with an offline default provider.** The product core never
imports a vendor SDK directly — it talks only to a `ModelProvider` interface,
and the default, and only exercised, implementation is a fully offline mock
(ADR 0001, ADR 0002). This keeps the entire demo and test suite runnable with
no API key and no network access, and makes "no network calls in core" a
structural property of the codebase, not just a policy.

**2. Deterministic anonymization — removal, not masking.** Direct identifiers
(names, emails, profile URLs) are dropped entirely rather than obfuscated,
and each connection gets a stable pseudonymous id derived with HMAC-SHA256
under a private salt (ADR 0003). Removal is a stronger and simpler guarantee
than masking, and it is enforced by an allowlist-only output model rather than
relying on every call site to remember to redact.

**3. A single network-capable module plus a field allowlist at the model
boundary.** Only `content_machine/providers/` may perform network I/O, and the
only path into a provider call is `strip_for_model()`, which passes an
explicit allowlist of fields — never names, emails, URLs, or free-text notes.
This concentrates the entire privacy-critical surface into one reviewable
choke point instead of spreading it across the codebase.

**4. Privacy-safe error reporting.** Validation and processing errors always
reference row numbers and column names, never field values, so debugging a
real export never requires printing personal data to a terminal, a log file,
or an issue tracker.

**5. Release gates and human approval, kept separate by kind.** Analysis
approval (a Founder reviewing structural output or a classifier's aggregate
accuracy) and publication approval (a Founder deciding a specific piece of
content is ready to post) are treated as two distinct gates, never conflated,
so approving one never silently approves the other.

## Verification and quality

329 automated tests, all offline, requiring no API key; `ruff` and `mypy`
clean. CI runs lint, type checks, the full test suite, and an automated
release security checklist that scans for private-data-shaped filenames,
secret-shaped literals, and non-example email addresses in tracked content.
A dry-run inspect mode is required before any real file is ever read for
real. The fresh-clone quickstart (clone → venv → install → demo → tests) was
verified end to end on 2026-07-23. The project uses Conventional Commits and
Semantic Versioning, and irreversible or architecturally significant decisions
get a dedicated ADR under [`docs/adr/`](adr/).

## Privacy and security

These are controls that reduce risk, not absolute guarantees. The relevant
documents are [`docs/privacy.md`](privacy.md) (data classification and
handling rules), [`docs/threat-model.md`](threat-model.md), `.gitignore`
hardening against export-shaped filenames, and a sanitized incident report
([`docs/security/linkedin-export-incident.md`](security/linkedin-export-incident.md))
describing a real near-miss and how the process caught and contained it. That
incident report exists specifically because writing it down honestly — even
when it was slightly embarrassing — is part of what makes the privacy claims
in this repository credible rather than aspirational.

## What was difficult

**1. Keeping privacy claims precise.** "Processed locally," "sent to a model,"
"committed to the repository," and "published publicly" are four genuinely
different claims, and it is easy to blur them in casual writing. Keeping the
documentation aligned with what the code actually does — as both evolved in
parallel — took repeated, deliberate review passes, not a one-time writeup.

**2. Building a useful classifier with deterministic code only, and
evaluating it honestly.** Without an LLM in the loop, role/seniority
classification has to come from keyword tables and precedence rules, which
is auditable but brittle at the edges. Building an evaluation harness and a
baseline-comparison tool (`audience compare-classifiers`) that could catch
regressions and overclaiming — rather than trusting a single accuracy number
— took real iteration.

**3. Operating a real-data boundary as a solo founder.** With no second
reviewer for privacy-critical changes, the safety net had to be structural:
mandatory dry-run modes, explicit authorization gates before any real file is
touched, and a documented incident writeup when the process caught an actual
mistake rather than pretending it never happened.

## Current limitations

- Single-module MVP: only Audience Intelligence is implemented in code.
- Vendor providers (`anthropic_provider.py`, `openai_provider.py`) are stubs —
  no network code path exists in this release, even with a key configured.
- Content and positioning phases (positioning, voice, drafting, review,
  repurpose, etc.) are roadmap items, not shipped code — see
  [`../ROADMAP.md`](../ROADMAP.md).
- Classification is heuristic and deterministic (keyword tables), not ML, and
  is English/Portuguese-focused.
- Not production-ready: no auth, no multi-tenant story, no database, no
  deployment story.
- Solo-founder project, AI-assisted engineering — scope and pace reflect
  that.

## What I would build next

Aligning with [`../ROADMAP.md`](../ROADMAP.md)'s phases as actual software —
Positioning & Creator Profile and Voice Vault, in particular — rather than as
a human-led workflow outside the codebase. Beyond the roadmap phases, two
things stand out: exercising a real model provider behind the existing
boundary (with the same allowlist and offline-by-default posture, under
explicit security review before it ships), and a richer evaluation harness
for the classifier as real-world title variety grows.

## Skills demonstrated

**Product:** problem selection, MVP scoping, roadmap discipline, honest status
reporting.

**Engineering:** Python, CLI design (Typer), deterministic pipelines,
schemas and data contracts, automated testing, CI.

**AI-assisted development:** a documented model-routing policy, supervised
agent workflows, human review gates on generated work.

**Privacy & security:** threat modeling, data classification, boundary
design, incident response.

**Documentation & delivery:** ADRs, runbooks, release gates, Conventional
Commits.

## Interview discussion prompts

- Why local-first instead of a hosted app?
- Which parts are deterministic and why not use a model everywhere?
- Where would a real model provider enter, and what stops private data from
  crossing that boundary?
- How is private information prevented from ever reaching the public repo,
  and what happened the one time the process caught a near-miss?
- What would need to change before production use?
