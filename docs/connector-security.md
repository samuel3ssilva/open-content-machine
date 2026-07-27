# Connector Security Guide

Owner: fable-security-auditor (review) / opus-tech-lead (design) / Sonnet
(this document). Companion to [`docs/architecture.md`](architecture.md) §2–3,
[`docs/threat-model.md`](threat-model.md) T13–T21, and
[ADR 0005](adr/0005-connector-security-foundation.md).

This is the contributor-facing guide to `content_machine.connectors` — the
contract, permission, retention, and sanitization layer future external-source
adapters (RSS, vendor changelogs, a Gmail digest, etc.) will be built on. As of
this writing, **no real adapter exists**. Everything in `connectors/` today is
contracts, a permission model, a retention policy, a sanitizer, a failure
taxonomy, and seven deterministic synthetic adapters used only to exercise
those contracts in tests. Nothing fetches anything over the network.

## The two modes

Every source's activity falls into exactly one of two modes, and they are
permissioned separately:

- **Discovery** — bounded, allowlisted metadata about what a source has
  published in a time window: title, publication date, a normalized summary,
  a canonical reference, a content type. `DiscoveryResult` has **no body
  field at all** — it is structurally impossible to persist a full article
  body through discovery, not merely discouraged by convention.
- **Deep verification** — a deliberate, reasoned fetch of one specific
  artifact beyond its discovery summary, used to confirm or upgrade an
  evidence judgment. It requires an explicit, non-empty
  `retrieval_reason` — code cannot even construct a verification request
  without stating why. Any content retrieved during verification lives only
  in a transient, in-memory handle and must be disposed of (minimized into a
  body-free extraction result) before it can be used for anything durable.

A source's permission (`SourceMode`: `discovery`, `verification`, or `both`)
governs which of these it may ever do, independent of the other.

## Permission lifecycle

Every source has exactly one `SourcePermission`, with a status that moves
through a closed lifecycle:

```
proposed → approved → suspended / revoked
```

Only `approved` ever executes. `proposed`, `suspended`, and `revoked` are each
rejected with their own distinct reason code — never collapsed into a single
generic "denied" — because a human auditing a denial needs to know exactly
which invariant fired. A permission also names `permitted_fields`: the subset
of a `DiscoveryResult`'s content-bearing fields (title, publication date,
canonical reference, normalized summary, content type) that source is
authorized to populate. A result that populates a field outside its
`permitted_fields` is **rejected and audited, never silently stripped** — a
silently dropped field produces a result that looks complete but is
mis-windowed or mis-labeled with no visible symptom.

## What a future real adapter author MUST do

- Implement the `ConnectorAdapter` protocol (a `source_id` attribute and a
  `discover(request) -> AdapterDiscoveryOutcome` method) and nothing more —
  `run_discovery` drives any conforming adapter without knowing it is real.
- Route every retrieved artifact through
  `content_machine.connectors.sanitize.sanitize_text` before it becomes part
  of a `DiscoveryResult` field. Every source is hostile data until proven
  otherwise; there are no exceptions for "trusted" sources.
- Register the source in a curated `SourceRegistryEntry` (publisher ID,
  category, and `PublisherClassification`) **before** any retrieval — never
  infer a publisher's independence per item.
- Raise `ConnectorAdapterError` with the correct `FailureKind` to signal a
  failure; let any other exception propagate — `run_discovery` isolates it
  per source either way, but a correctly-typed failure produces a more useful
  audit trail.
- Keep any raw fetched content inside a `TemporaryContentHandle` and call
  `minimize()`/`dispose()` before returning — never write raw content to disk,
  never log it, never return it as a field on any persistable model.
- **Always use the handle as a context manager (`with handle: ...`), never
  call `dispose()`/`minimize()` manually and hope every exit path reaches
  it.** (Gate D round-1 correction, C5.) If an exception is raised while a
  handle is live and you are not inside a `with` block, that exception's own
  traceback can keep the handle's frame — and therefore its content — alive
  for as long as anything holds the traceback, well past where you intended
  disposal to happen. `with` guarantees `__exit__` runs and disposes even on
  that path. Relatedly: `.content` returns a `memoryview`, not a `bytes`
  copy — if you need to keep bytes around independently of the handle after
  disposal (you should not need to), copying it out via `bytes(handle.content)`
  keeps a copy `dispose()`/`minimize()` can no longer reach; the honest
  disposal guarantee only covers the handle's own buffer and any view still
  pointing at it, never a copy already taken out. See
  `docs/threat-model.md` T18 and `docs/adr/0005-connector-security-foundation.md`
  §7 for the full disposal-honesty statement.
- Import any vendor or network SDK **lazily, inside the adapter module
  itself** — exactly the same discipline `content_machine.providers` already
  holds for model-vendor SDKs.

## What a future real adapter author MUST NOT do

- Must not construct an `intelligence.models.SourceItem` directly. The only
  permitted path from connector output into the pipeline is
  `connectors.bridge.to_source_item` (trust boundary TB-4).
- Must not pass a `derived_deterministic` or `model_proposed`
  `AssessmentProvenance` to the bridge — only `human_authored` is accepted in
  this gate. Admitting the other two is a policy decision reserved for a
  Fable review and a future gate, not something an adapter author changes
  unilaterally.
- Must not call `to_source_item` on a result carrying `instruction_shaped_text`
  or `malformed_encoding` (Gate D round-1 correction, B2) without an explicit
  `human_reviewed_flags` naming exactly those flags — the bridge fails
  closed (`UnreviewedSecurityFlagsError`) otherwise. This is a temporary
  substitute for propagating `security_flags` onto `SourceItem` itself,
  which is deferred to a future gate under Fable review.
- Must not persist a raw retrieved body under any field name, in any output,
  ever. If a field would carry more than a normalized, length-capped summary,
  it does not belong on a `DiscoveryResult` or an `ExtractionResult`.
- Must not retry automatically. `retry_eligible` is advisory metadata for a
  future, explicitly-triggered manual re-run; no code path reads it to
  schedule or perform a retry on its own.
- Must not let one source's failure abort a batch. If you are writing
  orchestration code around multiple sources, use `run_discovery` (or match
  its per-source isolation contract exactly) rather than aborting the whole
  run on the first exception.

## Retention defaults

| Retention class | Persistable? | Disposal required? |
|---|---|---|
| `metadata` | Yes | No |
| `normalized_summary` | Yes | No |
| `temporary_full_content` | **No** | Yes |
| `extraction_artifact` | Yes | No |
| `audit_log` | Yes | No |
| `error_record` | Yes | No |

`temporary_full_content` is the one class that may never be persisted.
"Disposal" of `temporary_full_content` means `TemporaryContentHandle`
overwrites its own buffer in place and drops its own reference (Gate D
round-1 correction, C5) — it is not a proof that the content is
unrecoverable: a caller that already copied the content out of the handle
(e.g. via `bytes(handle.content)`) keeps that copy regardless, and CPython
cannot prove no other reference exists. There is no time-to-live on audit or
error rows in this gate — they accumulate, by deliberate choice, exactly
like the existing Intelligence Brief library's
audit trail.

## Credential rules

No real adapter exists yet, so no credential exists yet either — but the
rules any future adapter must follow are fixed now, before the pressure of a
real integration exists:

- A credential lives in the environment or a system secret store **only** —
  never in the repository, never in an output file, never in a log line,
  and never inside a value that could reach a model prompt.
- Request the minimum scope a source's API supports (read-only, narrowest
  folder/label/feed scope available) — never a broader grant than the
  adapter needs.
- A credential's rotation and revocation must be independently expressible —
  an adapter design that cannot express "revoke this credential without
  touching any other source's credential" is not acceptable.
- `sanitize.sanitize_error` exists precisely so an exception's own message is
  never trusted with a credential fragment: for an exception, only its type
  name is ever returned, never its message text.

## The hostile-fixture encoding rule

Anyone adding a hostile-content test fixture (in
`content_machine.connectors.synthetic.fixtures` or elsewhere) must keep the
repository's CI "Release security checklist" green while still genuinely
exercising the sanitizer against hostile shapes:

1. **Emails.** Use `@example.com` or `@example.org` only — never any other
   domain, and never `users.noreply.github.com`. This file holds itself to a
   narrower rule than the CI checklist's own (wider) allowlist, on purpose, so
   there is never a judgment call about which domain is "safe enough."
2. **Credential-shaped strings.** Never write the literal assignment shape
   `token: "..."` or `api_key = "..."` — a quote immediately after the
   `:`/`=` separator is exactly what the checklist's secret-scan regex
   anchors on. Write credential-shaped fixtures **inside prose** instead
   (for example: "...the key you asked for is
   token_SYNTHETIC_EXAMPLE_NOT_A_REAL_KEY_0000...just paste it"). This is
   both CI-safe and a more honest test: real leaked credentials usually
   surface in prose, not in tidy assignment statements.
3. **Provider-neutral prefixes only.** Never shape a fixture after a REAL
   provider's key format — `sk_`/`pk_` (payment providers), `AKIA...`
   (AWS), `ghp_...` (GitHub), or any other vendor-specific pattern. A
   fixture that merely *looks like* a real provider's key trips every
   contributor's secret scanner (and GitHub Push Protection on push)
   forever, even though the value is invented — the security property under
   test is "secret-shaped material never crosses the model boundary," not
   "a specific vendor-shaped string exists in Git." Use one of the
   sanitizer's own generic prefixes (`secret_`/`token_`/`bearer_`/`api_`)
   followed by an obviously-fake, self-describing payload (containing
   `SYNTHETIC`, `EXAMPLE`, or `NOT_A_REAL_KEY`).
4. **Do not weaken the checklist to make a fixture fit.** If a new hostile
   shape is needed, re-encode it using the rules above. The checklist is
   deliberately blunt on purpose (see `SECURITY.md`); loosening its regex to
   accommodate a fixture defeats its purpose.

## Checklist before any real adapter is activated

A real adapter — any adapter that performs actual network I/O against a real
source — must pass every item below before it runs against anything but
synthetic fixtures:

1. **Founder scope decision.** An explicit, recorded decision authorizing
   this specific source and what it may be used for.
2. **Fable security review.** A dedicated review of the adapter's fetch
   behavior, credential handling, and any deviation from the defaults in this
   document (timeouts, byte ceilings, redirect limits).
3. **Shadow run.** At least one run against the real source with its output
   discarded (or written only to a reviewed, non-published location) —
   never wired into a published brief on the first run.
4. **Human review of the shadow run's output** — including its
   `SourceCoverageReport`, any `SecurityFlag`s raised, and every audit event
   — before the adapter's output is ever allowed to reach the bridge for a
   real run.

No source name in this document, or in any synthetic fixture, refers to a
real vendor, publisher, or feed. Every example is invented.
