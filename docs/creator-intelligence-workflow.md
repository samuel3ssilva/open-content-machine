# Creator Intelligence Workflow (generic description)

This documents the *process* used to turn a creator's private material into
editorial assets. It contains no private findings — all real outputs live
outside the repository and are never committed (see
[`docs/source-approval-gate.md`](source-approval-gate.md) and
[`docs/private-workspace.md`](private-workspace.md)).

## Pipeline

```
private folder
  │  1. source inspect --dry-run      (metadata-safe inventory, no bodies)
  ▼
triage CSVs (TEXT / PROJECT-DOCS / MEDIA)   ← Founder approves per file
  │  2. deterministic local extraction (stats, structure, dates, terms;
  ▼      identifier redaction → sanitized copies; originals untouched)
sanitized qualitative packages (per intended_use)
  │  3. Founder approves packages individually for model-context analysis
  ▼
qualitative synthesis (model context, sanitized packages only)
  │  creator profile · authority map · positioning · style guide ·
  ▼  content pillars · scored ideas · first drafts
private editorial artifacts — every one labeled
"DRAFT — NOT APPROVED FOR PUBLICATION"
  │  4. Fable privacy/factuality review + one focused revision
  ▼
Founder manual review → manual publication (never automatic)
```

## Boundaries enforced at every step

1. Raw source files never enter model context — only sanitized packages the
   Founder approved individually.
2. Sensitive packages (e.g. personal stories) can stay blocked indefinitely.
3. Analysis approval ≠ publication approval; drafts carry a non-approval
   label until the Founder flips it manually.
4. Facts, autobiographical claims, editorial interpretation, and gaps are
   tagged separately in the profile artifacts; nothing is invented.
5. Provenance (file id → artifact) is tracked so any source's contribution
   can be removed on request.
6. The public repository receives only process documentation like this file
   — never profiles, voice guides, positioning results, or drafts.
