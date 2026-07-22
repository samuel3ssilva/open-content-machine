# Role classification

How `content_machine.audience.classify.classify_role` turns a job-title
string into a coarse professional *family*, and how `infer_seniority`
(`content_machine.audience.normalize`) independently derives a *level* from
the same title. Both are plain deterministic local code — no model calls, no
network I/O (`docs/privacy.md` rule 3/4) — and both consume only the
(already-normalized) position string, never a name, email, or profile URL
(`docs/privacy.md` rule 6).

This document explains the model for engineers extending the vocabulary
tables and for anyone auditing a classification decision. It does not itself
contain any real person's data — every example title below is invented.

## Family vs. seniority

*Family* is the professional **function** (engineering, marketing, sales,
...). *Seniority* is the **level** (individual contributor, manager, VP,
C-level, ...). They are parsed independently from the same normalized title
by two separate rule tables, and a seniority word must never, on its own,
decide a family. Concretely: `founder_executive` is reserved for general
executive leadership / ownership (founder, CEO, managing partner, ...); a
"Director of Engineering" or "Head of Product" keeps its **function**
(engineering, product) with a director/head **seniority** extracted
separately. See `test_family_and_seniority_are_independent` in
`tests/test_classify.py` for the canonical worked examples.

## The seven-tier precedence model

`classify_role` evaluates ordered tiers; the **first tier that produces any
hit wins**, and no later tier can override it:

| Tier | Name | What it catches | Confidence |
|------|------|------------------|------------|
| T0 | Ownership override | founder/co-founder/owner/dono(a) tokens | high |
| T1 | Exact/phrase functional | multi-word functional phrases and C-suite acronyms | high, or medium if hedged |
| T2 | Strong domain keywords | single unambiguous functional tokens | high |
| T3 | Recognized professions | professions outside the listed families (physician, jornalista, ...) | high |
| T4 | General executive/owner-lite | CEO, president, managing director, empreendedor, ... | high or medium |
| T5 | Weak/ambiguous | lone generic tokens (analyst, consultant, coordinator, ...) | low |
| T6 | Unknown | nothing matched, or a seniority-only title | unknown |

A compound title whose keywords span two or more families at the *same*
tier resolves to the highest-priority family (`_FAMILY_PRIORITY` in
`classify.py`), with confidence downgraded to `medium` — e.g. "Data
Scientist and Product Manager" → `engineering_data_ai` (engineering outranks
product), medium.

Ownership (T0) dominates everything, including a compound like "Founder &
CTO" or the reversed "CTO e Fundador" — both resolve to `founder_executive`,
high, regardless of the functional term alongside it.

## Confidence semantics

- **high** — an unambiguous functional match (T1/T2 with a single family), a
  recognized profession (T3), or an unambiguous executive term (CEO, T4).
- **medium** — a compound functional title (≥2 families), a deliberately
  hedged functional rule (e.g. "Sales Engineer" → sales, "Scrum Master" →
  engineering), or a genuinely ambiguous C-level acronym (CRO, CPO, CDO — see
  the decision table below).
- **low** — weak/incomplete evidence: a lone generic token or abbreviation
  with no domain to disambiguate (e.g. bare "Analyst").
- **unknown** — empty title, or no rule fired. **Never forced**: a title
  with zero evidence, or a seniority-only title ("Director" alone), is left
  `unknown` rather than guessed into a family.

`other` is reserved for two cases: a clearly-recognized profession not in
the listed families (T3, high — e.g. "Physician", "Piloto"), and a
recognized-but-non-functional term with no domain (T5, low — bare
"Consultant"/"Partner"/"Sócio").

## Metric-integrity rule

The evaluation harness (`content_machine.audience.evaluate`) must never let
a classifier get *more* credit for forcing an ambiguous title into a family
instead of leaving it `unknown`:

- `unknown` predictions are **excluded** from every precision denominator
  and surfaced separately as `unknown_rate`.
- `overall_classified_precision` is computed only over rows the classifier
  actually placed (`predicted family != unknown`).
- `high_confidence_precision` is computed only over `high`-confidence rows
  (never `unknown` by construction).

So driving ambiguous titles out of `unknown` cannot inflate precision — it
can only raise `unknown_rate`, or (if the forced guess is wrong) lower
precision. This is why the vocabulary-expansion rule for this module is
**"when unsure, leave it out"**: an `unknown` prediction is acceptable; a
wrong, forced one is not.

## Running the evaluation harness

```bash
.venv/bin/python -c "
from content_machine.audience.evaluate import evaluate_csv
report = evaluate_csv('tests/fixtures/labeled_titles_synthetic.csv')
print(report.model_dump_json(indent=2))
"
```

`evaluate_csv(path)` loads a labeled CSV (columns: `title`,
`expected_family`, `expected_seniority` — synthetic titles only) and returns
an `EvaluationReport`: `n`, `high_confidence_precision`,
`overall_classified_precision`, `unknown_rate`, family/seniority confusion
matrices, and aggregated `top_error_patterns`. The report is aggregate by
construction — it holds only family/seniority labels and counts, **never a
raw title** (`docs/privacy.md` rule 6), so a committed report can never leak
a fixture title. `tests/test_evaluate.py::test_report_contains_no_raw_titles`
enforces this.

To run it against your own labeled sample, point `evaluate_csv` at any CSV
with the three required columns. **Never point it at `data/private/` or a
real export** — this harness and its fixtures are for synthetic regression
testing only; see `docs/real-data-runbook.md` for the separate, gated
procedure for real data.

## Documented edge-case decisions (Sprint 1.1)

Each row below has a dedicated regression test in `tests/test_classify.py`
(search for the row's keyword in the test name) so a future vocabulary
change cannot silently flip the decision.

| Title pattern | Family | Seniority impact | Confidence | Rationale |
|---|---|---|---|---|
| `Product Owner` | product | — | high | "PO" stays product per convention; bare `po` deliberately **not** mapped (Purchase Order / too ambiguous). |
| `Scrum Master`, `Agile Coach` | engineering_data_ai | — | medium | Delivery-facilitation roles, not core engineering — but overwhelmingly embedded in software delivery teams for this audience. Hedged like "Sales Engineer". |
| `CRO` (abbreviation) | sales_bd_partnerships | — | **medium** | Ambiguous: Chief *Revenue* Officer (sales) vs Chief *Risk* Officer (finance). Revenue reading chosen, never high. |
| `Chief Revenue Officer` (spelled out) | sales_bd_partnerships | — | high | Unambiguous once spelled out — same asymmetry as CDO below. |
| `CPO` (abbreviation) | product | — | **medium** | Ambiguous: Chief *Product* Officer vs Chief *People* Officer vs Chief *Privacy* Officer. Product reading chosen given this app's tech/startup audience, never high. |
| `CDO` (abbreviation) | engineering_data_ai | — | **medium** | Ambiguous: Chief *Data* Officer vs Chief *Digital* Officer. Both readings are technology-adjacent, so still routed here, but never high. |
| `Chief Data Officer` (spelled out) | engineering_data_ai | — | high | Unambiguous once spelled out. |
| `CHRO` | operations_people_finance_legal | — | high | Unambiguous (Chief Human Resources Officer); no ambiguity to hedge. |
| `Jornalista` | other | — | high | Pure journalism is a distinct recognized profession (T3), kept separate from content/social-media roles, which stay `marketing_growth_content`. |
| `Product Marketing` | marketing_growth_content | — | high | Explicitly marketing, not product, per ticket decision. |
| `Cientista` (bare) | **unknown** | — | unknown | Genuinely ambiguous (data / political / physical scientist, ...) — deliberately left unmapped. `Cientista de Dados` (qualified) is unaffected and stays high engineering. |
| `Especialista` (bare) | **unknown** | — | unknown | No domain named — too generic to map to any family. |
| `Fiscal` (PT, bare) | **unknown** | — | unknown | Genuinely ambiguous between tax/finance ("Auditor Fiscal") and a generic inspector role ("Fiscal de Trânsito") unrelated to finance. |
| `Arquiteto`/`Arquiteta` (bare) | other | — | high | Reads as a building architect (T3) — only reached because it comes after T1's `arquiteto de software/soluções/dados/cloud` phrases, so `Arquiteto de Software` still resolves to engineering first. |
| `Dono`/`Dona` (PT "owner") | founder_executive | — | high | Mirrors the existing English "owner" false-friend guard. `Dono de Produto/Processo/Serviço` (role stewardship, PT for product/process/service owner) is **excluded** and falls through to its function (e.g. `product`) instead. |
| `Empreendedor(a)` | founder_executive | — | **medium** | Weaker evidence than an explicit founder/owner claim — a self-identifier that doesn't always denote a formal ownership stake, so it lives in T4, not T0. |
| `Qualidade` (PT, bare) | operations_people_finance_legal | — | high | Bare PT usage overwhelmingly means manufacturing/process quality control, not software QA — software QA is covered separately via `qa`/`quality assurance`/`qa engineer`. |
| `BI` (bare abbreviation) | *(not mapped)* | — | — | Deliberately left unexpanded (too ambiguous as a 2-letter token). Phrase-level context is different: `Analista de BI`/`BI Analyst` are explicit T1 phrase rules (engineering_data_ai, high) because the full phrase is unambiguous. |
| `Tech Lead` | engineering_data_ai | — | high | Common, unambiguous mixed-language title; safe as a 2-word phrase (cannot fire on unrelated "tech"-prefixed words like fintech/biotech). |
| `Apaixonado por Tecnologia` / `Passionate about Technology` | unknown | — | unknown | Fable ruling (Sprint 1.1): enthusiasm clauses ("apaixonado por X", "passionate about X", "entusiasta de X"...) carry no functional evidence and are discarded before matching. A title that is only enthusiasm stays `unknown`; a real function ahead of the clause ("Engenheira de Software apaixonada por dados") still classifies normally. |
| bare `Analista` / `Analyst` | unknown | individual_contributor | unknown | Fable ruling (Sprint 1.1): cross-domain token (finance/ops/marketing as often as data) — mapping it to a family was unjustified inference. Qualified forms (`Analista de Dados`, `Analista Financeiro`...) classify via their domain. |
| bare `Coordenador` / `Coordinator` | unknown | manager_lead | unknown | Fable ruling (Sprint 1.1): seniority word, not a function; family stays unknown while seniority buckets independently. |

## Extending the vocabulary tables

Add new entries to the existing `_T0_OWNERSHIP` / `_T1_FUNCTIONAL_PHRASES` /
`_T2_STRONG_DOMAIN` / `_T3_PROFESSIONS` / `_T4_EXECUTIVE` / `_T5_WEAK` tuples
in `classify.py` — do not restructure the tier engine itself (that is
Opus/Fable-level design work; see `docs/model-routing.md`). Rules:

1. Pick the tier that matches the evidence strength (see the table above),
   not the tier that produces the outcome you want.
2. Never map a seniority-only or genuinely ambiguous token to a family. If
   you are unsure, leave it out — `unknown` is an acceptable, honest result;
   a forced, wrong classification is not (this is exactly what the
   metric-integrity rule above protects, and what Fable audits).
3. Add a dedicated regression test for any non-obvious decision, following
   the pattern in the "Sprint 1.1 vocabulary-expansion decisions" section of
   `tests/test_classify.py`.
4. Add labeled rows to `tests/fixtures/labeled_titles_synthetic.csv`
   (synthetic titles only) and re-run the evaluation harness to confirm the
   quality bar still holds (`high_confidence_precision >= 0.90`,
   `overall_classified_precision >= 0.90`, `unknown_rate < 0.25`).
5. If your change alters classification of any title used in
   `examples/synthetic-connections.csv`, regenerate
   `examples/expected-output/report.md`/`.json` with the canonical salt
   (`CONTENT_MACHINE_SALT=open-content-machine-canonical-example-salt`, see
   `tests/test_golden_outputs.py`) and PII-scan the result before
   committing.
