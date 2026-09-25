"""Orchestration and dispatch of stored host-side assertions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import (
    AssertionGroupResult,
    AssertionResult,
    EvaluationReport,
    EvaluationVerdict,
    StimulusRecord,
    immutable_evaluation_fields,
)
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
        group_names = {group.group_id: group.name for group in assertion_set.groups}
        stimuli = tuple(
            _stimulus_record(
                stimulus,
                group_name=(
                    None
                    if stimulus.group_id is None
                    else group_names.get(stimulus.group_id, "")
                ),
                tick_period_ns=metadata.tick_period_ns,
            )
            for stimulus in assertion_set.stimuli
        )
        results = []
        for assertion in assertion_set.assertions:
            key = (assertion.peripheral, assertion.assertion)
            handler = EVALUATOR_REGISTRY.get(key)
            if handler is None:
                raise UnsupportedAssertionError(
                    f"No evaluator is registered for {key[0]}.{key[1]} "
                    f"(assertion {assertion.assertion_id})"
                )
            group_name = next(
                group.name for group in assertion_set.groups if group.group_id == assertion.group_id
            )
            results.append(replace(handler(assertion, context), group_name=group_name))

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
        group_results = tuple(
            _group_result(group.group_id, group.name, stimuli, tuple(results))
            for group in assertion_set.groups
            if any(result.group_id == group.group_id for result in results)
            or any(stimulus.group_id == group.group_id for stimulus in stimuli)
        )
        return EvaluationReport(
            test_id=metadata.test_id,
            application_test_id=metadata.application_test_id,
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
            assertion_groups=group_results,
            stimuli=stimuli,
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


def _group_result(
    group_id: int,
    name: str,
    stimuli: tuple[StimulusRecord, ...],
    results: tuple[AssertionResult, ...],
) -> AssertionGroupResult:
    members = tuple(result for result in results if result.group_id == group_id)
    verdicts = tuple(result.verdict for result in members)
    if EvaluationVerdict.FAIL in verdicts:
        verdict = EvaluationVerdict.FAIL
    elif not verdicts or EvaluationVerdict.INCONCLUSIVE in verdicts:
        verdict = EvaluationVerdict.INCONCLUSIVE
    else:
        verdict = EvaluationVerdict.PASS
    return AssertionGroupResult(
        group_id=group_id,
        name=name,
        verdict=verdict,
        stimuli=tuple(stimulus for stimulus in stimuli if stimulus.group_id == group_id),
        assertion_results=members,
    )


def _stimulus_record(stimulus, *, group_name: str | None, tick_period_ns: int) -> StimulusRecord:
    arguments = immutable_evaluation_fields(dict(stimulus.arguments))
    return StimulusRecord(
        instruction_id=stimulus.instruction_id,
        group_id=stimulus.group_id,
        group_name=group_name,
        subject_name=stimulus.subject_name,
        tick=stimulus.tick,
        time_ns=stimulus.tick * tick_period_ns,
        peripheral=stimulus.peripheral,
        channel=stimulus.channel,
        operation=stimulus.operation,
        arguments=arguments,
        message=_stimulus_message(
            stimulus.subject_name,
            stimulus.operation,
            dict(stimulus.arguments),
            stimulus.tick,
        ),
    )


def _stimulus_message(
    subject_name: str,
    operation: str,
    arguments: dict[str, object],
    tick: int,
) -> str:
    if operation in {"write", "transmit"} and "data" in arguments:
        return f"{subject_name} sent {arguments['data']} at tick {tick}."
    if operation == "set_state" and "action" in arguments:
        return f"{subject_name} was set {str(arguments['action']).lower()} at tick {tick}."
    details = ", ".join(f"{name}={value}" for name, value in arguments.items())
    suffix = f" ({details})" if details else ""
    return f"{subject_name} {operation.replace('_', ' ')}{suffix} at tick {tick}."


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
