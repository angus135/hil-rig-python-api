"""Orchestration and dispatch of stored host-side assertions."""

from __future__ import annotations

from datetime import datetime, timezone

from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import EvaluationReport, EvaluationVerdict
from hilrig.evaluation.registry import EVALUATOR_REGISTRY
from hilrig.exceptions import EvaluationError, UnsupportedAssertionError
from hilrig.results import ORIGINAL_ASSERTION_SET_ID, CapturedRunIR, CaptureStatus


class AssertionEvaluator:
    """Evaluate one stored assertion set against a finalized captured run."""

    def evaluate(
        self,
        captured_run: CapturedRunIR,
        *,
        assertion_set_id: str = ORIGINAL_ASSERTION_SET_ID,
    ) -> EvaluationReport:
        if not isinstance(captured_run, CapturedRunIR):
            raise TypeError("captured_run must be a CapturedRunIR")
        metadata = captured_run.metadata
        if metadata.status is CaptureStatus.IN_PROGRESS:
            raise EvaluationError("A captured run must be finalized before evaluation")

        assertion_set = captured_run.assertion_set(assertion_set_id)
        context = EvaluationContext(captured_run)
        results = []
        for assertion in assertion_set.assertions:
            key = (assertion.peripheral, assertion.assertion)
            handler = EVALUATOR_REGISTRY.get(key)
            if handler is None:
                raise UnsupportedAssertionError(
                    f"No evaluator is registered for {key[0]}.{key[1]} "
                    f"(assertion {assertion.assertion_id})"
                )
            results.append(handler(assertion, context))

        application_errors = tuple(captured_run.iter_application_errors())
        non_recoverable_error_count = sum(not error.recoverable for error in application_errors)
        warnings = _warnings(
            capture_status=metadata.status,
            expected_tick_count=metadata.expected_tick_count,
            received_tick_count=metadata.received_tick_count,
            assertion_count=len(results),
            application_error_count=len(application_errors),
            non_recoverable_error_count=non_recoverable_error_count,
        )
        verdict = _overall_verdict(
            tuple(result.verdict for result in results),
            capture_status=metadata.status,
            non_recoverable_error_count=non_recoverable_error_count,
        )
        return EvaluationReport(
            test_id=metadata.test_id,
            run_id=metadata.run_id,
            test_name=metadata.test_name,
            capture_database=captured_run.database_path.name,
            capture_status=metadata.status,
            expected_tick_count=metadata.expected_tick_count,
            received_tick_count=metadata.received_tick_count,
            assertion_set_id=assertion_set.assertion_set_id,
            compiled_ir_version=assertion_set.compiled_ir_version,
            evaluated_at=datetime.now(timezone.utc).isoformat(),
            verdict=verdict,
            assertion_results=tuple(results),
            warnings=warnings,
        )


def evaluate_assertions(
    captured_run: CapturedRunIR,
    *,
    assertion_set_id: str = ORIGINAL_ASSERTION_SET_ID,
) -> EvaluationReport:
    """Convenience function using the standard assertion evaluator registry."""
    return AssertionEvaluator().evaluate(
        captured_run,
        assertion_set_id=assertion_set_id,
    )


def _overall_verdict(
    verdicts: tuple[EvaluationVerdict, ...],
    *,
    capture_status: CaptureStatus,
    non_recoverable_error_count: int,
) -> EvaluationVerdict:
    if EvaluationVerdict.FAIL in verdicts:
        return EvaluationVerdict.FAIL
    if (
        not verdicts
        or EvaluationVerdict.INCONCLUSIVE in verdicts
        or capture_status is not CaptureStatus.COMPLETE
        or non_recoverable_error_count
    ):
        return EvaluationVerdict.INCONCLUSIVE
    return EvaluationVerdict.PASS


def _warnings(
    *,
    capture_status: CaptureStatus,
    expected_tick_count: int,
    received_tick_count: int,
    assertion_count: int,
    application_error_count: int,
    non_recoverable_error_count: int,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if capture_status is not CaptureStatus.COMPLETE:
        warnings.append(
            f"Capture status is {capture_status.value}; received {received_tick_count} of "
            f"{expected_tick_count} expected fixed-tick results."
        )
    if not assertion_count:
        warnings.append("The selected assertion set contains no assertions.")
    if application_error_count:
        warnings.append(
            f"The capture contains {application_error_count} application-layer errors, "
            f"including {non_recoverable_error_count} non-recoverable errors."
        )
    return tuple(warnings)
