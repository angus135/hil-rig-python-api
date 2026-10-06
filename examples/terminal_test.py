"""Small test definition loadable by the ``hil-rig`` terminal application."""

from hilrig import DigitalState, FrequencyMode, LogicVoltage, StartMode, Test


def build_test() -> Test:
    """Build a fresh test definition each time the terminal runs this file."""
    test = Test(name="Terminal digital example")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    feedback = test.digital_input(channel=0)
    feedback.configure(voltage=LogicVoltage.V3_3)

    command = test.digital_output(channel=0)
    command.configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    command.high(at_ms=100)
    command.low(at_ms=200)

    test.expect(feedback).high(at_ms=100)
    test.expect(feedback).remain_high(from_ms=100, until_ms=150)
    return test
