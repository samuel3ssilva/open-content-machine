# Roadmap

The approved build order for Open Content Machine. Each phase ships as its
own module, following the pattern in [`docs/architecture.md`](docs/architecture.md):
pure module + CLI subcommand + schemas in `schemas/`.

Content and positioning artifacts produced to date (see
[`docs/MVP_STATUS.md`](docs/MVP_STATUS.md)) come from a human-led,
AI-assisted editorial workflow carried out outside this codebase, not from
shipped software. **As software**, only Phase 1 is in progress; every later
phase below — including Positioning & Creator Profile and Voice Vault — is
planned and unstarted as code.

## 0. AI & Claude Intelligence Brief (weekly) — synthetic v0.1 shipped

A weekly run that turns many authorized signals into a small set of ranked,
evidence-checked, actionable topics, ending in `awaiting_founder_review`.
Shipped as `content-machine intelligence weekly-run`: a timezone-aware
seven-day window, deterministic run identity, idempotent re-runs and explicit
regeneration, atomic outputs with rollback, a persistent topic library
(lifecycle, merges and aliases, decay and staleness, weekly score/rank/tier
deltas), and eight artifacts per run.

**Runs only against synthetic fixtures, fully offline.** No connector, no
scheduler, and no real source is implemented; ranking is not yet calibrated
against real signals. Real-source work stays gated on an explicit Founder
scope decision plus a Fable security and privacy review.

This work anticipates parts of Phase 5 (prioritization) and Phase 8 (evidence
checking) below; the remaining phases and their order are unchanged.

## 1. Foundation & security — in progress

Repository scaffolding, license, architecture and privacy governance,
threat model, security policy, model-routing rules, and the Audience
Intelligence MVP itself (validate → anonymize → report over a connections
CSV, fully offline). This is the bootstrap sprint.

## 2. Audience Intelligence — planned

Deeper analysis of the anonymized connection graph: audience segmentation,
completeness and quality signals, and longitudinal growth statistics, laying
the groundwork for the audience map referenced in the product vision.

## 3. Positioning & Creator Profile — planned

Turns audience intelligence and the creator's own inputs into an explicit
editorial position: what the creator talks about, for whom, and why, as a
structured, reviewable artifact rather than a one-off document.

## 4. Voice Vault — planned

A local, private store of the creator's own writing and speech samples used
to characterize their voice, so later drafting stays recognizably theirs
instead of generic.

## 5. Oracle — planned

Local reasoning over the creator's positioning, audience, and voice to
surface prioritized content ideas grounded in what is already known about
the creator, rather than generic suggestions.

## 6. Interview Panel — planned

A structured interview flow that pulls context directly from the creator
(experiences, opinions, examples) to fill gaps that documents and network
data cannot cover.

## 7. Draft in Your Voice — planned

First-draft generation that combines positioning, voice, and interview
context, always producing something the creator reviews and edits, never
something published automatically.

## 8. Evidence Check — planned

Fact-checking and source-grounding of draft claims before they are treated
as ready for review, keeping generated content honest about what it does and
does not know.

## 9. Writer's Council — planned

Multiple review passes (e.g. structure, tone, accuracy) over a draft, each
with a narrow mandate, to catch issues a single pass would miss.

## 10. Revision Loop — planned

A structured way for the creator to give feedback on a draft and get a
revised version that actually incorporates it, with a visible history of
what changed and why.

## 11. Repurpose — planned

Native adaptations of an approved piece for different platforms (LinkedIn,
X, Instagram, Substack), respecting each platform's format instead of just
reformatting the same text.

## 12. Integrations & analytics — planned

Learning from how published content actually performs, feeding that signal
back into positioning and future content ideas, and integrations that make
publishing and tracking less manual.
