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
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from content_machine.audience.classify import Confidence, classify_role
from content_machine.audience.normalize import infer_seniority

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
