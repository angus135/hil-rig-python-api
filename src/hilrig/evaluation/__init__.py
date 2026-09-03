"""Host-side assertion evaluation over persistent captured runs."""

from hilrig.evaluation.evaluator import AssertionEvaluator, evaluate_assertions
from hilrig.evaluation.models import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    AssertionResult,
    EvaluationReport,
    EvaluationVerdict,
)

__all__ = [
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "AssertionEvaluator",
    "AssertionResult",
    "EvaluationReport",
    "EvaluationVerdict",
    "evaluate_assertions",
]
