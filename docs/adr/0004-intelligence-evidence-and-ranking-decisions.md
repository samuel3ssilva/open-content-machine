# ADR 0004 — Intelligence Brief evidence and ranking decisions (D1–D6)

- Status: Accepted (D1 not yet implemented)
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

### D1 — Tier-1 waiver for an uncorroborated first-party-authoritative source (NOT YET IMPLEMENTED)

Recorded in `ranking.py` (`_tier1_eligibility` docstring) for M4, not
implemented in Gate A:

> Tier 1 may waive the independent-source requirement only when
> `evidence_type` in `{deprecation_notice, security_advisory,
> official_spec_change, official_api_behavior_change}` AND `evidence >= 4`
> AND `practical_consequence >= 4` AND `marketing_risk` is `False` AND the
> claim is directly verifiable in the artifact AND
> `first_party_authoritative` is `True`. Benefit, performance, vendor
> self-benchmark, institutional opinion, and promotional announcements
> never qualify. The absence of independent analysis must remain explicit
> in the output.

`official_spec_change` in this decision text denotes the existing
`spec_change` evidence-type literal (see `EvidenceType` in `models.py`) —
no separate literal by that name exists or is needed.

**MEASURED CONSTRAINT: as issued, D1 is unreachable.** Reaching evidence
>= 4 already requires an independent source in every rubric branch
(verified across 561 combinations), so the waiver (evidence >= 4 AND
first_party_authoritative AND NOT independent) can never fire.
Implementing D1 verbatim yields an exception path that never triggers —
conservative and faithful, but inert. Resolution requires a Founder
ruling: (a) restate the threshold as evidence >= 3, which is exactly what
`evid_3_first_party_authoritative` provides and what the existing
`first_party_authoritative_candidate` diagnostic already tracks, or (b)
authorize a new rubric branch letting a directly-verifiable first-party
authoritative artifact reach 4 without independence.

Until that ruling, `ranking.py`'s `_tier1_eligibility` continues to require
`has_independent_evidence` unconditionally, and
`first_party_authoritative_candidate` remains a diagnostic-only field that
never admits a topic to Tier 1 on its own.

**M4 entry blockers**, beyond the D1 threshold ruling above:

1. **`marketing_risk` semantics** (see D-fix in this round, "Gate B
   hardening"): the flag is now a presence fact — set whenever a
   first-party-promotional or claim-carrying first-party-commentary member
   is present, cleared only by genuine independent evidence — computed
   once in `cluster._evidence_level_and_marketing_risk`, independent of
   which rubric branch fires. M4 must read this fact, not re-derive it
   from `evidence_anchor_id`.
2. **`claim_directly_verifiable_in_artifact`** — D1's text requires "the
   claim is directly verifiable in the artifact" as a separate condition
   from `first_party_authoritative`. No such field exists on `SourceItem`
   today; M4 cannot implement D1's waiver without adding it (a schema
   change, itself Opus/Fable-reviewable work).
3. **Institutional opinion has no representation in the taxonomy.** D1
   explicitly excludes "institutional opinion" from ever qualifying for
   the waiver, but a think-tank or similar institutional opinion piece is
   currently authored as a non-subject `independent_analysis` — which
   counts as full independent corroboration under
   `_INDEPENDENT_EVIDENCE_TYPES`. D1's exclusion list has no way to
   distinguish "genuine independent analysis" from "institutional opinion
   dressed as independent analysis" with the current `EvidenceType` enum.

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

## Consequences

- D1 remains formally recorded but inert until the Founder rules on (a) or
  (b) above; M4 must not silently pick one.
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
