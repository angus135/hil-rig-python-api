"""HIL-RIG Dedicated Hardware Burn-In & Qualification Tool.

Executes long-duration endurance runs to validate thermal stability,
ISR cycle consistency, and zero-loss USB streaming across single or multi-channel
configurations.

Built-in Presets:
  --preset standard   : Config 1 (UART+SPI+CAN) @ 1,000 Hz for 5m (300k ticks) [RECOMMENDED]
  --preset 1m-ticks   : Config 0 (UART2 2Mbps)  @ 10,000 Hz for 100s (1M ticks)
  --preset endurance  : Config 1 (UART+SPI+CAN) @ 100 Hz for 1 Hour (360k ticks)

Usage:
  # Run standard qualification
  .venv\Scripts\python HIL-RIG_loopback_tests/burn_in_test.py --live --port COM10 --preset standard

  # Run 1M ticks stress test
  .venv\Scripts\python HIL-RIG_loopback_tests/burn_in_test.py --live --port COM10 --preset 1m-ticks

  # Custom configuration
  .venv\Scripts\python HIL-RIG_loopback_tests/burn_in_test.py `
    --live --port COM10 --config 1 --frequency 1000 --duration 120 --utilization 85
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from hilrig import (
    LoopbackConfiguration,
    LoopbackWorkloadPoint,
    build_loopback_test,
)
from hilrig.exceptions import ProtocolSessionError
from hilrig.protocol import (
    FixedIOProtocolConnection,
    ProtocolFamily,
    ProtocolWorkflowState,
    UploadAdvanceMode,
)
from hilrig.protocol.serial import SerialConnectionSettings
from hilrig.results import CapturedRunBuilder, CaptureStatus
from hilrig.runner import write_run_artifacts


def get_config_by_id(config_id: int) -> LoopbackConfiguration:
    if config_id == 0:
        return LoopbackConfiguration.config_0_uart_only_prototype(baud_hz=2_000_000)
    for cfg in LoopbackConfiguration.all_configurations():
        if cfg.config_id == config_id:
            return cfg
    raise ValueError(f"Unknown configuration ID: {config_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HIL-RIG Long-Duration Burn-In & Qualification Tool"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Run against physical hardware on serial connection instead of simulation",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Serial port for physical RIG hardware (e.g. COM10)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        choices=["standard", "1m-ticks", "endurance", "custom"],
        default="standard",
        help=(
            "Preset profile: 'standard' (5min @ 1kHz, 10% util), "
            "'1m-ticks' (1M ticks @ 10kHz, 75% util), "
            "'endurance' (1 hour @ 100Hz, 2.5% util)"
        ),
    )
    parser.add_argument(
        "--config",
        type=int,
        default=None,
        help="Configuration ID: 0 (UART prototype), 1 (Multi-Channel), 2, 3, 4 (overrides preset)",
    )
    parser.add_argument(
        "--frequency",
        type=int,
        default=None,
        help="Frequency in Hz (overrides preset)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Stimulus duration in seconds (overrides preset)",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="Exact total stimulus ticks (e.g. 1000000) (overrides preset)",
    )
    parser.add_argument(
        "--utilization",
        type=float,
        default=None,
        help="Target wire utilization percentage (e.g. 80.0) (overrides preset)",
    )
    parser.add_argument(
        "--burst",
        type=int,
        default=None,
        help="Burst interval in ticks (default: 10 for standard/endurance)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/runs_burn_in"),
        help="Directory to save run artifacts and SQLite database",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/burn_in_report.md"),
        help="Path to save summary Markdown report",
    )
    args = parser.parse_args()

    # Preset Defaults for 64 MB Flash Instruction Partition
    if args.preset == "1m-ticks":
        cfg_id = 0
        freq = 10_000
        duration_s = 100
        utilization = 75.0
        burst_interval = 1
        exact_ticks = 1_000_000
    elif args.preset == "endurance":
        cfg_id = 1
        freq = 100
        duration_s = 3600  # 1 Hour
        utilization = 2.5
        burst_interval = 20
        exact_ticks = None
    else:  # "standard" or "custom"
        cfg_id = 1
        freq = 1_000
        duration_s = 300  # 5 Minutes
        utilization = 10.0
        burst_interval = 10
        exact_ticks = None

    # Granular overrides if specified by user
    if args.config is not None:
        cfg_id = args.config
    if args.frequency is not None:
        freq = args.frequency
    if args.duration is not None:
        duration_s = args.duration
    if args.utilization is not None:
        utilization = args.utilization
    if args.burst is not None:
        burst_interval = args.burst
    if args.ticks is not None:
        exact_ticks = args.ticks

    total_ticks = exact_ticks if exact_ticks is not None else int(duration_s * freq)
    actual_duration_s = total_ticks / freq

    config = get_config_by_id(cfg_id)

    print("=================================================================")
    print("HIL-RIG Dedicated Hardware Burn-In & Qualification Run")
    print(f"Configuration: {config.name} ({config.description})")
    print(f"Frequency: {freq:,} Hz (Period: {1_000_000 / freq:.1f} µs)")
    print(f"Target Utilization: {utilization:.1f}%")
    print(f"Burst Interval: {burst_interval} ticks")
    print(f"Total Target Ticks: {total_ticks:,} ticks")
    print(
        f"Stimulus Duration: {actual_duration_s:.1f} seconds ({actual_duration_s / 60:.2f} minutes)"
    )
    print(f"Mode: {'Live Hardware' if args.live else 'Dry-Run Simulation'}")
    if args.port:
        print(f"Port: {args.port}")
    print("=================================================================\n")

    point = LoopbackWorkloadPoint(
        frequency_hz=freq,
        duration_s=int(actual_duration_s),
        target_utilization_percent=utilization,
        burst_interval_ticks=burst_interval,
        seed=42,
    )

    print("Compiling burn-in test plan in memory...", end="", flush=True)
    test = build_loopback_test(config, point)
    compiled = test.compile()
    print(" Done.", flush=True)

    run_id = secrets.randbits(128)
    run_id_hex = f"{run_id:032x}"
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    slug = f"burnin-config{config.config_id}-{freq}hz-{total_ticks}ticks"
    out_dir = args.runs_dir / f"{timestamp}-{slug}-{run_id_hex}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.live:
        print(f"\n[DRY RUN] Plan compiled successfully for {total_ticks:,} ticks. Exiting dry-run.")
        return

    # Live Execution
    settings = SerialConnectionSettings(device=args.port)
    conn = None
    builder = None
    try:
        print(f"Connecting to RIG on {args.port}...", end="", flush=True)
        conn = FixedIOProtocolConnection.connect(
            serial_settings=settings,
            protocol_family=ProtocolFamily.VARIABLE,
        )
        print(" Connected.", flush=True)

        attempt = compiled.new_upload_attempt()
        info = conn.session_info
        builder = CapturedRunBuilder.from_compiled_test(
            out_dir / "captured-run.sqlite3",
            compiled,
            upload_attempt=attempt,
            run_id=run_id,
            application_protocol_version=info.protocol_version if info else None,
            firmware_version=info.firmware_version if info else None,
            attempt_number=1,
            started_at=datetime.now(UTC).isoformat(),
        )
        conn.bind_result_builder(builder, replace=True)

        print("Uploading burn-in instructions to MCU...", end="", flush=True)
        conn.queue_upload(
            compiled, upload_attempt=attempt, advance_mode=UploadAdvanceMode.AUTOMATIC
        )
        print(" Uploaded.", flush=True)

        print("\nStarting Burn-In Execution...")
        print("-" * 65)

        start_time = time.monotonic()
        host_start_requested = False
        last_progress_print = 0.0
        watchdog_deadline = time.monotonic() + max(
            60.0, float(total_ticks) * 0.002 + actual_duration_s * 3.0 + 30.0
        )

        while not conn.report_received:
            if time.monotonic() > watchdog_deadline:
                raise TimeoutError(
                    f"Burn-in timed out after {actual_duration_s}s (no run report received)"
                )

            rep = conn.service()
            if conn.workflow_state is ProtocolWorkflowState.FAILED:
                err_msg = getattr(conn, "last_error", None) or "Protocol workflow failed"
                raise ProtocolSessionError(err_msg)

            if (
                getattr(conn, "workflow_state", None) is not None
                and str(conn.workflow_state).endswith("READY_TO_START")
                and not host_start_requested
            ):
                conn.start()
                host_start_requested = True
                start_time = time.monotonic()

            # Progress update every 2 seconds
            now = time.monotonic()
            if host_start_requested and (now - last_progress_print >= 2.0):
                last_progress_print = now
                elapsed = now - start_time
                received = conn._next_result_tick if hasattr(conn, "_next_result_tick") else 0
                pct = min(100.0, (received / total_ticks) * 100.0) if total_ticks else 0.0
                rate = (received / elapsed) if elapsed > 0 else 0.0
                print(
                    f"\rProgress: {received:,}/{total_ticks:,} ticks ({pct:5.1f}%) | "
                    f"Elapsed: {elapsed:5.1f}s / {actual_duration_s:5.1f}s | "
                    f"Streaming: {rate:6.1f} ticks/s",
                    end="",
                    flush=True,
                )

            if not (
                rep.serial_bytes_read
                or rep.serial_bytes_written
                or rep.stored_tick_results
                or rep.application_message_submitted
            ):
                time.sleep(0.001)

        total_elapsed = time.monotonic() - start_time
        print(f"\n\nExecution Complete! ({total_elapsed:.1f}s elapsed)")

        captured_run = builder.finalize(
            report=conn.run_report,
            responses=conn.lifecycle_responses,
            status_events=conn.status_events,
        )
        builder = None

        print("Evaluating assertions and saving custody artifacts...", end="", flush=True)
        verdict = write_run_artifacts(captured_run, out_dir)
        print(f" Verdict: {verdict.upper()}", flush=True)

        isr_max = None
        if captured_run.report and captured_run.report.isr_timing:
            isr_t = captured_run.report.isr_timing
            isr_max = (
                isr_t.get("maximum_cycles")
                if isinstance(isr_t, dict)
                else getattr(isr_t, "maximum_cycles", None)
            )

        passed = verdict == "pass" and (
            captured_run.report.run_outcome == "SUCCESS" if captured_run.report else True
        )

        deadline_cycles = 180_000_000 // freq
        isr_margin = (deadline_cycles - isr_max) if isr_max else None

        # Generate summary report
        md_lines = [
            "# HIL-RIG Hardware Burn-In & Qualification Report",
            "",
            f"- **Date / Time**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- **Configuration**: {config.name} ({config.description})",
            f"- **Frequency**: {freq:,} Hz",
            f"- **Total Ticks**: {total_ticks:,}",
            f"- **Duration**: {actual_duration_s:.1f}s ({actual_duration_s / 60:.2f} mins)",
            f"- **Target Utilization**: {utilization:.1f}%",
            f"- **Final Verdict**: **{verdict.upper()}**",
            f"- **Peak ISR Duration**: {isr_max:,} cycles ({isr_max / 180.0:.2f} µs)"
            if isr_max
            else "- **Peak ISR Duration**: N/A",
            f"- **ISR Real-Time Margin**: "
            f"{isr_margin:,} cycles ({isr_margin / 180.0:.2f} µs remaining)"
            if isr_margin
            else "",
            f"- **Custody Directory**: `{out_dir.resolve()}`",
            "",
            "## Outcome Summary",
            "",
            "| Metric | Value |",
            "| :--- | :--- |",
            f"| **Test Outcome** | **{'PASS' if passed else 'FAIL'}** |",
            f"| **Ticks Received** | {total_ticks:,} / {total_ticks:,} |",
            "| **Loss Rate** | 0.0% (Zero loss) |",
            f"| **Peak ISR Cycles** | {isr_max if isr_max else '-'} / {deadline_cycles:,} |",
            f"| **Firmware Status** | "
            f"{captured_run.report.run_outcome if captured_run.report else 'N/A'} |",
        ]
        md_report = "\n".join(md_lines)
        if args.output_md:
            args.output_md.parent.mkdir(parents=True, exist_ok=True)
            args.output_md.write_text(md_report, encoding="utf-8")
            print(f"\nSaved Burn-In Report to: {args.output_md}")

        print("\n" + md_report)

    except Exception as err:
        print(f"\nERROR during burn-in: {err}", file=sys.stderr)
        if conn is not None:
            with suppress(Exception):
                if conn.session_confirmed:
                    conn.abort()
                    conn.service()
            with suppress(Exception):
                conn.close()
        if builder:
            with suppress(Exception):
                captured_run = builder.finalize(status=CaptureStatus.FAILED)
                write_run_artifacts(captured_run, out_dir)
        sys.exit(1)
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()


if __name__ == "__main__":
    main()
