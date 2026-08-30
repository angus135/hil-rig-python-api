"""Small immutable models shared by the temporary Transport test harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

UINT32_MASK = 0xFFFF_FFFF
PROTOCOL_COMMIT = "a24fccc403007cbf6268ff7d0d21f50566a6b2de"
PYTHON_API_BRANCH_POINT_COMMIT = "1cd530dc1352c3b2a449eb8f0ab02684370c614b"
FIRMWARE_BRANCH = "test/DEV-138--protocol-test"
FIRMWARE_COMMIT = "c6c0af2af586108949c30fb4a12df54eb9dd2fda"
ENVELOPE_VERSION = 1


@dataclass(frozen=True, slots=True)
class SerialSelector:
    """Rules used to select exactly one USB CDC serial device."""

    port: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None

    def __post_init__(self) -> None:
        if self.port is not None and not self.port:
            raise ValueError("port must be non-empty when supplied")
        for name, value in (("vid", self.vid), ("pid", self.pid)):
            if value is not None and (type(value) is not int or not 0 <= value <= 0xFFFF):
                raise ValueError(f"{name} must be an integer in the range 0..65535")
        if self.serial_number is not None and not self.serial_number:
            raise ValueError("serial_number must be non-empty when supplied")
        if (
            self.port is None
            and self.vid is None
            and self.pid is None
            and self.serial_number is None
        ):
            raise ValueError("specify --port or at least one USB identity field")


@dataclass(frozen=True, slots=True)
class SerialDevice:
    """Resolved serial-port identity used for evidence and reconnects."""

    port: str
    description: str | None = None
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceBudgets:
    """Hard bounds for one connection service iteration."""

    receive_calls: int = 4
    event_reads: int = 16
    application_reads: int = 1
    output_attempts: int = 4
    process_calls: int = 2
    serial_read_bytes: int = 4096

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ReceivedApplicationMessage:
    """One opaque complete Application message tagged with its physical link generation."""

    data: bytes
    link_generation: int
    monotonic_ms: int


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """Public Transport event tagged with link and time context."""

    event: Any
    link_generation: int | None
    monotonic_ms: int


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """Bounded result from one call to ``service_once``."""

    progress: bool
    operation_budget_exhausted: bool
    current_service_gap_ms: int
    max_service_gap_ms: int


@dataclass(slots=True)
class ConnectionCounters:
    """Mutable counters retained for diagnostics and final evidence."""

    service_loops: int = 0
    late_loops: int = 0
    operation_budget_exhaustions: int = 0
    serial_bytes_read: int = 0
    serial_bytes_written: int = 0
    bytes_offered: int = 0
    bytes_consumed: int = 0
    partial_consumption_calls: int = 0
    zero_progress_receive_calls: int = 0
    zero_byte_receive_calls: int = 0
    retained_input_high_water: int = 0
    input_capacity_deferrals: int = 0
    events_read: int = 0
    application_messages_read: int = 0
    output_items_peeked: int = 0
    output_bytes_accepted: int = 0
    partial_writes: int = 0
    zero_byte_writes: int = 0
    write_timeouts: int = 0
    serial_read_failures: int = 0
    serial_write_failures: int = 0
    output_items_committed: int = 0
    commit_failures: int = 0
    max_staged_duration_ms: int = 0
    receive_statuses: dict[str, int] = field(default_factory=dict)
    process_statuses: dict[str, int] = field(default_factory=dict)

    def count_status(self, target: dict[str, int], status: object) -> None:
        name = getattr(status, "name", str(status))
        target[name] = target.get(name, 0) + 1
