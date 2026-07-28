# ADR 0006 — Private Endpoint Configuration Loader (Gate E0 §5)

- Status: Accepted
- Date: 2026-07-27
- Decider: Founder (scope authorization); implementation recorded by Sonnet
- Model responsible: Sonnet

## Context

`connectors/network.py` (ADR 0005, Gate E0.4) enforces a per-source hostname
allowlist (`NetworkFetcher.__init__`'s `source_allowed_hosts`), but nothing
in the repository could populate that allowlist from a REAL source, because
this is an open-source repository and a real source's hostname, endpoint,
and authorization state must never be committed to it (`CLAUDE.md` hard
privacy rule 1/2, `SECURITY.md`). Those real values live in the Founder's
private workspace, outside this repository, in a file this codebase must
never read at build time and must never see the content of.

At the same time, `docs/architecture.md` §2 confines all environment-variable
reads to `content_machine.config` — enforced by a static AST scan
(`tests/test_architecture_env_boundary.py`, Gate E0 R8) — so a loader outside
that package cannot simply call `os.getenv(...)` to find the private file's
path.

Separately, this ticket surfaced a concrete instance of a general pydantic v2
hazard (Fable ruling F6): `pydantic.ValidationError`'s default `str()`/`repr()`
embeds the OFFENDING INPUT VALUE (truncated only above roughly 48 characters,
verified directly against this repository's pinned pydantic version).
Applied to this module's contract, an invalid private config would place the
real hostname or endpoint into whatever caught and logged, printed, or
re-raised that exception — including CI output, if CI ever exercised a real
config (it does not; see below).

## Decision

### 1. Contract and loader ship in the public repo; real values never do

`content_machine.connectors.private_config` ships a pydantic contract
(`PrivateSourceEndpoint`/`PrivateSourceConfig`), a loader
(`load_private_source_config`), and a mapping helper
(`source_allowed_hosts_from_config`) that produces exactly the
`source_allowed_hosts` shape `NetworkFetcher`'s constructor already accepts.
Every example value in this module's own test file is
`example.com`/`example.org`-shaped and synthetic. The actual source id,
hostname, endpoint, limits, permission record, and authorization state for
any real connector source live in a file outside this repository; this
module does not create that file and never reads its content ahead of time
— only at runtime, from a path it is told about.

### 2. Path resolution goes through `config/` only — never `os.environ` directly

A new `Settings.private_source_config_path: Path | None` field (default
`None`) is the only place this path can come from `.env`/the environment
(`CONTENT_MACHINE_PRIVATE_SOURCE_CONFIG_PATH`). `load_private_source_config`
accepts either an already-constructed `Settings` instance or an explicit
`path` argument — it never imports `os` and never touches
`os.environ`/`os.getenv` itself, so the existing AST scan's boundary is
honored by construction, not by convention.

### 3. Fail closed, one distinct exception per invariant, never a silent default

Four fail-closed outcomes, each its own exception type under a common
`PrivateConfigError` base (mirroring the "one distinct code per invariant,
never collapsed" pattern already used by
`permissions.AuthorizationReasonCode` and `network._FetchReasonCode`):

- `MissingPrivateConfigError` — no path configured, or the configured path
  does not exist / is not a file. **This is the only path CI's test suite
  exercises** — `private_source_config_path` defaults to `None` and no test
  in the repository points it at a real file, so CI never requires a
  private config to exist.
- `UnreadablePrivateConfigError` — the path exists but could not be read.
- `InvalidPrivateConfigError` — the content is not valid JSON, or fails the
  contract's pydantic validation.
- `ExpiredPrivateConfigError` — at least one configured source's
  authorization (`authorized` / `authorized_until`) was not valid as of load
  time. One expired source fails the WHOLE load closed — never a silent
  per-source drop that lets the rest of the file's sources through
  unnoticed, matching the existing "reject, never silently strip" posture
  (`permissions.PermissionRegistry.enforce_discovery_fields`).

There is no default or fallback endpoint anywhere in this code path.

### 4. F6: sanitize before it leaves this module's hands, and hold secrets as `SecretStr`

Every raw exception this module catches while loading (`OSError`,
`json.JSONDecodeError`, `pydantic.ValidationError`) is reduced to
`sanitize_error(exc)` — reusing the existing sanitizer in
`connectors/sanitize.py` rather than inventing a second one — before being
attached to the `PrivateConfigError` subclass this module raises instead.
The raw exception itself is never re-raised, chained visibly, or logged.

Independently of that, `PrivateSourceEndpoint.hostname` and `.endpoint` are
typed `pydantic.SecretStr`, which pydantic itself masks in `repr()`,
`str()`, `model_dump()`, and `model_dump_json()` — a belt-and-suspenders
control so that even a caller who accidentally logs a whole config object
(bypassing this module's own sanitized-exception path entirely) still
cannot leak the real value that way.

### 5. Minimal wiring to `NetworkFetcher`

`source_allowed_hosts_from_config` produces a plain `dict[str, frozenset[str]]`
in the shape `NetworkFetcher.__init__`'s `source_allowed_hosts` parameter
already accepts. Nothing in `network.py` changes, and this ADR does not
extend the wiring to `NetworkFetcher`'s other constructor arguments (e.g.
per-source rate limits, which the contract captures as data but does not yet
drive) — that is left to a later, separately-scoped ticket.

## Consequences

- A real connector source can now be authorized to use `NetworkFetcher`
  without any real value ever entering this repository, by pointing
  `CONTENT_MACHINE_PRIVATE_SOURCE_CONFIG_PATH` at a file in the Founder's
  private workspace.
- CI's coverage of this module is necessarily limited to the fail-closed
  paths (missing/unreadable/invalid/expired) plus synthetic valid-config
  tests — it can never exercise a real config, by design.
- The `SecretStr` control and the sanitized-exception control are
  independent and overlapping (defense in depth): either one alone would
  already prevent the F6 leak this ADR addresses in this module's own code
  paths, but neither claims to be a general-purpose secret-scrubbing
  system, and no other module is exempted from choosing its own care when
  handling a `PrivateSourceEndpoint` it obtains from this loader.

## Alternatives considered

- **Read the private config path from `os.environ` inside this module
  directly.** Rejected: violates the existing, tested "only `config/` reads
  the environment" architecture rule for no real benefit — `Settings`
  already exists for exactly this purpose.
- **Let a raw `pydantic.ValidationError` propagate and rely on callers to
  sanitize it themselves.** Rejected: every caller would have to remember
  to do that correctly, every time, for a value this codebase treats as
  security-sensitive; centralizing the sanitized re-raise in the loader
  itself means the leak is closed once, not re-solved at every call site.
- **Silently drop only the expired source and continue loading the rest.**
  Rejected: a silent partial success is exactly the failure mode
  `permissions.PermissionRegistry.enforce_discovery_fields` already chose
  against for a different invariant ("a silently stripped
  `publication_date` would mis-window an item with no visible symptom") —
  the same reasoning applies here: a silently dropped source is a retrieval
  gap nobody is told about.
