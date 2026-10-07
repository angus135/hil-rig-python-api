"""Unit tests for loopback configurations, multi-channel workloads, and automated sweep engines."""

from __future__ import annotations

import pytest

from hilrig import (
    BurstinessSweep,
    ExposureClass,
    LoopbackConfiguration,
    LoopbackSweepCampaign,
    LoopbackWorkloadPoint,
    SweepStage,
    UtilizationCeilingSweep,
    build_loopback_test,
    check_workload_admissibility,
    compile_configuration_workload,
    get_exposure_duration_s,
    get_exposure_ticks,
    workload_point_for_exposure,
)


def test_loopback_configuration_presets_match_test_matrix_spec() -> None:
    configs = LoopbackConfiguration.all_configurations()
    assert len(configs) == 4

    c1 = LoopbackConfiguration.config_1_automotive_gateway()
    assert c1.config_id == 1
    assert c1.theoretical_payload_rate_kib == 413.0
    spi_channels_1 = [ch for ch in c1.channels if ch.peripheral == "spi"]
    uart_channels_1 = [ch for ch in c1.channels if ch.peripheral == "uart"]
    can_channels_1 = [ch for ch in c1.channels if ch.peripheral == "can"]
    assert len(spi_channels_1) == 1 and spi_channels_1[0].bit_rate_hz == 2_813_000
    assert len(uart_channels_1) == 1 and uart_channels_1[0].bit_rate_hz == 115_200
    assert len(can_channels_1) == 2 and all(ch.bit_rate_hz == 1_000_000 for ch in can_channels_1)

    c2 = LoopbackConfiguration.config_2_high_speed_sensor()
    assert c2.config_id == 2
    assert c2.theoretical_payload_rate_kib == 784.0
    assert len([ch for ch in c2.channels if ch.peripheral == "can"]) == 0
    assert len([ch for ch in c2.channels if ch.peripheral == "spi"]) == 1
    assert len([ch for ch in c2.channels if ch.peripheral == "uart"]) == 1

    c3 = LoopbackConfiguration.config_3_balanced_single_ch()
    assert c3.config_id == 3
    assert c3.theoretical_payload_rate_kib == 425.0
    assert [ch.bit_rate_hz for ch in c3.channels if ch.peripheral == "spi"] == [1_406_000]

    c4 = LoopbackConfiguration.config_4_full_saturation()
    assert c4.config_id == 4
    assert c4.theoretical_payload_rate_kib == 1879.0
    assert len([ch for ch in c4.channels if ch.peripheral == "spi"]) == 2
    assert len([ch for ch in c4.channels if ch.peripheral == "uart"]) == 2
    assert len([ch for ch in c4.channels if ch.peripheral == "can"]) == 2


def test_loopback_configuration_lookup_by_id() -> None:
    for cid in (0, 1, 2, 3, 4):
        cfg = LoopbackConfiguration.from_id(cid)
        assert cfg.config_id == cid

    with pytest.raises(ValueError, match="Unknown configuration id"):
        LoopbackConfiguration.from_id(-1)
    with pytest.raises(ValueError, match="Unknown configuration id"):
        LoopbackConfiguration.from_id(5)


def test_exposure_classes_and_tick_conversions() -> None:
    assert get_exposure_duration_s(ExposureClass.DISCOVERY) == 2
    assert get_exposure_duration_s(ExposureClass.CONFIRMATION) == 10
    assert get_exposure_duration_s(ExposureClass.SOAK_60S) == 60
    assert get_exposure_duration_s(ExposureClass.SOAK_5MIN) == 300

    # Minimum tick floor enforced at 100 Hz (1,000 ticks discovery floor)
    assert get_exposure_ticks(100, ExposureClass.DISCOVERY) == 1_000
    assert get_exposure_ticks(1_000, ExposureClass.DISCOVERY) == 2_000
    assert get_exposure_ticks(10_000, ExposureClass.DISCOVERY) == 20_000

    # Minimum tick floor enforced at 100 Hz (3,000 ticks confirmation floor)
    assert get_exposure_ticks(100, ExposureClass.CONFIRMATION) == 3_000
    assert get_exposure_ticks(1_000, ExposureClass.CONFIRMATION) == 10_000
    assert get_exposure_ticks(10_000, ExposureClass.CONFIRMATION) == 100_000

    # Minimum tick floor enforced at 100 Hz (6,000 ticks soak floor -> 60s)
    assert get_exposure_ticks(100, ExposureClass.SOAK_60S) == 6_000
    assert get_exposure_ticks(1_000, ExposureClass.SOAK_60S) == 60_000
    assert get_exposure_ticks(10_000, ExposureClass.SOAK_60S) == 600_000

    assert get_exposure_ticks(100, ExposureClass.SOAK_5MIN) == 30_000
    assert get_exposure_ticks(1_000, ExposureClass.SOAK_5MIN) == 300_000
    assert get_exposure_ticks(10_000, ExposureClass.SOAK_5MIN) == 900_000


def test_build_and_compile_loopback_test_for_all_configurations() -> None:
    for cid in (1, 2, 3, 4):
        cfg = LoopbackConfiguration.from_id(cid)
        point = workload_point_for_exposure(
            frequency_hz=1000,
            target_utilization_percent=25.0,
            exposure=ExposureClass.DISCOVERY,
            burst_interval_ticks=1,
            seed=42,
        )
        test = build_loopback_test(cfg, point)
        compiled = test.compile()
        assert compiled.expected_tick_count >= 2000
        assert len(compiled.instructions) > 0


def test_workload_admissibility_checks() -> None:
    cfg4 = LoopbackConfiguration.config_4_full_saturation()
    # Admissible point
    admissible_pt = workload_point_for_exposure(
        frequency_hz=1000,
        target_utilization_percent=50.0,
        burst_interval_ticks=1,
    )
    is_ok, reason = check_workload_admissibility(cfg4, admissible_pt)
    assert is_ok is True
    assert reason is None

    # Inadmissible extreme burst point (exceeding UART buffer)
    extreme_burst_pt = LoopbackWorkloadPoint(
        frequency_hz=100,
        duration_s=2,
        target_utilization_percent=100.0,
        burst_interval_ticks=200,
        seed=1,
    )
    is_ok2, reason2 = check_workload_admissibility(
        cfg4, extreme_burst_pt, uart_tx_buffer=100, spi_tx_buffer=100
    )
    assert is_ok2 is False
    assert reason2 is not None


def test_utilization_ceiling_sweep_self_discovery_and_confirmation() -> None:
    cfg1 = LoopbackConfiguration.config_1_automotive_gateway()
    sweep = UtilizationCeilingSweep(
        config=cfg1,
        frequency_hz=1000,
        coarse_points=(10.0, 50.0, 80.0, 90.0, 100.0),
        coarse_seeds=(1, 2),
        confirmation_seeds=(10, 20),
        confirmation_repeats=2,
        resolution_percent=2.0,
    )

    # Simulated ceiling at 85%
    def _simulated_runner(
        config: LoopbackConfiguration,
        point: LoopbackWorkloadPoint,
        stage: SweepStage,
    ) -> bool:
        return point.target_utilization_percent <= 85.0

    result = sweep.run(_simulated_runner)
    assert result.confirmed_stable_ceiling_percent is not None
    assert 80.0 <= result.confirmed_stable_ceiling_percent <= 85.0
    assert result.soak_passed is True
    assert result.total_runs > 0
    assert result.total_exposure_ticks > 0


def test_burstiness_sweep_discovery_and_confirmation() -> None:
    cfg1 = LoopbackConfiguration.config_1_automotive_gateway()
    sweep = BurstinessSweep(
        config=cfg1,
        frequency_hz=1000,
        target_utilization_percent=70.0,
        burst_intervals=(1, 2, 5, 10, 20),
        discovery_seeds=(1, 2),
        confirmation_seeds=(10, 20),
        confirmation_repeats=2,
    )

    # Simulated burst limit at 10 ticks
    def _simulated_runner(
        config: LoopbackConfiguration,
        point: LoopbackWorkloadPoint,
        stage: SweepStage,
    ) -> bool:
        return point.burst_interval_ticks <= 10

    result = sweep.run(_simulated_runner)
    assert result.confirmed_stable_burst_interval == 10
    assert result.soak_passed is True
    assert result.total_runs > 0


def test_loopback_sweep_campaign_orchestration_and_reporting() -> None:
    # Run a campaign with small subsets to verify full integration
    campaign = LoopbackSweepCampaign(
        configurations=(
            LoopbackConfiguration.config_1_automotive_gateway(),
            LoopbackConfiguration.config_2_high_speed_sensor(),
        ),
        frequencies_hz=(100, 1_000),
        include_sweep_1=True,
        include_sweep_2=True,
        sweep_2_config_ids=(1,),
    )

    executor = LoopbackSweepCampaign.create_simulated_executor(
        simulated_ceilings={(1, 100): 90.0, (1, 1000): 95.0, (2, 100): 50.0, (2, 1000): 90.0},
        simulated_burst_limits={(1, 100): 1, (1, 1000): 10},
    )

    report = campaign.run(executor)
    assert len(report.sweep_1_results) == 4  # 2 configs x 2 frequencies
    assert len(report.sweep_2_results) == 2  # 1 config x 2 frequencies

    doc = report.to_dict()
    assert "sweep_1_utilization_ceiling" in doc
    assert "sweep_2_burstiness" in doc

    md = report.to_markdown()
    assert "# HIL-RIG Loopback Characterization — Automated Sweep Campaign Report" in md
    assert "Sweep 1 — Utilization Ceiling Boundaries" in md
    assert "Sweep 2 — Burstiness Limits" in md
    assert "Cell-by-Cell Boundary Justification & Failure Attribution" in md
    assert "Verified Operating Point Resource Headroom & Diagnostics" in md
    assert "1 — Automotive Gateway" in md

    csv_text = report.to_csv()
    assert "Sweep 1 (Utilization)" in csv_text
    assert "Sweep 2 (Burstiness)" in csv_text
    assert "verified_boundary" in csv_text
    assert "limiting_domain" in csv_text


def test_stream_timing_analysis_bounded_vs_divergent() -> None:
    from types import SimpleNamespace

    from hilrig import (
        CommunicationPeripheral,
        analyze_stream_timing,
    )

    cfg = LoopbackConfiguration.config_2_high_speed_sensor()
    point = workload_point_for_exposure(
        frequency_hz=1000,
        target_utilization_percent=50.0,
        burst_interval_ticks=1,
    )
    compiled = compile_configuration_workload(cfg, point)

    # 1. Bounded timing with constant 1-tick phase shift
    bounded_caps = []
    for tr in compiled.channel_workloads[0].transfers:
        bounded_caps.append(
            SimpleNamespace(
                peripheral=CommunicationPeripheral.SPI,
                channel=0,
                tick=tr.tick + 1,  # 1-tick phase shift
                payload=b"\x00" * tr.payload_bytes,
            )
        )

    metrics_bounded = analyze_stream_timing(bounded_caps, compiled, max_allowed_phase_shift_ticks=2)
    spi_metric = next(m for m in metrics_bounded if m.peripheral == "spi")
    assert spi_metric.is_bounded is True
    assert spi_metric.is_creeping_divergent is False
    assert spi_metric.max_excess_phase_shift_ticks is not None

    # 2. Divergent timing where delay creeps up over time
    divergent_caps = []
    for idx, tr in enumerate(compiled.channel_workloads[0].transfers):
        divergent_caps.append(
            SimpleNamespace(
                peripheral=CommunicationPeripheral.SPI,
                channel=0,
                tick=tr.tick + 1 + (idx // 100),  # Accumulating creep
                payload=b"\x00" * tr.payload_bytes,
            )
        )

    metrics_divergent = analyze_stream_timing(
        divergent_caps, compiled, max_allowed_phase_shift_ticks=2
    )
    spi_div = next(m for m in metrics_divergent if m.peripheral == "spi")
    assert spi_div.is_bounded is False
    assert spi_div.is_creeping_divergent is True


def test_extract_run_diagnostics_and_extended_report() -> None:
    from types import SimpleNamespace
    from hilrig import extract_run_diagnostics

    # Construct synthetic run report with full extension metrics
    synthetic_report = SimpleNamespace(
        isr_timing={"maximum_cycles": 120_000},
        instruction_buffer={"minimum_unread_bytes": 4096},
        result_buffer={"peak_pending_bytes": 512, "reservation_failures": 0},
        flash={
            "instruction_refill_count": 2,
            "result_drain_count": 5,
            "max_service_time_cycles": 800,
            "contention_count": 0,
        },
        extension_data=None,
    )
    captured = SimpleNamespace(report=synthetic_report)

    diag = extract_run_diagnostics(captured, frequency_hz=1000)
    assert "isr" in diag
    assert diag["isr"]["max_cycles"] == 120_000
    assert diag["isr"]["deadline_cycles"] == 180_000
    assert diag["isr"]["margin_percent"] == 33.3
    assert diag["instruction_buffer"]["min_unread_bytes"] == 4096
    assert diag["instruction_buffer"]["headroom_percent"] == 50.0
    assert diag["result_buffer"]["peak_pending_bytes"] == 512
    assert diag["flash"]["refill_count"] == 2

