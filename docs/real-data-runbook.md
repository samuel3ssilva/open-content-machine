# Real-Data Runbook — Audience Intelligence

Owner: fable-principal-architect. This is the only approved procedure for
running the pipeline against a real connections export. It exists so the
Founder can authorize the real run with full visibility of what will happen.
No real-data output may ever be committed.

## Preconditions (all must hold)

1. The synthetic pipeline is green: full test suite, lint, typing, privacy
   tests, and the 8k-row performance test all pass.
2. The real export stays where the user keeps it (outside the repository) or
   in `data/private/`. It is never copied into the project tree.
3. A private salt is configured in `.env` (`CONTENT_MACHINE_SALT=...`) so
   pseudonym IDs are stable across runs. Generate one locally:
   `python -c "import secrets; print(secrets.token_hex(32))"`.
4. A private output directory exists outside the repository (recommended:
   next to the export itself). All `-o`/`--json` targets point there.
5. The Fable Security Auditor has issued **APPROVED FOR REAL LOCAL RUN** for
   the specific pipeline version (commit hash recorded in the approval).
6. The Founder has explicitly authorized the run in chat after reviewing the
   dry-run output.

## Step 1 — Dry-run (mandatory gate)

```bash
content-machine audience inspect "<path-to>/Connections.csv" --dry-run
```

Shows only metadata: file type/size, encoding, row count, column names,
accepted vs. ignored columns, identifiers that will be removed,
transformations, outputs that would be created, and offline confirmations.
It never prints row values and writes nothing to disk.

**Stop here.** Present the dry-run to the Founder and wait for authorization.

## Step 2 — Real run (only after authorization)

```bash
content-machine audience validate "<path-to>/Connections.csv"
content-machine audience report "<path-to>/Connections.csv" \
  -o "<private-dir>/audience-report.md" \
  --json "<private-dir>/audience-report.json"
```

What is processed in memory: names, emails, URLs, companies, titles, dates.
What is written to disk: pseudonymized/aggregated report only (no names, no
emails, no URLs). What crosses the network: nothing — the pipeline has no
network path.

## Step 3 — Post-run verification (mandatory)

```bash
# Outputs must be outside the repo; git must see nothing new
git status --short          # expect: clean
# Outputs must contain no direct identifiers (run against the private dir)
grep -cE '@|https?://' "<private-dir>/audience-report.json" || echo OK
```

Then review the report manually before acting on it. Treat it as
sensitive-by-default; aggregates over a small network can identify people.

## Optional Step 4 — Public sanitized export (separate authorization)

```bash
content-machine audience export-public "<private-dir>/audience-report.json" \
  -o "<private-dir>/public-example.json"
```

Suppresses every group under 10 records and labels the artifact as sanitized.
Even so: publication of any artifact derived from real data requires explicit
Founder approval, file-by-file. Nothing is committed automatically.

## Cleanup / rollback

All real-data state is: the export (user-managed) + the reports in the private
directory. Deleting them removes everything; the tool keeps no caches, logs
with values, or hidden copies. Rotating `CONTENT_MACHINE_SALT` invalidates all
pseudonyms.

## Incident rule

If any real value ever appears in a committed file, a log, or a public
artifact: stop, do not push, and follow the containment procedure used in
`docs/security/linkedin-export-incident.md` (forensics before remediation).
