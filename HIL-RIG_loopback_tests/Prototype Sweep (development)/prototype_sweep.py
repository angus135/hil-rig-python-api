r"""Dual-Peripheral Loopback Prototype Sweep (UART2 + SPI2).

Tests whole-rig characterization with an isolated dual-peripheral prototype
(UART2 TX -> UART2 RX @ 2.0 Mbps, SPI2 MOSI -> SPI2 MISO @ 5.625 Mbps).

Runs:
  1. Utilization Ceiling Sweep (Self-discovery -> Binary Refinement -> Confirmation -> Soak)
  2. Burstiness Sweep (Burst intervals [1, 2, 5, 10, 20, 50, 100])

Usage:
  .venv\Scripts\python "HIL-RIG_loopback_tests/Prototype Sweep (development)/prototype_sweep.py" `
    --live --port COM10 --frequencies 100 1000 10000
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hilrig import (
    CampaignReport,
    ExposureClass,
    LoopbackConfiguration,
    LoopbackSweepCampaign,
    Test,
    build_loopback_test,
    workload_point_for_exposure,
)


def build_test() -> Test:
    """Build single conservative test for interactive terminal runner compatibility."""
    config = LoopbackConfiguration.config_0_prototype()
    point = workload_point_for_exposure(
        frequency_hz=100,
        target_utilization_percent=50.0,
        exposure=ExposureClass.DISCOVERY,
        burst_interval_ticks=1,
        seed=1,
    )
    return build_loopback_test(config, point)


def main() -> None:
    parser = argparse.ArgumentParser(description="HIL-RIG Prototype Sweep (UART2 + SPI2)")
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
        "--uart-baud",
        type=int,
        default=2_000_000,
        help="UART baud rate in Hz (default: 2,000,000)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/Prototype Sweep (development)/runs_prototype"),
        help="Directory to save individual run artifacts and SQLite databases",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/Prototype Sweep (development)/prototype_report.md"),
        help="Path to save generated Markdown report",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/Prototype Sweep (development)/prototype_report.csv"),
        help="Path to save generated CSV report",
    )
    parser.add_argument(
        "--frequencies",
        type=int,
        nargs="+",
        default=[100, 1_000, 10_000],
        help="Frequencies in Hz (default: 100 1000 10000)",
    )
    args = parser.parse_args()

    config = LoopbackConfiguration.config_0_prototype(uart_baud_hz=args.uart_baud)
    selected_frequencies = tuple(args.frequencies)

    print("=================================================================")
    print("HIL-RIG Characterization — Prototype (UART2 + SPI2)")
    print(f"Configuration: {config.name} ({config.description})")
    print(
        f"Theoretical Max Payload: {config.theoretical_payload_rate_kib} KiB/s"
    )
    print(f"Frequencies: {selected_frequencies} Hz")
    print(f"Mode: {'Live Hardware' if args.live else 'Simulated / Dry-Run'}")
    if args.port:
        print(f"Port: {args.port}")
    if args.live:
        print(f"Run artifacts directory: {args.runs_dir.resolve()}")
    print("=================================================================\n")

    campaign = LoopbackSweepCampaign(
        configurations=(config,),
        frequencies_hz=selected_frequencies,
        include_sweep_1=True,
        include_sweep_2=True,
        sweep_2_config_ids=(0,),
    )

    if args.live:
        executor = LoopbackSweepCampaign.create_live_executor(
            runs_directory=args.runs_dir,
            port=args.port,
        )
    else:
        executor = LoopbackSweepCampaign.create_simulated_executor()

    report: CampaignReport = campaign.run(executor)

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
