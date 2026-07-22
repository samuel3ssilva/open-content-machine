# Release Gates — v0.1.0

Owner: fable-principal-architect / fable-security-auditor.
Status: **OPEN — verdict for the second real run NOT yet issued.**
v0.1.0 may only be tagged when every gate below is verifiably green.

## Scope of the release

First release whose pipeline has been validated against a real, private
connections export processed locally. Ships the hardened deterministic
classifier (family × seniority independence). No LLM classification, no real
providers, no publishing.

## Gate checklist

### G1 — Classification quality (measured, not asserted)

- [ ] HIGH-confidence precision **>= 90%** on the Founder-reviewed private
      sample (aggregate output of `audience evaluate-review`; unknown
      predictions excluded from every precision denominator).
- [ ] No known structural misclassification at HIGH confidence (specifically:
      zero functional director/head/VP/C-level titles routed to
      founder_executive in the reviewed sample and in the labeled fixture).
- [ ] Unknown role-family rate on the real export **targeted < 30%** — with
      the explicit rule that Unknown may NOT be reduced by forced
      classification; if the target is missed honestly, the release decision
      escalates to CEO+Founder with the number stated, rather than loosening
      rules to hit it.
- [ ] At least 4 of the 5 leading candidate segments are meaningfully
      classified (not "Unclassified" clusters).

### G2 — Multilingual coverage

- [ ] Labeled regression fixture >= 250 synthetic titles (PT, EN, mixed,
      accents, abbreviations, "@ company", compound, C-level functional,
      founders/owners, ambiguous, non-conventional).
- [ ] All mandated canonical regressions green (Director of Engineering,
      Head of Data, CTO/CMO, Head of Product, Product Designer, Sales
      Manager/Engineer, People Operations, Consultant, bare Partner).

### G3 — Human review

- [ ] Founder manual review of the 100-row private sample completed
      ("FOUNDER REVIEW COMPLETED") and evaluated locally; aggregate metrics
      recorded in the handoff (no titles, no private paths).
- [ ] Error patterns from the review triaged: each either fixed by rule,
      accepted as documented limitation, or deferred with rationale.

### G4 — Privacy and security

- [ ] Full privacy test suite green (sentinel leak tests, gitignore coverage,
      no-PII-in-logs, source-not-copied, network-call blocking).
- [ ] `git status` clean of any real-data artifact; no private file tracked;
      release security checklist (SECURITY.md) all PASS.
- [ ] Private outputs exist only in the external private directory with
      restrictive permissions.
- [ ] Committed evaluation/comparison artifacts contain aggregates only —
      no titles, no pseudonym ids, no private paths.

### G5 — Engineering gates

- [ ] pytest, ruff, mypy green locally; CI green on GitHub Actions at the
      release commit.
- [ ] Evaluation harness (`evaluate-review`) and classifier comparison
      (`compare-classifiers`) implemented, tested, and documented.
- [ ] Golden files reviewed by Fable: changes are expected consequences of
      the classifier correction, contain no private data, and no test
      expectation was altered merely to pass. *(Reviewed and approved for
      commit `33b5aa8`; re-review required if goldens change again.)*

### G6 — Second real local run (blocked until G1–G5 ready + approvals)

- [ ] Fable verdict **APPROVED FOR SECOND REAL LOCAL RUN** issued only after:
      synthetic metrics green, Founder review metrics computed, rule review
      finding no unjustified-inference pattern.
- [ ] Founder authorization for the second run received in chat.
- [ ] Second run executed per docs/real-data-runbook.md (same salt for stable
      ids); before/after comparison produced as aggregates only
      (unknown reduction, distribution deltas, reclassification counts,
      segment changes).
- [ ] Post-run: repo clean, PII scans of new outputs pass, tests still green.

### G7 — Final approvals

- [ ] Fable release audit: APPROVED FOR RELEASE v0.1.0.
- [ ] Founder approval to tag and publish v0.1.0 (no real-data content in the
      release; release notes state aggregate results only with Founder
      consent).

## Anti-gaming rules (binding)

1. Precision metrics never include unknown predictions in denominators;
   unknown_rate is reported separately. Improving one must not silently
   degrade the other — both are shown side by side in every report.
2. Rule additions must carry evidence-tier justification; mapping ambiguous
   or seniority-only tokens to a family to lower Unknown is a review blocker.
3. Golden/test expectation changes require Fable review with diff-level
   justification.
4. If any gate fails, the failure is reported as-is; gates are never
   reworded to fit the result.
