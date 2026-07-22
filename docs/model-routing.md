# Model Routing

How engineering work is divided between Claude models. The CTO (Claude, running
as Fable in the lead session) classifies every task before executing.

## Classification criteria

Data sensitivity · architectural impact · modules affected · ambiguity ·
reversibility · blast radius · debugging complexity · cost of a mistake.

## Routing table

| Model | Agents | Use for | Never for |
|---|---|---|---|
| **Fable 5** | fable-principal-architect, fable-security-auditor | Systemic architecture, threat modeling, privacy governance, trust boundaries, irreversible/transversal decisions, release audits, hard root-cause work | Boilerplate, formatting, simple docs, trivial tests, renames, scaffolding — and it must never receive raw CSVs, names, emails, URLs, tokens, or private Vault data |
| **Opus 4.8** | opus-tech-lead, opus-data-ai-engineer | Bounded-but-complex problems: module/interface design, provider layer, pipelines, central schemas, reviewing/integrating Sonnet's work, difficult bugs | Decisions that create new trust boundaries or irreversible commitments (escalate to Fable) |
| **Sonnet 5** | sonnet-implementation-engineer, sonnet-qa-docs | Daily execution: well-specified tickets, CLI commands, validators, transforms, fixtures, tests, CI, docs, changelog, releases | Core architecture, privacy policy, permission model, public contracts, paid infra, security-relevant changes |

## Escalation paths

- Sonnet → Opus: ambiguity, undefined contract, failing integration.
- Opus → Fable: data-leak risk, core architecture change, irreversible decision,
  new trust boundary, security modification, unresolvable ambiguity.

## Accountability

Every issue, PR, and execution report records the responsible model
(`Model responsible: Fable | Opus | Sonnet`). The Bootstrap Sprint allocation is
recorded in `docs/HANDOFF_TO_CEO.md`.
