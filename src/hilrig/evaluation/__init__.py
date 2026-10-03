"""Host-side assertion evaluation over persistent captured runs."""

from hilrig.evaluation.evaluator import AssertionEvaluator, evaluate_assertions
from hilrig.evaluation.models import (
    EVALUATION_REPORT_SCHEMA_VERSION,
    AssertionGroupResult,
    AssertionResult,
    EvaluationReport,
    EvaluationVerdict,
    StimulusRecord,
)

__all__ = [
    "EVALUATION_REPORT_SCHEMA_VERSION",
    "AssertionEvaluator",
    "AssertionGroupResult",
    "AssertionResult",
    "EvaluationReport",
    "EvaluationVerdict",
    "StimulusRecord",
    "evaluate_assertions",
]
