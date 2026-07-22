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

## Deleting everything

All state is local files. Deleting your private workspace (and any reports you
generated) removes every trace; the tool keeps no hidden copies, caches, or
telemetry.
