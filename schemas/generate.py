"""Export JSON Schemas from the Pydantic models.

Run with: ``python schemas/generate.py`` (from the repo root, with the package
installed or ``src`` on the path). Regenerates the ``*.schema.json`` files in
this directory. Keeping these committed gives contributors a public, versioned
reference for the frozen data contracts (ADR 0001 §3, ADR 0003 §1).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running directly from a source checkout without installation.
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from content_machine.audience.classify import RoleClassification  # noqa: E402
from content_machine.audience.compare import ComparisonReport  # noqa: E402
from content_machine.audience.evaluate import (  # noqa: E402
    EvaluationReport,
    ReviewAggregateReport,
)
from content_machine.audience.public_report import PublicReport  # noqa: E402
from content_machine.audience.report import AudienceReport  # noqa: E402
from content_machine.audience.run_comparison import (  # noqa: E402
    ReclassificationStats,
    RunComparison,
)
from content_machine.ingestion.csv_loader import RawConnection  # noqa: E402
from content_machine.privacy.anonymizer import AnonymizedConnection  # noqa: E402

_OUT_DIR = Path(__file__).resolve().parent

_MODELS = {
    "raw_connection.schema.json": RawConnection,
    "anonymized_connection.schema.json": AnonymizedConnection,
    "role_classification.schema.json": RoleClassification,
    "audience_report.schema.json": AudienceReport,
    "public_report.schema.json": PublicReport,
    "evaluation_report.schema.json": EvaluationReport,
    "review_aggregate_report.schema.json": ReviewAggregateReport,
    "classifier_comparison_report.schema.json": ComparisonReport,
    "run_comparison.schema.json": RunComparison,
    "reclassification_stats.schema.json": ReclassificationStats,
}


def main() -> None:
    """Write one JSON Schema file per model."""
    for filename, model in _MODELS.items():
        schema = model.model_json_schema()
        target = _OUT_DIR / filename
        target.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {target.relative_to(_OUT_DIR.parent)}")


if __name__ == "__main__":
    main()
