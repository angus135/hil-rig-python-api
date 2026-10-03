"""PWM Loopback Test sweeping frequency and duty cycle over time.

Designed for the HIL-RIG board running at 1 kHz tick rate (1 ms tick duration).
Exercises PWM Generator LV (Channel 0) looped back into PWM Capture CH1 (Channel 0).

Required Hardware Wiring:
-------------------------
* Connect PWM OUTPUT Channel 0 (LV / 3.3V) -> PWM INPUT Channel 0 (PWM IN 1 / Capture CH1)
  (Ensure 3.3V logic level)

Usage:
------
* In the HIL-RIG terminal:
    HIL-RIG> run "examples\\pwm_loopback_test.py"
* Or execute directly:
    python examples/pwm_loopback_test.py
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hilrig import (
    FrequencyMode,
    LogicVoltage,
    StartMode,
    Test,
)


@dataclass(frozen=True)
class PwmSweepStep:
    """Definition of one step in the PWM frequency and duty cycle sweep."""

    frequency_hz: int
    duty_cycle: float
    duration_ms: int = 300
    settle_ms: int = 50
    freq_tolerance_ratio: float = 0
    duty_tolerance_abs: float = 0


# A short and clean sweep covering frequencies from 1 kHz to 20 kHz and duty cycles from 20% to 80%
DEFAULT_SWEEP_STEPS: tuple[PwmSweepStep, ...] = (
    PwmSweepStep(frequency_hz=1_000, duty_cycle=0.50, duration_ms=300),
    PwmSweepStep(frequency_hz=2_500, duty_cycle=0.25, duration_ms=300),
    PwmSweepStep(frequency_hz=5_000, duty_cycle=0.75, duration_ms=300),
    PwmSweepStep(frequency_hz=10_000, duty_cycle=0.20, duration_ms=300),
    PwmSweepStep(frequency_hz=20_000, duty_cycle=0.80, duration_ms=300),
)


def build_test_for_steps(steps: Sequence[PwmSweepStep] = DEFAULT_SWEEP_STEPS) -> Test:
    """Build a PWM loopback test sweeping through the specified steps."""
    if not steps:
        raise ValueError("At least one sweep step must be provided.")

    test = Test(name="PWM LV -> Capture CH1 Loopback Sweep")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    # Configure only PWM Capture CH1 (Input Channel 0) and PWM Gen LV (Output Channel 0)
    pwm_cap = (
        test.pwm_input(channel=0)
        .named("PWM_CAP_CH1")
        .configure(
            voltage=LogicVoltage.V3_3,
        )
    )

    initial_step = steps[0]
    pwm_gen = (
        test.pwm_output(channel=0)
        .named("PWM_GEN_LV")
        .configure(
            voltage=LogicVoltage.V3_3,
            initial_frequency_hz=initial_step.frequency_hz,
            initial_duty_cycle=initial_step.duty_cycle,
            initially_enabled=True,
        )
    )

    current_ms = 0
    for idx, step in enumerate(steps):
        # For subsequent steps, schedule the frequency and duty update
        if idx > 0:
            pwm_gen.set(
                frequency_hz=step.frequency_hz,
                duty_cycle=step.duty_cycle,
                at_ms=current_ms,
            )

        # Allow settle time after transition before expecting steady-state measurement
        step_end_ms = current_ms + step.duration_ms
        assert_from_ms = current_ms + step.settle_ms
        assert_until_ms = step_end_ms - 10

        min_freq = max(0.0, step.frequency_hz * (1.0 - step.freq_tolerance_ratio))
        max_freq = step.frequency_hz * (1.0 + step.freq_tolerance_ratio)
        min_duty = max(0.0, round(step.duty_cycle - step.duty_tolerance_abs, 4))
        max_duty = min(1.0, round(step.duty_cycle + step.duty_tolerance_abs, 4))

        group_label = (
            f"Step {idx + 1}: {step.frequency_hz} Hz @ {int(round(step.duty_cycle * 100))}% duty"
        )
        with test.group(group_label):
            test.expect(pwm_cap).frequency_remain_within(
                minimum_hz=min_freq,
                maximum_hz=max_freq,
                from_ms=assert_from_ms,
                until_ms=assert_until_ms,
            )
            test.expect(pwm_cap).duty_cycle_remain_within(
                minimum_duty_cycle=min_duty,
                maximum_duty_cycle=max_duty,
                from_ms=assert_from_ms,
                until_ms=assert_until_ms,
            )

        current_ms = step_end_ms

    return test


def build_test() -> Test:
    """Build the test using the default sweep profile for the HIL-RIG terminal."""
    return build_test_for_steps(DEFAULT_SWEEP_STEPS)


if __name__ == "__main__":
    test = build_test()
    compiled = test.compile()
    total_ms = sum(s.duration_ms for s in DEFAULT_SWEEP_STEPS)
    print(f"Test Name: {test.name}")
    print(f"Sampling Mode: {compiled.frequency_mode}")
    print(f"Total Duration: {total_ms} ms ({total_ms} ticks)")
    print(f"Sweep Steps: {len(DEFAULT_SWEEP_STEPS)}")
    print(f"Instructions: {len(compiled.instructions)}")
    print(f"Assertions: {len(compiled.assertions)}")
    print("\nSweep Steps Plan:")
    t = 0
    for i, s in enumerate(DEFAULT_SWEEP_STEPS, 1):
        duty_pct = s.duty_cycle * 100
        print(
            f"  Step {i}: t={t:4d}ms..{t + s.duration_ms:4d}ms -> "
            f"{s.frequency_hz:5d} Hz, {duty_pct:4.1f}% duty"
        )
        t += s.duration_ms
    print("\nCompilation successful.")
