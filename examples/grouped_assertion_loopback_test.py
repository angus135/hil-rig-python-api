"""Runnable loopback test demonstrating named assertion groups and subjects.

Required wiring:
    * DOUT 1 (digital output index 0) -> DIN 10 (digital input index 9)
    * PWM LV 1 (PWM output index 0) -> PWM IN 1 (PWM input index 0)
    * UART 1 TX (UART index 0) -> UART 1 RX (UART index 0)

All signals use 3.3 V logic. The UART payloads are deliberately one byte each so
the receive assertions do not depend on serial stream-fragment aggregation.
"""

from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    Test,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)


def build_test() -> Test:
    """Build one grouped digital, PWM, and UART hardware loopback test."""
    test = Test(name="Grouped digital PWM and UART loopback")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    DIN_ch10 = test.digital_input(channel=9).named("DIN_ch10").configure(voltage=LogicVoltage.V3_3)
    DOUT_ch1 = (
        test.digital_output(channel=0)
        .named("DOUT_ch1")
        .configure(
            voltage=LogicVoltage.V3_3,
            initial_state=DigitalState.LOW,
        )
    )
    with test.group("Digital Loopback Tests"):
        test.expect(DIN_ch10).remain_low(from_ms=0, until_ms=90)
        DOUT_ch1.high(at_ms=100)
        test.expect(DIN_ch10).remain_high(from_ms=110, until_ms=190)
        DOUT_ch1.low(at_ms=200)
        test.expect(DIN_ch10).remain_low(from_ms=210, until_ms=300)

    PWM_IN_ch1 = test.pwm_input(channel=0).named("PWM_IN_ch1").configure(voltage=LogicVoltage.V3_3)
    PWM_OUT_LV_ch1 = (
        test.pwm_output(channel=0)
        .named("PWM_OUT_LV_ch1")
        .configure(
            voltage=LogicVoltage.V3_3,
            initial_frequency_hz=1_000,
            initial_duty_cycle=0.50,
            initially_enabled=True,
        )
    )
    with test.group("PWM Tests"):
        test.expect(PWM_IN_ch1).waveform_near(
            frequency_hz=1_000,
            frequency_tolerance_hz=20,
            duty_cycle=0.50,
            duty_cycle_tolerance=0.03,
            at_ms=100,
        )
        PWM_OUT_LV_ch1.set(frequency_hz=5_000, duty_cycle=0.25, at_ms=300)
        test.expect(PWM_IN_ch1).waveform_near(
            frequency_hz=5_000,
            frequency_tolerance_hz=100,
            duty_cycle=0.25,
            duty_cycle_tolerance=0.03,
            at_ms=400,
        )

    UART_ch1 = (
        test.uart(channel=0)
        .named("UART_ch1")
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )
    with test.group("UART Tests"):
        UART_ch1.write(data=b"\xa1", at_ms=500)
        test.expect(UART_ch1).receive(
            b"\xa1",
            from_ms=500,
            until_ms=600,
        )
        UART_ch1.write(data=b"\xa2", at_ms=700)
        test.expect(UART_ch1).receive(
            b"\xa2",
            from_ms=700,
            until_ms=800,
        )

    return test


if __name__ == "__main__":
    compiled = build_test().compile()
    print(f"Test: {compiled.name}")
    print(f"Frequency: {compiled.frequency_hz} Hz")
    print(f"Expected ticks: {compiled.expected_tick_count}")
    print(f"Instructions: {len(compiled.instructions)}")
    print(f"Assertion groups: {len(compiled.assertion_groups)}")
    for group in compiled.assertion_groups:
        assertion_count = sum(
            assertion.group_id == group.group_id for assertion in compiled.assertions
        )
        print(f"  {group.name}: {assertion_count} assertions")
