# ADR 0005 — Connector Security Foundation (Gate D)

- Status: Accepted — IMPLEMENTED, then SECURITY-BLOCKED by a mandatory Fable
  review, then CORRECTED in round 1 (see the "Fable review — round 1
  findings and dispositions" section below), then CORRECTED AGAIN in round 2
  (the final of up to 2 authorized local correction rounds; see the "Fable
  review — round 2 findings and dispositions" section below), which Fable's
  re-review returned SECURITY-APPROVED, conditioned on item D1 landing (it
  has). Merging is a separate, later decision, not implied by this status.
- Date: 2026-07-25 (original; round-1 and round-2 corrections same day)
- Decider: Founder (scope authorization for Gate D and for both correction
  rounds); implementation recorded by Opus (design) and Sonnet (contracts,
  harness, docs, round-1 and round-2 corrections)
- Model responsible: Sonnet

## Context

Every phase of the Intelligence Brief shipped so far (Gates A–C, ADR 0004) runs
against synthetic fixtures only. The product vision requires real external
signals eventually — RSS feeds, vendor changelogs, a Gmail digest — and those
sources are, by definition, untrusted: retrieved text can be hostile,
mislabeled, oversized, or simply wrong, and a source's standing to supply
"independent" evidence is not something the pipeline can safely infer per item.
Gate D builds the security and contract foundation those future connectors
will stand on, **before** any real adapter exists, so that the hard questions —
where does untrusted data enter, who may say what about a source, what happens
to a raw body, how does one bad source get contained — are answered once, in a
reviewable layer, rather than improvised inside the first real adapter under
delivery pressure.

Gate D originally shipped exactly three commits: (1) the contract layer
(`models`, `registry`, `permissions`, `retention`, `sanitize`, `failures`);
(2) the synthetic adapter harness, batch runner, and pipeline bridge; (3)
this ADR and its companion documents. A mandatory Fable security review of
that submission returned GATE D SECURITY-BLOCKED (see the round-1 findings
section below); round 1's correction commits, and round 2's (see the
round-2 findings section below), are each additional, scoped entirely to
`connectors/`, `tests/`, `schemas/`, and `docs/` — the Founder's constraint
that ranking and rendering stay untouched holds for both correction rounds.
`git diff cb2675f --name-only` over
`src/content_machine/{intelligence,audience,privacy,ingestion,providers,cli}`
is empty across the original submission and both correction rounds: Gate
D changed zero lines of ranking, rendering, or any other existing core
module. Nothing in this gate performs network I/O; there is no credential,
no scheduler, and no real adapter.

## Decision

### 1. Placement: `connectors/` is a sibling of `providers/`, never inside `sources/`

`src/content_machine/connectors/` is a new top-level package, a sibling of
`content_machine.providers` (the existing sole network-capable module for
model vendors) — not a subpackage of `content_machine.sources`, which
inventories the Founder's **private** biography material at metadata
granularity and lives entirely in the private data zone. `sources/` and
`connectors/` answer different questions under different trust boundaries:
`sources/` never reads file content and never performs network I/O by design;
`connectors/` is scoped from day one to eventually retrieve **public** external
content over the network. Conflating them would blur two boundaries that must
stay distinct. `connectors/` is declared the ONLY package that may ever hold
network I/O or a vendor/network SDK import (lazy, inside a concrete adapter
module) in a future gate — exactly the same discipline `providers/` already
holds for model-vendor SDKs.

### 2. Trust boundary TB-4 (retrieval → pipeline): `bridge.to_source_item` is the single choke point

`connectors.bridge.to_source_item` is the only code path in the entire
codebase permitted to construct an `intelligence.models.SourceItem` from
connector output. Every future adapter, in every future gate, funnels through
this one function. This makes the bridge the single, auditable place where
connector output either earns its way into the M1–M7 pipeline or is rejected
outright — recorded as trust boundary **TB-4** in `docs/architecture.md` §3,
alongside TB-1 (disk → repo), TB-2 (local → model provider), and TB-3 (report
→ publication).

### 3. Fail-closed provenance: only `human_authored` may cross the bridge in Gate D

`bridge.AssessmentProvenance` has three members: `human_authored`,
`derived_deterministic`, and `model_proposed`. All three are fully
representable on `AuthoredAssessment` — future gates may legitimately want the
other two — but `to_source_item` raises `AssessmentProvenanceNotPermitted` for
anything other than `human_authored`. **This is stated explicitly so it is
never mistaken for an oversight to "fix" later in a routine change:** admitting
`derived_deterministic` or `model_proposed` assessments is an admission-POLICY
change, not an implementation detail, and it is reserved for a Fable security/
privacy review paired with a future gate. No implementer may quietly relax this
check. The bridge also fails closed on two authored fields whose Pydantic
empty-default is actively harmful rather than merely incomplete: an empty
`topic_tags` would silently zero the relevance dimension's profile join, and
an empty `subject_entity_ids` would make every publisher look independent of
every subject. Both raise (`EmptyTopicTagsError`, `EmptySubjectEntityIdsError`)
before a `SourceItem` is ever constructed.

### 4. Bounded discovery vs. deep verification as two separately-permissioned modes

Discovery and verification are structurally distinct, not just conventionally
separated. `models.DiscoveryResult` has **no body/raw-content field at all** —
full-body persistence at discovery time is structurally impossible, not merely
discouraged, because the field does not exist on the model. Deep verification
(`models.VerificationRequest`) requires an explicit, non-empty
`retrieval_reason` — an unreasoned deep fetch cannot even be constructed — and
any content it retrieves lives only inside a `models.TemporaryContentHandle`,
an in-memory-only holder whose `minimize()` is the sole path from transient raw
content to a persistable, body-free `ExtractionResult`. `permissions.SourceMode`
(`discovery` / `verification` / `both`) governs which mode(s) a source's
permission authorizes independently of the other.

*(Scope note, added in the round-1 correction below: "structurally
impossible" above describes ONLY the absence of a body field — there is
nothing to bypass because the field does not exist. It does not describe
`DiscoveryResult`'s other bounds — `title`/`summary_normalized` length caps,
the `content_type` allowlist, `canonical_reference`'s scheme/length/
control-character checks — which are enforced by a validator at
construction plus `frozen=True` immutability, a real but different
guarantee from a field's outright absence. See the round-1 findings
section's B3 entry.)*

### 5. `SourceRegistry` is the authoritative, permission-independent source of publisher identity; unclassified fails closed

`registry.SourceRegistryEntry` makes `publisher_id`, `source_category`, and
`publisher_classification` properties of the **curated source**, decided once,
before any retrieval — never inferred per item, and never present on
`DiscoveryResult` itself. `PublisherClassification` is a closed enum
(`vendor_first_party`, `independent`, `community`, `unclassified`), and
`SourceRegistryEntry.may_supply_independence` is `True` only for `independent`
and `community`.

**Honest scope correction (Gate D round-2, D1 — mandated by Fable; this
section's overclaim is, in Fable's words, "the same overclaim species I
blocked in round 1").** The paragraph above describes
`may_supply_independence` as though it were an operative control that closes
the ad-hoc-independence hole. **At the time this ADR was written it was not**,
and the rest of this paragraph records that state for history.

**RESOLVED IN GATE E0 (E0.3, rulings R3/F2) — no longer deferred.** The
property is now consumed. `bridge.to_source_item` bakes the registry's answer
onto the item (`bridge.py:469`), and `intelligence.cluster._is_independent`
reads that field as an additional AND-conjunct (`cluster.py:276`,
`item.may_supply_independence is not False`). The field is **tri-state**:
`None` means no registry opinion and falls back to the pre-existing
subject-membership test, so no existing fixture regressed; `True` behaves as
before; `False` DENIES independence. Because it is a pure conjunct it can only
ever REMOVE independence, never grant it, and the bridge never emits `None` —
every connector-sourced item carries an explicit registry answer.

The historical description follows. Independence was decided, at the time,
solely by `intelligence.cluster.py`'s subject-membership test
(`_is_independent`, comparing an item's `subject_entity_ids` against a topic's
subjects) — a check that had no idea `SourceRegistryEntry` or
`PublisherClassification` exist. `may_supply_independence` was therefore a
**curated but not-yet-consumed classification**: the registry correctly
captured, per source, whether a human had judged it capable of supplying
independent evidence, and `unclassified` correctly failed exactly as closed as
`vendor_first_party` in that curation — but nothing then read the property to
gate anything a real item did.

**On the tempting mechanical predicate.** A natural-looking shortcut for that
future wiring is `vendor_first_party AND publisher_id not in subject_entity_ids`
— i.e. "a vendor's own publisher is never independent of a subject it
literally is." Fable's ruling: this predicate is **UNSOUND as a hard reject**.
A vendor's rigorous benchmark or release note about ANOTHER vendor's product
is genuinely independent evidence (see ADR 0004's evidence-type rubric,
`independent_analysis`/`benchmark_with_methodology`/`independent_implementation`)
— `vendor_first_party` describes the SOURCE's
relationship to itself, not its relationship to every subject it might ever
write about. The future shape this takes must be a reviewable FLAG for a
human to weigh, never an automatic hard reject baked into the predicate.
Critically, **`unclassified` still fails exactly as closed as
`vendor_first_party`** in the registry's own curation — an uncurated source
is never treated more favorably than a known vendor source until a human
curates it — that half of the original claim is accurate and unchanged.
Without this registry, "is this publisher independent?" would have to be
answered ad hoc, per item, by whichever future adapter or reviewer author
encounters it — the same class of silent, unauditable judgment call Gate A's
evidence rubric was built to eliminate; the registry's curation is real and
correct — and its consumption landed in Gate E0 (E0.3), as recorded above.

### 6. Permission model: proposed/approved/suspended/revoked × mode × permitted_fields; violations REJECTED + audited, never silently stripped

`permissions.SourcePermission` combines a lifecycle `PermissionStatus`
(`proposed` / `approved` / `suspended` / `revoked` — only `approved` ever
executes), a `SourceMode`, and a `permitted_fields` allowlist of
`DiscoveryResult`'s content-bearing field names (`title`, `publication_date`,
`canonical_reference`, `summary_normalized`, `content_type`; structural/
provenance fields need no permission). `PermissionRegistry.authorize` is the
single fail-closed gate every source must pass before discovery or
verification proceeds, with a **distinct reason code per invariant**
(`not_registered`, `status_proposed`, `status_suspended`, `status_revoked`,
`mode_mismatch`) — never collapsed into one generic "denied", because a human
auditing a denial needs to know which invariant fired.
`PermissionRegistry.enforce_discovery_fields` REJECTS (and audits) a
`DiscoveryResult` that populates a content-bearing field outside its source's
`permitted_fields`. **This is a deliberate implementer decision on behalf of
the spec, recorded here rather than left implicit:** enforcement is reject,
never silent-strip — a silently stripped `publication_date` would mis-window
an item with no visible symptom, which is strictly worse than a loud rejection.

### 7. Retention: no raw body persisted; disposal actively overwrites and drops the handle's own reference, not a guarantee of erasure; post-disposal access raises

Every value a connector ever produces belongs to exactly one
`retention.RetentionClass`. `temporary_full_content` is the one class that may
**never** be persisted; it exists only inside a `TemporaryContentHandle`.
Reading `.content` after disposal raises `ContentDisposedError` rather than
returning a stale or empty value silently.

**Honesty correction (Gate D round-1, C5).** This section originally
described `minimize()`/`dispose()` as returning a `DisposalRecord` "as
durable proof of disposal" and implied disposal was closer to a guarantee
than it is. A Fable review proved two gaps: (1) a caller that read
`.content` before `minimize()`/`dispose()` keeps ONE reference to the raw
bytes alive regardless of what the handle itself does afterward; (2) a
handle not used as a context manager can keep its content alive inside the
traceback frame of any exception raised while it is live. Neither gap is
fully closable in CPython — there is no way to force garbage collection or
prove no other reference exists. What round-1 changed, honestly stated:
`TemporaryContentHandle` now holds its buffer as a mutable `bytearray` and
exposes `.content` as a `memoryview` rather than a `bytes` copy;
`dispose()`/`minimize()` overwrite that buffer in place before dropping the
handle's own reference, so a caller-held `memoryview` obtained before
disposal reflects the scrubbed (zeroed) buffer afterward too. This is a
real, if partial, improvement over immutable `bytes` — it does **not**
reach a `bytes` copy a caller already extracted (e.g. `bytes(handle.content)`),
and it is not a proof of erasure at the OS/memory-page level. `DisposalRecord`
(canonical reference, retention class, byte count, and a reason — never the
content itself) is proof that the handle's own overwrite-and-drop ran, not a
guarantee the content is unrecoverable by any means. `audit_log` and
`error_record` are persistable with no time-to-live in this gate — a
deliberate choice, stated explicitly rather than left as an implicit gap,
exactly mirroring the existing Intelligence Brief library's audit trail.

### 8. Failure model: closed taxonomy, per-source isolation, retry ELIGIBILITY only

`failures.FailureKind` is a closed set (`timeout`, `rate_limited`,
`unavailable`, `invalid_response`, `unsupported_content`,
`extraction_failure`, `permission_revoked`, `partial_batch`).
`runner.run_discovery` is the only place that orchestrates more than one
source per call, and it isolates every adapter: an adapter that raises **any**
exception never aborts the batch, and its failure is recorded as exactly one
`SourceFailure` for that source while every other source's results are
unaffected. A source that fails partway through contributes **zero** results
for that run — whatever partial, in-process output the adapter may have
accumulated is discarded entirely, because a partial, unbounded-provenance
result set cannot be trusted to represent "what happened." `retry_eligible` on
both `SourceFailure` and `ConnectorAdapterError` is advisory metadata for a
future, explicitly-triggered manual re-run only — **no automatic retry exists
anywhere in this gate**; nothing reads the flag to schedule or perform one.

### Pinned constants

Each constant below has a dedicated literal-assert test (Gate C precedent: a
test that derives the expected value from the constant itself is a tautology
and does not count).

| Constant | Value | Rationale |
|---|---|---|
| `DEFAULT_MAX_BYTES` | 2,000,000 | Ceiling on one verification fetch's response size, bounding memory for a single in-flight `TemporaryContentHandle`. |
| `DEFAULT_TIMEOUT_SECONDS` | 20 | Ceiling on one verification fetch's wall-clock time, so one slow source cannot stall a run. |
| `DEFAULT_MAX_REDIRECTS` | 3 | Ceiling on redirect hops per verification fetch; exceeding it raises `redirect_chain_exceeded`, a defense against open-redirect/SSRF-style chains. |
| `DEFAULT_MAX_ITEMS_PER_SOURCE` | 50 | Ceiling on items returned per source per discovery window, bounding one source's contribution to a run. |
| `DEFAULT_MAX_REQUESTS_PER_RUN` | 200 | Ceiling on total adapter requests in one discovery run, bounding aggregate load regardless of source count. |
| `SUMMARY_MAX_CHARS` | 280 | Max length of `DiscoveryResult.summary_normalized`; aligned with `intelligence.library.NORMALIZED_SUMMARY_MAX_CHARS` so the two layers share one bound. |
| `TITLE_MAX_CHARS` | 300 | Max length of `DiscoveryResult.title`, bounding one field's contribution to storage and display. |
| `ALLOWED_CONTENT_TYPES` (closed allowlist) | `application/rss+xml`, `application/atom+xml`, `application/xml`, `text/xml`, `application/json`, `text/html`, `text/plain` | The complete set of content types a connector may ever claim to have discovered or verified; anything else is `unsupported_content`, a failure, not a silently-accepted value. |
| `CANONICAL_REFERENCE_MAX_CHARS` (round-1 addition) | 2,048 | Max length of `DiscoveryResult.canonical_reference` — added in the round-1 correction below (C3); this field was previously unbounded. |

## Fable review — round 1 findings and dispositions

Gate D's first submission returned **GATE D SECURITY-BLOCKED** from a
mandatory Fable security review. Every finding was proven by an executed
probe against the actual code, not inferred from reading it. This section
records what each finding was, what changed, and — as honestly as the
blockers themselves demanded — what remains deferred. Authorization for this
correction round came from the Founder in the orchestrator's chat (up to two
local correction rounds; this is round 1). Merging is a separate, later
decision and is not implied by this section.

### Blockers (all fixed)

- **B1 — permission bypass via unbound adapter identity.** Nothing checked
  that a `DiscoveryResult.source_id` an adapter returned matched the
  adapter's own registered `source_id`. Proven probe: an adapter registered
  and approved as `src_approved` returned a result self-labeled
  `source_id="src_REVOKED"`; the batch reported `all_succeeded`, and
  `enforce_discovery_fields` looked up (and applied) the REVOKED source's
  permission, not the approved one actually authorizing the run. Fixed in
  `runner.run_discovery`: any result whose `source_id` differs from the
  invoking adapter's own now fails the WHOLE source closed (zero results,
  one `FailureKind.source_id_mismatch` failure, never retry-eligible);
  `source_registry` (see C4) is now an active gate a source must clear
  before its adapter is even invoked; `DiscoveryResult.permission_ref` is
  reconstructed from the `PermissionRegistry` entry that actually authorized
  the run (`PermissionRegistry.get()`, added for this) rather than trusted
  from the adapter's self-report. `enforce_discovery_fields` also
  independently re-checks `status == approved`
  (`FieldEnforcementReasonCode.status_not_approved`) so the two enforcement
  points can never disagree.
- **B2 — every SecurityFlag destroyed at the bridge.** `SourceItem` has no
  `security_flags` field, so a hostile item flagged `instruction_shaped_text`
  crossed `bridge.to_source_item` with the injection text intact and the
  marker gone — the threat model's whole prompt-injection posture rests on
  flags being traceable for a human reviewer, which downstream they were
  not. Per the orchestrator's decision (the Founder's constraint against
  touching `intelligence/` in this gate rules out adding the field to
  `SourceItem`), `to_source_item` now fails closed
  (`UnreviewedSecurityFlagsError`) on `BLOCKING_SECURITY_FLAGS` —
  `instruction_shaped_text` (required) and `malformed_encoding` (this
  implementer's documented judgment call, since it also covers bidi/
  invisible-character deception after C2) — unless a caller names the exact
  flag(s) reviewed via the new `human_reviewed_flags` parameter.
- **B3 — validators are construction-time only; bounds were advisory.**
  Every contract model used `extra="forbid"` without `frozen=True`, and
  `runner.py` itself used `model_copy(update=...)` — which bypasses
  validation entirely — to add batch-level duplicate flags, making the
  bypassing pattern the in-tree idiom a future engineer would copy. Proven
  probes: `content_type` mutated post-construction to
  `application/x-evil`; `summary_normalized` mutated to 100,000 chars. Fixed
  by adding `frozen=True` to every contract model in `connectors/`
  (`DiscoveryResult`, `ExtractionResult`, `ConnectorAuditEvent`,
  `SourceFailure`, `DisposalRecord`, and every other value/result model —
  `ProvenanceMetadata`, `PermissionRef`, `SourcePermission`,
  `AuthorizationDecision`, `FieldEnforcementDecision`, `RetentionPolicy`,
  `TriageCandidate`, `SourceCoverage`, `SourceCoverageReport`,
  `BatchDiscoveryResult`) and replacing `runner.py`'s `model_copy` calls with
  re-validating construction (`_with_security_flags`/`_with_permission_ref`).
  **Documented honestly, not oversold:** `frozen=True` alone does NOT close
  the `model_copy(update=...)` bypass — Pydantic's own implementation writes
  directly to internal state and skips both validation and the frozen check
  even on a frozen model (proven and pinned by
  `test_discovery_result_model_copy_update_still_bypasses_frozen`). The real
  fix is that this codebase stopped using the pattern, not that `frozen`
  makes it safe to use.

### Required corrections (all fixed)

- **C1** — `sanitize.py`'s filesystem-path regex matched any two-or-more-
  segment absolute path, so an ordinary URL (`https://example.com/vendor/
  blog/post-42`) was misdetected as a filesystem path, redacted, and falsely
  flagged. Anchored the pattern on `~/`, `/Users/`, `/home/`, `/var/`, or a
  Windows drive letter, plus a negative lookbehind for an immediately
  preceding `://`.
- **C2** — the control-character strip covered C0/DEL only, so a zero-width
  space mid-word defeated the instruction-shaped heuristic and an RTL
  override character passed through unflagged, making rendered text differ
  from logical text. Added stripping of zero-width/bidi-control/invisible
  characters (U+200B–200F, U+202A–202E, U+2066–2069, U+FEFF), flagging
  `malformed_encoding`, applied before the instruction-shaped check.
- **C3** — `DiscoveryResult.canonical_reference` had no `max_length` and was
  never sanitized, unlike `title`/`summary_normalized`; a 5,000,021-char
  value validated, and this field is the sha256 input `bridge.derive_item_id`
  hashes and the value persisted as `SourceItem.stable_reference`. Added
  `CANONICAL_REFERENCE_MAX_CHARS` (2,048), an http/https scheme allowlist,
  and a control/whitespace-character rejection.
- **C4** — `source_registry` was accepted by `run_discovery` and never used;
  the review named this the root cause of B1. It is now an active
  fail-closed gate — see B1 above.
- **C5** — disposal was over-claimed in three places (code and two docs).
  See §7 above and the corrected `docs/threat-model.md` T18 for the code fix
  (mutable-buffer + memoryview) and the honest restatement (drops the
  handle's own reference and actively overwrites it; does not reclaim a
  `bytes` copy already taken out; CPython cannot prove no other reference
  exists). `docs/connector-security.md`'s adapter guide now requires `with`
  usage explicitly, addressing the traceback-frame probe.
- **C6** — `runner.py` passed `ConnectorAdapterError` instances through
  `sanitize_error`'s exception branch, which discards an exception's own
  message entirely — so every adapter failure logged the literal string
  `"ConnectorAdapterError"`, never the adapter's own structured reason.
  `exc.reason` is now routed through `sanitize_error`'s string branch
  (still redacted/capped) specifically for `ConnectorAdapterError`; foreign
  exceptions remain type-name-only.

### What is fixed vs. what remains deferred

**Fixed in this round:** B1, B2 (the fail-closed substitute), B3, C1–C6, and
the documentation-honesty corrections to this ADR (§4's bounds language,
§7's disposal language) and to `docs/threat-model.md` (T18, T19).

**Still deferred, stated explicitly so no reviewer is misled:**

- **B2's durable fix — DONE, no longer deferred (Gate E0, E0.1/E0.3).** This
  section previously said propagating `security_flags` onto `SourceItem`
  itself and surfacing them in the published brief for a human reviewer
  remained deferred to the gate that shipped the first real adapter. That
  gate is Gate E0: `SecurityFlag` now lives on `SourceItem` (and
  transitively on `TopicCluster`/`TieredTopic`) and is surfaced in the
  published weekly brief, alongside `may_supply_independence` propagation
  from the curated source registry. The round-1 fail-closed substitute
  (`UnreviewedSecurityFlagsError`) is unchanged and still in force — this is
  the additional, durable fix layered on top of it, not a replacement for
  it.
- **SSRF/timeout/redirect/byte ENFORCEMENT — real fetch code now exists,
  still unwired to any adapter (Gate E0, E0.4).** This section previously
  said `VerificationRequest.max_redirects`/`max_bytes`/`timeout_seconds` and
  `redirect_chain_flags`' hop-count check were tested logic over data only,
  with no actual network/fetch code anywhere in the codebase. As of E0.4,
  `connectors/network.py` (`NetworkFetcher`) is real, executing fetch code —
  HTTPS-only, a per-source hostname allowlist, blocked private/loopback/
  link-local/IP-literal addresses, DNS-rebinding-resistant address pinning
  with certificate verification still against the original hostname,
  manually-handled and bounded redirects re-validated per hop, separate
  connect/read timeouts, a mid-stream byte cap, a MIME allowlist, per-source
  rate limiting, and a live `authorize_retrieval` permission check — see
  `docs/threat-model.md` T15 for the full list and `SECURITY.md` for the
  public-facing summary. What remains genuinely deferred is wiring this
  fetch boundary to any actual source adapter; no adapter in this repository
  calls it yet, and doing so is its own future gate with its own Fable
  review of that specific integration.
- **No real adapter exists yet, and Gmail is sequenced last.** The synthetic
  adapters remain the only implementations of `ConnectorAdapter`. When real
  adapters are built, lower-sensitivity, simpler sources (RSS/vendor
  changelogs) are expected to come first; a Gmail-shaped adapter carries
  qualitatively higher sensitivity (mailbox content, personal
  correspondence — asset A6/T21) and requires its own dedicated Fable
  privacy review before activation, sequenced after the simpler adapters,
  not alongside them.

## Fable review — round 2 findings and dispositions

Round 1's corrections went back to Fable for re-review. **Verdict:
SECURITY-APPROVED**, conditioned on item D1 (the `may_supply_independence`
overclaim correction) landing — it has, in this round. This is the final of
the two locally authorized correction rounds; there is no round 3. Merging
remains a separate, later decision, not implied by this verdict.
Authorization for this round, like round 1, came from the Founder in the
orchestrator's chat.

### Required corrections (all fixed)

- **C1** — `triage()` tokenized tags for case-folding but never for matching
  itself, then tested a whole tag string against a token SET — a set can
  never contain a hyphen or a space, so every hyphenated or multi-word tag
  (6 of the real profile's 11 territory tags: `agent-cli`,
  `hooks-guardrails`, `multi-agent`, `memory-context`, `browser-agents`,
  `careers-skills`) was structurally unmatchable, and every affected item
  scored 0 identically to a genuinely irrelevant one — collapsing selection
  to the `canonical_reference` tiebreak (alphabetical URL order). Severity 1
  because raising triage selectivity is the only sanctioned release valve
  for the human authoring burden. Fixed by tokenizing each tag the same way
  as the haystack and requiring ALL of a tag's tokens to match.
- **C2** — the `human_reviewed_flags` override (round 1's B2 fail-closed
  substitute) correctly required naming every blocking flag and never
  admitted on `None`/empty, but left no trace once an item was overridden:
  no reviewer identity, no note, no audit event. Fixed additively (no
  existing check relaxed): `to_source_item` now requires a non-empty
  `reviewer_note` whenever `human_reviewed_flags` is passed and would
  otherwise admit the item, and a new companion function
  `to_source_item_with_audit` also returns a `ConnectorAuditEvent`
  (`event_kind=security`) recording the accepted flags, the note, and the
  item's derived id.
- **C3** — `run_discovery` accepted `outcome.results` with no length check
  against `max_items_per_source`, so a buggy or malicious adapter returning
  more than the ceiling was admitted in full; the ceiling was passed into
  the per-source `DiscoveryRequest` but only honored by well-behaved
  adapters. Fixed: the runner now truncates to the ceiling deterministically
  (first N in the adapter's own returned order), adds the excess to
  `dropped_count`, and sets `truncated=True` — never reported as a failure.
- **C4** — `connectors/__init__.py` exported the contract layer but not the
  workflow built on it (`run_discovery`, `BatchDiscoveryRequest`,
  `BatchDiscoveryResult`, `SourceCoverage`, `SourceCoverageReport`,
  `ConnectorAdapter`, `ConnectorAdapterError`, `to_source_item`,
  `AuthoredAssessment`, `AssessmentProvenance`,
  `UnreviewedSecurityFlagsError`), forcing every consumer, including the
  end-to-end test, to reach into submodules directly. Fixed: all now
  exported, `__all__` kept sorted.
- **C5** — `docs/connector-security.md`'s pre-activation checklist makes
  human review of the `SourceCoverageReport`, its `SecurityFlag`s, and the
  audit events mandatory, but the only way to read one was
  `model_dump_json()` in a REPL. Fixed: a pure `runner.format_coverage_report`
  helper renders a deterministic, human-readable text table; no clock, no
  I/O, no raw content (the report has no title/summary/content field to
  leak in the first place).

### Documentation fixes (all required)

- **D1 — mandated by Fable; this round's SECURITY-APPROVED verdict is
  conditioned on it.** Decision 5 above presented
  `SourceRegistryEntry.may_supply_independence` as an operative control
  closing the ad-hoc-independence hole. Fable proved it is dead code — zero
  call sites in `src/`, tests only — and independence is decided solely by
  `cluster.py`'s subject-membership test. Fixed: Decision 5 now states
  plainly that `may_supply_independence` is a curated but not-yet-consumed
  classification, added to the Deferred list, with Fable's ruling recorded
  that the tempting mechanical predicate (`vendor_first_party AND
  publisher_id not in subject_entity_ids`) is UNSOUND as a hard reject — a
  vendor's rigorous benchmark or release note about another vendor is
  genuinely independent evidence; the future shape is a reviewable flag,
  never a hard reject.
- **D2** — `connectors/__init__.py` and `models.py` both claimed
  `VerificationUpgrade` "is applied to the AUTHORED ASSESSMENT before
  bridging (in a later commit's `bridge.py`)"; that commit shipped and no
  such application path exists — `bridge.py` has zero references to
  `VerificationUpgrade`. Per the orchestrator's explicit decision, this
  round does NOT build the missing application function (the tree is
  already security-approved; no unreviewed admission-adjacent code is being
  added after that approval). Fixed: both docstrings, and this ADR's
  Deferred list, now state honestly that Gate D ships the
  `VerificationUpgrade` contract with no application path, wiring deferred
  to the first-real-adapter gate.
- **D3 (QA, LOW)** — a vacuous `assert x or True` in
  `test_connectors_registry.py`; `sanitize.py`'s docstring listing the
  redact step before the detect-instruction-shaped-text step, the reverse
  of execution order; and a missing regression guard. Fixed: dropped the
  dead assertion half, corrected the docstring order, and added a static
  AST scan (mirroring the existing no-network scan's full-tree walk and
  `TYPE_CHECKING` carve-out) denylisting `random`/`uuid` imports and bare
  `hash()` calls across `connectors/` — zero current occurrences, passes
  immediately, defense in depth against a future regression.
- **D4 (this section)** — records the round-2 review.

### Fable's three FUTURE-GATE findings (not blocking; not fixed this round)

Proven observations that do not block the SECURITY-APPROVED verdict but
must be resolved before the first real adapter is activated:

1. **The bridge has no LIVE permission check.** `to_source_item` trusts
   whatever `DiscoveryResult`/`AuthoredAssessment` pair it is handed; it
   does not itself re-verify against `PermissionRegistry` that the source
   is still `approved` at the moment of bridging (as opposed to at
   discovery time, which `run_discovery` does check). Between a discovery
   run and a bridging call, a permission could be suspended or revoked with
   nothing at the bridge noticing.
2. **The filesystem-path regex still over-redacts.** Round 1's C1 fix
   anchored `_FS_PATH_RE` on `~/`, `/Users/`, `/home/`, `/var/`, and Windows
   drive letters to stop misdetecting ordinary URL paths as filesystem
   paths — but a genuine URL path segment that happens to start with
   `/Users/`, `/home/`, or `/var/` (e.g. `https://example.com/users/42`)
   still matches and gets redacted. This fails SAFE (over-redaction, not
   under-detection), so it is not a security hole, but it is a real
   accuracy gap the anchoring did not fully close.
3. **`all_succeeded` is reported when the only source was skipped.**
   `run_discovery`'s status computation (`failed_count == 0` →
   `all_succeeded`) does not distinguish "every attempted source succeeded"
   from "zero sources were attempted because the only one was skipped
   (`skipped_not_approved`)" — a batch with one source, skipped, currently
   reports the same `all_succeeded` status as a batch that actually
   discovered something.

### Product review's FUTURE-GATE items (not blocking; not fixed this round)

1. **No persistence for `consecutive_failure_count`.** The field exists and
   is computed correctly WITHIN one `run_discovery` call from the caller-
   supplied `prior_consecutive_failures`, but no code anywhere actually
   persists a run's coverage report to seed the NEXT run's
   `prior_consecutive_failures` — wiring that persistence is out of scope
   for this gate (documented already in `runner.py`'s module docstring; the
   product review confirms it is unresolved).
2. **No zero-yield signal.** A source that succeeds but discovers zero
   items in a window looks identical, in the coverage report, to a source
   that was never expected to have anything new — there is no distinct
   marker for "ran fine, found nothing."
3. **Conflated skip reasons.** `SourceCoverage.skipped_not_approved` is set
   for both "not in `source_registry`" and "permission not approved" —
   two different operational conditions with different remediation paths,
   currently indistinguishable from the coverage row alone (the audit
   events do distinguish them via `reason_code`, but the coverage row does
   not).
4. **No matched-span detail for flags.** A `SecurityFlag` records THAT
   something matched (e.g. `instruction_shaped_text`) but not WHERE in the
   field or what the matched span was, beyond what remains visible in the
   (for `instruction_shaped_text`) unmodified field itself — a human
   reviewer has to re-scan the whole field rather than jump to the
   flagged substring.

### Entry conditions for the first-real-adapter gate

Before any real adapter (one that performs actual network I/O against a
real source) is activated, in addition to the existing
`docs/connector-security.md` checklist, the following must be resolved —
collecting every item this ADR has, across both correction rounds, deferred
to that gate:

1. **B2's durable fix** — add `security_flags` to
   `intelligence.models.SourceItem` and surface them in the published brief
   for a human reviewer, replacing round 1's fail-closed
   `UnreviewedSecurityFlagsError` substitute.
2. **The bridge's live permission check** (round-2 FUTURE-GATE finding
   above) — `to_source_item` must re-verify the source's permission status
   at bridging time, not rely solely on discovery-time enforcement.
3. **Wiring `may_supply_independence`** (D1 above) into the actual
   independence decision, including how it composes with `cluster.py`'s
   existing subject-membership test, and resolving the mechanical-predicate
   question Fable ruled unsound as a hard reject.
4. **Real SSRF/timeout/redirect/byte enforcement.** `VerificationRequest`'s
   bounds and `redirect_chain_flags`' hop-count check are real, tested logic
   over data a caller supplies, but nothing today executes an actual fetch —
   wiring these fields to a real network call, with a real timeout, a real
   redirect-following policy, and real byte-count enforcement against a live
   response, alongside its own dedicated Fable security review of the fetch
   path itself.

## Deferred

The following are intentionally **not** built in Gate D, so no reviewer or
future implementer is misled by their absence:

- **Coverage reporting is produced but not wired in.** `runner.SourceCoverageReport`
  is its own object; nothing in `connectors` imports or writes to `brief.py`,
  `weekly.py`, or the CLI. Wiring per-run coverage into the published brief is
  explicitly deferred to the gate that ships the first real adapter — ranking
  and rendering are untouched by Gate D end to end.
- **A Gmail pre-persistence exclusion hook is contracted but unimplemented.**
  The permission/retention contracts are general enough to express an
  email-specific exclusion rule before any content is retained, but no such
  hook exists in code yet; it is a future gate's job to implement one against
  a real Gmail-shaped adapter.
- **No real adapter exists.** The seven adapters in
  `connectors.synthetic.adapters` are deterministic, network-free, and exist
  solely to exercise the contract layer, the sanitizer, and the runner's
  per-source isolation against known scenarios (success, timeout, rate limit,
  malicious content, oversized/malformed/unsupported content, revoked
  permission, partial batch).
- **Ranking calibration is untouched.** Nothing in Gate D changes
  `ranking.py`'s weights, formula, or any tier-admission rule from ADR 0004.
- **The admission-policy question in Decision 3 is open, on purpose.** Whether
  `derived_deterministic`/`model_proposed` assessments should ever cross the
  bridge is left to a future Fable review plus a future gate — this ADR
  documents the current fail-closed rule, not a roadmap commitment to change
  it.
- **`SourceRegistryEntry.may_supply_independence` is curated but not yet
  consumed (Gate D round-2, D1).** The registry correctly captures, per
  source, a human's judgment of whether it can supply independent evidence
  — but nothing in `src/` reads the property today; independence is decided
  solely by `cluster.py`'s subject-membership test. Wiring
  `may_supply_independence` into that decision, including how the two
  compose, is deferred to the first-real-adapter gate under Fable review.
  See Decision 5's round-2 correction above for the full statement,
  including Fable's ruling that a mechanical `vendor_first_party AND
  publisher_id not in subject_entity_ids` predicate is unsound as a hard
  reject and must become a reviewable flag instead, never an automatic
  rejection.
- **`VerificationUpgrade` ships as a contract with no application path
  (Gate D round-2, D2).** `models.VerificationUpgrade` is a typed, bounded
  value `ExtractionResult` can carry, but nothing in this codebase applies
  it to an `AuthoredAssessment` before bridging — `bridge.py` has zero
  references to it. Earlier text in this ADR and in `models.py`'s
  docstrings claimed this application "happens in a later commit's
  `bridge.py`"; that commit shipped and no such application code exists.
  Per the orchestrator's decision, this correction round does not add an
  `apply_upgrade`-shaped function (the tree is already security-approved,
  and no unreviewed admission-adjacent code is being added after that
  approval). Wiring `VerificationUpgrade` into the authored-assessment path
  is deferred to the first-real-adapter gate.

## Consequences

- Every future real adapter must satisfy `runner.ConnectorAdapter` (a
  `source_id` attribute plus `discover(request) -> AdapterDiscoveryOutcome`)
  and must route its output through `bridge.to_source_item` — there is no
  second path into the pipeline, by construction.
- A real adapter cannot begin sending anything to a model or to the ranked
  pipeline until it has: a Founder scope decision, a Fable security review, a
  registry entry with a non-`unclassified` classification (if it is to ever
  supply independence), an `approved` permission with explicit
  `permitted_fields`, and a passing run through the sanitizer — the checklist
  is spelled out in `docs/connector-security.md`.
- Because `connectors/` changed zero lines in `intelligence`, `audience`,
  `privacy`, `ingestion`, `providers`, or `cli`, Gate D carries no regression
  risk to any already-shipped behavior; this is a purely additive foundation.

## Alternatives considered

- **Putting `connectors/` inside `sources/`** — rejected. `sources/` is
  private-data-zone metadata inventory with no network path by design;
  merging it with a package whose entire purpose is eventual **public**
  network retrieval would blur two trust boundaries that must stay legible
  and separately reviewable.
- **Letting `DiscoveryResult` carry `publisher_classification` directly** —
  rejected as fail-open. If classification travelled with each retrieved item
  rather than living on the curated `SourceRegistryEntry`, a source could be
  retrieved and its items admitted before any human ever classified it,
  defaulting (in practice) toward treating unknown sources as usable. Keeping
  classification solely on the registry, with `unclassified` failing closed,
  removes that failure mode structurally.
- **Silently stripping fields outside `permitted_fields`** — rejected as an
  invisible mis-windowing risk. A silently dropped `publication_date` produces
  a `DiscoveryResult` that looks complete but sorts and windows incorrectly,
  with no signal to a reviewer that anything was altered. Rejecting the whole
  result and recording an audited reason code is louder but honest.

## Amendment (2026-07-28) — `SUMMARY_MAX_CHARS` retention cap raised 280 → 2000

**Append-only: the "Pinned constants" table above is left exactly as it
shipped and is not a currently-accurate statement of the code; this section
is the current, authoritative correction.**

- **Date:** 2026-07-28.
- **Decider:** the Founder, who requested this change. Security authority
  for the correction is Fable's retention ruling of the same date (referred
  to elsewhere in this codebase's history as the "retention ruling"),
  reviewed and executed under that ruling.
- **Change:** `SUMMARY_MAX_CHARS` (governing `DiscoveryResult.summary_normalized`)
  goes from **280 to 2000**. The constant's single source of truth moved to
  `content_machine.intelligence.models.SUMMARY_RETENTION_MAX_CHARS`;
  `connectors.models.SUMMARY_MAX_CHARS` now re-exports that value so every
  existing import site keeps working, and there is exactly one place this
  number is defined.
- **Reason:** the first real connector run against a live source retained 20
  items, and **every one** had a `summary_normalized` truncated to exactly
  280 characters mid-sentence. That is too little material for the Founder
  to author the `AuthoredAssessment` that `bridge.to_source_item` requires
  (it admits only `human_authored` provenance) — the cap was, in practice,
  starving the human admission gate of the judgment material it depends on,
  which degrades a security-critical control and pushes toward
  rubber-stamping rather than genuine review.
- **Correction to this table's original rationale:** the row above states
  `SUMMARY_MAX_CHARS` was "aligned with
  `intelligence.library.NORMALIZED_SUMMARY_MAX_CHARS` so the two layers
  share one bound." That rationale was mis-motivated: there is no data flow
  between the two fields it names. `library.NORMALIZED_SUMMARY_MAX_CHARS`
  bounds text `build_normalized_summary()` derives locally, from a topic's
  own canonical title and ranking explanation — never a raw connector body.
  The real pair sharing one bound is `DiscoveryResult.summary_normalized` and
  `SourceItem.summary_normalized` — the same string crossing
  `bridge.to_source_item` — and `SourceItem`'s side was, until this
  amendment, unbounded (a bare `str` with no `Field` constraint). The
  "alignment" the original table describes was cosmetic, not structural; the
  actual half-open bound is what this amendment closes.
- **`NORMALIZED_SUMMARY_MAX_CHARS` is deliberately unchanged at 280.** It
  governs a different, locally-derived field with no connection to
  connector-sourced text, and Fable's ruling explicitly left it alone.
- **No detection weakened:** `sanitize_text` runs every detector (steps 1-6)
  before truncating to the cap at the very end of the pipeline; the cap
  never gated detection, so raising it does not change what gets flagged,
  only how much already-sanitized text survives to the retained record.
