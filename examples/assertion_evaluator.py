"""Run the assertion evaluator end to end without connected HIL-RIG hardware.

This example fabricates the typed ``TickResult`` values that the unfinished
application-message adapter will eventually produce. It deliberately creates passing,
failing, and inconclusive assertions so each report verdict is visible.
"""

import sys
from pathlib import Path
from secrets import randbits

from hilrig import (
    CapturedRunBuilder,
    FrequencyMode,
    LogicVoltage,
    PWMMeasurement,
    StartMode,
    Test,
    TickResult,
    evaluate_assertions,
)


def main() -> None:
    # Keep the evaluator's µ and ± symbols readable in Windows terminals.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    test = Test(name="Synthetic assertion evaluator example")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    digital_input = test.digital_input(channel=0)
    digital_input.configure(voltage=LogicVoltage.V3_3)
    analogue_input = test.analogue_input(channel=0).configure()
    pwm_input = test.pwm_input(channel=0)
    pwm_input.configure(voltage=LogicVoltage.V3_3)

    # These assertions pass against the synthetic measurements created below.
    test.expect(digital_input).low(at_tick=0)
    test.expect(digital_input).to_transition(
        from_state=False,
        to_state=True,
        between_ticks=(2, 4),
    )
    test.expect(digital_input).remain_high(from_tick=3, until_tick=6)
    test.expect(analogue_input).near(target_v=5, tolerance_v=0.01, at_tick=4)
    test.expect(pwm_input).waveform_near(
        frequency_hz=50_000,
        frequency_tolerance_hz=100,
        duty_cycle=0.5,
        duty_cycle_tolerance=0.01,
        at_tick=4,
    )

    # This assertion fails because the generated analogue input is 5 V.
    test.expect(analogue_input).remain_below(
        threshold_v=4.8,
        from_tick=0,
        until_tick=6,
    )

    # Tick 7 is stored as an execution problem, so this assertion is inconclusive.
    test.expect(digital_input).high(at_tick=7)

    compiled = test.compile()

    # Every execution gets its own folder, so rerunning the example never overwrites a
    # previous capture or report.
    run_id = randbits(128)
    output_directory = (
        Path(__file__).resolve().parent / "build" / f"assertion-evaluator-{run_id:032x}"
    )
    output_directory.mkdir(parents=True)

    builder = CapturedRunBuilder.from_compiled_test(
        output_directory / "captured-run.sqlite3",
        compiled,
        run_id=run_id,
    )

    # This loop stands in for the future application-message adapter. The compiled
    # expected tick count includes the final assertion tick plus one settling second.
    for tick in range(compiled.expected_tick_count):
        if tick == 7:
            builder.add_tick_result(TickResult.execution_problem(tick=tick, problem_detail=1))
            continue

        builder.add_tick_result(
            TickResult(
                tick=tick,
                digital_inputs=(tick >= 3,) + (False,) * 9,
                analogue_inputs_uv=(5_000_000, 0),
                pwm_inputs=(
                    PWMMeasurement(period_ns=20_000, duty_permyriad=5_000),
                    PWMMeasurement(period_ns=0, duty_permyriad=0),
                ),
            )
        )

    captured_run = builder.finalize()
    report = evaluate_assertions(captured_run)
    json_path = report.write_json(output_directory / "evaluation-report.json")
    markdown_path = report.write_markdown(output_directory / "evaluation-report.md")

    print(f"Capture status: {report.capture_status.value.upper()}")
    print(f"Overall verdict: {report.verdict.value.upper()}")
    print(
        f"Assertions: {report.passed_count} passed, {report.failed_count} failed, "
        f"{report.inconclusive_count} inconclusive"
    )
    print()
    for result in report.assertion_results:
        print(
            f"[{result.verdict.value.upper():12}] assertion {result.assertion_id}: {result.message}"
        )
    print()
    print(f"SQLite capture: {captured_run.database_path}")
    print(f"JSON report:    {json_path}")
    print(f"Markdown report: {markdown_path}")


if __name__ == "__main__":
    main()
