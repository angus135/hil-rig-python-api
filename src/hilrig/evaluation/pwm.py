"""Evaluation handlers for PWM-input assertions."""

from __future__ import annotations

from collections.abc import Callable

from hilrig.evaluation.common import (
    integer_argument,
    make_result,
    number_argument,
    range_message,
    range_verdict,
    unavailable_point_result,
)
from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import AssertionResult, EvaluationScalar, EvaluationVerdict
from hilrig.models.execution import CompiledAssertion
from hilrig.results import PWMMeasurement


def evaluate_period_near(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    period_ns = integer_argument(assertion, "period_ns")
    tolerance_ns = integer_argument(assertion, "tolerance_ns")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda measurement: abs(measurement.period_ns - period_ns) <= tolerance_ns,
        expected_text=f"{period_ns} ± {tolerance_ns} ns",
    )


def evaluate_frequency_near(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    frequency_hz = number_argument(assertion, "frequency_hz")
    tolerance_hz = number_argument(assertion, "tolerance_hz")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda measurement: abs(_frequency_hz(measurement) - frequency_hz) <= tolerance_hz,
        expected_text=f"{frequency_hz:g} ± {tolerance_hz:g} Hz",
    )


def evaluate_duty_cycle_near(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    duty_cycle = number_argument(assertion, "duty_cycle")
    tolerance = number_argument(assertion, "duty_cycle_tolerance")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda measurement: abs(measurement.duty_cycle - duty_cycle) <= tolerance,
        expected_text=f"{_percent(duty_cycle)} ± {_percent(tolerance)}",
    )


def evaluate_waveform_near(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    frequency_hz = number_argument(assertion, "frequency_hz")
    frequency_tolerance_hz = number_argument(assertion, "frequency_tolerance_hz")
    duty_cycle = number_argument(assertion, "duty_cycle")
    duty_cycle_tolerance = number_argument(assertion, "duty_cycle_tolerance")
    return _evaluate_point(
        assertion,
        context,
        accepts=lambda measurement: (
            abs(_frequency_hz(measurement) - frequency_hz) <= frequency_tolerance_hz
            and abs(measurement.duty_cycle - duty_cycle) <= duty_cycle_tolerance
        ),
        expected_text=(
            f"{frequency_hz:g} ± {frequency_tolerance_hz:g} Hz and "
            f"{_percent(duty_cycle)} ± {_percent(duty_cycle_tolerance)} duty cycle"
        ),
    )


def evaluate_frequency_remain_within(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    minimum_hz = number_argument(assertion, "minimum_hz")
    maximum_hz = number_argument(assertion, "maximum_hz")
    return _evaluate_metric_range(
        assertion,
        context,
        metric_name="frequency_hz",
        metric=lambda measurement: _frequency_hz(measurement),
        accepts=lambda value: minimum_hz <= value <= maximum_hz,
        expected_text=f"between {minimum_hz:g} and {maximum_hz:g} Hz",
    )


def evaluate_duty_cycle_remain_within(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    minimum = number_argument(assertion, "minimum_duty_cycle")
    maximum = number_argument(assertion, "maximum_duty_cycle")
    return _evaluate_metric_range(
        assertion,
        context,
        metric_name="duty_cycle",
        metric=lambda measurement: measurement.duty_cycle,
        accepts=lambda value: minimum <= value <= maximum,
        expected_text=f"between {_percent(minimum)} and {_percent(maximum)}",
    )


def _evaluate_point(
    assertion: CompiledAssertion,
    context: EvaluationContext,
    *,
    accepts: Callable[[PWMMeasurement], bool],
    expected_text: str,
) -> AssertionResult:
    tick = integer_argument(assertion, "tick")
    evidence = context.pwm_point(channel=assertion.channel, tick=tick)
    unavailable = unavailable_point_result(assertion, evidence)
    if unavailable is not None:
        return unavailable

    measurement = evidence.value
    assert measurement is not None
    observed = _measurement_observed(measurement)
    observed["tick"] = tick
    if measurement.period_ns == 0:
        return make_result(
            assertion,
            verdict=EvaluationVerdict.FAIL,
            message=f"No measurable PWM waveform was reported at tick {tick}.",
            observed=observed,
            valid_sample_count=1,
            violation_count=1,
            first_failure_tick=tick,
        )

    verdict = EvaluationVerdict.PASS if accepts(measurement) else EvaluationVerdict.FAIL
    message = (
        f"PWM measurement at tick {tick} satisfied the expected {expected_text}."
        if verdict is EvaluationVerdict.PASS
        else f"PWM measurement at tick {tick} did not satisfy the expected {expected_text}."
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed=observed,
        valid_sample_count=1,
        violation_count=int(verdict is EvaluationVerdict.FAIL),
        first_failure_tick=tick if verdict is EvaluationVerdict.FAIL else None,
    )


def _evaluate_metric_range(
    assertion: CompiledAssertion,
    context: EvaluationContext,
    *,
    metric_name: str,
    metric: Callable[[PWMMeasurement], float],
    accepts: Callable[[float], bool],
    expected_text: str,
) -> AssertionResult:
    from_tick = integer_argument(assertion, "from_tick")
    until_tick = integer_argument(assertion, "until_tick")
    valid_count = 0
    missing_count = 0
    invalid_count = 0
    violation_count = 0
    no_waveform_count = 0
    minimum_observed: float | None = None
    maximum_observed: float | None = None
    first_failure_tick: int | None = None
    first_failure_value: float | None = None

    for evidence in context.pwm_range(
        channel=assertion.channel,
        from_tick=from_tick,
        until_tick=until_tick,
    ):
        if evidence.missing:
            missing_count += 1
        elif evidence.invalid:
            invalid_count += 1
        else:
            measurement = evidence.value
            assert measurement is not None
            valid_count += 1
            if measurement.period_ns == 0:
                no_waveform_count += 1
                violation_count += 1
                if first_failure_tick is None:
                    first_failure_tick = evidence.tick
                continue

            value = metric(measurement)
            minimum_observed = value if minimum_observed is None else min(minimum_observed, value)
            maximum_observed = value if maximum_observed is None else max(maximum_observed, value)
            if not accepts(value):
                violation_count += 1
                if first_failure_tick is None:
                    first_failure_tick = evidence.tick
                    first_failure_value = value

    verdict = range_verdict(
        violation_count=violation_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    message = range_message(
        verdict=verdict,
        success=f"PWM {metric_name.replace('_', ' ')} remained {expected_text}.",
        failure=(
            f"PWM {metric_name.replace('_', ' ')} did not remain {expected_text}; "
            f"observed {violation_count} violations, first at tick {first_failure_tick}."
        ),
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={
            f"minimum_{metric_name}": minimum_observed,
            f"maximum_{metric_name}": maximum_observed,
            f"first_failure_{metric_name}": first_failure_value,
            "no_waveform_sample_count": no_waveform_count,
        },
        valid_sample_count=valid_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
        violation_count=violation_count,
        first_failure_tick=first_failure_tick,
    )


def _measurement_observed(measurement: PWMMeasurement) -> dict[str, EvaluationScalar]:
    frequency = None if measurement.period_ns == 0 else _frequency_hz(measurement)
    return {
        "period_ns": measurement.period_ns,
        "frequency_hz": frequency,
        "duty_permyriad": measurement.duty_permyriad,
        "duty_cycle": measurement.duty_cycle,
    }


def _frequency_hz(measurement: PWMMeasurement) -> float:
    return 1_000_000_000 / measurement.period_ns


def _percent(duty_cycle: float) -> str:
    return f"{duty_cycle * 100:g}%"
