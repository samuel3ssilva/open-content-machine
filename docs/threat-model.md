# Threat Model

Owner: fable-security-auditor. Revisit at every new module, integration, or
trust-boundary change. Method: lightweight STRIDE over the data-flow in
`docs/architecture.md` §3–4.

## Assets

- **A1** Real connections export(s) in `data/private/` (names, emails, URLs).
- **A2** The private salt and any API keys in `.env`.
- **A3** Anonymized outputs and reports (sensitive-by-default aggregates).
- **A4** The public repository's integrity and reputation.
- **A5** The Founder's accounts (GitHub, future platform accounts).
- **A6** Any future connector's target source, its retrieved content, and the
  run that processes it (asset introduced by Gate D; see ADR 0005).

## Actors

- **Founder/user** (trusted, may make mistakes).
- **Contributors / PR authors** (semi-trusted; code review gate).
- **Public readers** of the repo (untrusted).
- **Model vendors** (honest-but-curious; must never receive identifiers).
- **AI agents (Fable/Opus/Sonnet)** — treated as fallible operators: they get
  least privilege, never see `data/private/`, and their output is reviewed.

## Threats and controls

| ID | Threat | Asset | Controls (implemented this sprint unless noted) |
|----|--------|-------|--------------------------------------------------|
| T1 | Real CSV accidentally committed and pushed | A1, A4 | `.gitignore` (`data/private/*`, `*connections*.csv`); privacy test that paths are ignored; pre-push checklist in SECURITY.md; audit before first push |
| T2 | Secrets committed (`.env`, keys) | A2 | `.gitignore`; `.env.example` with empty values; secret-scan step in checklist; CI needs no secrets |
| T3 | PII leaks into anonymized output or report | A1, A3 | Allowlist-based anonymizer (removal, not masking); privacy tests grep outputs for emails/URLs/fixture names |
| T4 | PII sent to a model vendor | A1 | No real provider implemented this sprint; TB-2 choke point `strip_for_model()`; providers module is the only network-capable code |
| T5 | Pseudonyms reversed from a leaked report | A3 | HMAC with private salt (ADR 0003), not plain hashes; direct identifiers absent entirely; reports aggregate ≥ top-N only |
| T6 | PII or secrets in logs/tracebacks | A1, A2 | Error-message policy (row/column refs only); tests assert no personal values in CLI output on failure paths |
| T7 | Malicious or typosquatted dependency | A4, A2 | Minimal dependency set (typer, pydantic, pytest, ruff, mypy); new deps require review per agent rules |
| T8 | Malicious PR (backdoor, exfil in providers/) | A4 | Human + Opus review; CI runs offline; any network code outside `providers/` is a review blocker |
| T9 | Prompt injection via data files (a CSV cell containing instructions to an agent/model) | A1, A3 | Data is treated as data: deterministic pipeline this sprint; future model calls receive only normalized short fields (company/title), never free text, with structured outputs |
| T10 | Agent overreach (an AI agent reads private data or pushes) | A1, A4 | Agent definitions forbid `data/private/` and force-push; no bypassPermissions; integration only through reviewed commits |
| T11 | Loss of salt → broken longitudinal IDs | A3 | Documented recovery stance in ADR 0003 (source CSV is ground truth); warning when running with ephemeral salt |
| T12 | Small-population re-identification in published aggregates | A3 | Reports are for the user, not auto-published; build-in-public guidance forbids publishing raw aggregates; future: k-anonymity floor before any sharing feature |
| T13 | Source poisoning / hostile content from a retrieved artifact | A3, A4, A6 | Every source is treated as hostile data (`connectors.sanitize`); markup neutralized, control characters stripped, length capped; sanitized output is inert data with no execution path and never reaches a model boundary in this gate |
| T14 | Prompt injection via retrieved text (instructions embedded in a feed item) | A3, A6 | `sanitize.sanitize_text` flags `instruction_shaped_text` heuristically but leaves the text in place as inert data — the real control is architectural: connector output cannot reach a model boundary in Gate D at all, and only reaches the pipeline through the human-authored `bridge.to_source_item` choke point (TB-4). No claim of semantic detection is made — see the honesty subsection below |
| T15 | SSRF / redirect-chain abuse via a malicious or compromised source | A4, A6 | **Gate E0 (E0.4) update — this row was previously true and is no longer.** As of E0.4, `connectors/network.py` (`NetworkFetcher`, the module's one public name) is the enforced fetch boundary, and every fetch through it enforces, unconditionally: HTTPS-only scheme; rejection of credential-bearing URLs; a **per-source** hostname allowlist (never a single global pool, so a hostile discovery result from one source can never point retrieval at a different source's allowed host — Fable F3.3); blocking of loopback, private, link-local, unspecified, and multicast addresses, and of any URL whose hostname is itself an IP literal (Fable F3.2 — the classifier is pure, injectable, and unit-tested against a hand-written literal address table independent of its own implementation constants; the shipped default never special-cases `127.0.0.1`/`localhost`); an allowed-ports set; redirects handled entirely by hand (never library auto-follow) and bounded, with every hop re-validated through the exact same host-allowlist and address checks as the original request; DNS-rebinding defense (Fable F3.1) by resolving a hostname exactly once per hop and pinning the connection to that single vetted literal IP address — with TLS certificate verification still performed against the ORIGINAL hostname, never the pinned IP, so pinning the socket does not weaken what the certificate is checked against; separate connect/read timeouts; a streaming byte cap that aborts mid-download rather than after buffering a full oversized body; a MIME allowlist; per-source rate limiting; and a live, immediately-before-retrieval permission check (`PermissionRegistry.authorize_retrieval`) so a permission revoked or expired after an earlier discovery/planning step is still caught at the point of actual retrieval. No adapter in this repository is wired to call `NetworkFetcher` yet — this row now describes an enforced, tested boundary with no caller, not the absence of fetch code. |
| T16 | Oversized content / resource exhaustion from one source | A4, A6 | `DEFAULT_MAX_BYTES` (2,000,000), `DEFAULT_TIMEOUT_SECONDS` (20), `DEFAULT_MAX_ITEMS_PER_SOURCE` (50), and `DEFAULT_MAX_REQUESTS_PER_RUN` (200) bound one source's and one run's resource footprint; `sanitize_text` truncates and flags `oversized_truncated` |
| T17 | Credential leakage into logs, outputs, or prompts | A2, A6 | `sanitize.sanitize_error` discards an exception's own message entirely (type name only); `sanitize_text` detects and redacts credential-shaped substrings (`credential_shaped_text`); no credential of any kind exists in this gate — no real adapter, no secret store integration |
| T18 | Retention violation: a raw retrieved body ends up persisted | A1, A3, A6 | `DiscoveryResult` has no body field at all — this part remains structurally impossible to populate, since the field does not exist on the model. Disposal is a separate, WEAKER claim, corrected in Gate D round-1 (C5) after a Fable review: `TemporaryContentHandle` holds content in a mutable buffer and raises `ContentDisposedError` on post-disposal access; `dispose()`/`minimize()` overwrite the buffer in place before dropping the handle's own reference, so a caller-held view obtained before disposal reflects the scrub afterward too — but this is an active overwrite-and-drop, not a proof of erasure: it does not reach a `bytes` copy a caller already extracted (e.g. via `bytes(handle.content)`), and CPython cannot prove no other reference (a debugger, a live traceback frame, a C extension) exists. See ADR 0005 §7's round-1 correction |
| T19 | Permission bypass: a suspended or revoked source's adapter still executes, or an adapter misrepresents which source its results belong to | A4, A6 | `PermissionRegistry.authorize` is fail-closed with a distinct reason code per invariant (`status_suspended`, `status_revoked`, `mode_mismatch`, `not_registered`); `run_discovery` checks authorization (and, since Gate D round-1, curated `source_registry` membership) before ever calling an adapter, so a LOCALLY suspended/revoked/unregistered source's own adapter is never invoked. **Gate D round-1 correction (B1):** the pre-fix guarantee was narrower than this row previously stated — it covered only "that source's own adapter is never invoked," not whether an adapter APPROVED under one `source_id` could misrepresent its results as belonging to a DIFFERENT (e.g. suspended/revoked) `source_id`; a proven probe showed it could, and `enforce_discovery_fields` would apply the wrong source's permission entirely. `run_discovery` now independently rejects the WHOLE source (zero results, a dedicated `FailureKind.source_id_mismatch` failure) if any result it returns claims a `source_id` other than the invoking adapter's own, and reconstructs `permission_ref` from the authorized registry rather than trusting the adapter's self-report |
| T20 | One bad source corrupts or aborts a whole weekly run | A4 | `run_discovery` isolates every adapter: any exception from one adapter is recorded as a single `SourceFailure` for that source only, and every other source's results are unaffected; a batch is never all-or-nothing by construction (`BatchStatus.partial` when some sources fail and others succeed) |
| T21 | Future-Gmail-specific exposure (mailbox content, personal correspondence) | A1, A6 | No Gmail or email adapter exists in this gate; a pre-persistence exclusion hook is contracted for but unimplemented (see ADR 0005 "Deferred"); any real email connector requires its own Fable privacy review before activation, given the qualitatively higher sensitivity of mailbox content versus a public feed |

### Connector control honesty (T13–T21)

- **Prevented, structurally:** a `DiscoveryResult` can never carry a raw body
  — no such field exists on the model, so this is the one claim in this list
  that is structural in the strict sense (no validator to bypass, because
  there is nothing to validate).
- **Prevented, by construction plus immutability (Gate D round-1 correction
  — NOT "structural," see ADR 0005's round-1 findings):** length caps, the
  `content_type` allowlist, and `canonical_reference`'s scheme/control-
  character checks are enforced by a validator that runs at construction,
  and every contract model is now `frozen=True` so a later
  `model_copy(update=...)`/attribute-assignment cannot set a field to an
  out-of-bound value without re-running that validator. This is enforced by
  two mechanisms working together, not a guarantee that no out-of-bound
  instance can ever exist in principle — `model_construct()` still bypasses
  validation entirely, as it does for every Pydantic model. A source outside
  `permitted_fields` is rejected (not stripped) by the same
  construction-time logic, not by any structural absence of the field.
- **Detected, and now traceable across disposal:** a disposed
  `TemporaryContentHandle` can never be read again (raises); heuristic
  markers a human reviewer acts on — `instruction_shaped_text`,
  `credential_shaped_text`, `email_shaped_text`,
  `filesystem_path_shaped_text`, `malformed_encoding` (which, since Gate D
  round-1's C2 fix, also covers bidi-override/invisible-character
  deception), `duplicate_canonical_reference`, `conflicting_publication_date`.
  Gate D round-1 (B2) additionally fails closed at `bridge.to_source_item`
  on `instruction_shaped_text`/`malformed_encoding` unless a human explicitly
  reviews and names them — see ADR 0005's round-1 findings section.
- **Mitigated:** bounded, not eliminated — request/byte/redirect/item/run
  ceilings reduce blast radius without claiming to prevent every abuse of a
  hostile source.
- **Accepted:** Gate D ships no real adapter, so today the residual risk of
  T13–T20 is zero in practice; the controls above are the ones a real adapter
  will inherit, not evidence that a real adapter is risk-free.
- **Deferred:** semantic prompt-injection detection is not attempted and never
  claimed. **There is no claim of perfect, or even reliable, semantic
  detection anywhere in this layer.** The actual control is architectural:
  sanitized connector output is inert data with no instruction-execution
  channel, cannot reach a model boundary in this gate, and can only reach the
  M1–M7 pipeline through the authored, human-provenance bridge (TB-4).
  `SecurityFlag` markers are heuristic and traceable for a human reviewer —
  they are a marker, not a guarantee.

## Non-threats (out of scope, by design)

- Multi-user access control — single-user local tool.
- Server hardening — there is no server.
- DoS — nothing is exposed.

## Standing rules for agents and contributors

1. Never open `data/private/` contents in any tool or model context.
2. Never paste tokens/keys into chats, code, or issues.
3. Anything that adds network I/O, a dependency, or a new data field crossing
   TB-2 is security-relevant → Fable review required.
