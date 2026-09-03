"""Evaluation handlers for analogue-input assertions."""

from __future__ import annotations

from collections.abc import Callable

from hilrig.evaluation.common import (
    integer_argument,
    make_result,
    range_message,
    range_verdict,
    unavailable_point_result,
)
from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import AssertionResult, EvaluationVerdict
from hilrig.models.execution import CompiledAssertion


def evaluate_near(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    target_uv = integer_argument(assertion, "target_uv")
    tolerance_uv = integer_argument(assertion, "tolerance_uv")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda value: abs(value - target_uv) <= tolerance_uv,
        expected_text=f"{_volts(target_uv)} ± {_volts(tolerance_uv)}",
    )


def evaluate_within(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    minimum_uv = integer_argument(assertion, "minimum_uv")
    maximum_uv = integer_argument(assertion, "maximum_uv")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda value: minimum_uv <= value <= maximum_uv,
        expected_text=f"between {_volts(minimum_uv)} and {_volts(maximum_uv)}",
    )


def evaluate_remain_within(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    minimum_uv = integer_argument(assertion, "minimum_uv")
    maximum_uv = integer_argument(assertion, "maximum_uv")
    return _evaluate_range(
        assertion,
        context,
        accepts=lambda value: minimum_uv <= value <= maximum_uv,
        expected_text=f"between {_volts(minimum_uv)} and {_volts(maximum_uv)}",
    )


def evaluate_remain_above(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    threshold_uv = integer_argument(assertion, "threshold_uv")
    return _evaluate_range(
        assertion,
        context,
        accepts=lambda value: value > threshold_uv,
        expected_text=f"above {_volts(threshold_uv)}",
    )


def evaluate_remain_below(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    threshold_uv = integer_argument(assertion, "threshold_uv")
    return _evaluate_range(
        assertion,
        context,
        accepts=lambda value: value < threshold_uv,
        expected_text=f"below {_volts(threshold_uv)}",
    )


def _evaluate_point(
    assertion: CompiledAssertion,
    context: EvaluationContext,
    *,
    accepts: Callable[[int], bool],
    expected_text: str,
) -> AssertionResult:
    tick = integer_argument(assertion, "tick")
    evidence = context.analogue_point(channel=assertion.channel, tick=tick)
    unavailable = unavailable_point_result(assertion, evidence)
    if unavailable is not None:
        return unavailable

    actual_uv = int(evidence.value)
    verdict = EvaluationVerdict.PASS if accepts(actual_uv) else EvaluationVerdict.FAIL
    message = (
        f"Observed {_volts(actual_uv)} at tick {tick}, satisfying the expected value "
        f"{expected_text}."
        if verdict is EvaluationVerdict.PASS
        else f"Observed {_volts(actual_uv)} at tick {tick}; expected {expected_text}."
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={"tick": tick, "value_uv": actual_uv},
        valid_sample_count=1,
        violation_count=int(verdict is EvaluationVerdict.FAIL),
        first_failure_tick=tick if verdict is EvaluationVerdict.FAIL else None,
    )


def _evaluate_range(
    assertion: CompiledAssertion,
    context: EvaluationContext,
    *,
    accepts: Callable[[int], bool],
    expected_text: str,
) -> AssertionResult:
    from_tick = integer_argument(assertion, "from_tick")
    until_tick = integer_argument(assertion, "until_tick")
    valid_count = 0
    missing_count = 0
    invalid_count = 0
    violation_count = 0
    minimum_observed_uv: int | None = None
    maximum_observed_uv: int | None = None
    first_failure_tick: int | None = None
    first_failure_uv: int | None = None

    for evidence in context.analogue_range(
        channel=assertion.channel,
        from_tick=from_tick,
        until_tick=until_tick,
    ):
        if evidence.missing:
            missing_count += 1
        elif evidence.invalid:
            invalid_count += 1
        else:
            actual_uv = int(evidence.value)
            valid_count += 1
            minimum_observed_uv = (
                actual_uv if minimum_observed_uv is None else min(minimum_observed_uv, actual_uv)
            )
            maximum_observed_uv = (
                actual_uv if maximum_observed_uv is None else max(maximum_observed_uv, actual_uv)
            )
            if not accepts(actual_uv):
                violation_count += 1
                if first_failure_tick is None:
                    first_failure_tick = evidence.tick
                    first_failure_uv = actual_uv

    verdict = range_verdict(
        violation_count=violation_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    message = range_message(
        verdict=verdict,
        success=(
            f"Analogue input remained {expected_text} from tick {from_tick} through {until_tick}."
        ),
        failure=(
            f"Analogue input did not remain {expected_text}; observed {violation_count} "
            f"violations, first at tick {first_failure_tick} "
            f"({_volts(first_failure_uv)})."
        ),
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={
            "minimum_uv": minimum_observed_uv,
            "maximum_uv": maximum_observed_uv,
            "first_failure_uv": first_failure_uv,
        },
        valid_sample_count=valid_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
        violation_count=violation_count,
        first_failure_tick=first_failure_tick,
    )


def _volts(microvolts: int | None) -> str:
    if microvolts is None:
        return "unknown"
    return f"{microvolts / 1_000_000:g} V"
