# ADR 0007 — Corroboration/Confidence Semantics Fix (Part B/C, Round 4 Gate Items)

- Status: Accepted
- Date: 2026-08-01
- Decider: Fable (semantics ruling and merge gate); Founder (G3 keep-medium
  scope authorization); implementation recorded by Sonnet
- Model responsible: Sonnet

## Context

The real 2026-W31 run rendered the phrase "genuine independent corroboration
is present" (and equivalents) for topics that structurally could never have
two distinct sources — most visibly, single-reference topics whose cluster
has exactly one member. `tiers._assess_confidence` granted `high` confidence
to any `fact` claim with `evidence_level >= 4`, without checking whether that
evidence level was reached by ONE source or TWO: the anchor
`evid_4_independent_rigorous_alone` reaches level 4 with a single source, and
was rendered exactly like a genuinely corroborated topic.

Fable's ruling (Part B) was corrective in three further rounds, each closing
a gap measurement or QA proved by construction in the previous one:

- **Part B** (initial): `high` confidence now additionally requires
  `has_cross_source_corroboration` — either `independent_publisher_count >=
  2`, or `evidence_anchor_id` in a small `_CORROBORATED_ANCHORS` set (topics
  whose evidence type itself encodes a first-party leg plus a second,
  independent leg) together with `independent_publisher_count >= 1`. A
  single-source topic is capped at `medium`, with an explicit reason.
- **Part C** (follow-up, "the mirror-image prose defect"): the OLD
  catch-all confidence-reason text unconditionally said "no second, distinct
  source supports the claim," which is false whenever
  `independent_publisher_count >= 2` but `evidence_level < 4` (e.g. two
  `independent_analysis` items from different publishers, no first-party
  member — `evid_3_independent_only`). That topic is genuinely two-sourced,
  just capped at `medium` by the evidence-level gate, not by single-sourcing.
  The catch-all text was made unconditionally true instead.
- **Round 3** (final recheck defect, F1+F2): a `_CORROBORATED_ANCHORS`
  membership alone was not sufficient — both anchors in that set are reached
  by a first-party leg plus a SECOND, INDEPENDENT leg, and it is the
  independent leg that must actually be countable.
  `cluster._evidence_level_and_marketing_risk` sets the anchor off the raw
  evidence_type/publisher check alone, without consulting
  `_is_independent`/`may_supply_independence` — so a Gate E0.3
  registry-denied member could still drive the cluster to
  `evid_4_first_party_plus_independent` while contributing ZERO to
  `independent_publisher_count`. Requiring
  `independent_publisher_count >= 1` alongside the anchor closed that gap.

This round (the merge-gate round, post product review) found and fixed three
further defects in the RENDERED document specifically (not the underlying
`_assess_confidence` logic, which Round 3 already finalized):

- **P1** (regression introduced by this branch): Tier 2's "Principal
  evidence" line comma-spliced two NOUN phrases together — the
  evidence-level phrase ("strong evidence: a first-party source or rigorous
  independent evidence") and the old independence phrase ("independent
  evidence from a single source") — producing "...rigorous independent
  evidence, independent evidence from a single source," an ungrammatical
  sentence that says "independent evidence" twice. The independence clause
  is now a full VERB clause ("no second, independent source corroborates
  it"), joined with "and" instead of a bare comma.
- **P2**: Tier 1's "Evidence & confidence" line piped
  `tiers._assess_confidence`'s raw audit rationale (three `key=value`
  tokens, ~250 characters, e.g. `fact claim, medium confidence --
  claim_class=fact, evidence_level=4, independent_publisher_count=1:
  single-source independent evidence -- ...`) straight into reader-facing
  prose — the SAME string already duplicated verbatim in the appendix. Tier
  1 now routes through the same human-prose builder Tier 2 already used
  (`brief._human_evidence_sentence`, renamed from
  `_human_principal_evidence` since it is no longer Tier-2-specific). The
  raw audit rationale is UNCHANGED and remains the audit trail in
  `Tier1AppendixRecord.confidence_reason` / `brief.json`.
- **G1**: the executive summary's marketing-risk sentence ("N of the Top N
  topics were classified as marketing-risk claims that still require
  independent corroboration before acting") sat directly above topic blocks
  that each individually say no second source supports their claim.
  Technically scoped to marketing claims, its POSITION made it read as an
  all-clear on corroboration in general — exactly the question this whole
  fix exists to stop answering with an all-clear. The trailing clause is
  dropped, and a new sentence states the Top-N single-source count directly,
  in negated wording (see "Negated wording only" below).
- **G2**: `independent_publisher_count` is counted at publisher/venue
  granularity — every arXiv item in the shipped fixtures carries
  `publisher_id == "arxiv"`, so two independent research groups both
  publishing on arXiv count as ONE publisher. This raises the bar for
  reaching `high` confidence; it does not lower it. Leaving this
  undisclosed is still a measurement misstatement (an unlabeled number
  reads as more precise than it is), so a static methodology sentence
  (`brief.CORROBORATION_METHODOLOGY_NOTE`) now discloses the counting rule
  in the confidence/limitations appendix and in `brief.json`.

## Decision

### 1. Negated wording only, everywhere corroboration status is stated

Every rendered sentence this round touches states the ABSENCE of
corroboration ("no second, distinct independent source," "no independent
source corroborates it," "single-sourced: corroborate before publishing")
rather than an affirmative claim that corroboration exists or is absent in
a way that could later prove wrong. This mirrors Part B's original framing
("stop rendering single-source independence as corroboration") extended to
every new sentence this round adds (G1, G3): none of them may use an
affirmative `corroborat*` claim.

### 2. `has_cross_source_corroboration` is exposed publicly from `tiers.py`

`tiers._assess_confidence`'s internal cross-source-corroboration predicate
is now a public function, `tiers.has_cross_source_corroboration(
evidence_anchor_id, independent_publisher_count)`, called by both
`_assess_confidence` itself and by `brief.py`'s G1/G3 single-source
disclosures. This was a mid-round correction: the FIRST version of G1/G3
approximated "single-sourced" as `independent_publisher_count <= 1`, which
wrongly flagged a topic corroborated via the `evid_4_first_party_plus_
independent`/`evid_5_...` anchor path (which can reach `independent_
publisher_count == 1` — a first-party leg plus one independent leg) as
single-sourced, contradicting the `high` confidence rendered for it a
sentence earlier in the same executive summary. Delegating to the exact
predicate `_assess_confidence` already uses eliminates the possibility of
the two sections disagreeing about which topics are single-sourced.

### 3. G3: the content-opportunity gate stays `confidence in {high, medium}`

Fable and the Founder considered tightening the gate to `high` only, so
that a single-sourced `medium`-confidence topic could never become a
content opportunity. **Decision: keep the gate as `confidence in {high,
medium}`.** Rationale:

- Every content opportunity is a Founder-reviewed DRAFT, never an
  auto-publish path — nothing in this codebase publishes, sends, or
  schedules content from a brief (see `brief.REVIEW_STATUS`/`WeeklyBrief.
  review_status`, always `"awaiting_founder_review"`).
- A single-sourced-but-medium topic can still be a legitimate research
  lead the Founder wants surfaced for further reporting — excluding it
  entirely from the content-opportunities section would silently hide
  real signal, trading a disclosure problem for a visibility problem.
- Instead, each single-sourced opportunity now discloses it plainly in its
  own reason line (`"... -- single-sourced: corroborate before
  publishing"`, negated/advisory wording — see `brief.
  _build_content_opportunities`), so "nobody decided" is no longer true:
  the disclosure IS the decision, made visible at the point of use.

**This decision must be revisited immediately if any auto-publish path is
ever introduced** — the "Founder-reviewed drafts only" rationale is the
entire basis for keeping the gate at `medium`, and stops holding the moment
content can leave this pipeline without a human reading the disclosure
first.

**OPEN FOUNDER QUESTION** (named explicitly, not resolved by this ADR):
should the content-opportunity gate be tightened to `confidence == "high"`
only once/if any semi-automated publishing assistance is ever built on top
of this brief? This ADR keeps the gate at `{high, medium}` for the
Founder-reviewed-draft use case only and takes no position on any future,
different use case.

## Consequences

### Golden-hash re-pin rationale

`tests/test_connectors_end_to_end.py::
test_golden_existing_synthetic_fixture_output_is_unchanged_by_gate_d` pins a
sha256 hash of the rendered `brief.md`/`brief.json` for the shipped
synthetic fixture. This round re-pins BOTH hashes, for the second time
within this same round (a same-round correction, see decision #2 above):

1. First re-pin: `brief.BRIEF_VERSION` bumped ("gate-e0-m5-2" ->
   "gate-e0-m5-3" — see ADR 0009's sibling concern about this same branch
   never having bumped it originally), the new additive `tiers.
   CONFIDENCE_RUBRIC_VERSION`/`brief.confidence_rubric_version`/`brief.
   corroboration_methodology_note` fields, and the P1/P2/G1/G3 rendering
   fixes all changed `brief.md` text and `brief.json` shape on this
   fixture.
2. Second re-pin (same round): fixing the G1/G3 single-source predicate
   (decision #2) changed the Top-N single-source count on this fixture from
   10/10 to 8/10 (correctly excluding the two anchor-corroborated topics).

Both re-pins were verified the same way every prior re-pin in this test's
own history was verified: the structural counts asserted immediately above
the hash assertions (`len(clusters)`, `len(ranked)`, `len(tiered)`,
`tier1_count`, the top topic's id and score) are UNCHANGED — proving nothing
in `cluster.py`/`ranking.py`/`tiers.py`'s admission or scoring logic moved,
only rendered prose and additive schema fields. This is the established
pattern for this test (see its own accumulated re-pin history for Gate E0,
Part B, Part C, and Round 3, each documented inline) — a "golden" hash test
over rendered PROSE will legitimately need re-pinning whenever prose
changes, and that is not itself a defect; only a re-pin unaccompanied by a
structural-count proof would be.

### Other consequences

- `brief._human_principal_evidence` is renamed to `brief.
  _human_evidence_sentence` and is now called by both `_build_tier1_lean`
  and `_build_tier2` — a single evidence/corroboration sentence builder for
  both tiers, eliminating the prior Tier-1/Tier-2 inconsistency (P2) by
  construction rather than by parallel maintenance.
- `WeeklyBrief` gains two additive fields: `confidence_rubric_version` and
  `corroboration_methodology_note`. Neither is fed into `weekly.
  compute_run_id`/`compute_input_fingerprint` — see ADR 0009 for why
  semantics markers must never enter run identity.
- Every existing test asserting on the old rendered strings (Tier 1/2
  evidence lines, the executive summary's marketing sentence, the
  limitations-overlay bullet glyph) was updated in the same commit as the
  rendering change it covers — never left stale.

## Alternatives considered

- **Tighten the content-opportunity gate to `confidence == "high"` only
  (G3).** Rejected for this round — see decision #3 above; kept as a named
  open Founder question instead of a silent non-decision.
- **Approximate "single-sourced" as `independent_publisher_count <= 1` in
  `brief.py` (G1/G3), without exposing `tiers.
  has_cross_source_corroboration`.** Rejected after QA caught it
  contradicting the `high`-confidence rendering for anchor-corroborated
  topics on the real fixture — see decision #2 and the golden-hash
  re-pin's second entry.
- **Fold `CONFIDENCE_RUBRIC_VERSION`/`corroboration_methodology_note` into
  the existing `rubric_version`/`weights_version`/`taxonomy_version` trio
  on `WeeklyBrief`.** Rejected: those three are `ranking.py`'s own version
  markers for the six-dimension scoring rubric, which this round's changes
  never touch — conflating a confidence-semantics marker with the scoring
  rubric's markers would make a future rubric-only change look like it also
  touched confidence semantics, and vice versa.
