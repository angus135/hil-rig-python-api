"""Deterministic workload definitions for whole-rig loopback experiments."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR, Decimal
from random import Random

from hilrig.api import CAN, SPI, UART, Test
from hilrig.models.configuration import CANConfiguration, SPIConfiguration, UARTConfiguration


@dataclass(frozen=True, slots=True)
class LoopbackProfile:
    """Fixed communication configuration shared by one loopback sweep."""

    uart_baud_hz: int
    spi_clock_hz: int
    can_bitrate_hz: int
    uart_bits_per_byte: int = 10
    can_payload_bytes_per_frame: int = 8
    can_wire_bits_per_frame: int = 135
    max_uart_payload_bytes: int = 65535
    max_spi_payload_bytes: int = 65535

    def __post_init__(self) -> None:
        for name in (
            "uart_baud_hz",
            "spi_clock_hz",
            "can_bitrate_hz",
            "uart_bits_per_byte",
            "can_payload_bytes_per_frame",
            "can_wire_bits_per_frame",
            "max_uart_payload_bytes",
            "max_spi_payload_bytes",
        ):
            _positive_int(getattr(self, name), name=name)
        if self.can_payload_bytes_per_frame > 8:
            raise ValueError("can_payload_bytes_per_frame must not exceed 8")

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-compatible representation for manifests and hashing."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LoopbackWorkloadPoint:
    """Variables defining one distinct run within a loopback workload sweep."""

    frequency_hz: int
    duration_s: int
    target_utilization_percent: float
    burst_interval_ticks: int
    seed: int

    def __post_init__(self) -> None:
        _positive_int(self.frequency_hz, name="frequency_hz")
        _positive_int(self.duration_s, name="duration_s")
        _positive_int(self.burst_interval_ticks, name="burst_interval_ticks")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        _utilization_decimal(self.target_utilization_percent)

    @property
    def total_ticks(self) -> int:
        """Return the number of workload ticks before any result-drain allowance."""
        return self.frequency_hz * self.duration_s

    def to_dict(self) -> dict[str, int | float]:
        """Return a JSON-compatible requested-point representation."""
        return {
            "frequency_hz": self.frequency_hz,
            "duration_s": self.duration_s,
            "target_utilization_percent": self.target_utilization_percent,
            "burst_interval_ticks": self.burst_interval_ticks,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class ScheduledTransfer:
    """One compiled communication instruction at one execution tick."""

    tick: int
    payload_bytes: int
    wire_bits: int


@dataclass(frozen=True, slots=True)
class MaterializedTransfer:
    """One scheduled transfer with its deterministic payload bytes."""

    tick: int
    data: bytes
    wire_bits: int


@dataclass(frozen=True, slots=True)
class MaterializedLoopbackWorkload:
    """Payload evidence returned after applying a workload to a test."""

    uart: tuple[MaterializedTransfer, ...]
    spi: tuple[MaterializedTransfer, ...]
    can: tuple[MaterializedTransfer, ...]


@dataclass(frozen=True, slots=True)
class PeripheralWorkload:
    """Compiled workload and measured reality for one communication peripheral."""

    peripheral: str
    configured_bit_rate_hz: int
    requested_utilization_percent: float
    transfers: tuple[ScheduledTransfer, ...]
    actual_payload_bytes: int
    actual_wire_bits: int
    actual_utilization_percent: float
    active_tick_count: int
    average_payload_bytes_per_active_tick: float
    maximum_payload_bytes_per_active_tick: int

    @property
    def instruction_count(self) -> int:
        return len(self.transfers)


@dataclass(frozen=True, slots=True)
class CompiledLoopbackWorkload:
    """Requested loopback point and its exactly compiled communication schedule."""

    profile: LoopbackProfile
    point: LoopbackWorkloadPoint
    uart: PeripheralWorkload
    spi: PeripheralWorkload
    can: PeripheralWorkload
    workload_hash: str

    @property
    def instruction_count(self) -> int:
        return sum(item.instruction_count for item in (self.uart, self.spi, self.can))

    @property
    def payload_bytes(self) -> int:
        return sum(item.actual_payload_bytes for item in (self.uart, self.spi, self.can))

    def to_dict(self) -> dict[str, object]:
        """Return requested inputs and compiled reality as JSON-compatible data."""
        return {
            "workload_hash": self.workload_hash,
            "profile": self.profile.to_dict(),
            "point": self.point.to_dict(),
            "instruction_count": self.instruction_count,
            "payload_bytes": self.payload_bytes,
            "peripherals": {
                item.peripheral: {
                    "configured_bit_rate_hz": item.configured_bit_rate_hz,
                    "requested_utilization_percent": item.requested_utilization_percent,
                    "actual_utilization_percent": item.actual_utilization_percent,
                    "instruction_count": item.instruction_count,
                    "payload_bytes": item.actual_payload_bytes,
                    "wire_bits": item.actual_wire_bits,
                    "active_tick_count": item.active_tick_count,
                    "average_payload_bytes_per_active_tick": (
                        item.average_payload_bytes_per_active_tick
                    ),
                    "maximum_payload_bytes_per_active_tick": (
                        item.maximum_payload_bytes_per_active_tick
                    ),
                }
                for item in (self.uart, self.spi, self.can)
            },
        }


def compile_loopback_workload(
    profile: LoopbackProfile,
    point: LoopbackWorkloadPoint,
) -> CompiledLoopbackWorkload:
    """Compile one percentage target into deterministic UART, SPI, and CAN traffic."""
    if not isinstance(profile, LoopbackProfile):
        raise TypeError("profile must be a LoopbackProfile")
    if not isinstance(point, LoopbackWorkloadPoint):
        raise TypeError("point must be a LoopbackWorkloadPoint")

    uart = _compile_byte_workload(
        peripheral="uart",
        configured_bit_rate_hz=profile.uart_baud_hz,
        wire_bits_per_byte=profile.uart_bits_per_byte,
        max_payload_bytes=profile.max_uart_payload_bytes,
        point=point,
    )
    spi = _compile_byte_workload(
        peripheral="spi",
        configured_bit_rate_hz=profile.spi_clock_hz,
        wire_bits_per_byte=8,
        max_payload_bytes=profile.max_spi_payload_bytes,
        point=point,
    )
    can = _compile_can_workload(profile=profile, point=point)
    workload_hash = _workload_hash(profile, point)
    return CompiledLoopbackWorkload(
        profile=profile,
        point=point,
        uart=uart,
        spi=spi,
        can=can,
        workload_hash=workload_hash,
    )


def apply_loopback_communication_workload(
    test: Test,
    workload: CompiledLoopbackWorkload,
    *,
    uart: UART,
    spi: SPI,
    can_transmitter: CAN,
    can_frame_id: int = 0x321,
) -> MaterializedLoopbackWorkload:
    """Add one compiled UART/SPI/CAN schedule to an ordinary :class:`Test`.

    The resulting instructions are placed in named test groups so captured-run
    reports retain an informational record of the commanded traffic. Assertions
    remain the responsibility of the concrete loopback test because receive
    latency and channel topology are fixture-specific.
    """
    if not isinstance(test, Test):
        raise TypeError("test must be a Test")
    if not isinstance(workload, CompiledLoopbackWorkload):
        raise TypeError("workload must be a CompiledLoopbackWorkload")
    if test.configuration.frequency_mode.hertz != workload.point.frequency_hz:
        raise ValueError("The test frequency does not match the workload point")
    _require_profile_matches_channels(
        test,
        workload.profile,
        uart=uart,
        spi=spi,
        can_transmitter=can_transmitter,
    )
    if not isinstance(can_frame_id, int) or isinstance(can_frame_id, bool):
        raise TypeError("can_frame_id must be an integer")
    if not 0 <= can_frame_id <= 0x7FF:
        raise ValueError("can_frame_id must be an 11-bit standard CAN identifier")

    uart_random = Random(f"{workload.point.seed}:uart")
    uart_payloads = tuple(
        MaterializedTransfer(
            tick=transfer.tick,
            data=uart_random.randbytes(transfer.payload_bytes),
            wire_bits=transfer.wire_bits,
        )
        for transfer in workload.uart.transfers
    )
    with test.group("UART Workload"):
        for transfer in uart_payloads:
            uart.write(
                data=transfer.data,
                at_tick=transfer.tick,
            )

    spi_random = Random(f"{workload.point.seed}:spi")
    spi_payloads = tuple(
        MaterializedTransfer(
            tick=transfer.tick,
            data=spi_random.randbytes(transfer.payload_bytes),
            wire_bits=transfer.wire_bits,
        )
        for transfer in workload.spi.transfers
    )
    with test.group("SPI Workload"):
        for transfer in spi_payloads:
            spi.transfer(
                tx_data=transfer.data,
                rx_length=len(transfer.data),
                at_tick=transfer.tick,
            )

    can_random = Random(f"{workload.point.seed}:can")
    can_payloads = tuple(
        MaterializedTransfer(
            tick=transfer.tick,
            data=can_random.randbytes(transfer.payload_bytes),
            wire_bits=transfer.wire_bits,
        )
        for transfer in workload.can.transfers
    )
    with test.group("CAN Workload"):
        for transfer in can_payloads:
            can_transmitter.transmit(
                frame_id=can_frame_id,
                data=transfer.data,
                at_tick=transfer.tick,
            )
    return MaterializedLoopbackWorkload(
        uart=uart_payloads,
        spi=spi_payloads,
        can=can_payloads,
    )


def _compile_byte_workload(
    *,
    peripheral: str,
    configured_bit_rate_hz: int,
    wire_bits_per_byte: int,
    max_payload_bytes: int,
    point: LoopbackWorkloadPoint,
) -> PeripheralWorkload:
    wire_bit_budget = _wire_bit_budget(configured_bit_rate_hz, point)
    payload_byte_budget = wire_bit_budget // wire_bits_per_byte
    burst_ticks = _burst_ticks(point)
    transfers: list[ScheduledTransfer] = []
    for tick, payload_bytes in _distribute(payload_byte_budget, burst_ticks):
        remaining = payload_bytes
        while remaining:
            chunk = min(remaining, max_payload_bytes)
            transfers.append(
                ScheduledTransfer(
                    tick=tick,
                    payload_bytes=chunk,
                    wire_bits=chunk * wire_bits_per_byte,
                )
            )
            remaining -= chunk
    return _peripheral_workload(
        peripheral=peripheral,
        configured_bit_rate_hz=configured_bit_rate_hz,
        requested_utilization_percent=point.target_utilization_percent,
        duration_s=point.duration_s,
        transfers=tuple(transfers),
    )


def _require_profile_matches_channels(
    test: Test,
    profile: LoopbackProfile,
    *,
    uart: UART,
    spi: SPI,
    can_transmitter: CAN,
) -> None:
    for handle, expected_type, label in (
        (uart, UART, "uart"),
        (spi, SPI, "spi"),
        (can_transmitter, CAN, "can_transmitter"),
    ):
        if not isinstance(handle, expected_type) or handle._test is not test:
            raise TypeError(f"{label} must be a configured channel handle from this Test")
    uart_config = test.configuration.for_channel(uart.identity)
    spi_config = test.configuration.for_channel(spi.identity)
    can_config = test.configuration.for_channel(can_transmitter.identity)
    if not isinstance(uart_config, UARTConfiguration):
        raise ValueError("uart must be configured before applying a workload")
    if not isinstance(spi_config, SPIConfiguration):
        raise ValueError("spi must be configured before applying a workload")
    if not isinstance(can_config, CANConfiguration):
        raise ValueError("can_transmitter must be configured before applying a workload")
    configured_rates = (
        ("UART baud", uart_config.baud_hz, profile.uart_baud_hz),
        ("SPI clock", int(spi_config.baud.value), profile.spi_clock_hz),
        ("CAN bitrate", can_config.bitrate, profile.can_bitrate_hz),
    )
    for label, configured, requested in configured_rates:
        if configured != requested:
            raise ValueError(
                f"{label} {configured} does not match workload profile value {requested}"
            )


def _compile_can_workload(
    *,
    profile: LoopbackProfile,
    point: LoopbackWorkloadPoint,
) -> PeripheralWorkload:
    wire_bit_budget = _wire_bit_budget(profile.can_bitrate_hz, point)
    frame_budget = wire_bit_budget // profile.can_wire_bits_per_frame
    transfers = tuple(
        ScheduledTransfer(
            tick=tick,
            payload_bytes=profile.can_payload_bytes_per_frame * frame_count,
            wire_bits=profile.can_wire_bits_per_frame * frame_count,
        )
        for tick, frame_count in _distribute(frame_budget, _burst_ticks(point))
    )
    return _peripheral_workload(
        peripheral="can",
        configured_bit_rate_hz=profile.can_bitrate_hz,
        requested_utilization_percent=point.target_utilization_percent,
        duration_s=point.duration_s,
        transfers=transfers,
    )


def _peripheral_workload(
    *,
    peripheral: str,
    configured_bit_rate_hz: int,
    requested_utilization_percent: float,
    duration_s: int,
    transfers: tuple[ScheduledTransfer, ...],
) -> PeripheralWorkload:
    payload_bytes = sum(item.payload_bytes for item in transfers)
    wire_bits = sum(item.wire_bits for item in transfers)
    bytes_by_tick: dict[int, int] = {}
    for item in transfers:
        bytes_by_tick[item.tick] = bytes_by_tick.get(item.tick, 0) + item.payload_bytes
    active_tick_count = len(bytes_by_tick)
    average = payload_bytes / active_tick_count if active_tick_count else 0.0
    maximum = max(bytes_by_tick.values(), default=0)
    actual_utilization = 100 * wire_bits / (configured_bit_rate_hz * duration_s)
    return PeripheralWorkload(
        peripheral=peripheral,
        configured_bit_rate_hz=configured_bit_rate_hz,
        requested_utilization_percent=requested_utilization_percent,
        transfers=transfers,
        actual_payload_bytes=payload_bytes,
        actual_wire_bits=wire_bits,
        actual_utilization_percent=actual_utilization,
        active_tick_count=active_tick_count,
        average_payload_bytes_per_active_tick=average,
        maximum_payload_bytes_per_active_tick=maximum,
    )


def _burst_ticks(point: LoopbackWorkloadPoint) -> tuple[int, ...]:
    return tuple(range(0, point.total_ticks, point.burst_interval_ticks))


def _distribute(total_units: int, ticks: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    if total_units <= 0 or not ticks:
        return ()
    slot_count = len(ticks)
    scheduled: list[tuple[int, int]] = []
    previous_total = 0
    for index, tick in enumerate(ticks, start=1):
        # Ceil-based accumulation starts at tick zero while spreading fractional
        # units across the entire duration instead of front-loading the remainder.
        cumulative_total = (index * total_units + slot_count - 1) // slot_count
        units = cumulative_total - previous_total
        if units:
            scheduled.append((tick, units))
        previous_total = cumulative_total
    return tuple(scheduled)


def _wire_bit_budget(bit_rate_hz: int, point: LoopbackWorkloadPoint) -> int:
    requested = (
        Decimal(bit_rate_hz)
        * Decimal(point.duration_s)
        * _utilization_decimal(point.target_utilization_percent)
        / Decimal(100)
    )
    return int(requested.to_integral_value(rounding=ROUND_FLOOR))


def _workload_hash(profile: LoopbackProfile, point: LoopbackWorkloadPoint) -> str:
    document = {
        "definition": "hilrig.loopback-workload.v1",
        "profile": profile.to_dict(),
        "point": {
            **point.to_dict(),
            "target_utilization_percent": _canonical_decimal(point.target_utilization_percent),
        },
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_decimal(value: float) -> str:
    normalized = _utilization_decimal(value).normalize()
    return format(normalized, "f")


def _utilization_decimal(value: object) -> Decimal:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("target_utilization_percent must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("target_utilization_percent must be finite")
    decimal_value = Decimal(str(value))
    if not Decimal(0) <= decimal_value <= Decimal(100):
        raise ValueError("target_utilization_percent must be between 0 and 100")
    return decimal_value


def _positive_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
