from __future__ import annotations

import pytest

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
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
    UtilizationBinarySearch,
    apply_loopback_communication_workload,
    burst_sweep_points,
    compile_loopback_workload,
)
from hilrig import Test as HilRigTest


def _profile() -> LoopbackProfile:
    return LoopbackProfile(
        uart_baud_hz=1_000_000,
        spi_clock_hz=8_000_000,
        can_bitrate_hz=1_000_000,
        can_wire_bits_per_frame=100,
    )


def _point(**changes: object) -> LoopbackWorkloadPoint:
    values = {
        "frequency_hz": 1_000,
        "duration_s": 1,
        "target_utilization_percent": 50.0,
        "burst_interval_ticks": 1,
        "seed": 42,
    }
    values.update(changes)
    return LoopbackWorkloadPoint(**values)  # type: ignore[arg-type]


def test_loopback_workload_compiles_equal_wire_utilization() -> None:
    compiled = compile_loopback_workload(_profile(), _point())

    assert compiled.uart.actual_payload_bytes == 50_000
    assert compiled.uart.actual_wire_bits == 500_000
    assert compiled.spi.actual_payload_bytes == 500_000
    assert compiled.spi.actual_wire_bits == 4_000_000
    assert compiled.can.actual_payload_bytes == 40_000
    assert compiled.can.actual_wire_bits == 500_000
    assert compiled.uart.actual_utilization_percent == 50.0
    assert compiled.spi.actual_utilization_percent == 50.0
    assert compiled.can.actual_utilization_percent == 50.0
    assert len(compiled.workload_hash) == 64


def test_burst_interval_preserves_totals_and_increases_peak_bytes() -> None:
    smooth = compile_loopback_workload(_profile(), _point(burst_interval_ticks=1))
    burst = compile_loopback_workload(_profile(), _point(burst_interval_ticks=100))

    for smooth_item, burst_item in (
        (smooth.uart, burst.uart),
        (smooth.spi, burst.spi),
        (smooth.can, burst.can),
    ):
        assert burst_item.actual_payload_bytes == smooth_item.actual_payload_bytes
        assert burst_item.actual_wire_bits == smooth_item.actual_wire_bits
        assert burst_item.actual_utilization_percent == smooth_item.actual_utilization_percent
        assert (
            burst_item.maximum_payload_bytes_per_active_tick
            > smooth_item.maximum_payload_bytes_per_active_tick
        )


def test_fractional_can_rate_is_spread_across_the_full_duration() -> None:
    profile = LoopbackProfile(
        uart_baud_hz=460_800,
        spi_clock_hz=5_625_000,
        can_bitrate_hz=500_000,
        can_wire_bits_per_frame=135,
    )
    compiled = compile_loopback_workload(
        profile,
        _point(target_utilization_percent=10),
    )
    can_ticks = tuple(item.tick for item in compiled.can.transfers)

    assert len(can_ticks) == 370
    assert can_ticks[0] == 0
    assert can_ticks[-1] >= 997
    assert 1 not in can_ticks


def test_payloads_are_split_to_protocol_safe_instruction_sizes() -> None:
    compiled = compile_loopback_workload(
        _profile(),
        _point(burst_interval_ticks=1_000, target_utilization_percent=100),
    )

    assert max(item.payload_bytes for item in compiled.uart.transfers) <= 255
    assert max(item.payload_bytes for item in compiled.spi.transfers) <= 255
    assert all(item.payload_bytes == 8 for item in compiled.can.transfers)


def test_workload_hash_covers_profile_and_requested_point() -> None:
    first = compile_loopback_workload(_profile(), _point())
    same = compile_loopback_workload(_profile(), _point())
    changed = compile_loopback_workload(_profile(), _point(seed=43))

    assert first.workload_hash == same.workload_hash
    assert changed.workload_hash != first.workload_hash


def test_compiled_workload_can_populate_an_ordinary_grouped_test() -> None:
    profile = LoopbackProfile(
        uart_baud_hz=1_000_000,
        spi_clock_hz=5_625_000,
        can_bitrate_hz=1_000_000,
        can_wire_bits_per_frame=100,
    )
    workload = compile_loopback_workload(
        profile,
        _point(target_utilization_percent=1),
    )
    test = HilRigTest("Applied workload").configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )
    uart = test.uart(channel=0).configure(
        mode=UARTMode.TTL_3V3,
        baud_hz=profile.uart_baud_hz,
        parity=UARTParity.NONE,
        length=UARTLengthBits.EIGHT,
        stop=UARTStopBits.ONE,
    )
    spi = test.spi(channel=0).configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_5M625BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )
    can = test.can(channel=1).configure(bitrate=profile.can_bitrate_hz)

    materialized = apply_loopback_communication_workload(
        test,
        workload,
        uart=uart,
        spi=spi,
        can_transmitter=can,
    )
    compiled = test.compile()

    assert len(compiled.instructions) == workload.instruction_count
    assert {group.name for group in compiled.assertion_groups} >= {
        "UART Workload",
        "SPI Workload",
        "CAN Workload",
    }
    assert all(instruction.group_id is not None for instruction in compiled.instructions)
    assert len(materialized.uart) == workload.uart.instruction_count
    assert len(materialized.spi) == workload.spi.instruction_count
    assert len(materialized.can) == workload.can.instruction_count
    assert len(materialized.uart[0].data) == workload.uart.transfers[0].payload_bytes


def test_binary_search_repeats_each_boundary_and_midpoint() -> None:
    search = UtilizationBinarySearch(
        minimum_percent=0,
        maximum_percent=100,
        resolution_percent=10,
        repeats_per_point=3,
    )

    while not search.complete:
        percentage = search.next_percent()
        assert percentage is not None
        passed = percentage <= 60
        search.record(passed)

    result = search.result()
    assert result.highest_passing_percent == 60
    assert result.lowest_failing_percent == 70
    assert all(len(item.outcomes) == 3 for item in result.observations)


def test_binary_search_records_mixed_repeat_evidence() -> None:
    search = UtilizationBinarySearch(
        maximum_percent=100,
        resolution_percent=100,
        repeats_per_point=3,
    )
    for outcome in (True, False, True):
        search.record(outcome)
    for outcome in (False, True, False):
        search.record(outcome)

    result = search.result()
    assert result.highest_passing_percent == 0
    assert result.lowest_failing_percent == 100
    assert result.unstable_percentages == (0.0, 100.0)


def test_post_boundary_burst_points_apply_relative_headroom() -> None:
    points = burst_sweep_points(
        _point(),
        maximum_passing_percent=80,
        headroom_percent=10,
        burst_intervals_ticks=(1, 10, 100),
    )

    assert [point.target_utilization_percent for point in points] == [72.0, 72.0, 72.0]
    assert [point.burst_interval_ticks for point in points] == [1, 10, 100]


@pytest.mark.parametrize("percentage", [-1, 101, float("nan")])
def test_invalid_utilization_is_rejected(percentage: float) -> None:
    with pytest.raises(ValueError):
        _point(target_utilization_percent=percentage)
