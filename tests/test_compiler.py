import pytest

from hilrig import FrozenTestError
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
