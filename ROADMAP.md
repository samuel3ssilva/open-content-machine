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

## Next build focus — Intelligence Brief v0.1 (approved 2026-07-24)

The next module to be built is a weekly **Intelligence Brief** (see the
[product vision](docs/vision.md#near-term-product-focus-the-intelligence-brief)):
signal collection from authorized sources → clustering and deduplication →
explainable personal-relevance ranking → evidence verification → a tiered
weekly brief with a study queue, one practical experiment, and content
opportunities, always ending in a human-review state.

Scope notes:

- v0.1 is built and tested **entirely against synthetic local fixtures** —
  no real inbox or external-source connector is activated until its exact
  read-only scope is explicitly authorized and passes a security review.
- External public sources gain two bounded operating modes: a discovery
  scan limited to recent titles, dates, and summaries (no broad historical
  crawling), and deep verification restricted to topics that pass a
  preliminary relevance gate. This supersedes the earlier assumption that
  external sources enter only late, as contextualization for
  already-selected ideas.
- This phase pulls forward parts of Phase 5 (Oracle: prioritized,
  personally grounded topic selection) and Phase 8 (Evidence Check) as a
  standalone intelligence product. The remaining editorial phases below are
  unchanged and still gated on human approval; the brief's data contracts
  are designed so the editorial layer can attach later without rewriting
  the ranking and report layers.
- Ranking is never by mention frequency alone: frequency can flag a signal,
  but scores come from explainable, weighted dimensions (relevance,
  magnitude, practical consequence, evidence quality, experiment/learning
  potential, connection to the creator's live questions), each preserved
  with a per-topic breakdown.

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
