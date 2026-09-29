"""One independently runnable UART/SPI/CAN utilisation point.

This is the first rig-facing building block for an adaptive sweep. The terminal's
existing ``run`` command calls ``build_test()`` with the conservative default point.
Sweep orchestration can call ``build_test_for_point(point)`` for every independently
persisted run.

Required wiring:
    * UART 2 TX -> UART 2 RX
    * SPI 1 MOSI -> SPI 1 MISO
    * CAN 1 CAN_H/CAN_L <-> CAN 2 CAN_H/CAN_L, with common ground
"""

import json

from hilrig import (
    FrequencyMode,
    LoopbackProfile,
    LoopbackWorkloadPoint,
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
    apply_loopback_communication_workload,
    compile_loopback_workload,
)

PROFILE = LoopbackProfile(
    uart_baud_hz=1_000_000,
    spi_clock_hz=1_406_000,
    can_bitrate_hz=500_000,
    uart_bits_per_byte=10,
    can_payload_bytes_per_frame=8,
    can_wire_bits_per_frame=135,
)

DEFAULT_POINT = LoopbackWorkloadPoint(
    frequency_hz=100,
    duration_s=1,
    target_utilization_percent=80.0,
    burst_interval_ticks=1,
    seed=1,
)

CAN_FRAME_ID = 0x321


def _boundary_transfers(transfers):
    if len(transfers) <= 1:
        return transfers
    return (transfers[0], transfers[-1])


def build_test_for_point(point: LoopbackWorkloadPoint) -> Test:
    """Build one fresh test for one requested utilisation point."""
    try:
        frequency_mode = FrequencyMode(point.frequency_hz)
    except ValueError as error:
        raise ValueError(f"Unsupported frequency_hz: {point.frequency_hz}") from error

    workload = compile_loopback_workload(PROFILE, point)
    test = Test(
        name=(
            f"Communication loopback {point.target_utilization_percent:g}% "
            f"every {point.burst_interval_ticks} ticks"
        )
    )
    test.configure(
        frequency_mode=frequency_mode,
        start_mode=StartMode.IMMEDIATE,
    )

    UART_ch2 = (
        test.uart(channel=1)
        .named("UART_ch2")
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=PROFILE.uart_baud_hz,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )
    SPI_ch1 = (
        test.spi(channel=0)
        .named("SPI_ch1")
        .configure(
            role=SPIRole.MASTER,
            baud=SPIBaud.BAUD_1M406BIT,
            data_size=SPISize.SIZE_8BIT,
            mode=SPIMode.MODE_0,
            first_bit=SPIFirst.MSB,
        )
    )
    CAN_ch1 = test.can(channel=0).named("CAN_ch1").configure(bitrate=PROFILE.can_bitrate_hz)
    CAN_ch2 = test.can(channel=1).named("CAN_ch2").configure(bitrate=PROFILE.can_bitrate_hz)

    payloads = apply_loopback_communication_workload(
        test,
        workload,
        uart=UART_ch2,
        spi=SPI_ch1,
        can_transmitter=CAN_ch2,
        can_frame_id=CAN_FRAME_ID,
    )

    # Smoke-check the first and final transfers. Full-stream verification can be
    # introduced once capture aggregation behaviour has been characterized on-rig.
    with test.group("UART Workload"):
        for transfer in _boundary_transfers(payloads.uart):
            test.expect(UART_ch2).receive(
                transfer.data,
                allow_stream_match=True,
                from_tick=transfer.tick,
                until_tick=transfer.tick + 20,
            )
    with test.group("SPI Workload"):
        for transfer in _boundary_transfers(payloads.spi):
            test.expect(SPI_ch1).receive(
                transfer.data,
                allow_stream_match=True,
                from_tick=transfer.tick,
                until_tick=transfer.tick + 20,
            )
    with test.group("CAN Workload"):
        for transfer in _boundary_transfers(payloads.can):
            test.expect(CAN_ch1).receive(
                frame_id=CAN_FRAME_ID,
                data=transfer.data,
                from_tick=transfer.tick,
                until_tick=transfer.tick + 20,
            )
    return test


def build_test() -> Test:
    """Build the conservative point used by the existing terminal run command."""
    return build_test_for_point(DEFAULT_POINT)


if __name__ == "__main__":
    compiled_workload = compile_loopback_workload(PROFILE, DEFAULT_POINT)
    compiled_test = build_test().compile()
    print(json.dumps(compiled_workload.to_dict(), indent=2))
    print(f"Compiled test instructions: {len(compiled_test.instructions)}")
    print(f"Compiled test assertions: {len(compiled_test.assertions)}")
