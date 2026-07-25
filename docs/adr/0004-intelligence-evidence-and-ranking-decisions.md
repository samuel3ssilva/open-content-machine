# ADR 0004 — Intelligence Brief evidence and ranking decisions (D1–D6)

- Status: Accepted (D1 IMPLEMENTED — Founder final decision, Gate C: threshold evidence >= 3)
- Date: 2026-07-24
- Decider: Founder, recorded by opus-tech-lead / sonnet-implementation-engineer
- Model responsible: Sonnet

## Context

Gate A (`content_machine.intelligence.cluster` / `.ranking`) implements a
deterministic, explainable evidence rubric and a six-dimension ranking
formula over `SourceItem` records, entirely offline. Two correction rounds
(commits `bac2767`, `3bc7dbf`, `ff90539`) closed a series of measured
defects: evidence-type cells that silently fell to level 0, self-published
artifacts wrongly excluded from evidence, marketing-risk laundering, and
weak or confounded test comparisons. Each fix was authorized by a Founder
decision, referenced in code as `D1`–`D6`, but until now those decisions
existed only as source comments and commit messages, not as a durable
record — and M4 (Tier admission) will be implemented by an agent reading
`docs/`, not the git log.

This ADR is also informed by the "discovery-v1 lesson": an earlier,
undocumented ranking scheme silently let coverage volume and publisher
popularity leak into scores. Gate A's `RankingInputs` contract structurally
excludes `cluster_size`, member counts, `source_type`, `source_category`,
and publisher lists specifically to make that class of regression
impossible to reintroduce by accident — D1–D6 all operate within that
constraint.

## Decision

### D1 — Tier-1 waiver for an uncorroborated first-party-authoritative source (IMPLEMENTED, Gate C: threshold evidence >= 3)

Recorded in `ranking.py` (`_tier1_eligibility` docstring) for M4, and
implemented in `tiers.py` for Gate C:

> Tier 1 may waive the independent-source requirement only when
> `evidence_type` in `{deprecation_notice, security_advisory,
> official_spec_change, official_api_behavior_change}` AND `evidence >= 3`
> AND `practical_consequence >= 4` AND `marketing_risk` is `False` AND the
> claim is directly verifiable in the artifact AND
> `first_party_authoritative` is `True`. Benefit, performance, vendor
> self-benchmark, institutional opinion, and promotional announcements
> never qualify. The absence of independent analysis must remain explicit
> in the output.

`official_spec_change` in this decision text denotes the existing
`spec_change` evidence-type literal (see `EvidenceType` in `models.py`) —
no separate literal by that name exists or is needed.

**History — measured unreachable as originally issued (evidence >= 4).**
Reaching evidence >= 4 already requires an independent source in every
rubric branch (verified across 561 combinations), so the waiver as issued
(evidence >= 4 AND first_party_authoritative AND NOT independent) could
never fire. Implementing it verbatim yielded an exception path that never
triggered — conservative and faithful, but inert. Resolution required a
Founder ruling between two options: (a) restate the threshold as
evidence >= 3, exactly what `evid_3_first_party_authoritative` provides and
what the `first_party_authoritative_candidate` diagnostic (`ranking.py`)
already tracked, or (b) authorize a new rubric branch letting a
directly-verifiable first-party authoritative artifact reach 4 without
independence.

**Final ruling (Founder, Gate C): option (a).** The threshold is
`evidence_level >= 3`. This is now the REAL, live admission rule in
`tiers.d1_exception_fires` (`evidence_floor=3`) — not a diagnostic. When it
fires and the base Tier-1 rule (`ranking._tier1_eligibility`, which still
requires `has_independent_evidence` unconditionally and is unchanged by
this decision) does not independently admit the topic, the topic is
admitted to Tier 1 as an uncorroborated first-party-authoritative source
with no independent analysis. That absence of independent corroboration is
recorded in `TierAssignment.admission_reasons` and surfaced as an explicit
marker on the topic in the brief's Tier-1 rendering
(`brief.Tier1LeanItem.first_party_authoritative_note`) — never silently
implied. `ranking.py`'s `first_party_authoritative_candidate` diagnostic is
untouched by this decision (it is a distinct, narrower ranking.py-owned
fact — see its docstring — and ranking.py itself is out of scope for this
gate); it continues to never admit a topic to Tier 1 on its own.

**M4 entry blockers, as resolved this gate:**

1. **`marketing_risk` semantics** (Gate B hardening, prior round): the flag
   is a presence fact — set whenever a first-party-promotional or
   claim-carrying first-party-commentary member is present, cleared only
   by genuine independent evidence — computed once in
   `cluster._evidence_level_and_marketing_risk`, independent of which
   rubric branch fires. `tiers.py` reads this fact, never re-derives it.
2. **`claim_directly_verifiable_in_artifact`** — added to `SourceItem`
   (prior round) precisely so D1's "claim is directly verifiable in the
   artifact" conjunct, separate from `first_party_authoritative`, could be
   read directly off the anchor item.
3. **Institutional opinion needs no new taxonomy this gate.** D1
   explicitly excludes "institutional opinion" from ever qualifying for
   the waiver. This is handled by content, not by adding a taxonomy type:
   the D1 predicate's existing conjuncts already structurally exclude it —
   a promotional/benefit claim sets `marketing_risk` or fails
   `claim_directly_verifiable_in_artifact`, and a genuine opinion piece
   authored as a non-subject `independent_analysis` is, by definition, not
   `first_party_authoritative`. No topic can reach D1 admission by being
   dressed up as "independent analysis" while actually being institutional
   opinion, because `first_party_authoritative` (an explicit, separately
   computed fact) is one of the six required conjuncts. A dedicated
   "institutional opinion" evidence type remains unneeded for this gate.

### D2 — Isolated, uncorroborated secondary news is evidence level 1

A non-subject `announcement`/`release_note` about someone else, with no
first-party or independent signal anywhere else in the cluster, is weak,
single-source evidence — evidence level 1
(`evid_1_secondary_news_uncorroborated`), distinct from `evid_1_rumor`.
Repetition does not raise it: any number of distinct non-subject outlets
reporting the same isolated news still lands at level 1 (D6, repetition is
not evidence). If the same item is clustered with a first-party
authoritative/artifact member or genuine independent evidence, the higher
branch fires instead — D2 only decides the outcome when it is the
cluster's best signal. Implemented in
`cluster._evidence_level_and_marketing_risk`; see
`_SECONDARY_NEWS_TYPES`.

### D3 — Breaking-change consequence floor requires a direct or independent source

The consequence dimension's breaking-change floor (`ranking._score_consequence`)
fires only when ALL of: `change_class == "breaking_change"`;
`evidence_level >= 3`; and `has_direct_artifact_or_independent_source` is
`True`. That third fact (`cluster._evidence_level_and_marketing_risk`) is
`True` when the cluster has a first-party-authoritative,
non-subject-authoritative, or first-party-artifact member, OR genuine
independent evidence — never a count, never satisfied by
roundup/relay/duplicate/syndicated members.

**This round's fix (Gate B hardening):** `has_non_subject_authoritative`
was added to this fact. A 561-combination sweep proved
`evidence_level >= 3 ⟹ has_direct_artifact_or_independent_source` held in
every branch except one — `evid_3_non_subject_authoritative` — meaning the
third condition's only actual effect in the whole system was suppressing
third-party authoritative sources (e.g. a standards body's spec change, or
a security advisory not published by the vendor) from the floor, even
though those are real, uncorroborated third-party evidence exactly like
`first_party_authoritative`. See `test_consequence_floor_fires_for_a_third_party_security_advisory_breaking_change`
in `tests/test_intelligence_ranking.py`.

### D4 — Self-authored analysis is first-party commentary, capped at level 2

An `independent_analysis` published BY the cluster's own subject (the
subject analysing itself) is not independent — `_is_independent` already
excludes it on publisher grounds — and is classified as
`first_party_commentary`, capped at evidence level 2, never 3+. Its
authoring item's `contains_benefit_or_performance_claim` flag feeds
`marketing_risk` as one of two presence-fact inputs (the other being
`first_party_promotional`) — see D-fix below.

### D5 — Escalation policy: the prior non-escalation is ratified

D5 is a process decision rather than a scoring rule, which is why it leaves
no trace in the ranking code or its tests. During the previous gate, a
reviewer recommended escalating a privacy question (unrecognized topic-tag
values appearing in load-issue output) to the highest-risk review tier. The
orchestrator declined, on the grounds that the escalation triggers are
reviewer disagreement or a *change* to a security/privacy boundary, whereas
this was *enforcement* of the standing rule that errors reference field
names and never field values — a rule whose most conservative reading
already dictated the answer. The strictest option was applied instead: no
unrecognized tag value is ever echoed, in any shape.

The decision ratifies that judgment. The operative precedent for future
gates: escalate when a boundary would move or when reviewers deadlock, not
when the standing rule already determines the outcome and the conservative
reading is available.

### D6 — Repetition and coverage are not evidence; no "quiet beats popular" guarantee either way

Cluster size, member count, and repeated/syndicated coverage must never
change a topic's score — `RankingInputs` structurally excludes them, and
`cluster.py`'s evidence rubric reads only presence flags, never counts.
Concretely: (1) a well-covered vendor announcement's syndicated/relay
copies contribute zero additional points over the announcement alone; (2)
a quiet, uncorroborated but genuinely more relevant/consequential topic
CAN outrank a genuinely on-territory quiet topic with a smaller
cluster — the win must come from relevance/consequence/evidence, not
cluster size or territory; (3) a well-covered, genuinely more relevant
announcement CAN also win a tie against a quiet topic; (4) repetition of a
non-evidentiary type (roundup/relay) or a genuinely evidentiary type
(e.g. five distinct non-subject announcements) never compounds the
evidence level. See the `test_*` functions under the "D6" comment headers
in `tests/test_intelligence_ranking.py` and
`tests/test_intelligence_cluster.py`.

### D7 — Library retention scope: `canonical_title` only in v0.1; a normalized summary is deferred, not forbidden

`library.py`'s `TopicLibraryEntry` persists only `canonical_title` as prose
in v0.1 — it does not persist a `summary_normalized` field, even though
spec Sections 3/8 authorize persisting a normalized (generated) summary and
such a summary is not a raw body under Founder decision D (which forbids
raw bodies, not every derived string). This is a conservative
retention-scope choice: the gate erred toward the smaller persisted-field
surface rather than adding a new persisted prose field mid-sprint. A
normalized summary may be added in v0.2 to support editorial
reconsideration of a `deferred`/`stale` entry without re-fetching the
original signal. This does not move any trust boundary (the field, if
added, would still be a locally-generated derived string, never a raw
body/title, and still subject to the same no-raw-bodies test), so it does
not require Fable sign-off. `test_no_raw_bodies_or_prose_beyond_canonical_title_is_persisted`
pins today's (smaller) v0.1 field set; its docstring states that choice
explicitly rather than declaring a normalized summary forbidden forever.

## Consequences

- D1 is now a live admission path (evidence >= 3): it narrows how many
  topics rely on the base Tier-1 rule alone, and every topic it admits
  carries an explicit "no independent corroboration" marker through
  `admission_reasons` and the brief's Tier-1 rendering — this must never
  regress to an implicit/silent admission.
- D3's fix (adding `has_non_subject_authoritative`) is strictly more
  permissive for the breaking-change floor — it never *removes* the floor
  from a case that previously had it, only adds the one case that was
  incorrectly excluded.
- `marketing_risk` (Gate B hardening round, alongside this ADR) is now
  strictly more conservative: it can survive into evidence_level 3+ when
  no independent source clears it, which narrows Tier-1 admission relative
  to the pre-fix behavior — the intended direction per spec Section 5.2.

## Alternatives considered

- **Leave D1 unimplemented and undocumented** — rejected: M4 would
  otherwise have to re-derive the waiver's unreachability from scratch,
  or worse, implement it verbatim without realizing it never fires.
- **Guess at D5's content to keep this ADR "complete"** — rejected when
  D5's text was not available to the implementer; the gap was recorded
  honestly instead, and later filled from the decision as issued rather
  than reconstructed from the code.
