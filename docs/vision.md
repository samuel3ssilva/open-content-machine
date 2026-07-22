# Product Vision

Status: approved direction for the Bootstrap Sprint, 2026-07-22. This is a
vision document, not a specification — it describes the shape of the product
being built toward, in the order set by [`ROADMAP.md`](../ROADMAP.md).
Concrete, sprint-level requirements are owned separately by the CEO role; see
[`docs/product-requirements.md`](product-requirements.md).

## What Open Content Machine is

A local-first, privacy-first platform that turns a creator's own signal —
their professional network, their notes and experiences, their real work —
into audience intelligence, editorial positioning, and content drafted in
their own voice. It is built for one creator at a time, run on that
creator's own machine, with personal data never leaving it.

## Inputs

- **Professional network** — a legitimately obtained export of the
  creator's connections (e.g. LinkedIn), processed locally and never
  scraped.
- **Experiences, notes, and conversations** — the creator's own writing,
  thinking, and history, as the raw material for voice and positioning.
- **Real projects and experiments** — actual work the creator has done,
  as evidence for what they can credibly write about.
- **External sources and trends** — public information about the topics
  the creator covers, used to ground and contextualize ideas.
- **Creator interviews** — structured conversations with the creator to
  fill gaps that documents and network data cannot cover.

## Outputs

- **Audience map** — who the creator's network actually is, built from
  anonymized, aggregated data, never from named individuals.
- **Editorial positioning** — an explicit, reviewable statement of what the
  creator talks about, for whom, and why.
- **Prioritized content ideas** — grounded in the creator's audience,
  positioning, and real work, not generic suggestions.
- **Drafts in the creator's voice** — first drafts that read like the
  creator wrote them, always reviewed by the creator before anything moves
  forward.
- **Fact-check and review** — a check on draft claims and a structured
  review pass before a draft is considered ready.
- **Native adaptations** — versions of an approved piece adapted to each
  platform's format (LinkedIn, X, Instagram, Substack), not just reformatted
  text.
- **Learning from published results** — feeding back what actually happened
  after publication into future positioning and content decisions.

## First user

The Founder. Open Content Machine is built first for its own creator, using
its own outputs, before it is built for anyone else.

## First editorial focus

Claude and Anthropic, GPT and OpenAI, AI agents, Claude Code, AI coding, and
professional workflows around these tools — plus building this project
itself, in public, as source material.

## What is public and what never is

Code, documentation, schemas, and examples are public, under Apache-2.0, in
the `open-content-machine` repository. Personal data, private sources, and
credentials are never public: they stay local, under the rules in
[`docs/privacy.md`](privacy.md) and [`SECURITY.md`](../SECURITY.md), and
never cross into git, logs, or a model call.
