"""Diagnostic decoding and analysis parser for Run Report extensions."""

from __future__ import annotations

import struct
from dataclasses import dataclass

RUNTIME_LIMITS_RECORD_TYPE: int = 1
FAILURE_DETAIL_RECORD_TYPE: int = 2
FLASH_DETAIL_RECORD_TYPE: int = 3
UART_RECORD_TYPE: int = 4
SPI_RECORD_TYPE: int = 5
CAN_RECORD_TYPE: int = 6

EXECUTION_FAILURE_NAMES: dict[int, str] = {
    0: "None",
    1: "Not Prepared",
    2: "Instruction Underrun",
    3: "Instruction Corrupt",
    4: "Instruction Late",
    5: "Operation Rejected",
    6: "Instruction Consume",
    7: "Measurement Rejected",
    8: "Instruction Unconsumed",
}

OPERATION_OPCODE_NAMES: dict[int, str] = {
    1: "UART TX",
    2: "SPI TX",
    3: "CAN TX",
    4: "Analog Out",
    5: "Digital Out",
    6: "PWM Gen",
}

OPERATION_FAILURE_REASON_NAMES: dict[int, str] = {
    0: "None",
    1: "Invalid Arg",
    2: "Queue Full",
    3: "Busy",
    4: "Empty",
    5: "Not Configured",
    6: "Not Started",
    7: "Timing Error",
    8: "Filter Error",
    9: "Driver Fault",
    10: "Driver Rejected",
}

MEASUREMENT_TYPE_NAMES: dict[int, str] = {
    0: "Analog In",
    1: "Digital In",
    2: "PWM Cap",
    3: "UART RX",
    4: "SPI RX",
    5: "CAN RX",
}

MEASUREMENT_FAILURE_REASON_NAMES: dict[int, str] = {
    0: "None",
    1: "Reserve Failed",
    2: "Commit Failed",
    3: "Invalid Sample",
}

FLASH_COMMIT_STATUS_NAMES: dict[int, str] = {
    0: "OK",
    1: "Invalid State",
    2: "Invalid Lease",
    3: "Overflow",
    4: "Capacity Exceeded",
    5: "Internal Error",
}

SPI_TX_STATE_NAMES: dict[int, str] = {
    0: "Idle",
    1: "DMA Active",
    2: "Wait Drain",
    3: "Error",
}

CAN_LAST_ERROR_NAMES: dict[int, str] = {
    0: "None",
    1: "Stuff",
    2: "Form",
    3: "Ack",
    4: "BitRecessive",
    5: "BitDominant",
    6: "CRC",
    7: "SoftwareSet",
}


@dataclass(frozen=True, slots=True)
class ExtensionHeader:
    """Header of a diagnostic extension within a Run Report."""

    magic: bytes
    version: int
    header_bytes: int
    total_bytes: int
    record_count: int
    flags: int
    run_sequence: int  # monotonic attempt counter

    @property
    def rx_overrun_undetected(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def partial_run(self) -> bool:
        return bool(self.flags & 0x02)


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    """Hardware memory capacities and buffer sizing for the session (Type 1)."""

    core_clock_hz: int
    instr_ram_capacity: int
    result_ram_capacity: int
    uart_tx_capacity: int
    uart_rx_capacity: int
    spi_tx_bytes_cap: int
    spi_rx_bytes_cap: int
    spi_tx_pkt_cap: int
    can_tx_capacity: int
    can_rx_capacity: int


@dataclass(frozen=True, slots=True)
class FailureDetail:
    """Exact operation/measurement failure detail and tick boundary (Type 2)."""

    op_failure_valid: int
    execution_failure: int
    operation_index: int
    opcode: int
    channel: int
    op_failure_reason: int
    execution_boundary: int
    meas_failure_valid: int
    measurement_index: int
    measurement_type: int
    meas_channel: int
    meas_failure_reason: int

    @property
    def has_op_failure(self) -> bool:
        return bool(self.op_failure_valid)

    @property
    def has_meas_failure(self) -> bool:
        return bool(self.meas_failure_valid)

    @property
    def execution_failure_name(self) -> str:
        return EXECUTION_FAILURE_NAMES.get(
            self.execution_failure, f"Unknown ({self.execution_failure})"
        )

    @property
    def opcode_name(self) -> str:
        return OPERATION_OPCODE_NAMES.get(self.opcode, f"Unknown ({self.opcode})")

    @property
    def op_failure_reason_name(self) -> str:
        return OPERATION_FAILURE_REASON_NAMES.get(
            self.op_failure_reason, f"Unknown ({self.op_failure_reason})"
        )

    @property
    def measurement_type_name(self) -> str:
        return MEASUREMENT_TYPE_NAMES.get(
            self.measurement_type, f"Unknown ({self.measurement_type})"
        )

    @property
    def meas_failure_reason_name(self) -> str:
        return MEASUREMENT_FAILURE_REASON_NAMES.get(
            self.meas_failure_reason, f"Unknown ({self.meas_failure_reason})"
        )


@dataclass(frozen=True, slots=True)
class FlashDetail:
    """Flash drain and reserve failure details (Type 3)."""

    pending_bytes: int
    failed_reserve_bytes: int
    free_bytes_at_fail: int
    last_commit_status: int

    @property
    def last_commit_status_name(self) -> str:
        return FLASH_COMMIT_STATUS_NAMES.get(
            self.last_commit_status, f"Unknown ({self.last_commit_status})"
        )


@dataclass(frozen=True, slots=True)
class UartDiagnostics:
    """UART channel diagnostic snapshot (Type 4)."""

    channel: int
    flags: int
    tx_pending_bytes: int
    tx_peak_bytes: int
    tx_reject_count: int
    dma_error_count: int
    rx_unread_bytes: int
    rx_peak_bytes: int
    latched_faults: int

    @property
    def valid(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def tx_dma_active(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def started(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def configured(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def tx_dma_fault(self) -> bool:
        return bool(self.latched_faults & 0x01)

    @property
    def rx_dma_fault(self) -> bool:
        return bool(self.latched_faults & 0x02)


@dataclass(frozen=True, slots=True)
class SpiDiagnostics:
    """SPI channel diagnostic snapshot (Type 5)."""

    channel: int
    flags: int
    tx_pending_bytes: int
    tx_peak_bytes: int
    tx_in_flight_bytes: int
    tx_pending_packets: int
    tx_peak_packets: int
    tx_reject_count: int
    tx_dma_error_count: int
    tx_drain_timeouts: int
    rx_unread_bytes: int
    rx_peak_bytes: int
    tx_state: int

    @property
    def valid(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def started(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def configured(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def is_master(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def tx_state_name(self) -> str:
        return SPI_TX_STATE_NAMES.get(self.tx_state, f"Unknown ({self.tx_state})")


@dataclass(frozen=True, slots=True)
class CanDiagnostics:
    """CAN channel diagnostic snapshot (Type 6)."""

    channel: int
    flags: int
    tx_pending: int
    tx_peak: int
    tx_pending_mailbox: int
    rx_queued: int
    rx_peak: int
    rx_dropped: int
    tec: int
    rec: int
    last_error: int
    tsr: int
    esr: int
    error_count: int  # cumulative error IRQ / event count
    max_tec: int  # peak observed TEC
    max_rec: int  # peak observed REC

    @property
    def valid(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def tx_active(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def tx_error(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def last_error_name(self) -> str:
        return CAN_LAST_ERROR_NAMES.get(self.last_error, f"Unknown ({self.last_error})")


def decode_runtime_limits_record(payload: bytes) -> RuntimeLimits:
    """Decode a 26-byte Runtime Limits record payload (Type 1)."""
    if len(payload) < 26:
        raise ValueError(
            f"Runtime limits diagnostic payload must be at least 26 bytes, got {len(payload)}"
        )
    (
        core_clock_hz,
        instr_ram_capacity,
        result_ram_capacity,
        uart_tx_capacity,
        uart_rx_capacity,
        spi_tx_bytes_cap,
        spi_rx_bytes_cap,
        spi_tx_pkt_cap,
        can_tx_capacity,
        can_rx_capacity,
    ) = struct.unpack("<IIIHHHHHHH", payload[:26])
    return RuntimeLimits(
        core_clock_hz=core_clock_hz,
        instr_ram_capacity=instr_ram_capacity,
        result_ram_capacity=result_ram_capacity,
        uart_tx_capacity=uart_tx_capacity,
        uart_rx_capacity=uart_rx_capacity,
        spi_tx_bytes_cap=spi_tx_bytes_cap,
        spi_rx_bytes_cap=spi_rx_bytes_cap,
        spi_tx_pkt_cap=spi_tx_pkt_cap,
        can_tx_capacity=can_tx_capacity,
        can_rx_capacity=can_rx_capacity,
    )


def decode_failure_detail_record(payload: bytes) -> FailureDetail:
    """Decode a 15-byte Failure Detail record payload (Type 2)."""
    if len(payload) < 15:
        raise ValueError(
            f"Failure detail diagnostic payload must be at least 15 bytes, got {len(payload)}"
        )
    (
        op_failure_valid,
        execution_failure,
        operation_index,
        opcode,
        channel,
        op_failure_reason,
        execution_boundary,
        meas_failure_valid,
        measurement_index,
        measurement_type,
        meas_channel,
        meas_failure_reason,
    ) = struct.unpack("<BBBBBBIBBBBB", payload[:15])
    return FailureDetail(
        op_failure_valid=op_failure_valid,
        execution_failure=execution_failure,
        operation_index=operation_index,
        opcode=opcode,
        channel=channel,
        op_failure_reason=op_failure_reason,
        execution_boundary=execution_boundary,
        meas_failure_valid=meas_failure_valid,
        measurement_index=measurement_index,
        measurement_type=measurement_type,
        meas_channel=meas_channel,
        meas_failure_reason=meas_failure_reason,
    )


def decode_flash_detail_record(payload: bytes) -> FlashDetail:
    """Decode an 11-byte Flash Detail record payload (Type 3)."""
    if len(payload) < 11:
        raise ValueError(
            f"Flash detail diagnostic payload must be at least 11 bytes, got {len(payload)}"
        )
    (
        pending_bytes,
        failed_reserve_bytes,
        free_bytes_at_fail,
        last_commit_status,
    ) = struct.unpack("<IHIB", payload[:11])
    return FlashDetail(
        pending_bytes=pending_bytes,
        failed_reserve_bytes=failed_reserve_bytes,
        free_bytes_at_fail=free_bytes_at_fail,
        last_commit_status=last_commit_status,
    )


def decode_uart_record(payload: bytes) -> UartDiagnostics:
    """Decode a 22-byte UART diagnostic record payload (Type 4)."""
    if len(payload) < 22:
        raise ValueError(f"UART diagnostic payload must be at least 22 bytes, got {len(payload)}")
    (
        channel,
        flags,
        tx_pending_bytes,
        tx_peak_bytes,
        tx_reject_count,
        dma_error_count,
        rx_unread_bytes,
        rx_peak_bytes,
        latched_faults,
    ) = struct.unpack("<BBHHIIHHI", payload[:22])
    return UartDiagnostics(
        channel=channel,
        flags=flags,
        tx_pending_bytes=tx_pending_bytes,
        tx_peak_bytes=tx_peak_bytes,
        tx_reject_count=tx_reject_count,
        dma_error_count=dma_error_count,
        rx_unread_bytes=rx_unread_bytes,
        rx_peak_bytes=rx_peak_bytes,
        latched_faults=latched_faults,
    )


def decode_spi_record(payload: bytes) -> SpiDiagnostics:
    """Decode a 29-byte SPI diagnostic record payload (Type 5)."""
    if len(payload) < 29:
        raise ValueError(f"SPI diagnostic payload must be at least 29 bytes, got {len(payload)}")
    (
        channel,
        flags,
        tx_pending_bytes,
        tx_peak_bytes,
        tx_in_flight_bytes,
        tx_pending_packets,
        tx_peak_packets,
        tx_reject_count,
        tx_dma_error_count,
        tx_drain_timeouts,
        rx_unread_bytes,
        rx_peak_bytes,
        tx_state,
    ) = struct.unpack("<BBHHHHHIIIHHB", payload[:29])
    return SpiDiagnostics(
        channel=channel,
        flags=flags,
        tx_pending_bytes=tx_pending_bytes,
        tx_peak_bytes=tx_peak_bytes,
        tx_in_flight_bytes=tx_in_flight_bytes,
        tx_pending_packets=tx_pending_packets,
        tx_peak_packets=tx_peak_packets,
        tx_reject_count=tx_reject_count,
        tx_dma_error_count=tx_dma_error_count,
        tx_drain_timeouts=tx_drain_timeouts,
        rx_unread_bytes=rx_unread_bytes,
        rx_peak_bytes=rx_peak_bytes,
        tx_state=tx_state,
    )


def decode_can_record(payload: bytes) -> CanDiagnostics:
    """Decode a 35-byte CAN diagnostic record payload (Type 6)."""
    if len(payload) < 35:
        raise ValueError(f"CAN diagnostic payload must be at least 35 bytes, got {len(payload)}")

    (
        channel,
        flags,
        tx_pending,
        tx_peak,
        tx_pending_mailbox,
        rx_queued,
        rx_peak,
        rx_dropped,
        tec,
        rec,
        last_error,
        tsr,
        esr,
        error_count,
        max_tec,
        max_rec,
    ) = struct.unpack("<BBHHIHHIBBBIIIBB", payload[:35])

    return CanDiagnostics(
        channel=channel,
        flags=flags,
        tx_pending=tx_pending,
        tx_peak=tx_peak,
        tx_pending_mailbox=tx_pending_mailbox,
        rx_queued=rx_queued,
        rx_peak=rx_peak,
        rx_dropped=rx_dropped,
        tec=tec,
        rec=rec,
        last_error=last_error,
        tsr=tsr,
        esr=esr,
        error_count=error_count,
        max_tec=max_tec,
        max_rec=max_rec,
    )


def parse_run_report_extension(data: bytes) -> tuple[ExtensionHeader, list[tuple[int, bytes]]]:
    """Parse Run Report extension bytes into header and TLV records."""
    if len(data) < 12 or data[:2] != b"HR":
        raise ValueError("Invalid diagnostic extension header")

    magic = data[:2]
    version, header_bytes, total_bytes, record_count, flags, reserved = struct.unpack(
        "<BBBBBB", data[2:8]
    )
    (run_sequence,) = struct.unpack("<I", data[8:12])

    header = ExtensionHeader(
        magic=magic,
        version=version,
        header_bytes=header_bytes,
        total_bytes=total_bytes,
        record_count=record_count,
        flags=flags,
        run_sequence=run_sequence,
    )

    records: list[tuple[int, bytes]] = []
    offset = header_bytes
    while offset + 2 <= min(total_bytes, len(data)):
        rec_type, rec_len = struct.unpack_from("<BB", data, offset)
        rec_payload = data[offset + 2 : offset + 2 + rec_len]
        records.append((rec_type, rec_payload))
        offset += 2 + rec_len

    return header, records


__all__ = [
    "CAN_LAST_ERROR_NAMES",
    "CAN_RECORD_TYPE",
    "EXECUTION_FAILURE_NAMES",
    "FAILURE_DETAIL_RECORD_TYPE",
    "FLASH_COMMIT_STATUS_NAMES",
    "FLASH_DETAIL_RECORD_TYPE",
    "MEASUREMENT_FAILURE_REASON_NAMES",
    "MEASUREMENT_TYPE_NAMES",
    "OPERATION_FAILURE_REASON_NAMES",
    "OPERATION_OPCODE_NAMES",
    "RUNTIME_LIMITS_RECORD_TYPE",
    "SPI_RECORD_TYPE",
    "SPI_TX_STATE_NAMES",
    "UART_RECORD_TYPE",
    "CanDiagnostics",
    "ExtensionHeader",
    "FailureDetail",
    "FlashDetail",
    "RuntimeLimits",
    "SpiDiagnostics",
    "UartDiagnostics",
    "decode_can_record",
    "decode_failure_detail_record",
    "decode_flash_detail_record",
    "decode_runtime_limits_record",
    "decode_spi_record",
    "decode_uart_record",
    "parse_run_report_extension",
]
