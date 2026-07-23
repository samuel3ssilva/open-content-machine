# Public Repository vs. Private User Workspace

Open Content Machine formally separates two environments. This boundary is
trust boundary TB-1 in `docs/architecture.md` and is enforced by `.gitignore`,
privacy tests, and the pre-push checklist in `SECURITY.md`.

## The two environments

| | Public repository (this repo) | Private user workspace |
|---|---|---|
| Contents | Code, public prompts, schemas, synthetic data, documentation, tests, synthetic outputs | LinkedIn export, connections data, real profile, real articles, recommendations, e-mails, notes, intermediate private outputs |
| Location | Git-tracked tree | `data/private/` (git-ignored) — or **anywhere else on your machine** |
| Leaves your machine? | Yes (GitHub) | Never |

## Using a private file without moving it into the project

You do **not** need to copy real data into the repository. Every CLI command
takes a plain file path, which may point anywhere on disk:

```bash
content-machine audience validate "~/Documents/my-private-data/Connections.csv"
content-machine audience report "~/Documents/my-private-data/Connections.csv" -o ~/Documents/my-private-data/report.md
```

Recommendations:

1. **Preferred:** keep private inputs outside the repository entirely and pass
   absolute paths, writing outputs (`-o`, `--json`) to that same private
   location. Nothing is ever copied into the project automatically — the first
   version has no feature that moves files from the private workspace into the
   repository, by design.
2. **Alternative:** place files under `data/private/` inside the repo. That
   directory is git-ignored (verify anytime with
   `git check-ignore data/private/yourfile.csv`). Convenient, but option 1 is
   safer against tooling that scans the project tree.
3. Never place real data anywhere else inside the repository tree. If it
   happens by accident, move it out and run the SECURITY.md checklist before
   any push.

## What the pipeline does with private inputs

Reading a private CSV keeps all direct identifiers (names, e-mails, URLs) in
memory only. The anonymize/report steps write only allowlisted, pseudonymized
fields (ADR 0003). Still, treat generated reports as sensitive-by-default and
keep them in your private workspace — aggregates over a small network can be
identifying.

## Private source folders (Phase 1)

`content-machine source inspect` builds a metadata-safe inventory of a
private source folder (e.g. the Founder's biography material). To be precise
about what "metadata-safe" means at the byte level:

- **no semantic parsing, summarization, or editorial extraction occurs** —
  no text is interpreted, indexed, or excerpted;
- up to 512 bytes may be inspected for safe file-type detection (magic
  bytes);
- complete file bytes may be read locally, once, to compute the SHA-256 used
  for duplicate detection;
- **no content is ever displayed, written to any output, uploaded, or sent
  to a model.**

The inventory records file names, sizes, dates, a MIME guess, the duplicate
hash, and a provisional privacy category — see
[`docs/source-approval-gate.md`](docs/source-approval-gate.md) for the full
category lattice and gate.

```bash
content-machine source inspect ~/private/biography-material --dry-run \
  --output-dir ~/private/biography-material/_inventory
```

- `--dry-run` is required (this version only supports the safe, read-only
  scan).
- `--output-dir` is required and, like the source `FOLDER` itself, must be
  **outside the repository tree** — the command refuses to run otherwise.
- The scan never copies, extracts, or modifies anything under `FOLDER`;
  archives are never opened, symlinks are never followed, and hidden
  directories are never descended.
- By default, common dependency/generated directories are excluded from the
  scan entirely — not descended, not listed — including `node_modules`,
  `.git`, `dist`, `build`, `coverage`, `__pycache__`, `.venv`/`venv`, and
  similar tool-output directories. Pass `--include-all` to disable this and
  scan every directory. Terminal output reports how many such directories
  were skipped.
- Three private files are written to `--output-dir` (mode `0700` dir,
  `0600` files): `source-inventory-private.md`, `source-inventory-private.json`,
  and `source-review-private.csv`. None of the three ever contains the real
  filesystem path.
- Terminal output is **aggregate counts only** (totals, by-category,
  by-status, duplicates, bytes) — never an individual file name.

**Approval fields start empty by design.** `source-review-private.csv` has
`approved_for_analysis`, `intended_use`, and `founder_notes` columns that are
blank for every row. Nothing in this phase approves a file for anything.
Analysis of any file requires the Founder to set `approved_for_analysis`
explicitly, per file, in that private CSV — the full gate (including why
category D files can never be approved, and why category C needs a written
note) is defined in
[`docs/source-approval-gate.md`](docs/source-approval-gate.md).

## Deleting everything

All state is local files. Deleting your private workspace (and any reports you
generated) removes every trace; the tool keeps no hidden copies, caches, or
telemetry.
