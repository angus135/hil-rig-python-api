import pytest

from hilrig import FrequencyMode, FrozenTestError, StartMode
from hilrig import Test as HilRigTest
from hilrig.models.instructions import DigitalOutputAction


def test_preliminary_compile_preserves_test_id_and_groups_stably() -> None:
    test = HilRigTest(name="Out-of-order definition")
    output = test.digital_output(channel=0)
    output.high(at_tick=200)
    output.low(at_tick=100)
    output.toggle(at_tick=100)

    plan = test.compile()

    assert plan.test_id == test.test_id
    assert [slot.timestamp for slot in plan.time_slots] == [100, 200]
    assert [instruction.action for instruction in plan.time_slots[0].instructions] == [
        DigitalOutputAction.LOW,
        DigitalOutputAction.TOGGLE,
    ]
    assert [instruction.instruction_id for instruction in plan.time_slots[0].instructions] == [
        1,
        2,
    ]


def test_empty_internal_model_can_be_compiled() -> None:
    test = HilRigTest(name="Observation-only test")

    plan = test.compile()

    assert plan.time_slots == ()
    assert plan.expected_tick_count == 1_001


@pytest.mark.parametrize(
    ("frequency_mode", "expected_tick_count", "tick_period_ns"),
    [
        (FrequencyMode.HZ_100, 101, 10_000_000),
        (FrequencyMode.HZ_1K, 1_001, 1_000_000),
        (FrequencyMode.HZ_10K, 10_001, 100_000),
    ],
)
def test_observation_only_test_captures_one_second_inclusively(
    frequency_mode: FrequencyMode,
    expected_tick_count: int,
    tick_period_ns: int,
) -> None:
    test = HilRigTest(name="Observation duration")
    test.configure(frequency_mode=frequency_mode, start_mode=StartMode.IMMEDIATE)

    plan = test.compile()

    assert plan.expected_tick_count == expected_tick_count
    assert plan.tick_period_ns == tick_period_ns


def test_expected_tick_count_uses_latest_assertion_end_plus_one_second() -> None:
    test = HilRigTest(name="Assertion determines duration")
    test.configure(frequency_mode=FrequencyMode.HZ_1K, start_mode=StartMode.IMMEDIATE)
    output = test.digital_output(channel=0)
    digital_input = test.digital_input(channel=0)
    output.high(at_tick=200)
    test.expect(digital_input).remain_high(from_tick=100, until_tick=750)

    plan = test.compile()

    assert plan.expected_tick_count == 1_751


def test_expected_tick_count_uses_latest_stimulus_when_it_is_later() -> None:
    test = HilRigTest(name="Stimulus determines duration")
    test.configure(frequency_mode=FrequencyMode.HZ_100, start_mode=StartMode.IMMEDIATE)
    output = test.digital_output(channel=0)
    digital_input = test.digital_input(channel=0)
    test.expect(digital_input).high(at_tick=20)
    output.high(at_tick=80)

    plan = test.compile()

    assert plan.expected_tick_count == 181


def test_successful_preliminary_compilation_freezes_all_model_changes() -> None:
    test = HilRigTest(name="Frozen test")
    output = test.digital_output(channel=0)
    digital_input = test.digital_input(channel=0)
    test.compile()

    with pytest.raises(FrozenTestError):
        output.high(at_tick=1)

    with pytest.raises(FrozenTestError):
        test.expect(digital_input).high(at_tick=1)

    with pytest.raises(FrozenTestError):
        test.digital_output(channel=1)
