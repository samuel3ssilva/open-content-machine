# ADR 0008 — Founder-Approved Limitations Overlay

- Status: Accepted
- Date: 2026-08-01
- Decider: Fable (Part C ruling; Part A follow-up rekey ruling; P3 rendering
  ruling this round); implementation recorded by Sonnet
- Model responsible: Sonnet

## Context

Five per-item limitations the Founder approved for the real 2026-W31 run
never reached the rendered brief, because `SourceItem` has no `limitations`
field and never will — see `docs/privacy.md`: adding one would mean either
authoring it into every fixture/connector item, or inventing it after the
fact, and both routes risk a human-authored, run-specific caveat silently
becoming a pipeline input that downstream code (ranking, tiering, a future
model boundary) could accidentally read and act on.

Fable's Part C ruling approved a private, run-specific, human-authored
overlay file instead — a small JSON sidecar the Founder places alongside the
run's outputs BEFORE the CLI writes them. An earlier implementation of this
overlay keyed it by `topic_id`; Fable's Part A follow-up ruling rejected that
and required `item_id` keying instead (see decision #1).

This round (product review round 4) found the overlay's rendered
presentation had two remaining defects (P3): it rendered LAST in a topic's
block, after the recommendation it should have qualified, and it was
visually identical to the six machine-generated bullets around it — as
rendered, `- **Founder-noted limitation (human-authored):** ...` reads
exactly like `- **Recommended action:** ...`, so the Founder's own override
could be mistaken for model output.

## Decision

### 1. Keyed by `item_id`, never `topic_id`

A limitation is a fact about an ARTIFACT, authored per source item — the
overlay file's `limitations` object is keyed by `SourceItem.item_id` and
means it, not `topic_id`. Keying by topic would silently rescope a
per-artifact statement to whatever a merge-driven cluster happens to
contain at render time (a cluster's membership can change run to run as new
signals arrive), and `topic_id` is run-scoped — it does not exist until a
run has already executed, which would make it impossible to author the
overlay file BEFORE the run, breaking the fail-closed ordering decision #4
depends on.

### 2. The `item_topic_map` mechanism resolves item_id to its containing topic

`weekly.WeeklyRunResult.item_topic_map: dict[str, str]` maps every
`SourceItem.item_id` in the run's FULL corpus to the `topic_id` of the
`TopicCluster` that contains it as a `member_id`. Built inside
`run_weekly` from every cluster the run produced (not only the Top `TOP_N`),
so it covers discarded topics too — an overlay item naming a discarded
topic's member still resolves to a real `topic_id` for the corpus-membership
check, even though that topic has no rendered block (see decision #4,
condition 2).

This field lives on `WeeklyRunResult` ONLY: it is never added to
`WeeklyBrief` (that would leak raw `item_id` values into `brief.json`, a
structured, potentially more widely shared artifact) and is never written to
any output file (`weekly.write_weekly_run_outputs`'s staged output set does
not reference it). It exists solely so `cli/main.py` — the only caller with
both the overlay file's expected location and the already-computed
`WeeklyRunResult` — can validate the overlay's item_id keys and resolve each
to its containing topic_id BEFORE calling `brief.render_markdown`.

### 3. Multi-member rendering: two item_ids resolving to one topic both render

Two distinct `item_id`s resolving to the SAME `topic_id` is legitimate, not
a duplicate: both limitations render, one line each, in the overlay file's
own authored (JSON object) order. `brief._limitation_lines` never merges or
deduplicates by topic — attribution lives in the human-authored prose
itself, never a printed item_id/hash (limitation text may itself be
sensitive; the ruling requires it never appear in an error message either —
see decision #4).

### 4. Six fail-closed conditions, one exception type, no partial application

`content_machine.intelligence.limitations_overlay.load_limitations_overlay`
returns `None` only when the overlay file does not exist at all (documented
as NOT a failure — the render proceeds with no limitations composed).
Every other failure mode raises `LimitationsOverlayError` and the caller
(`cli/main.py`) MUST abort the entire render — write nothing:

1. an overlay item_id not in `item_topic_map` (outside this run's corpus);
2. an overlay item_id in the corpus whose containing topic is not in
   `rendered_topic_ids` (e.g. only in `brief.discarded` — no block exists
   to attach the limitation to);
3. a duplicate item_id (or any other) key anywhere in the overlay JSON
   (`json.loads`'s default "keep the last occurrence" behavior is
   overridden via a custom `object_pairs_hook` so this is caught rather
   than silently resolved);
4. an empty or whitespace-only limitation string (absence of a limitation
   means the key is omitted entirely, never an empty value);
5. the file is present but unparseable or schema-invalid;
6. the overlay's own `run_id` does not match the run actually being
   rendered (stale overlay authored against a prior/different run).

No condition is a silent per-item drop — a single bad entry fails the WHOLE
file closed, mirroring the same "reject, never silently strip" posture ADR
0006 already established for `PrivateSourceConfig`'s expired-source case.
Composition is ALL-OR-NOTHING: a validated overlay is composed ONLY into the
rendered `brief.md` string, never into the structured `WeeklyBrief` object,
so it can never reach `brief.json`, `topics.jsonl`, the pipeline
(`cluster`/`ranking`/`tiers`), `privacy.strip_for_model`, or `providers/`.

### 5. Rendering position and visual distinction (P3, this round)

`brief._limitation_lines` renders each limitation as a Markdown blockquote
(`> **Founder-noted limitation (human-authored):** ...`), not a plain
`- **Label:** ...` bullet — a blockquote is the one visual cue every
Markdown renderer (and a plain-text read of the file) shows distinctly from
a `-` list item, so the Founder's own override can never be mistaken for one
of the six machine-generated bullets around it.

The limitation now renders directly ABOVE `Recommended action` in both Tier
1 and Tier 2 blocks (previously last, after `Recommended action`/`Score`) —
the reader sees the caveat that qualifies a recommendation BEFORE the
recommendation itself, never after. Tier 3 items have no `Recommended
action` line (a Radar item is one paragraph, not a bulleted block), so this
reordering does not apply there; the blockquote styling does.

### 6. Manifest records only whether an overlay was applied, never its text

`weekly.RunManifest.limitations_overlay: LimitationsOverlayManifest`
records `present` (bool), `item_count` (int), `overlay_sha256` (str), and
`provenance` (str) — never the limitation text itself. `run_weekly` itself
never reads the overlay file and always sets this field to its "absent"
default; `cli/main.py` sets it AFTER `run_weekly` returns, from the overlay
file's own validated content, so it can never feed `run_id`/
`input_fingerprint` (both already computed before this field could possibly
be known) or change idempotency semantics (see ADR 0009 for the related
"idempotency skip gap" this round also closed).

## Consequences

- Authoring an overlay requires knowing a run's `item_id`s in advance
  (they are stable, content-addressed identifiers already present in the
  signals file the Founder controls) but NOT its `topic_id`s (which only
  exist after a run executes) — this is the direct payoff of decision #1.
- A limitation attached to an item whose topic later gets discarded (falls
  below the Top N in a re-run with different signals) will fail closed
  with condition 2 above, rather than silently vanishing — the Founder is
  told explicitly why the overlay could not be applied.
- The overlay mechanism adds no new persistent state: nothing about it is
  written to `topics.jsonl`/`score-history.jsonl`/`audit.jsonl` — only
  `run-manifest.json`'s three-field summary and `brief.md`'s composed text
  carry any trace that an overlay was ever applied.

## Alternatives considered

- **Key the overlay by `topic_id`.** Rejected by Fable's Part A ruling — see
  Context; the fundamental problem is that a limitation is a fact about an
  artifact, not about whatever cluster currently contains it, and `topic_id`
  does not exist at authoring time.
- **Merge/deduplicate two item_ids resolving to the same topic into one
  rendered line.** Rejected: two distinct human-authored statements about
  two distinct artifacts are not the same fact merely because clustering
  happened to group them — silently merging would lose information the
  Founder explicitly wrote down.
- **Allow partial application (skip only the bad overlay entries, render the
  rest).** Rejected: matches the existing "reject, never silently strip"
  posture already established elsewhere in this codebase (ADR 0006); a
  partially-applied, silently-incomplete overlay is worse than an aborted
  render with a clear error, especially for Founder-facing, low-frequency
  human review content.
