import pytest

from hilrig import FrequencyMode, FrozenTestError
from hilrig import Test as HilRigTest
from hilrig.models.instructions import DigitalLevel


def test_digital_output_handle_schedules_readable_instructions() -> None:
    test = HilRigTest("LED sequence")

    led = test.digital_out(2)
    led.high(at=10).low(at=20)

    instructions = tuple(test.instructions)
    assert len(instructions) == 2
    assert instructions[0].channel.index == 2
    assert instructions[0].level is DigitalLevel.HIGH
    assert instructions[1].level is DigitalLevel.LOW


def test_channel_handle_is_reused() -> None:
    test = HilRigTest("Stable handles")

    assert test.digital_out(0) is test.digital_out(0)


def test_configuration_is_updated_before_compilation() -> None:
    test = HilRigTest("Fast test")

    returned_test = test.configure(mode=FrequencyMode.KHZ_10)

    assert returned_test is test
    assert test.configuration.frequency_mode is FrequencyMode.KHZ_10


def test_successful_compilation_freezes_the_test() -> None:
    test = HilRigTest("Frozen test")
    test.digital_out(0).high(at=0)
    test.compile()

    with pytest.raises(FrozenTestError):
        test.digital_out(0).low(at=1)

    with pytest.raises(FrozenTestError):
        test.configure(mode=FrequencyMode.HZ_100)

    with pytest.raises(FrozenTestError):
        test.digital_out(1)


@pytest.mark.parametrize("name", ["", "   ", None])
def test_test_requires_a_name(name: str | None) -> None:
    with pytest.raises(ValueError):
        HilRigTest(name)  # type: ignore[arg-type]
