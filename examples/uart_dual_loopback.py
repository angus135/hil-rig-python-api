from hilrig import (
    FrequencyMode,
    StartMode,
    Test,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)


def build_test() -> Test:
    test = Test(name="UART1 and UART2 isolated loopback test")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    uart1 = test.uart(channel=0).named("UART_ch1")
    uart1.configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
        parity=UARTParity.NONE,
    )

    uart2 = test.uart(channel=1).named("UART_ch2")
    uart2.configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=115_200,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
        parity=UARTParity.NONE,
    )

    # Use single-byte payloads so each assertion tests channel activity,
    # independent of stream-fragment aggregation.
    uart1.write(data=b"\xa1", at_ms=100)
    uart1.write(data=b"\xa2", at_ms=300)
    uart2.write(data=b"\xb1", at_ms=500)
    uart2.write(data=b"\xb2", at_ms=700)

    test.expect(uart1).receive(b"\xa1", from_ms=100, until_ms=200)
    test.expect(uart1).receive(b"\xa2", from_ms=300, until_ms=400)
    test.expect(uart2).receive(b"\xb1", from_ms=500, until_ms=600)
    test.expect(uart2).receive(b"\xb2", from_ms=700, until_ms=800)

    return test


if __name__ == "__main__":
    build_test().compile()
