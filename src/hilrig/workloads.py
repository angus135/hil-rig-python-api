"""Deterministic workload definitions for whole-rig loopback experiments."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from random import Random
from typing import Any

from hilrig.api import CAN, SPI, UART, Test
from hilrig.models.configuration import (
    CANConfiguration,
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    SPIBaud,
    SPIConfiguration,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    StartMode,
    UARTConfiguration,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)

MAX_STIMULUS_TICKS: int = 900_000


class ExposureClass(str, Enum):
    """Authoritative run duration classes in accordance with test matrix."""

    DISCOVERY = "discovery"
    CONFIRMATION = "confirmation"
    SOAK_60S = "soak_60s"
    SOAK_5MIN = "soak_5min"

    @property
    def nominal_duration_s(self) -> int:
        """Return the standard nominal stimulus duration in seconds."""
        match self:
            case ExposureClass.DISCOVERY:
                return 2
            case ExposureClass.CONFIRMATION:
                return 10
            case ExposureClass.SOAK_60S:
                return 60
            case ExposureClass.SOAK_5MIN:
                return 300

    @property
    def duration_s(self) -> int:
        """Return the nominal duration in seconds (alias for backwards compatibility)."""
        return self.nominal_duration_s

    @property
    def minimum_ticks_floor(self) -> int:
        """Return the minimum required tick floor to prevent deceptive low-exposure passes."""
        match self:
            case ExposureClass.DISCOVERY:
                return 1_000
            case ExposureClass.CONFIRMATION:
                return 3_000
            case ExposureClass.SOAK_60S:
                return 6_000
            case ExposureClass.SOAK_5MIN:
                return 30_000

    def duration_s_for_frequency(self, frequency_hz: int) -> int:
        """Return duration in seconds enforcing minimum tick floor."""
        req_seconds_for_floor = (self.minimum_ticks_floor + frequency_hz - 1) // frequency_hz
        return max(self.nominal_duration_s, req_seconds_for_floor)

    def stimulus_ticks(self, frequency_hz: int, max_ticks: int = MAX_STIMULUS_TICKS) -> int:
        """Return the authorized stimulus tick count for a frequency, with floor and cap."""
        dur_s = self.duration_s_for_frequency(frequency_hz)
        return min(dur_s * frequency_hz, max_ticks)


def get_exposure_duration_s(exposure: ExposureClass | str, frequency_hz: int | None = None) -> int:
    """Return the duration in seconds for an exposure class, optionally applying frequency floor."""
    if isinstance(exposure, str):
        exposure = ExposureClass(exposure.lower())
    if frequency_hz is not None:
        return exposure.duration_s_for_frequency(frequency_hz)
    return exposure.nominal_duration_s


def get_exposure_ticks(frequency_hz: int, exposure: ExposureClass | str) -> int:
    """Return the stimulus ticks for a given frequency and exposure class."""
    if isinstance(exposure, str):
        exposure = ExposureClass(exposure.lower())
    return exposure.stimulus_ticks(frequency_hz)


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
class LoopbackChannelConfig:
    """Specification of one active or loopback-paired communication channel."""

    peripheral: str  # "uart", "spi", "can"
    channel_index: int  # 0 or 1
    name: str
    bit_rate_hz: int
    wire_bits_per_byte: int = 10
    can_payload_bytes_per_frame: int = 8
    can_wire_bits_per_frame: int = 135
    max_payload_bytes: int = 65535
    is_transmitter: bool = True
    rx_peer_channel_index: int | None = None
    spi_baud: SPIBaud | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "peripheral": self.peripheral,
            "channel_index": self.channel_index,
            "name": self.name,
            "bit_rate_hz": self.bit_rate_hz,
            "wire_bits_per_byte": self.wire_bits_per_byte,
            "can_payload_bytes_per_frame": self.can_payload_bytes_per_frame,
            "can_wire_bits_per_frame": self.can_wire_bits_per_frame,
            "max_payload_bytes": self.max_payload_bytes,
            "is_transmitter": self.is_transmitter,
            "rx_peer_channel_index": self.rx_peer_channel_index,
            "spi_baud": None if self.spi_baud is None else self.spi_baud.name,
        }


@dataclass(frozen=True, slots=True)
class LoopbackConfiguration:
    """Full hardware peripheral configuration for one of the 4 test configurations."""

    config_id: int
    name: str
    description: str
    purpose: str
    theoretical_payload_rate_kib: float
    channels: tuple[LoopbackChannelConfig, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config_id,
            "name": self.name,
            "description": self.description,
            "purpose": self.purpose,
            "theoretical_payload_rate_kib": self.theoretical_payload_rate_kib,
            "channels": [ch.to_dict() for ch in self.channels],
        }

    @classmethod
    def config_1_automotive_gateway(cls) -> LoopbackConfiguration:
        """Configuration 1 — Automotive Gateway (413 KiB/s theoretical payload)."""
        return cls(
            config_id=1,
            name="1 — Automotive Gateway",
            description="SPI 1x 2.813 Mbps, UART 1x 115.2 kbps, CAN 2x 500 kbps",
            purpose="Representative gateway",
            theoretical_payload_rate_kib=413.0,
            channels=(
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=0,
                    name="SPI_ch1",
                    bit_rate_hz=2_813_000,
                    wire_bits_per_byte=8,
                    spi_baud=SPIBaud.BAUD_2M813BIT,
                ),
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=1,
                    name="UART_ch2",
                    bit_rate_hz=115_200,
                    wire_bits_per_byte=10,
                ),
                LoopbackChannelConfig(
                    peripheral="can",
                    channel_index=1,
                    name="CAN_ch2",
                    bit_rate_hz=1_000_000,
                    is_transmitter=True,
                    rx_peer_channel_index=0,
                ),
                LoopbackChannelConfig(
                    peripheral="can",
                    channel_index=0,
                    name="CAN_ch1",
                    bit_rate_hz=1_000_000,
                    is_transmitter=False,
                ),
            ),
        )

    @classmethod
    def config_2_high_speed_sensor(cls) -> LoopbackConfiguration:
        """Configuration 2 — High-Speed Sensor (784 KiB/s theoretical payload)."""
        return cls(
            config_id=2,
            name="2 — High-Speed Sensor",
            description="SPI 1x 5.625 Mbps, UART 1x 1.0 Mbps, CAN: None",
            purpose="High-throughput single channel",
            theoretical_payload_rate_kib=784.0,
            channels=(
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=0,
                    name="SPI_ch1",
                    bit_rate_hz=5_625_000,
                    wire_bits_per_byte=8,
                    spi_baud=SPIBaud.BAUD_5M625BIT,
                ),
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=1,
                    name="UART_ch2",
                    bit_rate_hz=1_000_000,
                    wire_bits_per_byte=10,
                ),
            ),
        )

    @classmethod
    def config_3_balanced_single_ch(cls) -> LoopbackConfiguration:
        """Configuration 3 — Balanced Single-Ch (425 KiB/s theoretical payload)."""
        return cls(
            config_id=3,
            name="3 — Balanced Single-Ch",
            description="SPI 1x 1.406 Mbps, UART 1x 2.0 Mbps, CAN 1x 1.0 Mbps",
            purpose="Lower-rate isolation case",
            theoretical_payload_rate_kib=425.0,
            channels=(
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=0,
                    name="SPI_ch1",
                    bit_rate_hz=1_406_000,
                    wire_bits_per_byte=8,
                    spi_baud=SPIBaud.BAUD_1M406BIT,
                ),
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=1,
                    name="UART_ch2",
                    bit_rate_hz=2_000_000,
                    wire_bits_per_byte=10,
                ),
                LoopbackChannelConfig(
                    peripheral="can",
                    channel_index=1,
                    name="CAN_ch2",
                    bit_rate_hz=1_000_000,
                    is_transmitter=True,
                    rx_peer_channel_index=0,
                ),
            ),
        )

    @classmethod
    def config_4_full_saturation(cls) -> LoopbackConfiguration:
        """Configuration 4 — Full Saturation (1,879 KiB/s theoretical payload)."""
        return cls(
            config_id=4,
            name="4 — Full Saturation",
            description="SPI 2x 5.625 Mbps, UART 2x 2.0 Mbps, CAN 2x 1.0 Mbps",
            purpose="Maximum multi-channel stress",
            theoretical_payload_rate_kib=1879.0,
            channels=(
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=0,
                    name="SPI_ch1",
                    bit_rate_hz=5_625_000,
                    wire_bits_per_byte=8,
                    spi_baud=SPIBaud.BAUD_5M625BIT,
                ),
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=1,
                    name="SPI_ch2",
                    bit_rate_hz=5_625_000,
                    wire_bits_per_byte=8,
                    spi_baud=SPIBaud.BAUD_5M625BIT,
                ),
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=0,
                    name="UART_ch1",
                    bit_rate_hz=2_000_000,
                    wire_bits_per_byte=10,
                ),
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=1,
                    name="UART_ch2",
                    bit_rate_hz=2_000_000,
                    wire_bits_per_byte=10,
                ),
                LoopbackChannelConfig(
                    peripheral="can",
                    channel_index=1,
                    name="CAN_ch2",
                    bit_rate_hz=1_000_000,
                    is_transmitter=True,
                    rx_peer_channel_index=0,
                ),
                LoopbackChannelConfig(
                    peripheral="can",
                    channel_index=0,
                    name="CAN_ch1",
                    bit_rate_hz=1_000_000,
                    is_transmitter=False,
                ),
            ),
        )

    @classmethod
    def config_0_prototype(
        cls,
        uart_baud_hz: int = 2_000_000,
        spi_baud: SPIBaud = SPIBaud.BAUD_5M625BIT,
    ) -> LoopbackConfiguration:
        """Configuration 0 — Prototype (UART2 + SPI2) (881.9 KiB/s theoretical payload)."""
        spi_baud_hz = spi_baud.value
        uart_rate = uart_baud_hz / 10.0 / 1024.0
        spi_rate = spi_baud_hz / 8.0 / 1024.0
        return cls(
            config_id=0,
            name="0 — Prototype (UART2 + SPI2)",
            description=f"UART 1x {uart_baud_hz / 1_000_000:g} Mbps, SPI 1x {spi_baud_hz / 1_000_000:g} Mbps (UART2 TX->RX, SPI2 MOSI->MISO)",
            purpose="Dual-peripheral UART2 and SPI2 prototype characterization",
            theoretical_payload_rate_kib=round(uart_rate + spi_rate, 1),
            channels=(
                LoopbackChannelConfig(
                    peripheral="uart",
                    channel_index=1,
                    name="UART_ch2",
                    bit_rate_hz=uart_baud_hz,
                    wire_bits_per_byte=10,
                ),
                LoopbackChannelConfig(
                    peripheral="spi",
                    channel_index=1,
                    name="SPI_ch2",
                    bit_rate_hz=spi_baud_hz,
                    wire_bits_per_byte=8,
                    spi_baud=spi_baud,
                ),
            ),
        )

    @classmethod
    def config_0_uart_only_prototype(cls, baud_hz: int = 2_000_000) -> LoopbackConfiguration:
        """Legacy alias for Configuration 0."""
        return cls.config_0_prototype(uart_baud_hz=baud_hz)

    @classmethod
    def from_id(cls, config_id: int) -> LoopbackConfiguration:
        """Lookup configuration by index (0..4)."""
        match config_id:
            case 0:
                return cls.config_0_prototype()
            case 1:
                return cls.config_1_automotive_gateway()
            case 2:
                return cls.config_2_high_speed_sensor()
            case 3:
                return cls.config_3_balanced_single_ch()
            case 4:
                return cls.config_4_full_saturation()
            case _:
                raise ValueError(f"Unknown configuration id: {config_id} (expected 0..4)")

    @classmethod
    def all_configurations(cls) -> tuple[LoopbackConfiguration, ...]:
        """Return all 4 defined loopback configurations."""
        return (
            cls.config_1_automotive_gateway(),
            cls.config_2_high_speed_sensor(),
            cls.config_3_balanced_single_ch(),
            cls.config_4_full_saturation(),
        )


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


def workload_point_for_exposure(
    *,
    frequency_hz: int,
    target_utilization_percent: float,
    exposure: ExposureClass | str = ExposureClass.DISCOVERY,
    burst_interval_ticks: int = 1,
    seed: int = 1,
) -> LoopbackWorkloadPoint:
    """Convenience helper to create a workload point for a given exposure class."""
    duration_s = get_exposure_duration_s(exposure, frequency_hz=frequency_hz)
    return LoopbackWorkloadPoint(
        frequency_hz=frequency_hz,
        duration_s=duration_s,
        target_utilization_percent=target_utilization_percent,
        burst_interval_ticks=burst_interval_ticks,
        seed=seed,
    )


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
    channel_index: int = 0

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


@dataclass(frozen=True, slots=True)
class MultiChannelCompiledWorkload:
    """Compiled schedule for arbitrary multi-channel loopback configurations."""

    configuration: LoopbackConfiguration
    point: LoopbackWorkloadPoint
    channel_workloads: tuple[PeripheralWorkload, ...]
    workload_hash: str

    @property
    def instruction_count(self) -> int:
        return sum(ch.instruction_count for ch in self.channel_workloads)

    @property
    def payload_bytes(self) -> int:
        return sum(ch.actual_payload_bytes for ch in self.channel_workloads)

    def to_dict(self) -> dict[str, object]:
        return {
            "workload_hash": self.workload_hash,
            "configuration": self.configuration.to_dict(),
            "point": self.point.to_dict(),
            "instruction_count": self.instruction_count,
            "payload_bytes": self.payload_bytes,
            "channels": [
                {
                    "peripheral": item.peripheral,
                    "channel_index": item.channel_index,
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
                for item in self.channel_workloads
            ],
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


def compile_configuration_workload(
    configuration: LoopbackConfiguration,
    point: LoopbackWorkloadPoint,
) -> MultiChannelCompiledWorkload:
    """Compile a workload point for any multi-channel LoopbackConfiguration."""
    if not isinstance(configuration, LoopbackConfiguration):
        raise TypeError("configuration must be a LoopbackConfiguration")
    if not isinstance(point, LoopbackWorkloadPoint):
        raise TypeError("point must be a LoopbackWorkloadPoint")

    channel_workloads: list[PeripheralWorkload] = []
    for ch in configuration.channels:
        if not ch.is_transmitter:
            continue
        if ch.peripheral in {"uart", "spi"}:
            pw = _compile_byte_workload(
                peripheral=ch.peripheral,
                configured_bit_rate_hz=ch.bit_rate_hz,
                wire_bits_per_byte=ch.wire_bits_per_byte,
                max_payload_bytes=ch.max_payload_bytes,
                point=point,
                channel_index=ch.channel_index,
            )
        elif ch.peripheral == "can":
            pw = _compile_can_channel_workload(
                ch=ch,
                point=point,
            )
        else:
            raise ValueError(f"Unsupported peripheral kind: {ch.peripheral}")
        channel_workloads.append(pw)

    doc = {
        "definition": "hilrig.multi-channel-loopback-workload.v1",
        "configuration": configuration.to_dict(),
        "point": {
            **point.to_dict(),
            "target_utilization_percent": _canonical_decimal(point.target_utilization_percent),
        },
    }
    encoded = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
    workload_hash = hashlib.sha256(encoded).hexdigest()

    return MultiChannelCompiledWorkload(
        configuration=configuration,
        point=point,
        channel_workloads=tuple(channel_workloads),
        workload_hash=workload_hash,
    )


MAX_RIG_INSTRUCTION_STORAGE_BYTES: int = 66_977_792  # 63.875 MiB hardware flash instruction store


def estimate_workload_instruction_bytes(
    configuration: LoopbackConfiguration,
    point: LoopbackWorkloadPoint,
) -> int:
    """Estimate the total serialized instruction bytes for a workload point."""
    burst_slots = len(_burst_ticks(point))
    if burst_slots <= 0:
        return 0

    # Per-tick protocol container overhead (~12 bytes per active tick)
    total_bytes = burst_slots * 12

    for ch in configuration.channels:
        if not ch.is_transmitter:
            continue
        wire_bit_budget = _wire_bit_budget(ch.bit_rate_hz, point)
        if ch.peripheral in {"uart", "spi"}:
            payload_byte_budget = wire_bit_budget // ch.wire_bits_per_byte
            # LogicalOperation header (~4-6 bytes) per burst slot + data payload
            total_bytes += payload_byte_budget + (burst_slots * 6)
        elif ch.peripheral == "can":
            frame_budget = wire_bit_budget // ch.can_wire_bits_per_frame
            total_bytes += frame_budget * 12 + (burst_slots * 4)

    return total_bytes


def calculate_max_safe_duration_s(
    configuration: LoopbackConfiguration,
    frequency_hz: int,
    target_utilization_percent: float,
    *,
    burst_interval_ticks: int = 1,
    max_storage_bytes: int = MAX_RIG_INSTRUCTION_STORAGE_BYTES,
    headroom_fraction: float = 0.95,
) -> int:
    """Calculate the maximum test duration in seconds that safely fits within RIG flash storage."""
    test_pt = LoopbackWorkloadPoint(
        frequency_hz=frequency_hz,
        duration_s=1,
        target_utilization_percent=target_utilization_percent,
        burst_interval_ticks=burst_interval_ticks,
        seed=1,
    )
    bytes_per_second = estimate_workload_instruction_bytes(configuration, test_pt)
    if bytes_per_second <= 0:
        return 300

    safe_capacity = int(max_storage_bytes * headroom_fraction)
    max_duration_s = max(1, safe_capacity // bytes_per_second)
    return max_duration_s


def adapt_workload_point_duration(
    configuration: LoopbackConfiguration,
    point: LoopbackWorkloadPoint,
    *,
    max_duration_s: int | None = None,
    headroom_fraction: float = 0.95,
    max_storage_bytes: int = MAX_RIG_INSTRUCTION_STORAGE_BYTES,
    minimum_ticks_floor: int | None = None,
) -> LoopbackWorkloadPoint:
    """Adapt a workload point's duration so it stays within RIG flash storage capacity."""
    requested_duration_s = max_duration_s if max_duration_s is not None else point.duration_s
    safe_max_duration_s = calculate_max_safe_duration_s(
        configuration,
        frequency_hz=point.frequency_hz,
        target_utilization_percent=point.target_utilization_percent,
        burst_interval_ticks=point.burst_interval_ticks,
        max_storage_bytes=max_storage_bytes,
        headroom_fraction=headroom_fraction,
    )
    effective_duration_s = min(requested_duration_s, safe_max_duration_s)
    if minimum_ticks_floor is not None:
        min_duration_for_floor = (minimum_ticks_floor + point.frequency_hz - 1) // point.frequency_hz
        effective_duration_s = max(effective_duration_s, min_duration_for_floor)

    if effective_duration_s == point.duration_s:
        return point
    return replace(point, duration_s=effective_duration_s)


def check_workload_admissibility(
    configuration: LoopbackConfiguration,
    point: LoopbackWorkloadPoint,
    *,
    max_instruction_bytes: int = 8192,
    uart_tx_buffer: int = 2048,
    spi_tx_buffer: int = 4096,
    can_tx_queue: int = 83,
    max_storage_bytes: int = MAX_RIG_INSTRUCTION_STORAGE_BYTES,
) -> tuple[bool, str | None]:
    """Check compile-time representation constraints without running on-rig.

    Returns (True, None) if admissible, or (False, reason) if infeasible.
    """
    burst_slots = len(_burst_ticks(point))
    if burst_slots <= 0:
        return (True, None)

    for ch in configuration.channels:
        if not ch.is_transmitter:
            continue
        wire_bit_budget = _wire_bit_budget(ch.bit_rate_hz, point)

        if ch.peripheral in {"uart", "spi"}:
            payload_byte_budget = wire_bit_budget // ch.wire_bits_per_byte
            max_bytes_per_tick = (payload_byte_budget + burst_slots - 1) // burst_slots
            if ch.peripheral == "uart" and max_bytes_per_tick > uart_tx_buffer:
                return (
                    False,
                    (
                        f"UART ch{ch.channel_index} peak tick payload "
                        f"({max_bytes_per_tick} B) exceeds TX buffer ({uart_tx_buffer} B)"
                    ),
                )
            if ch.peripheral == "spi" and max_bytes_per_tick > spi_tx_buffer:
                return (
                    False,
                    (
                        f"SPI ch{ch.channel_index} peak tick payload "
                        f"({max_bytes_per_tick} B) exceeds TX buffer ({spi_tx_buffer} B)"
                    ),
                )
        elif ch.peripheral == "can":
            frame_budget = wire_bit_budget // ch.can_wire_bits_per_frame
            max_frames_per_tick = (frame_budget + burst_slots - 1) // burst_slots
            if max_frames_per_tick > can_tx_queue:
                return (
                    False,
                    (
                        f"CAN ch{ch.channel_index} peak frames "
                        f"({max_frames_per_tick}) exceeds TX queue ({can_tx_queue})"
                    ),
                )

    est_storage = estimate_workload_instruction_bytes(configuration, point)
    if est_storage > max_storage_bytes:
        return (
            False,
            (
                f"Estimated total instruction bytes ({est_storage:,} B) "
                f"exceeds maximum RIG flash instruction storage ({max_storage_bytes:,} B)"
            ),
        )

    return (True, None)


def build_loopback_test(
    configuration: LoopbackConfiguration,
    point: LoopbackWorkloadPoint,
    *,
    name: str | None = None,
    start_mode: StartMode = StartMode.IMMEDIATE,
    can_frame_id: int = 0x321,
    max_allowed_phase_shift_ticks: int = 1,
    observation_allowance_ticks: int | None = None,
    boundary_only_assertions: bool = True,
    enable_mixed_io: bool = True,
) -> Test:
    """Build a complete, executable Test for a given LoopbackConfiguration and point.

    Enforces stable bounded phase shift assertions across all active communication
    channels (UART, SPI, CAN) relative to the cumulative modeled wire completion tick (max 1 tick allowable offset).

    If enable_mixed_io is True (the default), Digital Inputs/Outputs, Analogue ADC
    sampling, and PWM capture channels are also configured and sampled on every
    execution tick to test full multi-domain ISR and bus concurrency.
    """
    try:
        frequency_mode = FrequencyMode(point.frequency_hz)
    except ValueError as error:
        raise ValueError(f"Unsupported frequency_hz: {point.frequency_hz}") from error

    test_name = name or (
        f"{configuration.name} @ {point.frequency_hz}Hz - "
        f"{point.target_utilization_percent:g}% every {point.burst_interval_ticks}t"
    )
    test = Test(name=test_name)
    test.configure(frequency_mode=frequency_mode, start_mode=start_mode)

    # 1. Mixed IO Channels (Digital, Analogue, PWM) sampled on every tick
    if enable_mixed_io:
        for d_ch in (0, 1, 2):
            test.digital_input(channel=d_ch).configure(voltage=LogicVoltage.V3_3)
            test.digital_output(channel=d_ch).configure(
                voltage=LogicVoltage.V3_3,
                initial_state=DigitalState.LOW,
            )
        for a_ch in (0, 1):
            test.analogue_input(channel=a_ch)
        test.pwm_output(channel=0).configure(
            voltage=LogicVoltage.V3_3,
            initial_frequency_hz=1_000,
            initial_duty_cycle=0.50,
            initially_enabled=True,
        )
        test.pwm_input(channel=0).configure(voltage=LogicVoltage.V3_3)

    uart_handles: dict[int, UART] = {}
    spi_handles: dict[int, SPI] = {}
    can_handles: dict[int, CAN] = {}

    # 2. Configure all referenced communication channels
    for ch in configuration.channels:
        if ch.peripheral == "uart" and ch.channel_index not in uart_handles:
            handle = (
                test.uart(channel=ch.channel_index)
                .named(ch.name)
                .configure(
                    mode=UARTMode.TTL_3V3,
                    baud_hz=ch.bit_rate_hz,
                    parity=UARTParity.NONE,
                    length=UARTLengthBits.EIGHT,
                    stop=UARTStopBits.ONE,
                )
            )
            uart_handles[ch.channel_index] = handle
        elif ch.peripheral == "spi" and ch.channel_index not in spi_handles:
            spi_baud = ch.spi_baud
            if spi_baud is None:
                # Find matching SPI baud enum
                for baud in SPIBaud:
                    if baud.value == ch.bit_rate_hz:
                        spi_baud = baud
                        break
            if spi_baud is None:
                spi_baud = SPIBaud.BAUD_2M813BIT
            handle = (
                test.spi(channel=ch.channel_index)
                .named(ch.name)
                .configure(
                    role=SPIRole.MASTER,
                    baud=spi_baud,
                    data_size=SPISize.SIZE_8BIT,
                    mode=SPIMode.MODE_0,
                    first_bit=SPIFirst.MSB,
                )
            )
            spi_handles[ch.channel_index] = handle
        elif ch.peripheral == "can" and ch.channel_index not in can_handles:
            handle = (
                test.can(channel=ch.channel_index).named(ch.name).configure(bitrate=ch.bit_rate_hz)
            )
            can_handles[ch.channel_index] = handle

    # Ensure paired CAN receiver channels are configured
    # even if not explicitly in transmitting channels
    for ch in configuration.channels:
        if ch.peripheral == "can" and ch.rx_peer_channel_index is not None:
            peer_idx = ch.rx_peer_channel_index
            if peer_idx not in can_handles:
                can_handles[peer_idx] = (
                    test.can(channel=peer_idx)
                    .named(f"CAN_ch{peer_idx + 1}")
                    .configure(bitrate=ch.bit_rate_hz)
                )

    # Effective allowable phase shift allowance
    shift_ticks = (
        observation_allowance_ticks
        if (observation_allowance_ticks is not None and observation_allowance_ticks <= 5)
        else max_allowed_phase_shift_ticks
    )

    # 3. Compile and apply workloads per channel with cumulative timing model
    compiled = compile_configuration_workload(configuration, point)
    for ch_wl in compiled.channel_workloads:
        seed_str = f"{point.seed}:{ch_wl.peripheral}:{ch_wl.channel_index}"
        rng = Random(seed_str)
        materialized = tuple(
            MaterializedTransfer(
                tick=tr.tick,
                data=rng.randbytes(tr.payload_bytes),
                wire_bits=tr.wire_bits,
            )
            for tr in ch_wl.transfers
        )

        # Calculate cumulative modeled wire service completion ticks
        transfer_windows: list[tuple[MaterializedTransfer, int]] = []
        prev_finish = 0
        for tr in materialized:
            wire_ticks = math.ceil(tr.wire_bits * point.frequency_hz / ch_wl.configured_bit_rate_hz)
            m_start = max(tr.tick, prev_finish)
            m_finish = m_start + max(1, wire_ticks)
            prev_finish = m_finish
            until_tick = m_finish + shift_ticks
            transfer_windows.append((tr, until_tick))

        group_name = f"{ch_wl.peripheral.upper()}_ch{ch_wl.channel_index + 1} Workload"
        with test.group(group_name):
            if ch_wl.peripheral == "uart":
                uart_h = uart_handles[ch_wl.channel_index]
                for tr in materialized:
                    uart_h.write(data=tr.data, at_tick=tr.tick)
                # Assertions on first and last (or all)
                for tr, until_tick in _filter_assertion_windows(
                    transfer_windows, boundary_only_assertions
                ):
                    test.expect(uart_h).receive(
                        tr.data,
                        allow_stream_match=True,
                        from_tick=tr.tick,
                        until_tick=until_tick,
                    )
            elif ch_wl.peripheral == "spi":
                spi_h = spi_handles[ch_wl.channel_index]
                for tr in materialized:
                    spi_h.transfer(
                        tx_data=tr.data,
                        rx_length=len(tr.data),
                        at_tick=tr.tick,
                    )
                for tr, until_tick in _filter_assertion_windows(
                    transfer_windows, boundary_only_assertions
                ):
                    test.expect(spi_h).receive(
                        tr.data,
                        allow_stream_match=True,
                        from_tick=tr.tick,
                        until_tick=until_tick,
                    )
            elif ch_wl.peripheral == "can":
                tx_can = can_handles[ch_wl.channel_index]
                cfg_ch = next(
                    (
                        c
                        for c in configuration.channels
                        if c.peripheral == "can" and c.channel_index == ch_wl.channel_index
                    ),
                    None,
                )
                rx_idx = (
                    cfg_ch.rx_peer_channel_index
                    if (cfg_ch and cfg_ch.rx_peer_channel_index is not None)
                    else (0 if ch_wl.channel_index == 1 else 1)
                )
                rx_can = can_handles[rx_idx]
                for tr in materialized:
                    tx_can.transmit(frame_id=can_frame_id, data=tr.data, at_tick=tr.tick)
                for tr, until_tick in _filter_assertion_windows(
                    transfer_windows, boundary_only_assertions
                ):
                    test.expect(rx_can).receive(
                        frame_id=can_frame_id,
                        data=tr.data,
                        from_tick=tr.tick,
                        until_tick=until_tick,
                    )

    return test


@dataclass(frozen=True, slots=True)
class TransferTimingRecord:
    """Detailed turnaround and phase shift metrics for one commanded transfer."""

    transfer_index: int
    commanded_tick: int
    modeled_start_tick: int
    modeled_finish_tick: int
    observed_completion_tick: int | None
    turnaround_offset_ticks: int | None
    excess_phase_shift_ticks: int | None
    passed_bounded_window: bool


@dataclass(frozen=True, slots=True)
class ChannelTimingMetrics:
    """Turnaround latency and phase-shift distribution for one active communication channel."""

    peripheral: str
    channel_index: int
    transfer_count: int
    matched_count: int
    min_offset_ticks: int | None
    median_offset_ticks: float | None
    p95_offset_ticks: float | None
    max_offset_ticks: int | None
    max_excess_phase_shift_ticks: int | None
    is_bounded: bool
    is_creeping_divergent: bool
    transfers: tuple[TransferTimingRecord, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "peripheral": self.peripheral,
            "channel_index": self.channel_index,
            "transfer_count": self.transfer_count,
            "matched_count": self.matched_count,
            "min_offset_ticks": self.min_offset_ticks,
            "median_offset_ticks": self.median_offset_ticks,
            "p95_offset_ticks": self.p95_offset_ticks,
            "max_offset_ticks": self.max_offset_ticks,
            "max_excess_phase_shift_ticks": self.max_excess_phase_shift_ticks,
            "is_bounded": self.is_bounded,
            "is_creeping_divergent": self.is_creeping_divergent,
        }


def analyze_stream_timing(
    captured_communications: list[Any],
    compiled_workload: MultiChannelCompiledWorkload,
    *,
    max_allowed_phase_shift_ticks: int = 1,
) -> tuple[ChannelTimingMetrics, ...]:
    """Reconstruct per-byte/per-frame transfer timing and verify bounded phase shift."""
    metrics_list: list[ChannelTimingMetrics] = []

    for ch_wl in compiled_workload.channel_workloads:
        p_name = ch_wl.peripheral.lower()
        ch_idx = ch_wl.channel_index

        # Filter captures matching this peripheral
        ch_caps = [
            c
            for c in captured_communications
            if getattr(c, "peripheral", None)
            and getattr(c.peripheral, "name", "").lower() == p_name
            and getattr(c, "channel", 0) == ch_idx
        ]

        transfers_records: list[TransferTimingRecord] = []
        offsets: list[int] = []
        excess_shifts: list[int] = []

        prev_finish = 0
        cap_idx = 0
        cap_byte_offset = 0

        for tr_idx, tr in enumerate(ch_wl.transfers):
            wire_ticks = max(
                1,
                math.ceil(
                    tr.wire_bits
                    * compiled_workload.point.frequency_hz
                    / ch_wl.configured_bit_rate_hz
                ),
            )
            m_start = max(tr.tick, prev_finish)
            m_finish = m_start + wire_ticks
            prev_finish = m_finish

            # Find matching capture tick
            obs_tick: int | None = None
            if p_name == "can":
                if cap_idx < len(ch_caps):
                    obs_tick = ch_caps[cap_idx].tick
                    cap_idx += 1
            else:
                bytes_needed = tr.payload_bytes
                while cap_idx < len(ch_caps) and bytes_needed > 0:
                    cap = ch_caps[cap_idx]
                    avail = len(cap.payload) - cap_byte_offset
                    if avail <= bytes_needed:
                        bytes_needed -= avail
                        obs_tick = cap.tick
                        cap_idx += 1
                        cap_byte_offset = 0
                    else:
                        cap_byte_offset += bytes_needed
                        obs_tick = cap.tick
                        bytes_needed = 0

            turnaround = (obs_tick - tr.tick) if obs_tick is not None else None
            excess = (obs_tick - m_finish) if obs_tick is not None else None
            passed = (
                (excess is not None and excess <= max_allowed_phase_shift_ticks)
                if obs_tick is not None
                else False
            )

            if turnaround is not None:
                offsets.append(turnaround)
            if excess is not None:
                excess_shifts.append(excess)

            transfers_records.append(
                TransferTimingRecord(
                    transfer_index=tr_idx,
                    commanded_tick=tr.tick,
                    modeled_start_tick=m_start,
                    modeled_finish_tick=m_finish,
                    observed_completion_tick=obs_tick,
                    turnaround_offset_ticks=turnaround,
                    excess_phase_shift_ticks=excess,
                    passed_bounded_window=passed,
                )
            )

        # Statistics
        matched = len(offsets)
        min_off = min(offsets) if offsets else None
        max_off = max(offsets) if offsets else None
        max_excess = max(excess_shifts) if excess_shifts else None

        sorted_offsets = sorted(offsets)
        median_off = sorted_offsets[len(sorted_offsets) // 2] if sorted_offsets else None
        p95_idx = int(len(sorted_offsets) * 0.95)
        p95_off = sorted_offsets[p95_idx] if sorted_offsets else None

        is_bounded = len(transfers_records) == matched and all(
            r.passed_bounded_window for r in transfers_records
        )

        # Check for creeping divergence (excess increasing steadily)
        is_divergent = False
        if len(excess_shifts) >= 5:
            first_third = excess_shifts[: len(excess_shifts) // 3]
            last_third = excess_shifts[-(len(excess_shifts) // 3) :]
            if (sum(last_third) / len(last_third)) - (
                sum(first_third) / len(first_third)
            ) > max_allowed_phase_shift_ticks:
                is_divergent = True

        metrics_list.append(
            ChannelTimingMetrics(
                peripheral=p_name,
                channel_index=ch_idx,
                transfer_count=len(ch_wl.transfers),
                matched_count=matched,
                min_offset_ticks=min_off,
                median_offset_ticks=float(median_off) if median_off is not None else None,
                p95_offset_ticks=float(p95_off) if p95_off is not None else None,
                max_offset_ticks=max_off,
                max_excess_phase_shift_ticks=max_excess,
                is_bounded=is_bounded,
                is_creeping_divergent=is_divergent,
                transfers=tuple(transfers_records),
            )
        )

    return tuple(metrics_list)


def _filter_assertions(
    transfers: tuple[MaterializedTransfer, ...],
    boundary_only: bool,
) -> tuple[MaterializedTransfer, ...]:
    if not boundary_only or len(transfers) <= 2:
        return transfers
    return (transfers[0], transfers[-1])


def _filter_assertion_windows(
    transfer_windows: list[tuple[MaterializedTransfer, int]],
    boundary_only: bool,
) -> list[tuple[MaterializedTransfer, int]]:
    if not boundary_only or len(transfer_windows) <= 2:
        return transfer_windows
    return [transfer_windows[0], transfer_windows[-1]]


def apply_loopback_communication_workload(
    test: Test,
    workload: CompiledLoopbackWorkload,
    *,
    uart: UART,
    spi: SPI,
    can_transmitter: CAN,
    can_frame_id: int = 0x321,
) -> MaterializedLoopbackWorkload:
    """Add one compiled UART/SPI/CAN schedule to an ordinary :class:`Test`."""
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
    channel_index: int = 0,
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
        channel_index=channel_index,
    )


def _compile_can_channel_workload(
    *,
    ch: LoopbackChannelConfig,
    point: LoopbackWorkloadPoint,
) -> PeripheralWorkload:
    wire_bit_budget = _wire_bit_budget(ch.bit_rate_hz, point)
    frame_budget = wire_bit_budget // ch.can_wire_bits_per_frame
    transfers = tuple(
        ScheduledTransfer(
            tick=tick,
            payload_bytes=ch.can_payload_bytes_per_frame * frame_count,
            wire_bits=ch.can_wire_bits_per_frame * frame_count,
        )
        for tick, frame_count in _distribute(frame_budget, _burst_ticks(point))
    )
    return _peripheral_workload(
        peripheral="can",
        configured_bit_rate_hz=ch.bit_rate_hz,
        requested_utilization_percent=point.target_utilization_percent,
        duration_s=point.duration_s,
        transfers=transfers,
        channel_index=ch.channel_index,
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
    channel_index: int = 0,
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
        channel_index=channel_index,
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
