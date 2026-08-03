import pytest

from hilrig import DigitalState, FrequencyMode, StartMode
from hilrig import Test as HilRigTest
from hilrig.models.assertions import (
    DigitalInputPointAssertion,
    DigitalInputRemainHighAssertion,
    DigitalInputTransitionAssertion,
)


def test_point_assertions_store_high_and_low_at_converted_ticks() -> None:
    test = HilRigTest(name="Point assertions")
    digital_input = test.digital_input(channel=0)

    test.expect(digital_input).high(at_ms=100)
    test.expect(digital_input).low(at_s=0.2)

    high, low = tuple(test.assertions)
    assert isinstance(high, DigitalInputPointAssertion)
    assert high.timestamp == 100
    assert high.expected_state is DigitalState.HIGH
    assert isinstance(low, DigitalInputPointAssertion)
    assert low.timestamp == 200
    assert low.expected_state is DigitalState.LOW
    assert high.channel is digital_input.identity


def test_remain_high_supports_tick_millisecond_and_second_ranges() -> None:
    test = HilRigTest(name="Range assertions")
    digital_input = test.digital_input(channel=0)

    test.expect(digital_input).remain_high(from_tick=10, until_tick=20)
    test.expect(digital_input).remain_high(from_ms=30, until_ms=40)
    test.expect(digital_input).remain_high(from_s=0.05, until_s=0.06)

    assertions = tuple(test.assertions)
    assert all(isinstance(item, DigitalInputRemainHighAssertion) for item in assertions)
    assert [(item.from_tick, item.until_tick) for item in assertions] == [
        (10, 20),
        (30, 40),
        (50, 60),
    ]


def test_transition_assertion_stores_states_and_converted_range() -> None:
    test = HilRigTest(name="Transition assertion")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )
    digital_input = test.digital_input(channel=0)

    test.expect(digital_input).to_transition(
        from_state=False,
        to_state=True,
        between_ms=(9, 11),
    )

    assertion = tuple(test.assertions)[0]
    assert isinstance(assertion, DigitalInputTransitionAssertion)
    assert assertion.from_state is DigitalState.LOW
    assert assertion.to_state is DigitalState.HIGH
    assert (assertion.from_tick, assertion.until_tick) == (90, 110)


def test_transition_must_actually_change_state() -> None:
    test = HilRigTest(name="Invalid transition")
    expectation = test.expect(test.digital_input(channel=0))

    with pytest.raises(ValueError, match="must change state"):
        expectation.to_transition(
            from_state=True,
            to_state=True,
            between_ticks=(0, 1),
        )


def test_expect_rejects_a_channel_from_another_test() -> None:
    first = HilRigTest(name="First")
    second = HilRigTest(name="Second")

    with pytest.raises(TypeError, match="from this Test"):
        first.expect(second.digital_input(channel=0))
