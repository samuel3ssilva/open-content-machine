# Building in Public

This document is the narrative backbone for writing about Open Content
Machine as it is built — the problem, the principles, the milestones, and
the story themes we consider fair game. It is a source for content, not
content itself: nothing here should be published verbatim without review,
and nothing here reports a result that has not actually happened.

## The narrative

Open Content Machine exists because a creator's most valuable signal —
their professional network, their notes, their real work — usually goes
unused, or gets handed to a cloud tool that was never designed to keep it
private. We are building the opposite: a local-first system that turns that
signal into audience intelligence, positioning, and content, without the
data ever leaving the creator's machine.

## The problem

Creators who want to write from real audience insight face a bad trade:
either they guess, or they upload their network and their notes to a
third-party service with no real guarantee about what happens to that data.
Open Content Machine's bet is that the trade is false — you can get
genuine audience intelligence and content help from local, deterministic
processing plus tightly scoped model calls, without giving up your data.

## Privacy principles (for public retelling)

- Local-first: the product runs on the creator's machine; no cloud
  infrastructure, no telemetry.
- Deterministic before generative: plain code handles validation,
  normalization, deduplication, and statistics; model calls are used only
  where deterministic code cannot do the job.
- Data minimization at every boundary: model calls receive an explicit
  allowlist of fields, never names, emails, or profile URLs.
- Removal, not masking: direct identifiers are stripped out of anonymized
  outputs entirely, not obfuscated.
- Everything is inspectable: the repository is public, so the privacy
  claims above can be checked against the actual code.

See [`docs/privacy.md`](privacy.md) and [`docs/threat-model.md`](threat-model.md)
for the full engineering detail behind these principles.

## Milestones

- **Bootstrap sprint** (current) — repository scaffolding, architecture and
  privacy governance, threat model, and the Audience Intelligence MVP:
  validate, anonymize, and report over a synthetic connections CSV, fully
  offline.
- Later milestones will be added here as they are reached, in the order set
  by [`ROADMAP.md`](../ROADMAP.md). No milestone is described here before it
  actually ships.

## Candidate story themes

These are angles worth writing about as the project progresses. None of them
should be written up with invented numbers or outcomes — only what actually
happened, described honestly, including what did not work.

- **Why we started with audience.** Before drafting anything in a creator's
  voice, you need to know who is actually listening — starting with
  audience intelligence rather than content generation.
- **What a professional network can and cannot reveal.** A connections
  export tells you a lot about who someone is connected to and very little
  about what those people actually care about — the difference between a
  connection and an audience.
- **How we minimize personal-data exposure.** The concrete mechanics:
  removal instead of masking, HMAC pseudonymization with a private salt,
  allowlisted fields at the model boundary, and why each choice was made.
- **How GPT (CEO/product) and Claude (CTO/engineering) split leadership.**
  The division of responsibility between product direction and engineering
  execution across two model vendors, and what that split looks like day to
  day.
- **How Fable, Opus, and Sonnet split engineering.** The internal routing
  of engineering work by task type — architecture and security, bounded
  design, daily implementation — and what that division catches or misses.
- **What broke in the first sprint.** TBD — to be filled in honestly once
  the sprint is further along; not written in advance.
- **How much was done locally before any model call.** A concrete
  accounting of how much of the Audience Intelligence pipeline is plain
  deterministic code versus anything resembling a model call, once that
  pipeline exists.

## Never publish

- Real names, emails, or profile URLs from anyone's professional network.
- Raw or aggregate connection data of any kind, real or resembling real
  data closely enough to be mistaken for it.
- Salts, keys, or any other secret.
- Private notes, interview transcripts, or other unpublished source
  material from the creator that was not written for publication.
