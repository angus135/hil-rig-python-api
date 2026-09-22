"""Basic definition usable by both the terminal and direct Python execution."""

from hilrig import DigitalState, FrequencyMode, LogicVoltage, StartMode, Test


def build_test() -> Test:
    """Return a fresh digital input/output test for one terminal run."""
    test = Test(name="Digital input/output example")
    test.configure(
        frequency_mode=FrequencyMode.HZ_100,
        start_mode=StartMode.IMMEDIATE,
    )

    digital_input_ch1 = test.digital_input(channel=0)
    digital_input_ch1.configure(voltage=LogicVoltage.V3_3)

    ditigal_output_ch10 = test.digital_output(channel=9)
    ditigal_output_ch10.configure(voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW)
    
    ditigal_output_ch10.high(at_s=3)
    ditigal_output_ch10.low(at_s=6)

    test.expect(digital_input_ch1).remain_high( from_s=3.1, until_s=6)
    return test
