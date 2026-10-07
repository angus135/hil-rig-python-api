"""Isolated UART2 Prototype Burstiness Sweep.

Executes ONLY Sweep 2 (Burstiness Characterization) across configured frequencies
for the UART2 loopback channel at 2,000,000 baud.

Usage:
  .venv\\Scripts\\python HIL-RIG_loopback_tests/uart_prototype_burst_sweep.py --live --port COM10
  .venv\\Scripts\\python HIL-RIG_loopback_tests/uart_prototype_burst_sweep.py --live --port COM10 --frequencies 10000
  .venv\\Scripts\\python HIL-RIG_loopback_tests/uart_prototype_burst_sweep.py --live --port COM10 --frequencies 100 1000 10000
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hilrig import (
    BurstinessSweep,
    CampaignReport,
    LoopbackConfiguration,
    LoopbackSweepCampaign,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="HIL-RIG UART2 Prototype Burstiness Sweep")
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
        "--baud",
        type=int,
        default=2_000_000,
        help="UART baud rate in Hz (default: 2,000,000)",
    )
    parser.add_argument(
        "--utilization",
        type=float,
        default=75.0,
        help="Target wire utilization during burst testing (default: 75.0%%)",
    )
    parser.add_argument(
        "--burst-intervals",
        type=int,
        nargs="+",
        default=[1, 2, 5, 10, 20, 50, 100],
        help="Burst interval sizes in ticks to evaluate (default: 1 2 5 10 20 50 100)",
    )
    parser.add_argument(
        "--frequencies",
        type=int,
        nargs="+",
        default=[100, 1_000, 10_000],
        help="Frequencies in Hz to evaluate (default: 100 1000 10000)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/runs_uart_burst"),
        help="Directory to save individual run artifacts and SQLite databases",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/uart_burst_report.md"),
        help="Path to save generated Markdown report",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/uart_burst_report.csv"),
        help="Path to save generated CSV report",
    )
    args = parser.parse_args()

    uart_config = LoopbackConfiguration.config_0_uart_only_prototype(baud_hz=args.baud)
    selected_frequencies = tuple(args.frequencies)
    burst_intervals = tuple(args.burst_intervals)

    print("=================================================================")
    print("HIL-RIG Characterization — Isolated UART2 Burstiness Sweep")
    print(f"Configuration: {uart_config.name} ({uart_config.description})")
    print(f"Baud rate: {args.baud:,} Hz | Max Wire Rate: {uart_config.theoretical_payload_rate_kib} KiB/s")
    print(f"Target Utilization: {args.utilization:.1f}%")
    print(f"Burst Intervals: {burst_intervals}")
    print(f"Frequencies: {selected_frequencies} Hz")
    print(f"Mode: {'Live Hardware' if args.live else 'Simulated / Dry-Run'}")
    if args.port:
        print(f"Port: {args.port}")
    if args.live:
        print(f"Run artifacts directory: {args.runs_dir.resolve()}")
    print("=================================================================\n")

    if args.live:
        executor = LoopbackSweepCampaign.create_live_executor(
            runs_directory=args.runs_dir,
            port=args.port,
        )
    else:
        executor = LoopbackSweepCampaign.create_simulated_executor()

    burst_results = []
    for freq in selected_frequencies:
        engine = BurstinessSweep(
            config=uart_config,
            frequency_hz=freq,
            target_utilization_percent=args.utilization,
            burst_intervals=burst_intervals,
        )
        res = engine.run(executor)
        burst_results.append(res)

    report = CampaignReport(
        sweep_1_results=(),
        sweep_2_results=tuple(burst_results),
    )

    md_content = report.to_markdown()
    print("\n" + md_content)

    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(md_content, encoding="utf-8")
        print(f"\nSaved Markdown report to: {args.output_md}")

    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        args.output_csv.write_text(report.to_csv(), encoding="utf-8")
        print(f"Saved CSV report to: {args.output_csv}")


if __name__ == "__main__":
    main()
