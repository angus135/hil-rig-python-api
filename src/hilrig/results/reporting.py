"""Human-readable reports for captured-run metadata and lifecycle evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from hilrig.results.diagnostics import (
    CAN_RECORD_TYPE,
    FAILURE_DETAIL_RECORD_TYPE,
    FLASH_DETAIL_RECORD_TYPE,
    RUNTIME_LIMITS_RECORD_TYPE,
    SPI_RECORD_TYPE,
    UART_RECORD_TYPE,
    CanDiagnostics,
    FailureDetail,
    FlashDetail,
    RuntimeLimits,
    SpiDiagnostics,
    UartDiagnostics,
    decode_can_record,
    decode_failure_detail_record,
    decode_flash_detail_record,
    decode_runtime_limits_record,
    decode_spi_record,
    decode_uart_record,
    parse_run_report_extension,
)
from hilrig.results.models import (
    ApplicationResponseRecord,
    RigStatusRecord,
)

if TYPE_CHECKING:
    from hilrig.results.ir import CapturedRunIR


def render_run_metadata_markdown(run: CapturedRunIR) -> str:
    """Render the durable run metadata and lifecycle evidence as Markdown."""
    metadata = run.metadata
    report = run.report
    responses = tuple(run.iter_lifecycle_responses())
    status_events = tuple(run.iter_status_events())
    integrity_issues = tuple(run.iter_integrity_issues())

    lines = [
        f"# HIL-RIG Run Metadata: {_heading(metadata.test_name)}",
        "",
        f"**Capture status:** `{metadata.status.value.upper()}`  ",
        f"**Run ID:** `{metadata.run_id_hex}`  ",
        f"**Attempt:** `{metadata.attempt_number}`",
        "",
        "This report summarizes the host capture metadata and the firmware lifecycle "
        "evidence retained in `captured-run.sqlite3`. Assertion outcomes are in "
        "`evaluation-report.md`.",
        "",
        "## Test and run",
        "",
        _key_value_table(
            (
                ("Test name", metadata.test_name),
                ("Definition test ID", metadata.test_id_hex),
                ("Application test ID", metadata.application_test_id_hex),
                ("Run ID", metadata.run_id_hex),
                ("Attempt number", metadata.attempt_number),
                ("Capture database", run.database_path.name),
            )
        ),
        "",
        "## Capture timing and counts",
        "",
        _key_value_table(
            (
                ("Tick period", f"{metadata.tick_period_ns} ns"),
                ("Expected ticks", metadata.expected_tick_count),
                ("Received ticks", metadata.received_tick_count),
                ("Missing ticks", metadata.missing_tick_count),
                ("First tick", _optional(metadata.first_tick)),
                ("Last tick", _optional(metadata.last_tick)),
                ("Created at", metadata.created_at),
                ("Started at", _optional(metadata.started_at)),
                ("Finalized at", _optional(metadata.finalized_at)),
                ("Completed at", _optional(metadata.completed_at)),
            )
        ),
        "",
        "## RIG and protocol",
        "",
        _key_value_table(
            (
                ("Application protocol version", _optional(metadata.application_protocol_version)),
                ("Firmware version", _optional(metadata.firmware_version)),
                ("Result IR schema", metadata.schema_version),
            )
        ),
        "",
        "## Firmware Run Report",
        "",
    ]

    if report is None:
        lines.append("No firmware Run Report was persisted.")
    else:
        lines.extend(
            [
                _key_value_table(
                    (
                        ("Report schema version", report.schema_version),
                        ("Valid sections", f"0x{report.valid_sections:08x}"),
                        ("Run outcome", report.run_outcome),
                        ("Execution outcome", report.execution_outcome),
                        ("Result status", report.result_status),
                        ("Expected ticks", report.expected_tick_count),
                        ("Emitted ticks", report.result_ticks_emitted),
                        ("Tick period", f"{report.tick_period_us} us"),
                        ("Last completed boundary", _optional(report.last_completed_boundary)),
                        ("Failure source", report.failure_source),
                        ("Failure stage", report.failure_stage),
                        ("Failure reason", report.failure_reason),
                        ("Raw report bytes", len(report.raw_bytes)),
                        ("Extension bytes", len(report.extension_data)),
                    )
                ),
                "",
                "### Diagnostic sections",
                "",
            ]
        )
        diagnostics = (
            ("ISR timing", report.isr_timing),
            ("Instruction buffer", report.instruction_buffer),
            ("Result buffer", report.result_buffer),
            ("Flash", report.flash),
        )
        if any(values is not None for _, values in diagnostics):
            lines.extend(
                [
                    "| Section | Values |",
                    "|---|---|",
                    *(
                        f"| {_cell(name)} | {_cell(_format_mapping(values))} |"
                        for name, values in diagnostics
                        if values is not None
                    ),
                ]
            )
        else:
            lines.append("No diagnostic sections were marked valid.")

        if report.extension_data:
            lines.extend(["", "### Extension diagnostics", ""])
            try:
                header, records = parse_run_report_extension(report.extension_data)
                flags_desc = f"0x{header.flags:02x}"
                flag_notes = []
                if header.rx_overrun_undetected:
                    flag_notes.append("RX_OVERRUN_UNDETECTED")
                if header.partial_run:
                    flag_notes.append("PARTIAL_RUN")
                if flag_notes:
                    flags_desc += f" ({', '.join(flag_notes)})"

                lines.extend(
                    [
                        _key_value_table(
                            (
                                ("Run sequence", header.run_sequence),
                                ("Extension version", header.version),
                                ("Header bytes", header.header_bytes),
                                ("Total bytes", header.total_bytes),
                                ("Record count", header.record_count),
                                ("Flags", flags_desc),
                            )
                        ),
                        "",
                    ]
                )

                # Type 1: Runtime Limits
                limit_records = [
                    decode_runtime_limits_record(payload)
                    for rec_type, payload in records
                    if rec_type == RUNTIME_LIMITS_RECORD_TYPE
                ]
                for limits in limit_records:
                    lines.extend(["#### Runtime limits", "", _runtime_limits_table(limits), ""])

                # Type 2: Failure Detail
                failure_records = [
                    decode_failure_detail_record(payload)
                    for rec_type, payload in records
                    if rec_type == FAILURE_DETAIL_RECORD_TYPE
                ]
                for failure in failure_records:
                    lines.extend(["#### Failure detail", "", _failure_detail_table(failure), ""])

                # Type 3: Flash Detail
                flash_records = [
                    decode_flash_detail_record(payload)
                    for rec_type, payload in records
                    if rec_type == FLASH_DETAIL_RECORD_TYPE
                ]
                for flash_detail in flash_records:
                    lines.extend(["#### Flash detail", "", _flash_detail_table(flash_detail), ""])

                # Type 4: UART Diagnostics
                uart_records = [
                    decode_uart_record(payload)
                    for rec_type, payload in records
                    if rec_type == UART_RECORD_TYPE
                ]
                if uart_records:
                    lines.extend(
                        [
                            "#### UART diagnostics",
                            "",
                            "| Channel | Flags | TX Pending (Peak) | TX Rejects | "
                            "DMA Errors | RX Unread (Peak) | Faults |",
                            "|---:|---|---|---:|---:|---|---|",
                            *(_uart_diagnostics_row(uart) for uart in uart_records),
                            "",
                        ]
                    )

                # Type 5: SPI Diagnostics
                spi_records = [
                    decode_spi_record(payload)
                    for rec_type, payload in records
                    if rec_type == SPI_RECORD_TYPE
                ]
                if spi_records:
                    lines.extend(
                        [
                            "#### SPI diagnostics",
                            "",
                            "| Channel | Flags | TX Pending (Peak) | In-Flight | "
                            "TX Packets (Peak) | TX Rejects | DMA Errors | "
                            "Drain Timeouts | RX Unread (Peak) | State |",
                            "|---:|---|---|---|---|---:|---:|---:|---|---|",
                            *(_spi_diagnostics_row(spi) for spi in spi_records),
                            "",
                        ]
                    )

                # Type 6: CAN Diagnostics
                can_records = [
                    decode_can_record(payload)
                    for rec_type, payload in records
                    if rec_type == CAN_RECORD_TYPE
                ]
                if can_records:
                    lines.extend(
                        [
                            "#### CAN diagnostics",
                            "",
                            "| Channel | Flags | Errors | Max TEC/REC | TEC/REC | "
                            "TX Pending (Peak) | RX Queued (Peak) | RX Dropped | "
                            "Last Error | TSR / ESR |",
                            "|---:|---|---:|---|---|---|---|---:|---|---|",
                            *(_can_diagnostics_row(can) for can in can_records),
                            "",
                        ]
                    )

                # Other / Unrecognized records
                known_types = {
                    RUNTIME_LIMITS_RECORD_TYPE,
                    FAILURE_DETAIL_RECORD_TYPE,
                    FLASH_DETAIL_RECORD_TYPE,
                    UART_RECORD_TYPE,
                    SPI_RECORD_TYPE,
                    CAN_RECORD_TYPE,
                }
                other_records = [
                    (rec_type, payload)
                    for rec_type, payload in records
                    if rec_type not in known_types
                ]
                if other_records:
                    lines.extend(
                        [
                            "#### Other extension records",
                            "",
                            "| Type | Length | Payload (hex) |",
                            "|---:|---:|---|",
                            *(
                                f"| {rec_type} | {len(payload)} | 0x{payload.hex()} |"
                                for rec_type, payload in other_records
                            ),
                            "",
                        ]
                    )
            except ValueError:
                lines.append(f"Raw extension bytes (undecoded): `0x{report.extension_data.hex()}`")

    lines.extend(["", "## Lifecycle responses", ""])
    if responses:
        lines.extend(
            [
                "| Scope | Outcome | Reason | Detail | Tick | Control command | Global command |",
                "|---|---|---|---:|---:|---|---|",
                *(_response_row(response) for response in responses),
            ]
        )
    else:
        lines.append("No lifecycle responses were persisted.")

    lines.extend(["", "## RIG status events", ""])
    if status_events:
        lines.extend(
            [
                "| Observed at | Origin | State | Flags | Application test ID | Failure |",
                "|---|---|---|---:|---|---|",
                *(_status_row(event) for event in status_events),
            ]
        )
    else:
        lines.append("No RIG status events were persisted.")

    lines.extend(["", "## Collection integrity", ""])
    if integrity_issues:
        lines.extend(
            [
                "| Code | Detail |",
                "|---|---|",
                *(f"| {_cell(issue.code)} | {_cell(issue.detail)} |" for issue in integrity_issues),
            ]
        )
    else:
        lines.append("No host-side collection integrity issues were recorded.")

    return "\n".join(lines) + "\n"


def write_run_metadata_markdown(run: CapturedRunIR, path: str | Path) -> Path:
    """Write a human-readable run metadata report and return its absolute path."""
    output = _output_path(path, suffix=".md")
    output.write_text(render_run_metadata_markdown(run), encoding="utf-8", newline="\n")
    return output


def _key_value_table(values: Iterable[tuple[str, object]]) -> str:
    lines = ["| Field | Value |", "|---|---|"]
    lines.extend(f"| {_cell(name)} | {_cell(_optional(value))} |" for name, value in values)
    return "\n".join(lines)


def _response_row(response: ApplicationResponseRecord) -> str:
    return (
        f"| {_cell(response.scope)} | {_cell(response.outcome)} | {_cell(response.reason)} | "
        f"{response.detail} | {_optional(response.tick)} | "
        f"{_cell(_optional(response.control_command))} | "
        f"{_cell(_optional(response.global_control_command))} |"
    )


def _status_row(event: RigStatusRecord) -> str:
    application_test_id = (
        "-" if event.application_test_id is None else f"{event.application_test_id:032x}"
    )
    failure = "/".join((event.failure_source, event.failure_stage, event.failure_reason))
    return (
        f"| {_cell(event.observed_at)} | {_cell(event.origin)} | {_cell(event.state)} | "
        f"0x{event.flags:08x} | `{application_test_id}` | {_cell(failure)} |"
    )


def _format_mapping(values: dict[str, int] | None) -> str:
    if values is None:
        return "-"
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def _optional(value: object) -> str:
    return "-" if value is None else str(value)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _runtime_limits_table(limits: RuntimeLimits) -> str:
    return _key_value_table(
        (
            ("Core clock", f"{limits.core_clock_hz} Hz ({limits.core_clock_hz / 1e6:.1f} MHz)"),
            ("Instruction buffer RAM", f"{limits.instr_ram_capacity} bytes"),
            ("Result buffer RAM", f"{limits.result_ram_capacity} bytes"),
            ("UART TX / RX ring", f"{limits.uart_tx_capacity} / {limits.uart_rx_capacity} bytes"),
            ("SPI TX / RX buffer", f"{limits.spi_tx_bytes_cap} / {limits.spi_rx_bytes_cap} bytes"),
            ("SPI TX descriptor queue", f"{limits.spi_tx_pkt_cap} packets"),
            ("CAN TX / RX queue", f"{limits.can_tx_capacity} / {limits.can_rx_capacity} packets"),
        )
    )


def _failure_detail_table(fail: FailureDetail) -> str:
    rows: list[tuple[str, object]] = []
    if fail.has_op_failure:
        rows.extend(
            [
                ("Operation failure", "YES"),
                ("Execution failure", fail.execution_failure_name),
                (
                    "Operation opcode",
                    f"{fail.opcode_name} (channel={fail.channel}, op_idx={fail.operation_index})",
                ),
                ("Operation failure reason", fail.op_failure_reason_name),
                ("Execution boundary tick", fail.execution_boundary),
            ]
        )
    else:
        rows.append(("Operation failure", "NO"))

    if fail.has_meas_failure:
        rows.extend(
            [
                ("Measurement failure", "YES"),
                (
                    "Measurement type",
                    f"{fail.measurement_type_name} "
                    f"(channel={fail.meas_channel}, meas_idx={fail.measurement_index})",
                ),
                ("Measurement failure reason", fail.meas_failure_reason_name),
            ]
        )
    else:
        rows.append(("Measurement failure", "NO"))

    return _key_value_table(rows)


def _flash_detail_table(flash_detail: FlashDetail) -> str:
    return _key_value_table(
        (
            ("Pending result bytes", flash_detail.pending_bytes),
            ("Failed reserve bytes", flash_detail.failed_reserve_bytes),
            ("Free RAM at failure", flash_detail.free_bytes_at_fail),
            ("Last commit status", flash_detail.last_commit_status_name),
        )
    )


def _uart_diagnostics_row(uart: UartDiagnostics) -> str:
    faults: list[str] = []
    if uart.tx_dma_fault:
        faults.append("TX_DMA_FAULT")
    if uart.rx_dma_fault:
        faults.append("RX_DMA_FAULT")
    fault_str = ", ".join(faults) if faults else "-"
    return (
        f"| {uart.channel} | 0x{uart.flags:02x} | "
        f"{uart.tx_pending_bytes} ({uart.tx_peak_bytes}) | "
        f"{uart.tx_reject_count} | {uart.dma_error_count} | "
        f"{uart.rx_unread_bytes} ({uart.rx_peak_bytes}) | {_cell(fault_str)} |"
    )


def _spi_diagnostics_row(spi: SpiDiagnostics) -> str:
    return (
        f"| {spi.channel} | 0x{spi.flags:02x} | "
        f"{spi.tx_pending_bytes} ({spi.tx_peak_bytes}) | {spi.tx_in_flight_bytes} | "
        f"{spi.tx_pending_packets} ({spi.tx_peak_packets}) | {spi.tx_reject_count} | "
        f"{spi.tx_dma_error_count} | {spi.tx_drain_timeouts} | "
        f"{spi.rx_unread_bytes} ({spi.rx_peak_bytes}) | {_cell(spi.tx_state_name)} |"
    )


def _can_diagnostics_row(can: CanDiagnostics) -> str:
    return (
        f"| {can.channel} | 0x{can.flags:02x} | {can.error_count} | "
        f"{can.max_tec}/{can.max_rec} | {can.tec}/{can.rec} | "
        f"{can.tx_pending} ({can.tx_peak}) | {can.rx_queued} ({can.rx_peak}) | "
        f"{can.rx_dropped} | 0x{can.last_error:02x} | "
        f"0x{can.tsr:08x} / 0x{can.esr:08x} |"
    )


def _heading(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _output_path(path: str | Path, *, suffix: str) -> Path:
    output = Path(path).expanduser().resolve()
    if output.suffix.lower() != suffix:
        raise ValueError(f"Output path must end in {suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


__all__ = ["render_run_metadata_markdown", "write_run_metadata_markdown"]
