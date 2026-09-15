"""Evaluation handlers for digital-input assertions."""

from __future__ import annotations

from hilrig.evaluation.common import (
    integer_argument,
    make_result,
    range_message,
    range_verdict,
    text_argument,
    unavailable_point_result,
)
from hilrig.evaluation.context import EvaluationContext
from hilrig.evaluation.models import AssertionResult, EvaluationVerdict
from hilrig.exceptions import EvaluationError
from hilrig.models.execution import CompiledAssertion


def evaluate_state_at_tick(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    tick = integer_argument(assertion, "tick")
    expected = _state_argument(assertion, "expected_state")
    evidence = context.digital_point(channel=assertion.channel, tick=tick)
    unavailable = unavailable_point_result(assertion, evidence)
    if unavailable is not None:
        return unavailable

    actual = bool(evidence.value)
    verdict = EvaluationVerdict.PASS if actual is expected else EvaluationVerdict.FAIL
    actual_name = _state_name(actual)
    expected_name = _state_name(expected)
    message = (
        f"Digital input was {actual_name} at tick {tick}, as expected."
        if verdict is EvaluationVerdict.PASS
        else f"Expected {expected_name} at tick {tick}, but observed {actual_name}."
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={"tick": tick, "state": actual_name},
        valid_sample_count=1,
        violation_count=int(verdict is EvaluationVerdict.FAIL),
        first_failure_tick=tick if verdict is EvaluationVerdict.FAIL else None,
    )


def evaluate_remain_high(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    return _evaluate_remain_state(assertion, context, expected=True)


def evaluate_remain_low(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    return _evaluate_remain_state(assertion, context, expected=False)


def evaluate_transition(
    assertion: CompiledAssertion,
    context: EvaluationContext,
) -> AssertionResult:
    from_tick = integer_argument(assertion, "from_tick")
    until_tick = integer_argument(assertion, "until_tick")
    from_state = _state_argument(assertion, "from_state")
    to_state = _state_argument(assertion, "to_state")
    valid_count = 0
    missing_count = 0
    invalid_count = 0
    high_count = 0
    low_count = 0
    previous_tick: int | None = None
    previous_value: bool | None = None
    transition_from_tick: int | None = None
    transition_to_tick: int | None = None

    for evidence in context.digital_range(
        channel=assertion.channel,
        from_tick=from_tick,
        until_tick=until_tick,
    ):
        if evidence.missing:
            missing_count += 1
            previous_tick = None
            previous_value = None
            continue
        if evidence.invalid:
            invalid_count += 1
            previous_tick = None
            previous_value = None
            continue

        actual = bool(evidence.value)
        valid_count += 1
        high_count += int(actual)
        low_count += int(not actual)
        if (
            transition_from_tick is None
            and previous_tick is not None
            and previous_tick + 1 == evidence.tick
            and previous_value is from_state
            and actual is to_state
        ):
            transition_from_tick = previous_tick
            transition_to_tick = evidence.tick
        previous_tick = evidence.tick
        previous_value = actual

    if transition_from_tick is not None:
        verdict = EvaluationVerdict.PASS
        message = (
            f"Observed the requested {_state_name(from_state)} to "
            f"{_state_name(to_state)} transition between ticks "
            f"{transition_from_tick} and {transition_to_tick}."
        )
    elif missing_count or invalid_count:
        verdict = EvaluationVerdict.INCONCLUSIVE
        message = (
            "The requested transition was not observed, but missing or invalid samples "
            "mean that it could have occurred without being captured."
        )
    else:
        verdict = EvaluationVerdict.FAIL
        message = (
            f"No adjacent {_state_name(from_state)} to {_state_name(to_state)} transition "
            f"was observed from tick {from_tick} through {until_tick}."
        )

    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={
            "high_sample_count": high_count,
            "low_sample_count": low_count,
            "transition_from_tick": transition_from_tick,
            "transition_to_tick": transition_to_tick,
        },
        valid_sample_count=valid_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
        violation_count=int(verdict is EvaluationVerdict.FAIL),
    )


def _evaluate_remain_state(
    assertion: CompiledAssertion,
    context: EvaluationContext,
    *,
    expected: bool,
) -> AssertionResult:
    from_tick = integer_argument(assertion, "from_tick")
    until_tick = integer_argument(assertion, "until_tick")
    valid_count = 0
    missing_count = 0
    invalid_count = 0
    violation_count = 0
    high_count = 0
    low_count = 0
    first_failure_tick: int | None = None

    for evidence in context.digital_range(
        channel=assertion.channel,
        from_tick=from_tick,
        until_tick=until_tick,
    ):
        if evidence.missing:
            missing_count += 1
        elif evidence.invalid:
            invalid_count += 1
        else:
            actual = bool(evidence.value)
            valid_count += 1
            high_count += int(actual)
            low_count += int(not actual)
            if actual is not expected:
                violation_count += 1
                if first_failure_tick is None:
                    first_failure_tick = evidence.tick

    verdict = range_verdict(
        violation_count=violation_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    expected_name = _state_name(expected)
    message = range_message(
        verdict=verdict,
        success=(
            f"Digital input remained {expected_name} from tick {from_tick} through {until_tick}."
        ),
        failure=(
            f"Digital input did not remain {expected_name}; observed {violation_count} "
            f"contrary samples, first at tick {first_failure_tick}."
        ),
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
    )
    return make_result(
        assertion,
        verdict=verdict,
        message=message,
        observed={
            "high_sample_count": high_count,
            "low_sample_count": low_count,
        },
        valid_sample_count=valid_count,
        missing_sample_count=missing_count,
        invalid_sample_count=invalid_count,
        violation_count=violation_count,
        first_failure_tick=first_failure_tick,
    )


def _state_argument(assertion: CompiledAssertion, name: str) -> bool:
    state = text_argument(assertion, name)
    if state == "HIGH":
        return True
    if state == "LOW":
        return False
    raise EvaluationError(
        f"Assertion {assertion.assertion_id} argument {name!r} must be HIGH or LOW"
    )


def _state_name(value: bool) -> str:
    return "HIGH" if value else "LOW"
