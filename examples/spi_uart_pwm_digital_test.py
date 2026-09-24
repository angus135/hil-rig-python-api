"""Multi-peripheral test exercising SPI (Channel 0 / SPI 1), UART (Channel 1 / UART 2),
PWM loopback (LV Output Channel 0 -> PWM Input Channel 0 / PWM IN 1), and Digital loopback
(Digital Output Channel 0 / DOUT 1 -> Digital Input Channel 9 / DIN 10).

Hardware Wiring Guide:
----------------------
1. Digital Loopback:
   - Connect DIGITAL OUTPUT Channel 0 (DOUT 1) -> DIGITAL INPUT Channel 9 (DIN 10)
   (3.3V logic level)

2. PWM Loopback:
   - Connect PWM OUTPUT Channel 0 (LV, 3.3V)   -> PWM INPUT Channel 0 (PWM IN 1)
   (3.3V logic level)
   * Note: If wired to PWM IN 2, change test.pwm_input(channel=0) to channel=1.

3. SPI Loopback:
   - Connect SPI Channel 0 (SPI 1) MOSI        -> SPI Channel 0 (SPI 1) MISO
   (3.3V logic level)

4. UART Loopback:
   - Connect UART Channel 1 (UART 2) TX        -> UART Channel 1 (UART 2) RX
   (3.3V logic level)
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
    """Construct and return the multi-peripheral HIL-RIG test with full assertions."""
    test = Test(name="SPI1, UART2, PWM LV->CH1, and DOUT1->DIN10 Multi-Peripheral Test")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    # =========================================================================
    # 1. Digital Loopback: Digital Output 0 (DOUT 1) -> Digital Input 9 (DIN 10)
    # =========================================================================
    din10 = test.digital_input(channel=9).configure(voltage=LogicVoltage.V3_3)
    dout1 = test.digital_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_state=DigitalState.LOW,
    )

    # Initial state verification (0 - 50 ms)
    test.expect(din10).remain_low(from_ms=0, until_ms=50)

    # Pulse 1: High at 50 ms, Low at 150 ms (100 ms pulse)
    dout1.high(at_ms=50)
    test.expect(din10).remain_high(from_ms=55, until_ms=145)
    dout1.low(at_ms=150)
    test.expect(din10).remain_low(from_ms=155, until_ms=250)

    # Pulse 2: High at 250 ms, Low at 400 ms (150 ms pulse)
    dout1.high(at_ms=250)
    test.expect(din10).remain_high(from_ms=255, until_ms=395)
    dout1.low(at_ms=400)
    test.expect(din10).remain_low(from_ms=405, until_ms=500)

    # Fast toggling pattern (500 ms - 700 ms) with midpoint sampling
    for t_ms in range(500, 700, 20):
        dout1.high(at_ms=t_ms)
        test.expect(din10).high(at_ms=t_ms + 5)
        dout1.low(at_ms=t_ms + 10)
        test.expect(din10).low(at_ms=t_ms + 15)

    test.expect(din10).remain_low(from_ms=705, until_ms=1000)

    # =========================================================================
    # 2. PWM Loopback: PWM Output Channel 0 (LV) -> PWM Input Channel 0 (PWM IN 1)
    # =========================================================================
    pwm_in1 = test.pwm_input(channel=0).configure(voltage=LogicVoltage.V3_3)
    pwm_out_lv = test.pwm_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_frequency_hz=1_000,
        initial_duty_cycle=0.50,
        initially_enabled=True,
    )

    # Stage 1: 1 kHz @ 50% duty (0.0s - 0.3s)
    test.expect(pwm_in1).frequency_remain_within(
        minimum_hz=990,
        maximum_hz=1010,
        from_s=0.05,
        until_s=0.28,
    )
    test.expect(pwm_in1).duty_cycle_remain_within(
        minimum_duty_cycle=0.48,
        maximum_duty_cycle=0.52,
        from_s=0.05,
        until_s=0.28,
    )

    # Stage 2: 10 kHz @ 25% duty (0.3s - 0.6s)
    pwm_out_lv.set(frequency_hz=10_000, duty_cycle=0.25, at_s=0.3)
    test.expect(pwm_in1).frequency_remain_within(
        minimum_hz=9_800,
        maximum_hz=10_200,
        from_s=0.35,
        until_s=0.58,
    )
    test.expect(pwm_in1).duty_cycle_remain_within(
        minimum_duty_cycle=0.23,
        maximum_duty_cycle=0.27,
        from_s=0.35,
        until_s=0.58,
    )

    # Stage 3: 50 kHz @ 80% duty (0.6s - 1.0s)
    pwm_out_lv.set(frequency_hz=50_000, duty_cycle=0.80, at_s=0.6)
    test.expect(pwm_in1).frequency_remain_within(
        minimum_hz=49_000,
        maximum_hz=51_000,
        from_s=0.65,
        until_s=0.98,
    )
    test.expect(pwm_in1).duty_cycle_remain_within(
        minimum_duty_cycle=0.78,
        maximum_duty_cycle=0.82,
        from_s=0.65,
        until_s=0.98,
    )

    # =========================================================================
    # 3. SPI Channel 0 (SPI 1): Master Transfers and Receive Assertions
    # =========================================================================
    spi1 = test.spi(channel=0)
    spi1.configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_5M625BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )

    # Schedule SPI transfers & expect matching loopback receipts
    spi_pkt1 = b"\xAA\x55\x01\x02"
    spi1.transfer(tx_data=spi_pkt1, rx_length=4, at_ms=100)
    test.expect(spi1).receive(spi_pkt1, from_ms=100, until_ms=250)

    spi_pkt2 = b"\xDE\xAD\xBE\xEF\xCA\xFE"
    spi1.transfer(tx_data=spi_pkt2, rx_length=6, at_ms=300)
    test.expect(spi1).receive(spi_pkt2, from_ms=300, until_ms=450)

    spi_pkt3 = b"\x00\xFF\x11\x22\x33\x44\x55\x66"
    spi1.transfer(tx_data=spi_pkt3, rx_length=8, at_ms=600)
    test.expect(spi1).receive(spi_pkt3, from_ms=600, until_ms=750)

    spi_pkt4 = b"\x5A\xA5"
    spi1.transfer(tx_data=spi_pkt4, rx_length=2, at_ms=850)
    test.expect(spi1).receive(spi_pkt4, from_ms=850, until_ms=990)

    # =========================================================================
    # 4. UART Channel 1 (UART 2): TTL 3.3V Serial Writes & Receive Assertions
    # =========================================================================
    uart2 = test.uart(channel=1)
    uart2.configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )

    # Schedule UART transmissions & expect matching loopback receipts
    uart_msg1 = b"\x01\x02\x03\x04READY\r\n"
    uart2.write(data=uart_msg1, at_ms=80)
    test.expect(uart2).receive(uart_msg1, from_ms=80, until_ms=180)

    uart_msg2 = "START_SYSTEM_TEST\n"
    uart2.write_text(data=uart_msg2, encoding="ascii", at_ms=200)
    test.expect(uart2).receive_text(uart_msg2, encoding="ascii", from_ms=200, until_ms=300)

    uart_msg3 = b"\x55\xAA\x12\x34\x56\x78"
    uart2.write(data=uart_msg3, at_ms=450)
    test.expect(uart2).receive(uart_msg3, from_ms=450, until_ms=550)

    uart_msg4 = "STATUS_OK_STAGE2\n"
    uart2.write_text(data=uart_msg4, encoding="ascii", at_ms=750)
    test.expect(uart2).receive_text(uart_msg4, encoding="ascii", from_ms=750, until_ms=850)

    uart_msg5 = "TEST_COMPLETE\r\n"
    uart2.write_text(data=uart_msg5, encoding="ascii", at_ms=900)
    test.expect(uart2).receive_text(uart_msg5, encoding="ascii", from_ms=900, until_ms=1000)

    return test


if __name__ == "__main__":
    test_instance = build_test()
    compiled = test_instance.compile()
    print("=================================================================")
    print("Multi-Peripheral Test Compiled Successfully!")
    print("=================================================================")
    print(f"Test Name:           {compiled.name}")
    print(f"Test ID:             0x{compiled.test_id:032x}")
    print(f"Frequency:           {compiled.frequency_hz} Hz ({compiled.frequency_mode})")
    print(f"Expected Ticks:      {compiled.expected_tick_count} ticks ({compiled.expected_tick_count / compiled.frequency_hz:.2f} s)")
    print(f"Instructions:        {len(compiled.instructions)}")
    print(f"Assertions:          {len(compiled.assertions)}")
    print("=================================================================")
