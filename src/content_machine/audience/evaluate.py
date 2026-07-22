"""Deterministic evaluation harness for the role-family / seniority classifier.

Plain local code (no model calls). Given a set of hand-labeled titles it reports
precision, an unknown rate, confusion matrices, and aggregated error patterns so
regressions in :func:`content_machine.audience.classify.classify_role` and
:func:`content_machine.audience.normalize.infer_seniority` are measurable rather
than anecdotal.

Privacy: the labeled inputs are SYNTHETIC titles only. The report is aggregate
by construction -- ``top_error_patterns`` and the confusion cells hold only
family/seniority labels and counts, NEVER a raw title, so a committed
:class:`EvaluationReport` can never carry a title value (docs/privacy.md rule 6).

Metric integrity (ticket OPUS-1.1 §7, audited by Fable)
-------------------------------------------------------
Precision must never reward *forcing* a genuinely ambiguous title into a family:

* ``unknown`` predictions are EXCLUDED from every precision denominator and are
  surfaced separately as ``unknown_rate``.
* ``overall_classified_precision`` is computed only over rows the classifier
  actually placed (predicted family != ``unknown``).
* ``high_confidence_precision`` is computed only over high-confidence rows
  (which are never ``unknown`` by construction).

So driving ambiguous titles out of ``unknown`` cannot inflate precision -- it
can only raise ``unknown_rate`` or, if the forced guess is wrong, lower
precision.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.classify import Confidence, RoleFamily, classify_role
from content_machine.audience.normalize import infer_seniority, strip_accents

_UNKNOWN = "unknown"


class LabeledTitle(BaseModel):
    """One synthetic, hand-labeled evaluation example."""

    model_config = ConfigDict(extra="forbid")

    title: str
    expected_family: str
    expected_seniority: str


class ConfusionCell(BaseModel):
    """A single (expected, predicted) cell of a confusion matrix with its count."""

    model_config = ConfigDict(extra="forbid")

    expected: str
    predicted: str
    count: int


class ErrorPattern(BaseModel):
    """An aggregated expected->predicted family error and how often it occurs.

    Holds only family labels and a count -- never a raw title -- so it is safe to
    commit and to surface in reports.
    """

    model_config = ConfigDict(extra="forbid")

    expected: str
    predicted: str
    count: int


class EvaluationReport(BaseModel):
    """Aggregate quality metrics over a labeled set. No raw titles by design."""

    model_config = ConfigDict(extra="forbid")

    n: int
    high_confidence_precision: float
    overall_classified_precision: float
    unknown_rate: float
    family_confusion: list[ConfusionCell] = Field(default_factory=list)
    seniority_confusion: list[ConfusionCell] = Field(default_factory=list)
    top_error_patterns: list[ErrorPattern] = Field(default_factory=list)


def _confusion(pairs: Counter[tuple[str, str]]) -> list[ConfusionCell]:
    """Deterministically order confusion cells: by count desc, then labels."""
    ordered = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return [
        ConfusionCell(expected=exp, predicted=pred, count=count)
        for (exp, pred), count in ordered
    ]


def evaluate(labeled: list[LabeledTitle]) -> EvaluationReport:
    """Compute classification metrics over ``labeled``.

    Deterministic and pure. See the module docstring for the metric-integrity
    contract (unknown predictions never enter a precision denominator).
    """
    n = len(labeled)

    high_denom = 0
    high_correct = 0
    classified_denom = 0
    classified_correct = 0
    unknown_predictions = 0

    family_pairs: Counter[tuple[str, str]] = Counter()
    seniority_pairs: Counter[tuple[str, str]] = Counter()
    error_pairs: Counter[tuple[str, str]] = Counter()

    for row in labeled:
        result = classify_role(row.title)
        predicted_family = result.family.value
        predicted_seniority = infer_seniority(row.title)

        family_pairs[(row.expected_family, predicted_family)] += 1
        seniority_pairs[(row.expected_seniority, predicted_seniority)] += 1

        family_correct = predicted_family == row.expected_family

        if predicted_family == _UNKNOWN:
            unknown_predictions += 1
        else:
            classified_denom += 1
            if family_correct:
                classified_correct += 1

        if result.confidence is Confidence.high:
            high_denom += 1
            if family_correct:
                high_correct += 1

        if not family_correct:
            error_pairs[(row.expected_family, predicted_family)] += 1

    # A precision over an empty denominator is defined as 1.0 (no classified
    # prediction was wrong). This never masks a regression on the labeled sets we
    # actually run, which always contain high-confidence and classified rows.
    high_precision = high_correct / high_denom if high_denom else 1.0
    classified_precision = (
        classified_correct / classified_denom if classified_denom else 1.0
    )
    unknown_rate = unknown_predictions / n if n else 0.0

    top_errors = sorted(
        error_pairs.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
    )
    top_error_patterns = [
        ErrorPattern(expected=exp, predicted=pred, count=count)
        for (exp, pred), count in top_errors
    ]

    return EvaluationReport(
        n=n,
        high_confidence_precision=round(high_precision, 4),
        overall_classified_precision=round(classified_precision, 4),
        unknown_rate=round(unknown_rate, 4),
        family_confusion=_confusion(family_pairs),
        seniority_confusion=_confusion(seniority_pairs),
        top_error_patterns=top_error_patterns,
    )


_REQUIRED_COLUMNS = ("title", "expected_family", "expected_seniority")


def load_labeled_csv(path: str | Path) -> list[LabeledTitle]:
    """Read a labeled CSV (columns: title, expected_family, expected_seniority).

    Errors reference the row number and column name, never a field value
    (docs/privacy.md rule 3), since a private Founder-reviewed sample may be run
    through this later.
    """
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [c for c in _REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"Labeled CSV is missing required column(s): {', '.join(missing)}."
            )
        labeled: list[LabeledTitle] = []
        # DictReader row 1 is the header; data rows start at line 2.
        for line_number, raw in enumerate(reader, start=2):
            values = {c: (raw.get(c) or "").strip() for c in _REQUIRED_COLUMNS}
            if not any(values.values()):
                continue  # skip fully blank lines
            for column in _REQUIRED_COLUMNS:
                if not values[column]:
                    raise ValueError(
                        f"Row {line_number}: empty required column '{column}'."
                    )
            labeled.append(LabeledTitle(**values))
    return labeled


def evaluate_csv(path: str | Path) -> EvaluationReport:
    """Convenience wrapper: load a labeled CSV and evaluate it in one call."""
    return evaluate(load_labeled_csv(path))


# ---------------------------------------------------------------------------
# Founder review consumption (ticket OPUS-1.1b §1)
# ---------------------------------------------------------------------------
#
# A private, LOCAL-ONLY review file (never entering the repo, logs, or fixtures)
# records the classifier's OWN predictions plus the Founder's yes/no judgement.
# Columns (predicted columns are named role_family / seniority / confidence):
#
#   position, role_family, seniority, confidence, rule_evidence,
#   family_correct, expected_family, seniority_correct, expected_seniority,
#   reviewer_notes
#
# Privacy contract (docs/privacy.md rules 3 & 6, enforced here):
#   * ``position``, ``rule_evidence`` and ``reviewer_notes`` are NEVER read into
#     the aggregate model, printed, or logged -- they may hold raw titles or free
#     text. Only label/answer columns are retained.
#   * Predictions are TRUSTED as recorded (they were produced by the version
#     under review); we never re-run ``classify_role`` here. Re-deriving would
#     measure the CURRENT code against the Founder's judgement of the OLD code.
#   * Every metric mirrors evaluate.py's integrity rule: unknown-confidence rows
#     are excluded from precision denominators and surfaced as a separate rate;
#     an empty precision/accuracy denominator is defined as 1.0.
#   * Validation errors reference the ROW NUMBER and column name with the set of
#     valid options -- never a field value.

# The 10 role families and 7 seniority buckets an ``expected_*`` cell may hold.
VALID_FAMILIES: frozenset[str] = frozenset(f.value for f in RoleFamily)
VALID_SENIORITY: frozenset[str] = frozenset(
    {
        "founder_owner",
        "c_level",
        "vp_head_director",
        "manager_lead",
        "individual_contributor",
        "entry_student",
        "unknown",
    }
)

# Case/accent-insensitive yes/no tokens (also y/n and PT sim/nao/não). Answers
# are normalized with :func:`strip_accents` + casefold before lookup, so "Não"
# and "NAO" both land in ``_NO_TOKENS``.
_YES_TOKENS: frozenset[str] = frozenset({"yes", "y", "sim"})
_NO_TOKENS: frozenset[str] = frozenset({"no", "n", "nao"})
_ANSWER_HELP = "yes, no, y, n, sim, nao (or empty)"

_REVIEW_REQUIRED_COLUMNS: tuple[str, ...] = (
    "role_family",
    "confidence",
    "family_correct",
    "expected_family",
    "seniority_correct",
    "expected_seniority",
)


@dataclass(frozen=True)
class ReviewRecord:
    """One parsed review row -- ONLY the aggregate-safe columns are retained.

    ``position``, ``rule_evidence`` and ``reviewer_notes`` are deliberately
    absent so a raw title or free-text note can never reach an aggregate or a
    printed line. ``*_answer`` is ``True``/``False`` for yes/no, ``None`` when
    the Founder left the cell blank.
    """

    predicted_family: str
    predicted_confidence: str
    family_answer: bool | None
    expected_family: str | None
    seniority_answer: bool | None
    expected_seniority: str | None


class ConfidencePrecision(BaseModel):
    """Family-correctness precision within one predicted confidence level."""

    model_config = ConfigDict(extra="forbid")

    confidence: str
    denom: int
    correct: int
    precision: float


class CategoryCount(BaseModel):
    """A category label (a role family) with an associated count."""

    model_config = ConfigDict(extra="forbid")

    category: str
    count: int


class ReviewAggregateReport(BaseModel):
    """Aggregate-only summary of a Founder review. No titles or notes by design.

    Every field is a count, rate, or label -- there is no path for a ``position``
    or ``reviewer_notes`` value to appear here.
    """

    model_config = ConfigDict(extra="forbid")

    records_total: int
    records_reviewed: int
    records_unanswered: int
    family_answered: int
    family_unanswered: int
    seniority_answered: int
    seniority_unanswered: int
    family_accuracy: float
    seniority_accuracy: float
    confidence_precision: list[ConfidencePrecision] = Field(default_factory=list)
    unknown_confidence_rate: float = 0.0
    family_confusion: list[ConfusionCell] = Field(default_factory=list)
    errors_per_family: list[CategoryCount] = Field(default_factory=list)


def _normalize_answer(raw: str | None) -> str:
    """Classify a yes/no cell into 'yes' | 'no' | 'empty' | 'invalid'.

    Case- and accent-insensitive. Never returns or echoes the raw value.
    """
    text = strip_accents(raw or "").casefold().strip()
    if not text:
        return "empty"
    if text in _YES_TOKENS:
        return "yes"
    if text in _NO_TOKENS:
        return "no"
    return "invalid"


def load_review_csv(path: str | Path) -> list[ReviewRecord]:
    """Read a private Founder review CSV into aggregate-safe records.

    Read-only: the file is opened for reading and never written back. Raises
    ``ValueError`` with row numbers and column names (never field values) on a
    missing column, an unrecognized yes/no answer, or an ``expected_*`` value
    outside the valid family / seniority sets.
    """
    csv_path = Path(path)
    errors: list[str] = []
    records: list[ReviewRecord] = []
    families_help = ", ".join(sorted(VALID_FAMILIES))
    seniority_help = ", ".join(sorted(VALID_SENIORITY))

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [c for c in _REVIEW_REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                "Review CSV is missing required column(s): " + ", ".join(missing) + "."
            )

        # DictReader row 1 is the header; data rows start at line 2.
        for line_number, raw in enumerate(reader, start=2):
            cells = {c: (raw.get(c) or "").strip() for c in _REVIEW_REQUIRED_COLUMNS}
            if not any(cells.values()):
                continue  # fully blank line

            family_state = _normalize_answer(cells["family_correct"])
            seniority_state = _normalize_answer(cells["seniority_correct"])
            if family_state == "invalid":
                errors.append(
                    f"Row {line_number}: unrecognized value in column "
                    f"'family_correct' (allowed: {_ANSWER_HELP})."
                )
            if seniority_state == "invalid":
                errors.append(
                    f"Row {line_number}: unrecognized value in column "
                    f"'seniority_correct' (allowed: {_ANSWER_HELP})."
                )

            expected_family = cells["expected_family"] or None
            if expected_family is not None and expected_family not in VALID_FAMILIES:
                errors.append(
                    f"Row {line_number}: 'expected_family' is not a valid family "
                    f"(allowed: {families_help})."
                )
                expected_family = None
            expected_seniority = cells["expected_seniority"] or None
            if expected_seniority is not None and expected_seniority not in VALID_SENIORITY:
                errors.append(
                    f"Row {line_number}: 'expected_seniority' is not a valid "
                    f"seniority bucket (allowed: {seniority_help})."
                )
                expected_seniority = None

            records.append(
                ReviewRecord(
                    predicted_family=cells["role_family"],
                    predicted_confidence=cells["confidence"].casefold(),
                    family_answer=_answer_to_bool(family_state),
                    expected_family=expected_family,
                    seniority_answer=_answer_to_bool(seniority_state),
                    expected_seniority=expected_seniority,
                )
            )

    if errors:
        raise ValueError(
            "Review CSV has "
            f"{len(errors)} invalid cell(s):\n  - " + "\n  - ".join(errors)
        )
    return records


def _answer_to_bool(state: str) -> bool | None:
    """Map a normalized answer state to a tri-state boolean."""
    if state == "yes":
        return True
    if state == "no":
        return False
    return None  # 'empty' (invalid is reported separately and never reaches here)


def evaluate_review(records: list[ReviewRecord]) -> ReviewAggregateReport:
    """Aggregate a Founder review into metrics. Deterministic and pure.

    Accuracy/precision are computed over ANSWERED rows only (yes / (yes+no)); an
    empty denominator is defined as 1.0 (mirrors :func:`evaluate`). Unknown-
    confidence rows are excluded from the per-confidence precision denominators
    and reported as ``unknown_confidence_rate`` instead.
    """
    family_answered = [r for r in records if r.family_answer is not None]
    seniority_answered = [r for r in records if r.seniority_answer is not None]
    reviewed = sum(
        1
        for r in records
        if r.family_answer is not None or r.seniority_answer is not None
    )

    fam_yes = sum(1 for r in family_answered if r.family_answer)
    sen_yes = sum(1 for r in seniority_answered if r.seniority_answer)
    family_accuracy = fam_yes / len(family_answered) if family_answered else 1.0
    seniority_accuracy = sen_yes / len(seniority_answered) if seniority_answered else 1.0

    conf_denom: Counter[str] = Counter()
    conf_correct: Counter[str] = Counter()
    unknown_confidence = 0
    for r in family_answered:
        level = r.predicted_confidence
        if level not in ("high", "medium", "low"):
            unknown_confidence += 1
            continue
        conf_denom[level] += 1
        if r.family_answer:
            conf_correct[level] += 1

    confidence_precision = [
        ConfidencePrecision(
            confidence=level,
            denom=conf_denom[level],
            correct=conf_correct[level],
            precision=round(
                conf_correct[level] / conf_denom[level] if conf_denom[level] else 1.0, 4
            ),
        )
        for level in ("high", "medium", "low")
    ]
    unknown_confidence_rate = (
        round(unknown_confidence / len(family_answered), 4) if family_answered else 0.0
    )

    # Confusion + error tallies use family_answer is False (an explicit "no").
    confusion_pairs: Counter[tuple[str, str]] = Counter()
    errors_per_family: Counter[str] = Counter()
    for r in records:
        if r.family_answer is False:
            errors_per_family[r.predicted_family] += 1
            if r.expected_family is not None:
                confusion_pairs[(r.expected_family, r.predicted_family)] += 1

    errors_ordered = sorted(errors_per_family.items(), key=lambda kv: (-kv[1], kv[0]))

    return ReviewAggregateReport(
        records_total=len(records),
        records_reviewed=reviewed,
        records_unanswered=len(records) - reviewed,
        family_answered=len(family_answered),
        family_unanswered=len(records) - len(family_answered),
        seniority_answered=len(seniority_answered),
        seniority_unanswered=len(records) - len(seniority_answered),
        family_accuracy=round(family_accuracy, 4),
        seniority_accuracy=round(seniority_accuracy, 4),
        confidence_precision=confidence_precision,
        unknown_confidence_rate=unknown_confidence_rate,
        family_confusion=_confusion(confusion_pairs),
        errors_per_family=[
            CategoryCount(category=cat, count=count) for cat, count in errors_ordered
        ],
    )


def evaluate_review_csv(path: str | Path) -> ReviewAggregateReport:
    """Convenience wrapper: load a review CSV and aggregate it in one call."""
    return evaluate_review(load_review_csv(path))


def render_review_report(report: ReviewAggregateReport) -> str:
    """Render a review aggregate as plain text. Aggregate labels/counts only.

    By construction this can only emit counts, rates, and family/seniority
    labels -- there is no branch that could print a title or note value.
    """
    lines: list[str] = []
    lines.append("Founder review — aggregate summary")
    lines.append("")
    lines.append(f"Records in file:        {report.records_total}")
    lines.append(f"Records reviewed:       {report.records_reviewed}")
    lines.append(f"Records unanswered:     {report.records_unanswered}")
    lines.append("")
    lines.append(
        f"Family answered:        {report.family_answered} "
        f"(unanswered: {report.family_unanswered})"
    )
    lines.append(f"Family accuracy:        {report.family_accuracy:.1%}")
    lines.append(
        f"Seniority answered:     {report.seniority_answered} "
        f"(unanswered: {report.seniority_unanswered})"
    )
    lines.append(f"Seniority accuracy:     {report.seniority_accuracy:.1%}")
    lines.append("")
    lines.append("Family precision by predicted confidence (answered rows):")
    for cp in report.confidence_precision:
        lines.append(
            f"  {cp.confidence:<6} {cp.precision:.1%} "
            f"({cp.correct}/{cp.denom})"
        )
    lines.append(
        f"  unknown-confidence rate: {report.unknown_confidence_rate:.1%} "
        "(excluded from precision denominators)"
    )
    lines.append("")
    lines.append("Family confusion (expected → predicted, 'no' rows with expected):")
    if report.family_confusion:
        for cell in report.family_confusion:
            lines.append(f"  {cell.expected} → {cell.predicted}: {cell.count}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("Errors per predicted family (family marked 'no'):")
    if report.errors_per_family:
        for item in report.errors_per_family:
            lines.append(f"  {item.category}: {item.count}")
    else:
        lines.append("  (none)")
    lines.append("")
    return "\n".join(lines)
