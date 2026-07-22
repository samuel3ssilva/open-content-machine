# Source Approval Gate — Private Biography Material

Owner: fable-principal-architect. Binding for every agent and every phase.
Applies to any private source folder inventoried by `content-machine source
inspect` (first case: the Founder's biography project).

## The gate

**No document body may be read, summarized, analyzed, embedded, or sent to
any model until ALL of the following hold for that specific file:**

1. The file appears in a completed Phase-1 inventory (metadata only).
2. The Founder has set `approved_for_analysis` to an explicit affirmative in
   the private review CSV — per file, never per folder, never by default.
   Blank, "false", or missing means NOT approved.
3. The file's `intended_use` is one of the allowed purposes
   (creator_profile, authority_map, experience_vault, timeline, story_index,
   voice_vault, positioning). `do_not_use` and empty block analysis.
4. The file is category A or B **and** the approval postdates the inventory
   run that categorized it (re-inventory invalidates prior approvals for
   files whose hash changed).
5. Category C (third-party/confidential) files additionally require a
   written Founder note acknowledging third-party content; category D
   (restricted) files are never analyzable — a D approval is treated as an
   error and reported, not honored.

## Structural rules

- Provisional categories are triage hints, not permissions. Nothing in the
  code may treat A/B as pre-approval; the inventory data model deliberately
  has no approval field.
- Approval lives only in the private review CSV, outside the repository.
  The repository never contains the review file, its backups, or its values.
- Analysis outputs derived from approved files (creator profile, authority
  map, story index, voice vault) are PRIVATE-zone artifacts. They reach the
  public zone only through an explicit, human-invoked sanitization/export
  step with its own Founder approval — publication approval is never implied
  by analysis approval.
- Model boundary: even for approved files, content sent to any external
  model in a future phase must pass a strip/minimization step defined by ADR
  before the provider abstraction is allowed to see it. No such step exists
  yet; therefore no external model may receive biography content at all.
- Every future analysis artifact must carry provenance
  (`SourceReference.file_id` list) so any file's contribution can be located
  and removed on request ("right to retract").

## Enforcement

- Phase-2 tooling must refuse to process any file whose gate conditions
  fail, and must report refusals as counts (never file contents).
- Tests must cover: unapproved file rejected; category-D approval rejected;
  folder-level or wildcard approval rejected; changed-hash invalidation.
- Any violation is a privacy incident: stop, contain, report per
  docs/security/ incident procedure.
