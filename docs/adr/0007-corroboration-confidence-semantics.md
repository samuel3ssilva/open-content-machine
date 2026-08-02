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

## Round 5 (2026-08-01, this branch): further rendering and wording fixes

Three reviewers (Fable, product, QA) found five more defects in the
RENDERED document on this same branch, none of which touch
`tiers._assess_confidence`'s confidence-level logic (unchanged; no
`CONFIDENCE_RUBRIC_VERSION` bump) or any ranking/tiering/admission
computation:

- **F1** (Fable, reversing an earlier "it's just a disjunction"
  classification): `tiers._classify_claim_class`'s fact-branch reason text
  called `has_direct_artifact_or_independent_source`'s fourth disjunct
  (`independent_publisher_count > 0`, i.e. `has_genuine_independent_
  evidence` in `cluster.py`) "genuine independent corroboration" — but that
  disjunct requires only ONE independent publisher, not two; "corroboration"
  implies a second, confirming source, so the word was simply wrong for the
  case where that disjunct is the ONLY one holding (`evid_4_independent_
  rigorous_alone`: the first three disjuncts are all false by construction
  for that anchor). Fixed to "evidence from at least one publisher
  structurally independent of the subject" — evidence, not corroboration.
  `BRIEF_VERSION` bumped `"gate-e0-m5-3"` → `"gate-e0-m5-4"` (document
  wording only; `CONFIDENCE_RUBRIC_VERSION` is unchanged).
- **P1** (product, MUST-FIX, "worse than before"): the shared evidence
  sentence still didn't parse — `"Strong evidence: a first-party source or
  rigorous independent evidence, and no second, independent source
  corroborates it."` conjoined a noun phrase with a finite clause after a
  colon that opens an explanatory scope, so the warning read as part of the
  DEFINITION of "strong" rather than a limit on it — inverting the exact
  signal this whole ADR exists to deliver. Fixed two ways: (1) the evidence
  phrase and the independence clause are now two separate sentences, not one
  comma-joined sentence; (2) `evidence_level == 4`'s phrase, previously the
  disjunction `"a first-party source or rigorous independent evidence"`
  (ambiguous — evidence_level 4 is reached by two structurally different
  anchors), is now resolved to the disjunct the anchor actually selected
  (`brief._evidence_level_phrase`, same principle as F1's appendix-text
  resolution, applied to the higher-consequence Tier 1/2 BODY text instead).
- **P2** (product, new contradiction on this branch, reviewed by nobody):
  dropping confidence high → medium (Part B/C above) flipped both Tier 2
  fact topics' recommended action `read` → `save`, but the Study Queue gates
  light-study only on `claim_class == 'fact'` (no confidence/action gate),
  so the SAME topic could read "Light study: ..." in the Study Queue and
  "Recommended action: save" in its own Tier 2 block with no visible
  connection between the two. See the "Study Queue / Tier 2 action"
  decision below.
- **P3** (product): the G2 methodology note (see Decision #2 above) was
  unfindable — nothing in the executive summary pointed to it, and the
  Top-N single-source sentence it qualifies is only true by construction on
  a fixture where every topic happens to hold exactly one item. The
  existing `"(see appendix for method)"` convention (already used by the
  reading/study-time header lines) is now applied to this sentence too.
- **P4** (product): the G3 single-sourced disclosure (Decision #3 above)
  was appended to the tail of the section's longest, most machine-shaped
  line. `ContentOpportunity` gains a structured `single_sourced: bool`
  field (the base `reason` text no longer carries the suffix baked in), and
  the Markdown renderer puts the disclosure on its own line — the same
  "give the caveat a visually distinct line" principle P3 (round 4)
  established for the Founder-limitation blockquote, applied here to a
  machine-generated caveat instead (a plain sub-bullet, not the blockquote
  glyph itself — that glyph stays reserved for human-authored text, per
  round 4's own reasoning for introducing it).
- **P5** (product, minor): each Tier 1 block stated "medium confidence"
  twice in adjacent lines (`why_it_matters` and `evidence_and_confidence`
  both ended with a claim_class/confidence sentence); Tier 2 stated it a
  third time via a bare `- **Confidence:** medium` bullet under a sentence
  that just said the same thing. `_human_why_it_matters` no longer restates
  claim_class/confidence (that stays the evidence line's job); the bare
  Tier 2 `Confidence` bullet is removed (superseded by `principal_evidence`,
  which already ends with the same information framed as a full sentence).

### Decision: Study Queue / Tier 2 action (P2)

Two ways to resolve the contradiction were on the table: (a) gate the
light-study queue on the topic's `recommended_action` (e.g. `read` only),
or (b) keep the existing `claim_class == 'fact'` gate and make the two
lines visibly agree instead. **Decision: (b).** `Tier2Item` gains a
`recommended_action_reason` field (mirroring `Tier1LeanItem`'s field of the
same name, which round 4 already required — "Tier 1 already prints its
reason"), rendered next to Tier 2's `Recommended action` line instead of
the previous bare action word. Rationale:

- Changing the light-study GATE means touching `library._derive_current_
  status`'s `study_queue` lifecycle state too (its own docstring says it
  "mirrors `brief._build_study_queue`'s light-study criterion" — the two
  are deliberately kept in lockstep). That is a persisted-lifecycle-state
  change, not a rendering fix, and a materially larger, riskier edit for a
  wording-contradiction ticket to make unreviewed.
  `library.TopicLibraryEntry.current_status` is read back into next week's
  run (`prior_library`) and is part of this module's own persisted
  contract — the kind of change this codebase's routing rules ask to be
  reviewed above the daily-execution level, not folded into a same-round
  prose fix.
- The two lines are not actually incompatible once each states *why*. A
  Tier 2 `fact` topic lands here precisely because it lacks genuine
  cross-source corroboration, so it is medium confidence and its action is
  `save` — but "save for later" and "queued for light study" are NOT
  actually contradictory instructions once each states *why*: `save` means
  "don't rush to act on it, the evidence stands but isn't doubly attested
  yet" (now visible via `recommended_action_reason`), and the Study Queue's
  own reason line already says the same thing in different words ("Tier 2
  fact-classified topic, rank N, medium confidence"). The apparent
  contradiction was a DISCLOSURE gap (the "why" for `save` was invisible
  next to the bare word), not a genuine logical conflict between "queue it
  for a light read" and "it's not corroborated enough to read now and move
  on" for a `fact`-classified, medium-confidence topic.
- This keeps the fix inside `brief.py`'s existing "give the reader the
  reason, not just the label" pattern (P2 round 4's own Tier 1 precedent),
  rather than introducing a new gating rule that would need its own
  justification for why `read`-only is the right cut line (e.g. should a
  `high`-confidence-but-Tier-2 topic with action `read` still be excluded
  from light study if some OTHER criterion changes it to `save` later?).

If a future round finds this still reads as contradictory in practice, the
gate-on-action alternative above remains available — it was not rejected on
technical grounds, only deferred as the larger, riskier edit for this round.

### Round 7 (2026-08-01, this branch): Study Queue / Tier 2 action, revisited

Product review reproduced exactly the failure mode the paragraph above
anticipated: round 5's `recommended_action_reason` fix stated *why* the
evidence is thin, not *when* to act on it, so the rendered Tier 2 line still
read "save — ... — save for later review" while the Study Queue's own line,
three lines below, scheduled the SAME topic for a light study now and
billed minutes for it against the run's `Estimated study time`. Since every
Tier 2 `fact`/`medium` topic hits both rules by construction (`_recommend_
action` returns `save` for that exact combination; `_build_study_queue`'s
light-study gate is `claim_class == "fact"`), this was not an edge case —
the apparent "study it now vs. save it for later" contradiction is
guaranteed to render together for every such topic.

**Decision: still (b), not gate-on-action — refine the disclosure instead
of widening the edit.** The gate-on-action alternative remains available and
was reconsidered, but rejected again for the same reason round 5 gave: it
requires changing `library._derive_current_status`'s persisted `study_queue`
lifecycle state in lockstep (its docstring commits to mirroring this
module's light-study criterion), which is a persisted-contract change this
codebase's routing rules ask to be reviewed above the daily-execution level,
not folded into a same-round wording fix. Two changes, both confined to
rendered prose, close the actual gap instead:

- `_build_study_queue`'s light-study `reason` now states WHEN as well as
  WHY: a light-study pick is framed explicitly as "a short read now to stay
  current," distinct from the deeper follow-up that the same topic's own
  `save` action defers pending further corroboration. "Light study" was
  always the lighter-weight queue (as opposed to "deep study") — this makes
  that distinction legible next to the topic's own recommended action
  instead of leaving the reader to infer it.
- `tiers._recommend_action`'s fact/medium (`save`) reason text is
  rewritten to drop the P1-style defect the same reviewer flagged in the
  same breath: it said "save" twice, embedded two raw `key=value`
  diagnostic tokens in reader-facing prose, and buried its caveat behind a
  colon that opened an explanatory scope with the limitation sitting
  inside it — structurally the same construction P1 (round 5) removed from
  the evidence line directly above it. The new text is two plain clauses,
  no `key=value` tokens, no colon-opened scope, and states the deferral
  once.

This keeps the same "give the reader the reason, not just the label"
pattern the original decision (b) established, applied a second time to
close the gap that pattern's first application left open. The gate-on-action
alternative is still not rejected on technical grounds — if a future round
finds the disclosure insufficient even once both lines state their own
why/when, that alternative (and the `library.py` lifecycle-state work it
requires) remains the next escalation.

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
- Round 5: `ContentOpportunity` gains an additive `single_sourced: bool`
  field; `Tier2Item` gains an additive `recommended_action_reason: str`
  field. Both are structured (discoverable in `brief.json`, not only
  Markdown), consistent with every other additive field this ADR's rounds
  have introduced.

### Deferred work

Fable swept `src/` for every unrecorded deferral this ADR's rulings depend
on and found two, neither previously written down anywhere as deferred —
an unrecorded deferral is a silent non-decision, so both are recorded here
explicitly rather than left implicit in code comments only:

(i) **`cluster._evidence_level_and_marketing_risk` sets `has_independent_
rigorous`/`has_independent_analysis` without consulting `may_supply_
independence`.** The anchor-selection logic that drives `evidence_anchor_id`
reads the raw evidence-type/publisher-affiliation check only — it does not
ask the Gate E0.3 independence registry whether a given member is actually
*allowed* to supply independence. This means a registry-denied member
(`may_supply_independence=False`) can still drive a cluster to an anchor
like `evid_4_first_party_plus_independent` even though it contributes zero
to `independent_publisher_count`. The Round 3 fix (F1/F2 of that round —
requiring `independent_publisher_count >= 1` alongside `_CORROBORATED_
ANCHORS` membership in `tiers.has_cross_source_corroboration`) MITIGATES
this downstream, at the confidence-gating layer, but the root cause —
anchor selection itself not consulting the registry — is untouched. A
root-cause fix belongs to its own scoped ruling (it would change
`cluster.py`, which is outside this ADR's and this branch's scope fence)
rather than being folded into a confidence-semantics ADR.

(ii) **No authored `originating_entity_id` exists on `SourceItem`.**
`independent_publisher_count` (and `CORROBORATION_METHODOLOGY_NOTE`'s G2
disclosure, above) counts at `publisher_id` granularity — but for
aggregator venues (e.g. arXiv, a preprint server hosting work from many
unaffiliated research groups under one `publisher_id`), the entity that
actually matters for independence is the ORIGINATING research group or
organization, not the hosting venue. No field on `SourceItem` currently
distinguishes them; `originating_entity_id` (defaulting to `publisher_id`
for every non-aggregator source, where the two already coincide) is the
natural future corroboration-dedup key, but adding it is a schema change to
`SourceItem` — outside this ADR's scope fence — and needs its own ADR to
work through the aggregator-detection question it would raise (how is
"this publisher is an aggregator" determined, and by whom). The G2
methodology note (Decision #2 above, findable via the P3 pointer added this
round) is the interim disclosure: it tells the reader the counting rule is
coarser than author/research-group granularity, so the measurement is
honest about its own limits even without the future field.

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
