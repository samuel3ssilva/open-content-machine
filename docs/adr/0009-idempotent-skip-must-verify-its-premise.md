# ADR 0009 — An Idempotent Skip Must Verify Its Premise, Not Assume It (R1)

- Status: Accepted
- Date: 2026-08-01
- Decider: Fable (merge gate, root-cause ruling); implementation recorded by
  Sonnet
- Model responsible: Sonnet

## Context

This branch's whole purpose is to fix how the rendered brief describes
corroboration (ADR 0007) and to let a Founder attach human-authored
limitations to it (ADR 0008). None of that reaches a Founder who already has
a completed run on disk: `weekly.compute_run_id` hashes six inputs
(`week_label`, `input_fingerprint`, `code_version`, `profile_version`,
`window_start`, `window_end`) — NONE of which this branch changes.
`code_version` is the installed package's `__version__`, unmoved by this
branch. Re-running this branch on the real 2026-W31 signals with the real
2026-W31 profile therefore produces the IDENTICAL `run_id` AND the identical
`input_fingerprint` the pre-fix run already produced, so
`write_weekly_run_outputs`'s idempotency skip — matching `run_id` and
`regenerate=False` — fires, `outcome.wrote` is `False`, and the on-disk
`brief.md` still says "genuine independent corroboration" verbatim, exactly
as many times as it did before this branch existed. The fix cannot reach the
user who most needs it without an explicit `--regenerate`, and nothing told
them that.

Fable's root-cause finding, and the reason this is its own ADR rather than a
line item under ADR 0007: **this branch changed rendering semantics TWICE
(the corroboration-language fix across Round 1–3, and this round's P1/P2/G1/
G3 fixes) and never bumped `BRIEF_VERSION` until this round.** A `run_id`
identity guard would therefore not have caught its own introducing branch —
even a Founder who diligently checked "did `code_version` change?" would
have seen no signal, because nothing recorded that rendering semantics had
moved.

Fable considered, and explicitly REJECTED, folding rendering/confidence
semantics into `run_id` identity (e.g. hashing `BRIEF_VERSION`/
`CONFIDENCE_RUBRIC_VERSION` into `compute_run_id`): doing so would silently
UN-GUARD every historical output directory the moment either marker changes
— a completed, Founder-reviewed run from a prior week would suddenly compute
a different `run_id` than the one recorded in its own `run-manifest.json`,
making that historical `run_id` non-recomputable from the same inputs ever
again, and breaking the "same inputs, same run_id, always" invariant
`weekly.py`'s own module docstring documents as load-bearing. Run identity
must stay about the INPUTS (the week, the signals, the profile, the code
version, the window) — not about how those inputs happen to be currently
rendered.

## Decision

### 1. The skip verifies its premise by comparing, not by trusting `run_id`

`weekly.write_weekly_run_outputs` already builds `staged: dict[Path, str]`
— every file this call WOULD write, computed purely from `result` (already
fully computed by the caller; this is formatting, not extra pipeline work).
This round moves that construction to BEFORE the skip decision, so on the
`run_id`-match path it can compare every staged file against its on-disk
counterpart (`_staged_differs_from_disk`) BEFORE deciding to skip: an exact
byte-compare for every file except `run-manifest.json`, which is compared
via a canonicalised view instead (see the Q1 addendum below) —

- **All equal** → today's clean skip: `outcome.wrote` is `False`,
  `outcome.stale` is `False`, `skipped_reason` text UNCHANGED from before
  this round.
- **Any difference** → STILL skipped — this function never writes without
  `regenerate=True`, on this path or any other — but `outcome.stale` is
  `True` and `outcome.stale_files` names every differing filename.

This changes neither `run_id` nor `input_fingerprint` computation, and only
ever READS against an existing `output_dir` on the skip path — it never
writes there, regenerate or not, before the (unchanged) point where a
`regenerate=True` call proceeds to `_atomic_write_all`.

### 2. The warning is UNCONDITIONAL, not gated on an overlay being present

Before this round, `cli/main.py` only warned on a skip when a limitations
overlay was present and validated for THIS invocation (the "idempotency skip
gap" ADR 0008 covers) — a Founder with no overlay file got no signal
whatsoever that their on-disk run was stale. This round adds a general
warning, printed whenever `outcome.stale` is `True`, regardless of whether
an overlay is involved:

> `WARNING: the run already completed on disk at {output_dir}
> (run_id={outcome.run_id}) does NOT match what the CURRENT code would
> produce for it -- N file(s) differ: {names}. ... Pass --regenerate to
> redo this run's outputs with the current code.`

The round-2 overlay-specific warning (which reads the ON-DISK manifest's own
`limitations_overlay.present` field to detect a narrower divergence) is kept
verbatim as a MORE SPECIFIC message for that one case — it is now a special
case of this general mechanism, not the only path to a warning. In practice
the two conditions usually co-fire: an overlay applied to `result` before
`write_weekly_run_outputs` is called changes `staged["brief.md"]`'s content,
which the byte-compare already catches — the overlay-specific check's value
is its more precise wording ("does NOT carry it"), not new detection
coverage.

### 3. The manifest delta is the human-readable complement to the byte-compare

`RunManifest` gains two additive fields this round, `brief_version` and
`confidence_rubric_version` (mirrored from `WeeklyBrief`'s own fields of the
same name — see ADR 0007), specifically so that when R1's warning fires, a
human comparing the on-disk `run-manifest.json` against the currently
installed package can see WHY at a glance, without needing to run the
byte-compare themselves or diff `brief.md` by hand. The byte-compare is the
backstop that fires even if a future change forgets to bump either marker
(exactly the failure mode this branch itself exhibited); the manifest delta
is the fast, human-legible diagnostic for the common case where a marker WAS
bumped correctly.

### 4. Addendum (2026-08-01, QA blocker Q1): the comparison canonicalises wall-clock-only fields

QA reproduced, against the real CLI, that step 1 above as first implemented
was itself broken: two back-to-back `runner.invoke` calls 1.1s apart, same
code, same inputs, nothing changed, ALWAYS flagged `run-manifest.json` as
differing and fired the loud staleness warning — because `execution_
timestamp` is captured fresh on every invocation (`cli/main.py`) and was
included verbatim in the RAW byte-compare `_staged_differs_from_disk`
described in step 1. `weekly._manifest_content_differs` now compares
`run-manifest.json` specifically (every other staged file is still an exact
byte-compare, unchanged) via a CANONICALISED view: both the staged and
on-disk JSON are parsed, every field in `weekly._MANIFEST_COMPARISON_
EXCLUDED_FIELDS` (today just `execution_timestamp` — audited against every
other `RunManifest` field for anything else derived from `datetime.now()`/
`date.today()`; there is nothing else) is popped from BOTH before comparing
the remaining dicts for equality. This changes only the COMPARISON: the
manifest actually staged and written to disk is untouched and still records
the real `execution_timestamp` verbatim, so the audit trail is unaffected —
only the "is this on-disk run still what current code would produce"
question stops being answered incorrectly by a field that was never
supposed to gate it in the first place.

## Consequences

### Operator runbook requirement

Applying this fix to an EXISTING week's output directory requires an
explicit `content-machine intelligence weekly-run --regenerate ...` — the
CLI's `weekly-run` command epilog and `docs/MVP_STATUS.md` both now say so.
**The operator MUST archive a copy of the directory first.**
`weekly._atomic_write_all`'s `.bak.tmp` move-asides are ROLLBACK STAGING
FOR A SINGLE `_atomic_write_all` CALL — the module deletes them on success
(see `_atomic_write_all`'s final loop, `backup_path.unlink(missing_ok=True)`
once every file has landed) — they are NOT a preservation mechanism across
runs. Once `--regenerate` completes successfully, the pre-fix on-disk
content is gone; there is no built-in undo. This is stated plainly, not
implied, in both the CLI epilog and the runbook doc.

### Other consequences

- `WeeklyWriteOutcome` gains two additive fields, `stale: bool = False` and
  `stale_files: list[str] = Field(default_factory=list)` — a caller that
  only checked `outcome.wrote`/`outcome.skipped_reason` before this round
  continues to work unchanged; the new fields are opt-in for callers that
  want the finer signal.
- The byte-compare adds one read-and-compare pass per staged file on every
  `run_id`-matching skip (previously zero reads beyond the manifest). For
  this pipeline's output sizes (eight files, a handful of KB to low MB each
  for a weekly brief) this is negligible; it was not benchmarked separately
  because `tests/test_performance.py::test_8000_rows` (the one wall-clock
  performance test in this repository) exercises the CSV/audience pipeline,
  not `weekly.py`.
- F2 (2026-08-01 Fable blocker, "verify, don't assume"): `_staged_differs_
  from_disk`'s per-file read originally caught only `OSError`. A corrupt or
  non-UTF-8 on-disk file raises `UnicodeDecodeError` (a `ValueError`, not an
  `OSError`) from `read_text(encoding="utf-8")`, which crashed the skip path
  instead of marking that file stale. `UnicodeDecodeError` is now in the
  same except tuple as `OSError` — an unreadable-as-text on-disk file counts
  as DIFFERING, the same treatment already given to a missing file.
- A Founder who runs `weekly-run` repeatedly without `--regenerate` on an
  UNCHANGED codebase sees no new output: the clean-skip path is silent, and
  the comparison behind it is CANONICALISED (Q1, 2026-08-01 QA blocker) —
  `_staged_differs_from_disk` compares `run-manifest.json` with every
  wall-clock-only field (`execution_timestamp`, the only one — see
  `RunManifest`'s own docstring) normalised out of both the staged and
  on-disk views before comparing, so two genuine, unmutated invocations of
  the identical run compare equal even though each one's `execution_
  timestamp` is a real, distinct wall-clock reading. Before Q1, this claim
  was FALSE as written: `execution_timestamp` is captured fresh on every CLI
  invocation and was included verbatim in the RAW byte-compare, so it always
  differed between any two invocations, and R1's staleness warning fired on
  every routine rerun regardless of whether anything had actually changed —
  defeating R1's entire purpose by training the Founder to ignore the
  warning or reflexively pass `--regenerate` (itself destructive without a
  manual backup, per the runbook requirement above). The manifest FILE
  written to disk is unaffected by Q1 — it still records the real
  `execution_timestamp` verbatim; only the R1 COMPARISON is canonicalised.

## Alternatives considered

- **Fold `BRIEF_VERSION`/`CONFIDENCE_RUBRIC_VERSION` into `compute_run_id`.**
  Rejected by Fable — see Context; this would silently un-guard every
  historical output directory and make past `run_id`s non-recomputable.
- **Warn only when an overlay is present (keep the round-2 behavior as the
  only warning path).** Rejected — this is precisely the gap that let the
  headline defect reach a real Founder run: a Founder with no overlay file
  got no signal at all that their on-disk brief was stale.
- **Always overwrite on every invocation (drop the idempotency skip
  entirely).** Rejected — this would reintroduce the exact problem
  idempotency was built to prevent (duplicated append-only library/
  score-history/audit rows on a routine re-run with no input changes; see
  `weekly.py`'s own module docstring, §3) merely to fix a narrower staleness
  problem; the byte-compare-before-skip approach fixes staleness without
  sacrificing idempotency's own guarantee.
- **Make the byte-compare itself write the corrected files without
  `--regenerate` when it detects staleness.** Rejected — `write_weekly_run_
  outputs`'s documented contract is that it NEVER writes without
  `regenerate=True` once a completed run exists; silently "fixing" a stale
  run on a plain re-invocation would remove the operator's chance to archive
  the pre-fix directory first (see the runbook requirement above) and would
  make output non-deterministic with respect to the `regenerate` flag alone.
