"""Dense fixed-size instruction stress test.

This test emits fixed-size instructions for UART2, SPI1, and CAN2 on every
logical tick. Set ``TARGET_UTILIZATION_PERCENT`` to 20, 40, 60, 80, 100, or
higher and repeat the run until the selected peripheral reaches its practical
limit.

Required wiring:
    * UART 2 TX -> UART 2 RX
    * SPI 1 MOSI -> SPI 1 MISO
    * CAN 1 CAN_H/CAN_L <-> CAN 2 CAN_H/CAN_L, with common ground
"""

from hilrig import (
    FrequencyMode,
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


STRESS_DURATION_SECONDS = 1  # Set to 1-2 seconds (e.g. 1s = 10,000 ticks, 2s = 20,000 ticks at 10 kHz)
EXECUTION_FREQUENCY_HZ = 10_000
BREAKING_POINT_TICKS = STRESS_DURATION_SECONDS * EXECUTION_FREQUENCY_HZ
TARGET_UTILIZATION_PERCENT = 80
UART_BAUD_HZ = 460_800
SPI_BAUD_HZ = 5_625_000
CAN_BITRATE_HZ = 500_000
UART_BITS_PER_BYTE = 10  # 8N1
CAN_WIRE_BITS_PER_FRAME = 135  # standard 8-byte frame at 500 kbps = 270 us wire time
CAN_FRAME_ID = 0x321


def _fixed_payload_size(*, wire_bits_per_second: int, bits_per_unit: int) -> int:
    """Choose one integer payload size per tick near the requested utilization."""
    requested = (
        wire_bits_per_second
        * TARGET_UTILIZATION_PERCENT
        // (100 * EXECUTION_FREQUENCY_HZ * bits_per_unit)
    )
    return max(1, requested)


CAN_INTERVAL_TICKS = 3  # Send 1 frame every 3 ticks (300 us) for 90% CAN bus load at 500 kbps


def _can_interval_ticks() -> int:
    """Return the tick interval between 8-byte CAN frames."""
    return CAN_INTERVAL_TICKS


def build_test() -> Test:
    """Construct a fixed-size instruction stress test."""
    test = Test(name="Fixed-size UART SPI CAN instruction stress")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    uart2 = test.uart(channel=1).configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=UART_BAUD_HZ,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )
    spi1 = test.spi(channel=0).configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_5M625BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )
    can1 = test.can(channel=0).configure(bitrate=CAN_BITRATE_HZ)
    can2 = test.can(channel=1).configure(bitrate=CAN_BITRATE_HZ)

    uart_payload_size = _fixed_payload_size(
        wire_bits_per_second=UART_BAUD_HZ,
        bits_per_unit=UART_BITS_PER_BYTE,
    )
    spi_payload_size = _fixed_payload_size(
        wire_bits_per_second=SPI_BAUD_HZ,
        bits_per_unit=8,
    )
    can_interval = _can_interval_ticks()
    uart_payload = b"U" * uart_payload_size
    spi_payload = b"S" * spi_payload_size
    can_payload = b"C" * 8

    drain_ticks = 10  # Drain ticks at the end of the stimulus burst (1 ms at 10 kHz)
    stimulus_ticks = max(1, BREAKING_POINT_TICKS - drain_ticks)

    for tick in range(stimulus_ticks):
        uart2.write(data=uart_payload, at_tick=tick)
        spi1.transfer(
            tx_data=spi_payload,
            rx_length=spi_payload_size,
            at_tick=tick,
        )
        if tick % can_interval == 0:
            can2.transmit(
                frame_id=CAN_FRAME_ID,
                data=can_payload,
                at_tick=tick,
            )

    last_stimulus_tick = stimulus_ticks - 1
    last_can_tick = (last_stimulus_tick // can_interval) * can_interval

    # First-tick assertions (verifies startup latency <= 1 tick drift)
    test.expect(uart2).receive(
        uart_payload,
        from_tick=0,
        until_tick=2,
    )
    test.expect(spi1).receive(
        spi_payload,
        from_tick=0,
        until_tick=2,
    )
    # CAN frame wire time at 500 kbps is 270 us (2.7 ticks) -> arrives in tick 2 or 3
    test.expect(can1).receive(
        frame_id=CAN_FRAME_ID,
        data=can_payload,
        from_tick=0,
        until_tick=4,
    )

    # End-of-burst assertions (strictly bounds total accumulated drift to <= 1 tick)
    test.expect(uart2).receive(
        uart_payload,
        from_tick=last_stimulus_tick,
        until_tick=last_stimulus_tick + 2,
    )
    test.expect(spi1).receive(
        spi_payload,
        from_tick=last_stimulus_tick,
        until_tick=last_stimulus_tick + 2,
    )
    test.expect(can1).receive(
        frame_id=CAN_FRAME_ID,
        data=can_payload,
        from_tick=last_can_tick,
        until_tick=last_can_tick + 4,
    )
    return test


if __name__ == "__main__":
    compiled = build_test().compile()
    uart_payload_size = _fixed_payload_size(
        wire_bits_per_second=UART_BAUD_HZ,
        bits_per_unit=UART_BITS_PER_BYTE,
    )
    spi_payload_size = _fixed_payload_size(
        wire_bits_per_second=SPI_BAUD_HZ,
        bits_per_unit=8,
    )
    can_interval = _can_interval_ticks()
    print(f"Test: {compiled.name}")
    print(f"Dense ticks: {BREAKING_POINT_TICKS}")
    print(f"Instructions: {len(compiled.instructions)}")
    uart_bytes_per_second = uart_payload_size * EXECUTION_FREQUENCY_HZ
    spi_bytes_per_second = spi_payload_size * EXECUTION_FREQUENCY_HZ
    can_frames_per_second = EXECUTION_FREQUENCY_HZ / can_interval
    can_bits_per_second = can_frames_per_second * CAN_WIRE_BITS_PER_FRAME
    print(f"Requested utilization: {TARGET_UTILIZATION_PERCENT}%")
    print(f"UART: {uart_payload_size} bytes/tick @ {UART_BAUD_HZ/1e6:.1f} MBaud ({100 * uart_bytes_per_second * UART_BITS_PER_BYTE / UART_BAUD_HZ:.1f}% wire time)")
    print(f"SPI: {spi_payload_size} bytes/tick @ {SPI_BAUD_HZ/1e6:.3f} MHz ({100 * spi_bytes_per_second * 8 / SPI_BAUD_HZ:.1f}% wire time)")
    print(f"CAN: 1 frame every {can_interval} ticks ({can_frames_per_second:.1f} frames/s, {100 * can_bits_per_second / CAN_BITRATE_HZ:.1f}% wire time)")
