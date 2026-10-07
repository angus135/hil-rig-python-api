r"""Isolate the prototype-sweep 100 Hz, 57% utilization, 150-second point with deep diagnostics.

Uses the same UART2 + SPI2 configuration and execution path as
``prototype_sweep.py``, but runs standalone with exact protocol diagnostics,
allowing the error and RIG status to be read and inspected BEFORE any reset is sent.

Usage:
  .\.venv\Scripts\python "HIL-RIG_loopback_tests\Prototype Sweep (development)\prototype_100hz_57pct_isolation.py" --live --port COM10
"""

from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
import secrets
import sys
import time
from typing import Any

from hilrig.exceptions import ProtocolSessionError
from hilrig.protocol import (
    FixedIOProtocolConnection,
    ProtocolFamily,
    ProtocolWorkflowState,
    UploadAdvanceMode,
)
from hilrig.protocol.serial import SerialConnectionSettings
from hilrig.results import CapturedRunBuilder
from hilrig.workloads import (
    LoopbackConfiguration,
    LoopbackWorkloadPoint,
    Test,
    adapt_workload_point_duration,
    build_loopback_test,
    calculate_max_safe_duration_s,
    estimate_workload_instruction_bytes,
)
from hilrig.evaluation import evaluate_assertions

FREQUENCY_HZ = 100
TARGET_UTILIZATION_PERCENT = 57.0
NOMINAL_DURATION_S = 60
BURST_INTERVAL_TICKS = 1
SEED = 100


def workload_point(duration_s: int = NOMINAL_DURATION_S, adapt: bool = True) -> LoopbackWorkloadPoint:
    """Return the single workload point under investigation, optionally adapting duration."""
    pt = LoopbackWorkloadPoint(
        frequency_hz=FREQUENCY_HZ,
        duration_s=duration_s,
        target_utilization_percent=TARGET_UTILIZATION_PERCENT,
        burst_interval_ticks=BURST_INTERVAL_TICKS,
        seed=SEED,
    )
    if adapt:
        config = LoopbackConfiguration.config_0_prototype()
        return adapt_workload_point_duration(config, pt)
    return pt


def build_test(uart_baud: int = 2_000_000, duration_s: int = NOMINAL_DURATION_S) -> Test:
    """Build the isolated test definition."""
    config = LoopbackConfiguration.config_0_prototype(uart_baud_hz=uart_baud)
    point = workload_point(duration_s=duration_s)
    return build_loopback_test(config, point)


def inspect_compiled_tick(compiled: Any, target_tick: int) -> None:
    """Print instructions and transfers around the target tick."""
    print(f"\n--- Compiled Instructions around Tick {target_tick} ---")
    surrounding = [
        inst for inst in compiled.instructions if abs(inst.tick - target_tick) <= 3
    ]
    if not surrounding:
        print(f"  No explicit instructions found within +/-3 ticks of tick {target_tick} (idle/no-op ticks).")
    else:
        for inst in surrounding:
            marker = "==>" if inst.tick == target_tick else "   "
            args_str = ", ".join(f"{k}={v}" for k, v in inst.arguments.items())
            print(f"  {marker} Tick {inst.tick:5d} | {inst.subject_name:12s} | Op: {inst.operation:16s} | {args_str}")


def _purge_serial(conn: Any) -> None:
    if conn is None:
        return
    port_obj = getattr(conn, "serial_port", None)
    if port_obj is not None:
        with suppress(Exception):
            if hasattr(port_obj, "reset_input_buffer"):
                port_obj.reset_input_buffer()
            if hasattr(port_obj, "reset_output_buffer"):
                port_obj.reset_output_buffer()
    incoming = getattr(conn, "_incoming", None)
    if incoming is not None:
        with suppress(Exception):
            incoming.clear()


def _reset_connection(conn: Any) -> None:
    if conn is None or getattr(conn, "closed", False):
        return
    conn.result_adapter = None
    _purge_serial(conn)

    port_obj = getattr(conn, "serial_port", None)
    if port_obj is not None and hasattr(conn, "application"):
        with suppress(Exception):
            raw_reset = conn.application.encode(conn.application.build_reset_application())
            raw_framed = len(raw_reset).to_bytes(2, "little") + raw_reset
            port_obj.write(raw_framed)
            if hasattr(port_obj, "flush"):
                port_obj.flush()
        time.sleep(0.05)

    _purge_serial(conn)

    conn._retire_active_upload()
    conn.result_adapter = None
    conn._run_report = None
    conn._report_deadline = None
    conn._report_timed_out = False
    conn._status_events = []
    conn._lifecycle_responses = []
    conn._accumulated_errors = []
    conn._pending_operation = None
    conn._gated_operation = None
    conn._pending_output = None
    conn._pending_output_offset = 0
    conn._transport_delivery_pending = False
    conn._upload_operations.clear()

    if conn.session_confirmed:
        with suppress(Exception):
            conn.get_status()

    readiness_wait = time.monotonic() + 4.0
    next_query = time.monotonic() + 0.8
    while time.monotonic() < readiness_wait and not conn.ready_for_upload:
        rep = conn.service()
        if (
            conn.session_confirmed
            and not conn.ready_for_upload
            and conn.workflow_state is ProtocolWorkflowState.WAITING_FOR_READY
            and time.monotonic() >= next_query
        ):
            with suppress(Exception):
                conn.get_status()
            next_query = time.monotonic() + 0.8
        if not (
            rep.serial_bytes_read
            or rep.serial_bytes_written
            or rep.application_message_submitted
        ):
            time.sleep(0.002)

    conn._pending_operation = None
    conn._transport_delivery_pending = False


def _get_connection(port: str) -> Any:
    settings = SerialConnectionSettings(device=port)
    for attempt in range(4):
        time.sleep(0.3 if attempt == 0 else 1.0)
        conn = None
        try:
            conn = FixedIOProtocolConnection.connect(
                serial_settings=settings, protocol_family=ProtocolFamily.VARIABLE
            )
            _purge_serial(conn)

            if attempt > 0:
                with suppress(Exception):
                    raw_reset = conn.application.encode(conn.application.build_reset_application())
                    raw_framed = len(raw_reset).to_bytes(2, "little") + raw_reset
                    conn.serial_port.write(raw_framed)
                    if hasattr(conn.serial_port, "flush"):
                        conn.serial_port.flush()
                time.sleep(0.05)
                _purge_serial(conn)

            deadline = time.monotonic() + 6.0
            next_query = time.monotonic() + 1.0
            while time.monotonic() < deadline and (
                not conn.session_confirmed or not conn.ready_for_upload
            ):
                rep = conn.service()
                if (
                    conn.session_confirmed
                    and not conn.ready_for_upload
                    and conn.workflow_state is ProtocolWorkflowState.WAITING_FOR_READY
                    and time.monotonic() >= next_query
                ):
                    with suppress(Exception):
                        conn.get_status()
                    next_query = time.monotonic() + 1.0
                if not (
                    rep.serial_bytes_read
                    or rep.serial_bytes_written
                    or rep.application_message_submitted
                ):
                    time.sleep(0.002)

            if conn.session_confirmed and conn.ready_for_upload:
                return conn
            with suppress(Exception):
                conn.close()
        except Exception:
            if conn is not None:
                with suppress(Exception):
                    conn.close()

    raise ProtocolSessionError(f"Failed to establish ready-for-upload session on {port}")


def run_live_isolated(
    port: str,
    runs_dir: Path,
    reset_on_failure: bool = False,
    reset_on_success: bool = True,
) -> bool:
    """Execute the isolated test point directly with error preservation before reset."""
    config = LoopbackConfiguration.config_0_prototype()
    point = workload_point()

    print("=================================================================")
    print("HIL-RIG Isolated Failure Point Diagnostic Runner")
    print(f"Configuration: {config.name} ({config.description})")
    print(
        f"Point: {point.frequency_hz} Hz, {point.target_utilization_percent:g}% utilization, "
        f"{point.duration_s}s, burst={point.burst_interval_ticks}, seed={point.seed}"
    )
    print(f"Ticks: {point.total_ticks}")
    print(f"Port: {port}")
    print(f"Reset on failure: {reset_on_failure}")
    print("=================================================================\n")

    test = build_loopback_test(config, point)
    print("Compiling test definition...", end="", flush=True)
    compiled = test.compile()
    print(f" done. ({compiled.instruction_count} instructions, {compiled.expected_tick_count} expected ticks)")

    run_id = secrets.randbits(128)
    run_id_hex = f"{run_id:032x}"
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir = runs_dir / f"{timestamp}-config0-{point.frequency_hz}hz-{run_id_hex[:8]}"
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "captured-run.sqlite3"

    print(f"Connecting to {port} and establishing clean ready-for-upload state...")
    conn = _get_connection(port)

    info = conn.session_info
    print(f"Session established: Firmware={info.firmware_version if info else 'unknown'}, "
          f"Protocol={info.protocol_version if info else 'unknown'}")

    try:
        attempt = compiled.new_upload_attempt()
        builder = CapturedRunBuilder.from_compiled_test(
            db_path,
            compiled,
            upload_attempt=attempt,
            run_id=run_id,
            application_protocol_version=info.protocol_version if info else None,
            firmware_version=info.firmware_version if info else None,
            attempt_number=1,
            started_at=datetime.now(UTC).isoformat(),
        )
        conn.bind_result_builder(builder, replace=True)

        print("\nStarting instruction upload (monitoring tick-by-tick)...")
        conn.queue_upload(compiled, upload_attempt=attempt, advance_mode=UploadAdvanceMode.AUTOMATIC)

        last_progress_print = time.monotonic()
        while not conn.report_received:
            rep = conn.service()

            # Periodic progress print
            if time.monotonic() - last_progress_print > 0.5:
                delivered = getattr(conn, "_upload_messages_delivered", 0)
                ticks_rx = getattr(conn, "_next_result_tick", 0)
                total_ticks = compiled.expected_tick_count
                state = conn.workflow_state.name if conn.workflow_state else "UNKNOWN"
                if getattr(conn, "_started", False):
                    pct = (ticks_rx / total_ticks * 100.0) if total_ticks > 0 else 0.0
                    print(
                        f"  [Executing] Results streaming: {ticks_rx}/{total_ticks} ticks ({pct:.1f}%) | State: {state}    ",
                        end="\r",
                        flush=True,
                    )
                else:
                    print(
                        f"  [Uploading] Upload delivered: {delivered} msgs | State: {state}    ",
                        end="\r",
                        flush=True,
                    )
                last_progress_print = time.monotonic()

            # If workflow failed or error occurred, handle IMMEDIATELY before reset
            if conn.workflow_state is ProtocolWorkflowState.FAILED:
                err_msg = getattr(conn, "last_error", None) or "Protocol workflow failed"
                raise ProtocolSessionError(err_msg)

            if str(conn.workflow_state).endswith("READY_TO_START") and not getattr(conn, "_started", False):
                print(f"\nUpload complete! Starting test execution on RIG...")
                conn.start()
                setattr(conn, "_started", True)

            if not (
                rep.serial_bytes_read
                or rep.serial_bytes_written
                or rep.stored_tick_results
                or rep.application_message_submitted
            ):
                time.sleep(0.001)

        print("\nTest execution finished. Finalizing captured database...")
        captured_run = builder.finalize(
            report=conn.run_report,
            finished_at=datetime.now(UTC).isoformat(),
        )
        eval_report = evaluate_assertions(captured_run)
        print(f"Result Verdict: {eval_report.overall_verdict.value}")

        if reset_on_success:
            print("Cleaning up RIG state with RESET_APPLICATION...")
            _reset_connection(conn)

        return eval_report.overall_verdict.value == "pass"

    except Exception as err:
        print(f"\n\n=================================================================")
        print(f"CAPTURE ERROR BEFORE RESET:")
        print(f"Exception: {type(err).__name__}: {err}")
        print("=================================================================")

        # 1. Print Last Application Response
        last_resp = getattr(conn, "last_application_response", None)
        if last_resp is not None:
            print("\n[Last Application Response from RIG]")
            print(f"  Outcome: {getattr(last_resp, 'outcome', None)}")
            print(f"  Reason:  {getattr(last_resp, 'reason', None)}")
            print(f"  Detail:  {getattr(last_resp, 'detail', None)}")
            print(f"  Tick:    {getattr(last_resp, 'tick_number', None)}")
            print(f"  Scope:   {getattr(last_resp, 'scope', None)}")
            print(f"  Command: {getattr(last_resp, 'command', None)}")

            rejected_tick = getattr(last_resp, "tick_number", None)
            if rejected_tick is not None:
                inspect_compiled_tick(compiled, rejected_tick)

        # 2. Print Accumulated Errors
        accum_errors = getattr(conn, "accumulated_errors", ())
        if accum_errors:
            print(f"\n[Accumulated Protocol Error Records ({len(accum_errors)})]")
            for idx, err_rec in enumerate(accum_errors):
                print(f"  [{idx+1}] Category={getattr(err_rec, 'category', None)} | "
                      f"Tick={getattr(err_rec, 'tick', None)} | "
                      f"Detail={getattr(err_rec, 'detail', None)} | "
                      f"Recoverable={getattr(err_rec, 'recoverable', None)}")

        # 3. State summary
        print(f"\nCurrent Workflow State: {conn.workflow_state}")
        print(f"Upload delivered count: {getattr(conn, '_upload_messages_delivered', 0)}")

        # 4. Conditional reset
        if reset_on_failure:
            print("\nResetting RIG state as requested (--reset-on-failure)...")
            _reset_connection(conn)
        else:
            print("\n[PRESERVED] No RESET_APPLICATION sent. RIG remains in current faulted state.")

        return False
    finally:
        with suppress(Exception):
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolate the prototype sweep failure at 100 Hz and 57% utilization"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Run against physical hardware instead of simulation",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Serial port for physical RIG hardware (e.g. COM10)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path(
            "HIL-RIG_loopback_tests/Prototype Sweep (development)/runs_100hz_57pct_isolation"
        ),
        help="Directory for live-run artifacts and captured data",
    )
    parser.add_argument(
        "--reset-on-failure",
        action="store_true",
        default=False,
        help="Send RESET_APPLICATION if a failure occurs (default: False to preserve state)",
    )
    args = parser.parse_args()

    if args.live:
        if not args.port:
            parser.error("--port is required when running with --live (e.g. --port COM10)")
        success = run_live_isolated(
            port=args.port,
            runs_dir=args.runs_dir,
            reset_on_failure=args.reset_on_failure,
        )
    else:
        from hilrig import LoopbackSweepCampaign, SweepStage
        config = LoopbackConfiguration.config_0_prototype()
        point = workload_point()
        executor = LoopbackSweepCampaign.create_simulated_executor()
        outcome = executor(config, point, SweepStage.SOAK_VALIDATION)
        success = outcome.passed

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
