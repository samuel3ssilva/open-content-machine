# ADR 0002 — Model provider abstraction

- Status: Accepted
- Date: 2026-07-22
- Decider: fable-principal-architect (Fable)
- Model responsible: Fable

## Context

The product will eventually call LLMs (classification, drafting, revision), and
the leadership uses both Anthropic and OpenAI models. Hard-coding one vendor SDK
into the core would create lock-in, complicate testing, and blur the trust
boundary where personal data could leak into API calls.

## Decision

1. The core depends only on an abstract **`ModelProvider`** protocol in
   `content_machine.providers`: `complete(request) -> ModelResponse`, plus
   `name` and `is_available()`.
2. Three planned implementations: **MockProvider** (deterministic, offline,
   default), **AnthropicProvider**, **OpenAIProvider**. Vendor SDKs are
   imported lazily inside their own module only, and are **optional extras** —
   the base install has no vendor dependency.
3. Provider selection lives in typed config (`CONTENT_MACHINE_PROVIDER`,
   default `mock`). No module other than `providers` may perform network I/O.
4. Every important model interaction uses **structured outputs validated
   against Pydantic schemas**; free-form text responses are not trusted.
5. Data crossing this boundary must first pass `privacy.strip_for_model()`
   (allowlist of fields; never names, emails, URLs). This is trust boundary
   TB-2 in docs/architecture.md.
6. **This sprint ships only MockProvider wired end-to-end.** Real providers are
   stubs that raise a clear "not configured" error; implementing them is a
   future ticket with security review.

## Consequences

- The demo and all tests run with no API key; CI needs no secrets.
- A single choke point exists for auditing what data can reach a vendor.
- Slight indirection cost — acceptable for testability and vendor neutrality.

## Alternatives considered

- **LangChain / agent frameworks** — rejected for now: heavy dependency
  surface, harder to audit, no current need (technical_defaults forbids it
  without proven necessity).
- **Direct Anthropic SDK in core** — rejected: lock-in and a diffuse trust
  boundary.
