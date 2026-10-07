"""Unit tests for diagnostic decoding and analysis parsing."""

from __future__ import annotations

import struct

import pytest

from hilrig.results.diagnostics import (
    CAN_RECORD_TYPE,
    FAILURE_DETAIL_RECORD_TYPE,
    FLASH_DETAIL_RECORD_TYPE,
    RUNTIME_LIMITS_RECORD_TYPE,
    SPI_RECORD_TYPE,
    UART_RECORD_TYPE,
    decode_can_record,
    decode_failure_detail_record,
    decode_flash_detail_record,
    decode_runtime_limits_record,
    decode_spi_record,
    decode_uart_record,
    parse_run_report_extension,
)


def _build_extension_header(
    *,
    magic: bytes = b"HR",
    version: int = 1,
    header_bytes: int = 12,
    total_bytes: int = 12,
    record_count: int = 0,
    flags: int = 0,
    reserved: int = 0,
    run_sequence: int = 42,
) -> bytes:
    return (
        magic
        + struct.pack("<BBBBBB", version, header_bytes, total_bytes, record_count, flags, reserved)
        + struct.pack("<I", run_sequence)
    )


def _build_runtime_limits_payload(
    *,
    core_clock_hz: int = 180_000_000,
    instr_ram_capacity: int = 65536,
    result_ram_capacity: int = 131072,
    uart_tx_capacity: int = 1024,
    uart_rx_capacity: int = 2048,
    spi_tx_bytes_cap: int = 4096,
    spi_rx_bytes_cap: int = 4096,
    spi_tx_pkt_cap: int = 64,
    can_tx_capacity: int = 32,
    can_rx_capacity: int = 64,
) -> bytes:
    return struct.pack(
        "<IIIHHHHHHH",
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
    )


def _build_failure_detail_payload(
    *,
    op_failure_valid: int = 1,
    execution_failure: int = 5,  # Operation Rejected
    operation_index: int = 2,
    opcode: int = 3,  # CAN TX
    channel: int = 0,
    op_failure_reason: int = 2,  # Queue Full
    execution_boundary: int = 150,
    meas_failure_valid: int = 1,
    measurement_index: int = 1,
    measurement_type: int = 5,  # CAN RX
    meas_channel: int = 0,
    meas_failure_reason: int = 1,  # Reserve Failed
) -> bytes:
    return struct.pack(
        "<BBBBBBIBBBBB",
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
    )


def _build_flash_detail_payload(
    *,
    pending_bytes: int = 4096,
    failed_reserve_bytes: int = 128,
    free_bytes_at_fail: int = 64,
    last_commit_status: int = 3,  # Overflow
) -> bytes:
    return struct.pack(
        "<IHIB",
        pending_bytes,
        failed_reserve_bytes,
        free_bytes_at_fail,
        last_commit_status,
    )


def _build_uart_record_payload(
    *,
    channel: int = 0,
    flags: int = 0x87,  # VALID | TX_DMA_ACTIVE | STARTED | CONFIGURED
    tx_pending_bytes: int = 12,
    tx_peak_bytes: int = 128,
    tx_reject_count: int = 0,
    dma_error_count: int = 0,
    rx_unread_bytes: int = 34,
    rx_peak_bytes: int = 256,
    latched_faults: int = 0,
) -> bytes:
    return struct.pack(
        "<BBHHIIHHI",
        channel,
        flags,
        tx_pending_bytes,
        tx_peak_bytes,
        tx_reject_count,
        dma_error_count,
        rx_unread_bytes,
        rx_peak_bytes,
        latched_faults,
    )


def _build_spi_record_payload(
    *,
    channel: int = 1,
    flags: int = 0x87,  # VALID | STARTED | CONFIGURED | MASTER
    tx_pending_bytes: int = 64,
    tx_peak_bytes: int = 512,
    tx_in_flight_bytes: int = 32,
    tx_pending_packets: int = 2,
    tx_peak_packets: int = 8,
    tx_reject_count: int = 0,
    tx_dma_error_count: int = 0,
    tx_drain_timeouts: int = 0,
    rx_unread_bytes: int = 16,
    rx_peak_bytes: int = 128,
    tx_state: int = 1,  # DMA Active
) -> bytes:
    return struct.pack(
        "<BBHHHHHIIIHHB",
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
    )


def _build_can_record_payload(
    *,
    channel: int = 1,
    flags: int = 0x03,
    tx_pending: int = 2,
    tx_peak: int = 5,
    tx_pending_mailbox: int = 0x07,
    rx_queued: int = 4,
    rx_peak: int = 10,
    rx_dropped: int = 0,
    tec: int = 15,
    rec: int = 20,
    last_error: int = 0,
    tsr: int = 0x12345678,
    esr: int = 0x87654321,
    error_count: int = 99,
    max_tec: int = 45,
    max_rec: int = 55,
) -> bytes:
    return struct.pack(
        "<BBHHIHHIBBBIIIBB",
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
    )


def test_parse_run_report_extension_header_valid() -> None:
    raw = _build_extension_header(
        magic=b"HR",
        version=1,
        header_bytes=12,
        total_bytes=12,
        record_count=0,
        flags=0x03,  # RX overrun undetected | partial run
        run_sequence=123456,
    )
    header, records = parse_run_report_extension(raw)

    assert header.magic == b"HR"
    assert header.version == 1
    assert header.header_bytes == 12
    assert header.total_bytes == 12
    assert header.record_count == 0
    assert header.flags == 0x03
    assert header.rx_overrun_undetected is True
    assert header.partial_run is True
    assert header.run_sequence == 123456
    assert records == []


def test_parse_run_report_extension_invalid_magic() -> None:
    raw = b"XX\x01\x0c\x0c\x00\x00\x00\x00\x00\x00\x00"
    with pytest.raises(ValueError, match="Invalid diagnostic extension header"):
        parse_run_report_extension(raw)


def test_parse_run_report_extension_too_short() -> None:
    raw = b"HR\x01\x08"
    with pytest.raises(ValueError, match="Invalid diagnostic extension header"):
        parse_run_report_extension(raw)


def test_decode_runtime_limits_record_26_bytes() -> None:
    payload = _build_runtime_limits_payload()
    assert len(payload) == 26
    limits = decode_runtime_limits_record(payload)
    assert limits.core_clock_hz == 180_000_000
    assert limits.instr_ram_capacity == 65536
    assert limits.result_ram_capacity == 131072
    assert limits.uart_tx_capacity == 1024
    assert limits.uart_rx_capacity == 2048
    assert limits.spi_tx_bytes_cap == 4096
    assert limits.spi_rx_bytes_cap == 4096
    assert limits.spi_tx_pkt_cap == 64
    assert limits.can_tx_capacity == 32
    assert limits.can_rx_capacity == 64


def test_decode_failure_detail_record_15_bytes() -> None:
    payload = _build_failure_detail_payload()
    assert len(payload) == 15
    fail = decode_failure_detail_record(payload)
    assert fail.has_op_failure is True
    assert fail.execution_failure_name == "Operation Rejected"
    assert fail.operation_index == 2
    assert fail.opcode_name == "CAN TX"
    assert fail.channel == 0
    assert fail.op_failure_reason_name == "Queue Full"
    assert fail.execution_boundary == 150
    assert fail.has_meas_failure is True
    assert fail.measurement_index == 1
    assert fail.measurement_type_name == "CAN RX"
    assert fail.meas_channel == 0
    assert fail.meas_failure_reason_name == "Reserve Failed"


def test_decode_flash_detail_record_11_bytes() -> None:
    payload = _build_flash_detail_payload()
    assert len(payload) == 11
    flash = decode_flash_detail_record(payload)
    assert flash.pending_bytes == 4096
    assert flash.failed_reserve_bytes == 128
    assert flash.free_bytes_at_fail == 64
    assert flash.last_commit_status_name == "Overflow"


def test_decode_uart_record_22_bytes() -> None:
    payload = _build_uart_record_payload()
    assert len(payload) == 22
    uart = decode_uart_record(payload)
    assert uart.channel == 0
    assert uart.valid is True
    assert uart.tx_dma_active is True
    assert uart.started is True
    assert uart.configured is True
    assert uart.tx_pending_bytes == 12
    assert uart.tx_peak_bytes == 128
    assert uart.rx_unread_bytes == 34
    assert uart.rx_peak_bytes == 256


def test_decode_spi_record_29_bytes() -> None:
    payload = _build_spi_record_payload()
    assert len(payload) == 29
    spi = decode_spi_record(payload)
    assert spi.channel == 1
    assert spi.valid is True
    assert spi.started is True
    assert spi.configured is True
    assert spi.is_master is True
    assert spi.tx_pending_bytes == 64
    assert spi.tx_in_flight_bytes == 32
    assert spi.tx_state_name == "DMA Active"


def test_decode_can_record_35_bytes() -> None:
    payload = _build_can_record_payload(
        channel=2,
        flags=0x80,
        tx_pending=10,
        tx_peak=20,
        tx_pending_mailbox=0x05,
        rx_queued=15,
        rx_peak=30,
        rx_dropped=500,
        tec=128,
        rec=96,
        last_error=3,
        tsr=0xAABBCCDD,
        esr=0xEEFF0011,
        error_count=1234567,
        max_tec=250,
        max_rec=180,
    )
    assert len(payload) == 35

    diag = decode_can_record(payload)
    assert diag.channel == 2
    assert diag.flags == 0x80
    assert diag.valid is True
    assert diag.tx_pending == 10
    assert diag.tx_peak == 20
    assert diag.tx_pending_mailbox == 0x05
    assert diag.rx_queued == 15
    assert diag.rx_peak == 30
    assert diag.rx_dropped == 500
    assert diag.tec == 128
    assert diag.rec == 96
    assert diag.last_error == 3
    assert diag.last_error_name == "Ack"
    assert diag.tsr == 0xAABBCCDD
    assert diag.esr == 0xEEFF0011
    assert diag.error_count == 1234567
    assert diag.max_tec == 250
    assert diag.max_rec == 180


def test_decode_short_payloads_raise() -> None:
    with pytest.raises(ValueError, match="Runtime limits"):
        decode_runtime_limits_record(b"\x00" * 25)
    with pytest.raises(ValueError, match="Failure detail"):
        decode_failure_detail_record(b"\x00" * 14)
    with pytest.raises(ValueError, match="Flash detail"):
        decode_flash_detail_record(b"\x00" * 10)
    with pytest.raises(ValueError, match="UART diagnostic"):
        decode_uart_record(b"\x00" * 21)
    with pytest.raises(ValueError, match="SPI diagnostic"):
        decode_spi_record(b"\x00" * 28)
    with pytest.raises(ValueError, match="CAN diagnostic"):
        decode_can_record(b"\x00" * 34)


def test_parse_run_report_extension_all_records() -> None:
    p1 = _build_runtime_limits_payload()
    p2 = _build_failure_detail_payload()
    p3 = _build_flash_detail_payload()
    p4 = _build_uart_record_payload()
    p5 = _build_spi_record_payload()
    p6 = _build_can_record_payload()

    records_data = (
        struct.pack("<BB", RUNTIME_LIMITS_RECORD_TYPE, len(p1))
        + p1
        + struct.pack("<BB", FAILURE_DETAIL_RECORD_TYPE, len(p2))
        + p2
        + struct.pack("<BB", FLASH_DETAIL_RECORD_TYPE, len(p3))
        + p3
        + struct.pack("<BB", UART_RECORD_TYPE, len(p4))
        + p4
        + struct.pack("<BB", SPI_RECORD_TYPE, len(p5))
        + p5
        + struct.pack("<BB", CAN_RECORD_TYPE, len(p6))
        + p6
    )

    hdr_data = _build_extension_header(
        magic=b"HR",
        version=1,
        header_bytes=12,
        total_bytes=12 + len(records_data),
        record_count=6,
        flags=0x01,
        run_sequence=77,
    )
    full_data = hdr_data + records_data

    header, records = parse_run_report_extension(full_data)
    assert header.run_sequence == 77
    assert header.record_count == 6
    assert len(records) == 6
    assert [r[0] for r in records] == [1, 2, 3, 4, 5, 6]


def test_composite_keying_disambiguation() -> None:
    test_id = 0x1234567890ABCDEF1234567890ABCDEF

    data_attempt_1 = _build_extension_header(run_sequence=1)
    data_attempt_2 = _build_extension_header(run_sequence=2)

    hdr1, _ = parse_run_report_extension(data_attempt_1)
    hdr2, _ = parse_run_report_extension(data_attempt_2)

    key1 = (test_id, hdr1.run_sequence)
    key2 = (test_id, hdr2.run_sequence)

    assert key1 != key2
    assert key1 == (test_id, 1)
    assert key2 == (test_id, 2)


def test_render_run_metadata_markdown_all_records(tmp_path) -> None:
    from hilrig.results.builder import CapturedRunBuilder
    from hilrig.results.models import (
        ANALOGUE_INPUT_CHANNEL_COUNT,
        DIGITAL_INPUT_CHANNEL_COUNT,
        PWM_INPUT_CHANNEL_COUNT,
        PWMMeasurement,
        RunReportRecord,
        TickResult,
    )
    from hilrig.results.reporting import render_run_metadata_markdown

    p1 = _build_runtime_limits_payload()
    p2 = _build_failure_detail_payload()
    p3 = _build_flash_detail_payload()
    p4 = _build_uart_record_payload()
    p5 = _build_spi_record_payload()
    p6 = _build_can_record_payload()

    records_data = (
        struct.pack("<BB", RUNTIME_LIMITS_RECORD_TYPE, len(p1))
        + p1
        + struct.pack("<BB", FAILURE_DETAIL_RECORD_TYPE, len(p2))
        + p2
        + struct.pack("<BB", FLASH_DETAIL_RECORD_TYPE, len(p3))
        + p3
        + struct.pack("<BB", UART_RECORD_TYPE, len(p4))
        + p4
        + struct.pack("<BB", SPI_RECORD_TYPE, len(p5))
        + p5
        + struct.pack("<BB", CAN_RECORD_TYPE, len(p6))
        + p6
    )

    hdr_data = _build_extension_header(
        magic=b"HR",
        version=1,
        header_bytes=12,
        total_bytes=12 + len(records_data),
        record_count=6,
        flags=0x01,
        run_sequence=99,
    )
    full_ext = hdr_data + records_data

    builder = CapturedRunBuilder(
        database_path=tmp_path / "test_all.sqlite3",
        test_id=0x1234,
        application_test_id=0x5678,
        run_id=0x9ABC,
        test_name="All Diagnostics Test",
        tick_period_ns=1000,
        expected_tick_count=1,
    )
    builder.add_tick_result(
        TickResult(
            tick=0,
            digital_inputs=(False,) * DIGITAL_INPUT_CHANNEL_COUNT,
            analogue_inputs_uv=(0,) * ANALOGUE_INPUT_CHANNEL_COUNT,
            pwm_inputs=(PWMMeasurement(period_ns=1000, duty_permyriad=5000),)
            * PWM_INPUT_CHANNEL_COUNT,
        )
    )
    report = RunReportRecord(
        schema_version=1,
        valid_sections=0x01,
        run_outcome="FAILED",
        execution_outcome="FAILED",
        result_status="PARTIAL",
        expected_tick_count=1,
        tick_period_us=1,
        last_completed_boundary=1,
        result_ticks_emitted=1,
        failure_source="EXECUTION_MANAGER",
        failure_stage="EXECUTION",
        failure_reason="OPERATION_REJECTED",
        isr_timing=None,
        instruction_buffer=None,
        result_buffer=None,
        flash=None,
        extension_data=full_ext,
        raw_bytes=b"raw",
    )
    run = builder.finalize(report=report)

    md = render_run_metadata_markdown(run)
    assert "### Extension diagnostics" in md
    assert "Run sequence" in md
    assert "99" in md
    assert "RX_OVERRUN_UNDETECTED" in md
    assert "#### Runtime limits" in md
    assert "180.0 MHz" in md
    assert "#### Failure detail" in md
    assert "Operation Rejected" in md
    assert "#### Flash detail" in md
    assert "Overflow" in md
    assert "#### UART diagnostics" in md
    assert "#### SPI diagnostics" in md
    assert "DMA Active" in md
    assert "#### CAN diagnostics" in md
    assert "99" in md  # error_count
    assert "45/55" in md  # max_tec/max_rec


def test_render_run_metadata_markdown_with_undecoded_extension(tmp_path) -> None:
    from hilrig.results.builder import CapturedRunBuilder
    from hilrig.results.models import (
        ANALOGUE_INPUT_CHANNEL_COUNT,
        DIGITAL_INPUT_CHANNEL_COUNT,
        PWM_INPUT_CHANNEL_COUNT,
        PWMMeasurement,
        RunReportRecord,
        TickResult,
    )
    from hilrig.results.reporting import render_run_metadata_markdown

    builder = CapturedRunBuilder(
        database_path=tmp_path / "test_undecoded.sqlite3",
        test_id=0x1234,
        application_test_id=0x5678,
        run_id=0x9ABC,
        test_name="Undecoded Test",
        tick_period_ns=1000,
        expected_tick_count=1,
    )
    builder.add_tick_result(
        TickResult(
            tick=0,
            digital_inputs=(False,) * DIGITAL_INPUT_CHANNEL_COUNT,
            analogue_inputs_uv=(0,) * ANALOGUE_INPUT_CHANNEL_COUNT,
            pwm_inputs=(PWMMeasurement(period_ns=1000, duty_permyriad=5000),)
            * PWM_INPUT_CHANNEL_COUNT,
        )
    )
    report = RunReportRecord(
        schema_version=1,
        valid_sections=0x01,
        run_outcome="SUCCESS",
        execution_outcome="COMPLETE",
        result_status="COMPLETE",
        expected_tick_count=1,
        tick_period_us=1,
        last_completed_boundary=1,
        result_ticks_emitted=1,
        failure_source="NONE",
        failure_stage="NONE",
        failure_reason="NONE",
        isr_timing=None,
        instruction_buffer=None,
        result_buffer=None,
        flash=None,
        extension_data=b"future_unparsed_data",
        raw_bytes=b"raw",
    )
    run = builder.finalize(report=report)

    md = render_run_metadata_markdown(run)
    assert "### Extension diagnostics" in md
    assert "Raw extension bytes (undecoded):" in md
