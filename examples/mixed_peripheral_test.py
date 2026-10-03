"""One-of-each-peripheral variable-message hardware test.

Required wiring:
    * DOUT 1 -> DIN 1
    * UART 2 TX -> UART 2 RX
    * SPI 1 MOSI -> SPI 1 MISO
    * CAN 1 CAN_H/CAN_L <-> CAN 2 CAN_H/CAN_L, with common ground

PWM LV output and analogue input are enabled for capture/inspection. The
analogue input has no assertion here because its external voltage is fixture-
dependent, and PWM LV has no PWM input assertion in this minimal test.
"""

from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    SPIBaud,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    StartMode,
    Test,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)


def build_test() -> Test:
    """Construct a mixed digital, communication, PWM, and analogue test."""
    test = Test(name="Mixed digital UART SPI CAN PWM analogue test")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    # Digital channel 1 loopback: DOUT 1 (index 0) -> DIN 1 (index 0).
    din1 = test.digital_input(channel=0).configure(voltage=LogicVoltage.V3_3)
    dout1 = test.digital_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )
    test.expect(din1).remain_low(from_ms=0, until_ms=100)
    dout1.high(at_ms=100)
    test.expect(din1).remain_high(from_ms=110, until_ms=190)
    dout1.low(at_ms=200)
    test.expect(din1).remain_low(from_ms=210, until_ms=300)

    # Analogue input channel 1 (index 0), enabled without a fixture-specific assertion.
    test.analogue_input(channel=0)

    # PWM LV output channel 1 (index 0).
    test.pwm_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_frequency_hz=1_000,
        initial_duty_cycle=0.50,
        initially_enabled=True,
    )

    # UART 2 loopback.
    uart2 = test.uart(channel=1).configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )
    uart_payload = b"MIXED_UART_OK\r\n"
    uart2.write(data=uart_payload, at_ms=300)
    test.expect(uart2).receive(uart_payload, from_ms=300, until_ms=450)

    # SPI 1 loopback.
    spi1 = test.spi(channel=0).configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_5M625BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )
    spi_payload = b"SPI!"
    spi1.transfer(tx_data=spi_payload, rx_length=len(spi_payload), at_ms=400)
    test.expect(spi1).receive(spi_payload, from_ms=400, until_ms=550)

    # CAN2 -> CAN1 cross-channel loopback.
    can1 = test.can(channel=0).configure(bitrate=500_000)
    can2 = test.can(channel=1).configure(bitrate=500_000)
    can_payload = b"CAN_OK!"
    can2.transmit(frame_id=0x321, data=can_payload, at_ms=500)
    test.expect(can1).receive(
        frame_id=0x321,
        data=can_payload,
        from_ms=500,
        until_ms=650,
    )

    return test


if __name__ == "__main__":
    compiled = build_test().compile()
    print(f"Test: {compiled.name}")
    print(f"Frequency: {compiled.frequency_hz} Hz")
    print(f"Expected ticks: {compiled.expected_tick_count}")
    print(f"Instructions: {len(compiled.instructions)}")
    print(f"Assertions: {len(compiled.assertions)}")
