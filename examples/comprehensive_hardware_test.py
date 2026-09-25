"""Comprehensive hardware stress test exercising fast Digital I/O (3 channels),
extreme PWM boundary conditions (50 Hz to 1 MHz, 1% to 99% duty), and Analogue Inputs.

Configured for 10 kHz tick frequency over 5.0 seconds (50,000 total ticks).

Hardware Wiring Guide:
----------------------
1. Digital Loopbacks:
   - Connect DIGITAL OUTPUT Channel 0 (DOUT 0)  ->  DIGITAL INPUT Channel 0 (DIN 0)
   - Connect DIGITAL OUTPUT Channel 1 (DOUT 1)  ->  DIGITAL INPUT Channel 1 (DIN 1)
   - Connect DIGITAL OUTPUT Channel 2 (DOUT 2)  ->  DIGITAL INPUT Channel 2 (DIN 2)
   (3.3V logic level)

2. PWM Loopback:
   - Connect PWM OUTPUT Channel 0 (PWM OUT 0)   ->  PWM INPUT Channel 0 (PWM IN 0)
   (3.3V logic level)

3. Analogue Inputs:
   - Connect ANALOGUE INPUT Channel 0 (AIN 0)   ->  3.3V rail (or test DC voltage 0-3.3V)
   - Connect ANALOGUE INPUT Channel 1 (AIN 1)   ->  GND (0V) (or test DC voltage 0-3.3V)
"""

from random import Random
from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    Test,
)


def build_test() -> Test:
    """Construct and return the extreme-boundary multi-peripheral hardware test."""
    test = Test(name="10kHz 5s Extreme-Boundary Stress Test (3xDI/DO, Extreme PWM 1%-99% @ 1MHz, 2xAI)")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    # =========================================================================
    # 1. Digital Channels 0, 1, 2 Configuration & Stimulus Patterns
    # =========================================================================
    digital_inputs = [
        test.digital_input(channel=ch).configure(voltage=LogicVoltage.V3_3)
        for ch in (0, 1, 2)
    ]

    # --- Channel 0: 50 Hz Periodic Square Wave ---
    dout0 = test.digital_output(channel=0).configure(
        voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW
    )
    for cycle in range(20):
        t_high = cycle * 2000 + 1000
        t_low = cycle * 2000 + 2000
        if t_high < 40000:
            dout0.high(at_tick=t_high)
            test.expect(digital_inputs[0]).remain_high(
                from_tick=t_high + 50, until_tick=t_low - 50
            )
        if t_low < 40000:
            dout0.low(at_tick=t_low)
            test.expect(digital_inputs[0]).remain_low(
                from_tick=t_low + 50, until_tick=t_low + 950
            )
    test.expect(digital_inputs[0]).remain_low(from_tick=0, until_tick=950)

    # --- Channel 1: Multi-rate Pulse Bursts (starting with HIGH initial state) ---
    dout1 = test.digital_output(channel=1).configure(
        voltage=LogicVoltage.V3_3, initial_state=DigitalState.HIGH
    )
    pulse_widths_ms = [25, 50, 75, 100, 50, 25, 150, 50, 100, 75, 50, 25]
    curr_tick = 0
    curr_state = DigitalState.HIGH
    for pw in pulse_widths_ms:
        if curr_tick >= 38000:
            break
        duration_ticks = pw * 10
        next_tick = curr_tick + duration_ticks
        if curr_state == DigitalState.HIGH:
            test.expect(digital_inputs[1]).remain_high(
                from_tick=curr_tick + 20, until_tick=next_tick - 20
            )
            dout1.low(at_tick=next_tick)
            curr_state = DigitalState.LOW
        else:
            test.expect(digital_inputs[1]).remain_low(
                from_tick=curr_tick + 20, until_tick=next_tick - 20
            )
            dout1.high(at_tick=next_tick)
            curr_state = DigitalState.HIGH
        curr_tick = next_tick

    # --- Channel 2: PRBS Pseudo-Random Bitstream ---
    dout2 = test.digital_output(channel=2).configure(
        voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW
    )
    rng = Random(0xCAFE_BABE)
    prbs_tick = 500
    prbs_state = DigitalState.LOW
    while prbs_tick < 39000:
        step_ticks = rng.choice([200, 300, 500, 800, 1000])
        next_state = DigitalState.HIGH if prbs_state == DigitalState.LOW else DigitalState.LOW
        if next_state == DigitalState.HIGH:
            dout2.high(at_tick=prbs_tick)
            test.expect(digital_inputs[2]).remain_high(
                from_tick=prbs_tick + 30, until_tick=prbs_tick + step_ticks - 30
            )
        else:
            dout2.low(at_tick=prbs_tick)
            test.expect(digital_inputs[2]).remain_low(
                from_tick=prbs_tick + 30, until_tick=prbs_tick + step_ticks - 30
            )
        prbs_state = next_state
        prbs_tick += step_ticks

    # =========================================================================
    # 2. Extreme PWM Boundary Sweep (50 Hz to 1 MHz, 1% to 99% Duty)
    # =========================================================================
    # 8 extreme stages across 4.0 seconds (each stage = 500 ms / 5,000 ticks)
    pwm_stages = [
        # (freq_hz, duty, start_s, end_s, freq_tolerance_hz, duty_tolerance)
        (50,        0.01, 0.0, 0.5, 2,      0.005),  # 50 Hz @ 1% duty (200 us pulse every 20 ms)
        (50,        0.99, 0.5, 1.0, 2,      0.005),  # 50 Hz @ 99% duty (200 us notch every 20 ms)
        (10_000,    0.02, 1.0, 1.5, 300,    0.008),  # 10 kHz @ 2% duty (2 us pulse every 100 us)
        (100_000,   0.02, 1.5, 2.0, 3_000,  0.010),  # 100 kHz @ 2% duty (200 ns pulse every 10 us)
        (100_000,   0.98, 2.0, 2.5, 3_000,  0.010),  # 100 kHz @ 98% duty (200 ns notch every 10 us)
        (500_000,   0.05, 2.5, 3.0, 20_000, 0.020),  # 500 kHz @ 5% duty (100 ns pulse every 2 us)
        (1_000_000, 0.05, 3.0, 3.5, 50_000, 0.030),  # 1 MHz @ 5% duty (50 ns pulse every 1 us) - EXTREME
        (1_000_000, 0.95, 3.5, 4.0, 50_000, 0.030),  # 1 MHz @ 95% duty (50 ns notch every 1 us) - EXTREME
    ]

    initial_stage = pwm_stages[0]
    pwm_out0 = test.pwm_output(channel=0).configure(
        voltage=LogicVoltage.V3_3,
        initial_frequency_hz=initial_stage[0],
        initial_duty_cycle=initial_stage[1],
        initially_enabled=True,
    )
    pwm_in0 = test.pwm_input(channel=0).configure(voltage=LogicVoltage.V3_3)

    # Schedule PWM shifts at stage boundaries
    for stage in pwm_stages[1:]:
        freq_hz, duty, start_s, _, _, _ = stage
        pwm_out0.set(frequency_hz=freq_hz, duty_cycle=duty, at_s=start_s)

    # Schedule assertions with 150ms settling allowance per stage
    for freq_hz, duty, start_s, end_s, f_tol, d_tol in pwm_stages:
        eval_start_s = round(start_s + 0.15, 2)
        eval_end_s = round(end_s - 0.05, 2)
        test.expect(pwm_in0).frequency_remain_within(
            minimum_hz=freq_hz - f_tol,
            maximum_hz=freq_hz + f_tol,
            from_s=eval_start_s,
            until_s=eval_end_s,
        )
        test.expect(pwm_in0).duty_cycle_remain_within(
            minimum_duty_cycle=duty - d_tol,
            maximum_duty_cycle=duty + d_tol,
            from_s=eval_start_s,
            until_s=eval_end_s,
        )

    # =========================================================================
    # 3. Analogue Inputs 0 & 1 (Continuous 10 kHz ADC Sampling)
    # =========================================================================
    ain0 = test.analogue_input(channel=0).configure()
    ain1 = test.analogue_input(channel=1).configure()

    test.expect(ain0).remain_within(minimum_v=0.0, maximum_v=3.6, from_s=0.0, until_s=4.0)
    test.expect(ain1).remain_within(minimum_v=0.0, maximum_v=3.6, from_s=0.0, until_s=4.0)

    return test


if __name__ == "__main__":
    test_instance = build_test()
    compiled = test_instance.compile()
    print("Test compiled successfully!")
    print(f"Test ID:             0x{compiled.test_id:032x}")
    print(f"Frequency:           {compiled.frequency_hz} Hz ({compiled.frequency_mode})")
    print(f"Expected Tick Count: {compiled.expected_tick_count} ticks ({compiled.expected_tick_count / compiled.frequency_hz:.2f} seconds)")
    print(f"Instruction Count:   {len(compiled.instructions)}")
    print(f"Assertion Count:     {len(compiled.assertions)}")
