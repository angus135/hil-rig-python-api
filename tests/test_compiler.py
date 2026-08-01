import pytest

from hilrig import Test as HilRigTest
from hilrig.exceptions import TimingError, ValidationError
from hilrig.models.instructions import DigitalLevel


def test_compile_orders_and_groups_instructions_stably() -> None:
    test = HilRigTest("Out-of-order definition")
    output = test.digital_out(0)
    output.high(at=200)
    output.low(at=100)
    output.high(at=100)

    plan = test.compile()

    assert [slot.timestamp for slot in plan.time_slots] == [100, 200]
    assert [instruction.level for instruction in plan.time_slots[0].instructions] == [
        DigitalLevel.LOW,
        DigitalLevel.HIGH,
    ]


def test_compile_is_idempotent() -> None:
    test = HilRigTest("Compile once")
    test.digital_out(0).high(at=0)

    first_plan = test.compile()
    second_plan = test.compile()

    assert second_plan is first_plan


def test_empty_test_cannot_be_compiled() -> None:
    test = HilRigTest("Empty")

    with pytest.raises(ValidationError, match="at least one instruction"):
        test.compile()

    assert not test.is_compiled


def test_negative_timestamp_is_rejected_during_compilation() -> None:
    test = HilRigTest("Invalid timing")
    test.digital_out(0).high(at=-1)

    with pytest.raises(TimingError, match="non-negative"):
        test.compile()

    assert not test.is_compiled
