"""Common validation and result construction for assertion handlers."""

from __future__ import annotations

import math
from collections.abc import Callable

from hilrig.evaluation.context import EvaluationContext, EvidenceSample
from hilrig.evaluation.models import (
    AssertionResult,
    EvaluationScalar,
    EvaluationVerdict,
    immutable_evaluation_fields,
)
from hilrig.exceptions import EvaluationError
from hilrig.models.execution import CompiledAssertion

AssertionHandler = Callable[[CompiledAssertion, EvaluationContext], AssertionResult]


def integer_argument(assertion: CompiledAssertion, name: str) -> int:
    value = assertion.arguments.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvaluationError(
            f"Assertion {assertion.assertion_id} argument {name!r} must be an integer"
        )
    return value


def number_argument(assertion: CompiledAssertion, name: str) -> float:
    value = assertion.arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationError(
            f"Assertion {assertion.assertion_id} argument {name!r} must be numeric"
        )
    converted = float(value)
    if not math.isfinite(converted):
        raise EvaluationError(
            f"Assertion {assertion.assertion_id} argument {name!r} must be finite"
        )
    return converted


def text_argument(assertion: CompiledAssertion, name: str) -> str:
    value = assertion.arguments.get(name)
    if not isinstance(value, str) or not value:
        raise EvaluationError(f"Assertion {assertion.assertion_id} argument {name!r} must be text")
    return value


def assertion_ticks(assertion: CompiledAssertion) -> tuple[int, int]:
    if "tick" in assertion.arguments:
        tick = integer_argument(assertion, "tick")
        return tick, tick
    return (
        integer_argument(assertion, "from_tick"),
        integer_argument(assertion, "until_tick"),
    )


def make_result(
    assertion: CompiledAssertion,
    *,
    verdict: EvaluationVerdict,
    message: str,
    observed: dict[str, EvaluationScalar],
    valid_sample_count: int,
    missing_sample_count: int = 0,
    invalid_sample_count: int = 0,
    violation_count: int = 0,
    first_failure_tick: int | None = None,
) -> AssertionResult:
    from_tick, until_tick = assertion_ticks(assertion)
    return AssertionResult(
        assertion_id=assertion.assertion_id,
        verdict=verdict,
        peripheral=assertion.peripheral,
        channel=assertion.channel,
        assertion=assertion.assertion,
        evaluated_from_tick=from_tick,
        evaluated_until_tick=until_tick,
        expected=immutable_evaluation_fields(dict(assertion.arguments)),
        observed=immutable_evaluation_fields(observed),
        valid_sample_count=valid_sample_count,
        missing_sample_count=missing_sample_count,
        invalid_sample_count=invalid_sample_count,
        violation_count=violation_count,
        first_failure_tick=first_failure_tick,
        message=message,
    )


def unavailable_point_result(
    assertion: CompiledAssertion,
    evidence: EvidenceSample,
) -> AssertionResult | None:
    if evidence.missing:
        return make_result(
            assertion,
            verdict=EvaluationVerdict.INCONCLUSIVE,
            message=f"No captured result was received for tick {evidence.tick}.",
            observed={"tick": evidence.tick, "availability": "missing"},
            valid_sample_count=0,
            missing_sample_count=1,
        )
    if evidence.invalid:
        condition = None if evidence.condition is None else evidence.condition.value
        return make_result(
            assertion,
            verdict=EvaluationVerdict.INCONCLUSIVE,
            message=f"The captured measurement at tick {evidence.tick} is invalid.",
            observed={
                "tick": evidence.tick,
                "availability": "invalid",
                "condition": condition,
                "problem_detail": evidence.problem_detail,
            },
            valid_sample_count=0,
            invalid_sample_count=1,
        )
    return None


def range_verdict(
    *,
    violation_count: int,
    missing_sample_count: int,
    invalid_sample_count: int,
) -> EvaluationVerdict:
    if violation_count:
        return EvaluationVerdict.FAIL
    if missing_sample_count or invalid_sample_count:
        return EvaluationVerdict.INCONCLUSIVE
    return EvaluationVerdict.PASS


def range_message(
    *,
    verdict: EvaluationVerdict,
    success: str,
    failure: str,
    missing_sample_count: int,
    invalid_sample_count: int,
) -> str:
    if verdict is EvaluationVerdict.PASS:
        return success
    if verdict is EvaluationVerdict.FAIL:
        return failure
    return (
        "No violation was observed, but the result is inconclusive because "
        f"{missing_sample_count} expected samples were missing and "
        f"{invalid_sample_count} samples were invalid."
    )
