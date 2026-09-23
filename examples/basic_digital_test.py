"""Basic definition usable by both the terminal and direct Python execution."""

from hilrig import DigitalState, FrequencyMode, LogicVoltage, StartMode, Test


def build_test() -> Test:
    """Return a fresh digital input/output test for one terminal run."""
    test = Test(name="Digital input/output example")
    test.configure(
        frequency_mode=FrequencyMode.HZ_100,
        start_mode=StartMode.IMMEDIATE,
    )

    digital_input_ch1 = test.digital_input(channel=9)
    digital_input_ch1.configure(voltage=LogicVoltage.V3_3)

    digital_output_ch1 = test.digital_output(channel=0)
    digital_output_ch1.configure(voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW)

    test.expect(digital_input_ch1).remain_low(from_s=0, until_s=3)
    digital_output_ch1.high(at_s=3)
    test.expect(digital_input_ch1).remain_high(from_s=3, until_s=6)
    digital_output_ch1.low(at_s=6)
    test.expect(digital_input_ch1).remain_low(from_s=6, until_s=10)

    return test
