# Incident Report — Real LinkedIn Data Export in Workspace

- Date: 2026-07-22 (Bootstrap Sprint)
- Auditor: fable-security-auditor (Fable)
- Severity: contained near-miss, no exposure
- Status: **CLOSED — no personal data was read, staged, committed, or stored in Git**

This report is intentionally sanitized: it contains no real file names beyond a
generic naming pattern, no file contents, no personal names, e-mails, URLs, and
no filesystem paths that identify the user.

## 1. What happened

During the bootstrap sprint, while two engineering agents were working, a
routine `git status` revealed a new **untracked** directory at the workspace
root whose name matched the standard LinkedIn full-data-export pattern
(`*LinkedInDataExport*.zip/` — an unzipped export folder). It had not been
present at session start (verified by the initial preflight directory listing).

## 2. How it entered the workspace

Not created by any agent, tool, or command in this session (the session command
history contains no download, unzip, copy, or move into the workspace). The most
plausible explanation is that the user unzipped or moved their own export into
the folder via the operating system while the session was running. The workspace
lives inside the user's Downloads area, which makes accidental drops likely —
this is recorded as a standing risk below.

## 3. Was the content opened or read?

**No.** Timeline of the only two interactions with that path:

1. `git status --short` displayed the directory **name** only (untracked).
2. A top-level `ls` of the directory was attempted with the explicit intent of
   confirming it was an export (file names only, never contents); it returned
   `No such file or directory` — the directory had already been removed from
   the workspace (again by the user, outside the session). Not even internal
   file names were ever listed.

No agent, subagent, or model context received any content from the export. The
parallel Sonnet/Opus agents' scopes were file-listed and did not include it.

## 4–6. Git exposure audit

Executed after containment (sanitized results):

| Check | Command (equivalent) | Result |
|---|---|---|
| Working tree / index | `git status --short --ignored` + pattern grep | no match — never present in the index |
| Tracked files | `git ls-files` + pattern grep (`linkedin`, `dataexport`, `connections`, `.zip`) | no match |
| All commits, all refs | `git log --all --name-only` + pattern grep | no match |
| Reflog | `git reflog` (5 entries, all documented doc/chore commits) | no match |
| Stash | `git stash list` | empty |
| Object database | `git cat-file --batch-all-objects --batch-check` + `git ls-tree` over every tree object | 34 blobs / 17 trees / 5 commits, all accounted for by tracked files; no matching name anywhere |
| Dangling/unreachable objects | `git fsck --unreachable --dangling` | none |

The directory was never staged: all `git add` invocations in this session used
explicit file lists, never `git add .` while the export was present.

## 7. Additional verification (tracked content hygiene)

- E-mail scan over tracked files: only `example.com`/`example.org`, tooling
  no-reply, and Contributor Covenant contact addresses. No personal e-mails.
- Secret-pattern scan (`api_key|token|password|secret` with long values): none.
- Personal filesystem path scan (machine username): none in tracked files.

## 8. Changes made to `.gitignore`

Before the incident, rules covered `data/private/*`, `.env*`, and
`*connections*.csv`. A full LinkedIn export contains much more (messages,
profile, invitations), so the following were added and committed immediately
(commit `420afa1`): `*LinkedInDataExport*`, `*DataExport*`, `messages.csv`,
`Invitations.csv`, `Profile.csv`. Verified live with `git check-ignore -v` on
representative paths (all match).

## 9. Recurrence prevention

- `.gitignore` now ignores any `*DataExport*` folder anywhere in the tree.
- Automated privacy tests (QA ticket SONNET-004) assert with `git check-ignore`
  that `data/private/` contents, `.env`, and export-pattern paths are ignored.
- SECURITY.md release checklist (run before every push) scans tracked files for
  export patterns, e-mails, and secrets.
- Agent charters forbid opening `data/private/` or any real export; project
  memory instructs future sessions to contain-without-reading if an export
  reappears.
- Explicit-path `git add` remains the required staging practice.

## 10. Conclusion and residual risk

**Conclusion: contained.** The export existed in the workspace for roughly one
minute, only its top-level directory name was ever observed, and it left no
trace in the working tree, index, history, reflog, stash, or object database.
The repository is safe to publish from this standpoint.

Residual risks:

1. The workspace sits inside the user's Downloads area, so future accidental
   drops are plausible. Mitigated by the ignore patterns and pre-push checklist;
   further mitigated when the user moves real data into `data/private/` as
   documented in `data/README.md`.
2. Ignore patterns are name-based; a renamed export would bypass them. The
   pre-push e-mail/URL scan is the backstop for that case.
3. This report relies on session records for the "never read" claim; the Git
   evidence, however, is independently verifiable by anyone with the repo.
