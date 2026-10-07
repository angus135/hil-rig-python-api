"""Automated loopback sweep campaign execution.

Demonstrates self-discovery, boundary refinement, repeated-seed confirmation,
and soak validation for:
  - Sweep 1: Utilization Ceiling Sweep across Configurations 1..4 and 3 frequencies
  - Sweep 2: Burstiness Sweep across candidate burst intervals at fixed utilization

Run standalone:
    python HIL-RIG_loopback_tests/loopback_sweep_campaign.py [--configs 1 4] [--frequencies 100 1000 10000]

Or run a single conservative test through the terminal:
    hil-rig run HIL-RIG_loopback_tests/loopback_sweep_campaign.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from hilrig import (
    CampaignReport,
    ExposureClass,
    LoopbackConfiguration,
    LoopbackSweepCampaign,
    LoopbackWorkloadPoint,
    Test,
    build_loopback_test,
    workload_point_for_exposure,
)


def build_test() -> Test:
    """Build default conservative test for terminal runner compatibility."""
    config = LoopbackConfiguration.config_1_automotive_gateway()
    point = workload_point_for_exposure(
        frequency_hz=100,
        target_utilization_percent=50.0,
        exposure=ExposureClass.DISCOVERY,
        burst_interval_ticks=1,
        seed=1,
    )
    return build_loopback_test(config, point)


def main() -> None:
    parser = argparse.ArgumentParser(description="HIL-RIG Automated Loopback Sweep Campaign")
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
        help="Serial port for physical RIG hardware (e.g. COM3 or /dev/ttyACM0)",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("HIL-RIG_loopback_tests/runs"),
        help="Directory to save individual run artifacts and SQLite databases",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=None,
        help="Path to save generated Markdown report",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Path to save generated CSV report",
    )
    parser.add_argument(
        "--configs",
        type=int,
        nargs="+",
        default=[1, 2, 3, 4],
        help="Configuration IDs to include (1..4)",
    )
    parser.add_argument(
        "--frequencies",
        type=int,
        nargs="+",
        default=[100, 1_000, 10_000],
        help="Frequencies in Hz (100, 1000, 10000)",
    )
    args = parser.parse_args()

    selected_configs = tuple(LoopbackConfiguration.from_id(cid) for cid in args.configs)
    selected_frequencies = tuple(args.frequencies)

    print("=================================================================")
    print("HIL-RIG Characterization — Automated Loopback Sweep Campaign")
    print(f"Configurations: {[cfg.name for cfg in selected_configs]}")
    print(f"Frequencies: {selected_frequencies} Hz")
    print(f"Mode: {'Live Hardware' if args.live else 'Simulated / Dry-Run'}")
    if args.port:
        print(f"Port: {args.port}")
    if args.live:
        print(f"Individual run artifacts directory: {args.runs_dir.resolve()}")
    print("=================================================================\n")

    campaign = LoopbackSweepCampaign(
        configurations=selected_configs,
        frequencies_hz=selected_frequencies,
        include_sweep_1=True,
        include_sweep_2=True,
        sweep_2_config_ids=(1, 4),
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
    print(md_content)

    if args.output_md:
        args.output_md.write_text(md_content, encoding="utf-8")
        print(f"\nSaved Markdown report to: {args.output_md}")

    if args.output_csv:
        args.output_csv.write_text(report.to_csv(), encoding="utf-8")
        print(f"Saved CSV report to: {args.output_csv}")


if __name__ == "__main__":
    main()
