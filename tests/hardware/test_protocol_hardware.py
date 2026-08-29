from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.hardware


def test_hardware_smoke(hardware_runner) -> None:
    runner, _ = hardware_runner
    runner.run_smoke()


def test_hardware_boundaries(hardware_runner) -> None:
    runner, _ = hardware_runner
    runner.run_boundaries()


def test_hardware_repeated_echo(hardware_runner) -> None:
    runner, _ = hardware_runner
    runner.run_repeat(100)


def test_hardware_reset_reconnect(hardware_runner) -> None:
    if os.getenv("HILRIG_TEST_MANUAL_RESET") != "1":
        pytest.skip("set HILRIG_TEST_MANUAL_RESET=1 for the manual reset test")
    runner, _ = hardware_runner
    runner.run_reset_reconnect(1, prompt=lambda message: input(f"{message}: "))


def test_hardware_soak(hardware_runner) -> None:
    if os.getenv("HILRIG_TEST_SOAK") != "1":
        pytest.skip("set HILRIG_TEST_SOAK=1 for long-running soak testing")
    runner, _ = hardware_runner
    duration = float(os.getenv("HILRIG_TEST_SOAK_SECONDS", "3600"))
    count = int(os.getenv("HILRIG_TEST_SOAK_COUNT", "10000"))
    runner.run_soak(duration_seconds=duration, count=count)
