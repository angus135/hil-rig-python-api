from pathlib import Path

import pytest

from hilrig import (
    CapturedRunBuilder,
    CaptureStatus,
    CommunicationPeripheral,
    CommunicationResult,
    ConfigurationError,
    EvaluationVerdict,
    PWMMeasurement,
    TickResult,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
    evaluate_assertions,
)
from hilrig import Test as HilRigTest


def _tick(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        digital_inputs=(False,) * 10,
        analogue_inputs_uv=(0, 0),
        pwm_inputs=(
            PWMMeasurement(period_ns=20_000, duty_permyriad=5_000),
            PWMMeasurement(period_ns=20_000, duty_permyriad=5_000),
        ),
    )


def test_named_assertion_groups_and_subject_names_survive_capture(
    tmp_path: Path,
) -> None:
    test = HilRigTest("Grouped assertions")
    uart = (
        test.uart(channel=0)
        .named("UART_ch1")
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )
    with test.group("UART Tests"):
        uart.write(data=b"PING", at_tick=0)
        test.expect(uart).receive(b"READY", from_tick=0, until_tick=1)
    compiled = test.compile()

    assert [(group.group_id, group.name) for group in compiled.assertion_groups] == [
        (0, "Assertions"),
        (1, "UART Tests"),
    ]
    assert compiled.assertions[0].group_id == 1
    assert compiled.assertions[0].subject_name == "UART_ch1"
    assert compiled.instructions[0].group_id == 1
    assert compiled.instructions[0].subject_name == "UART_ch1"

    builder = CapturedRunBuilder.from_compiled_test(tmp_path / "run.sqlite3", compiled)
    builder.add_tick_result(_tick(0))
    builder.add_communication_result(
        CommunicationResult(
            tick=0,
            peripheral=CommunicationPeripheral.UART,
            channel=0,
            payload=b"NOT READY",
        )
    )
    run = builder.finalize(status=CaptureStatus.INCOMPLETE)
    snapshot = run.original_assertion_set
    report = evaluate_assertions(run)
    markdown = report.to_markdown()

    assert snapshot.groups == compiled.assertion_groups
    assert snapshot.stimuli == compiled.instructions
    assert snapshot.assertions == compiled.assertions
    assert report.verdict is EvaluationVerdict.FAIL
    assert report.assertion_groups[0].name == "UART Tests"
    assert report.assertion_groups[0].verdict is EvaluationVerdict.FAIL
    assert "### UART Tests: FAIL" in markdown
    assert "**Commanded stimuli**" in markdown
    assert "UART_ch1 sent 0x50494e47 at tick 0." in markdown
    assert "UART_ch1 did not receive the expected payload" in markdown


def test_nested_groups_are_rejected_and_outer_group_is_restored() -> None:
    test = HilRigTest("No nested groups")
    uart = (
        test.uart(channel=0)
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )

    with test.group("Outer"):
        with (
            pytest.raises(ConfigurationError, match="nested groups are not supported"),
            test.group("Inner"),
        ):
            pass
        uart.write(data=b"A", at_tick=0)

    uart.write(data=b"B", at_tick=1)
    compiled = test.compile()

    assert compiled.instructions[0].group_id == 1
    assert compiled.instructions[1].group_id is None


def test_existing_expect_api_uses_the_default_group() -> None:
    test = HilRigTest("Default assertion group")
    sensor = test.analogue_input(channel=0).configure()
    test.expect(sensor).near(target_v=5, tolerance_v=0.01, at_tick=0)

    compiled = test.compile()

    assert compiled.assertions[0].group_id == 0
    assert compiled.assertions[0].subject_name == "analogue_input[0]"


def test_peripheral_names_are_unique_and_stable() -> None:
    test = HilRigTest("Named peripherals")
    first = test.uart(channel=0).named("UART_ch1")

    assert first.name == "UART_ch1"
    assert test.uart(channel=0).named("UART_ch1") is first
    with pytest.raises(ConfigurationError, match="already in use"):
        test.uart(channel=1).named("UART_ch1")


def test_group_exception_restores_outer_state() -> None:
    test = HilRigTest("Exception in group")
    uart = (
        test.uart(channel=0)
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )

    with pytest.raises(RuntimeError, match="boom"):
        with test.group("Failing group"):
            raise RuntimeError("boom")

    uart.write(data=b"AFTER", at_tick=0)
    compiled = test.compile()
    assert compiled.instructions[0].group_id is None


def test_assertion_group_compatibility_alias() -> None:
    test = HilRigTest("Compatibility alias")
    uart = (
        test.uart(channel=0)
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )
    group = test.assertion_group("Legacy Group")
    group.expect(uart).receive(b"OK", from_tick=0, until_tick=1)

    compiled = test.compile()
    assert compiled.assertions[0].group_id == 1
    assert compiled.assertion_groups[1].name == "Legacy Group"


def test_mixed_grouped_and_ungrouped_stimuli_report(tmp_path: Path) -> None:
    test = HilRigTest("Mixed stimuli")
    uart = (
        test.uart(channel=0)
        .named("UART_ch1")
        .configure(
            mode=UARTMode.TTL_3V3,
            baud_hz=115_200,
            parity=UARTParity.NONE,
            length=UARTLengthBits.EIGHT,
            stop=UARTStopBits.ONE,
        )
    )

    # Ungrouped stimulus
    uart.write(data=b"INIT", at_tick=0)

    # Grouped stimulus and expectation
    with test.group("Command Test"):
        uart.write(data=b"PING", at_tick=1)
        test.expect(uart).receive(b"PONG", from_tick=1, until_tick=2)

    compiled = test.compile()
    assert compiled.instructions[0].group_id is None
    assert compiled.instructions[1].group_id == 1

    builder = CapturedRunBuilder.from_compiled_test(tmp_path / "mixed.sqlite3", compiled)
    builder.add_tick_result(_tick(0))
    builder.add_tick_result(_tick(1))
    builder.add_communication_result(
        CommunicationResult(
            tick=1,
            peripheral=CommunicationPeripheral.UART,
            channel=0,
            payload=b"PONG",
        )
    )
    run = builder.finalize(status=CaptureStatus.INCOMPLETE)
    report = evaluate_assertions(run)

    assert report.verdict is EvaluationVerdict.INCONCLUSIVE
    assert len(report.assertion_groups) == 1
    assert report.assertion_groups[0].name == "Command Test"
    assert report.assertion_groups[0].verdict is EvaluationVerdict.PASS
    assert len(report.assertion_groups[0].stimuli) == 1
    assert report.assertion_groups[0].stimuli[0].message == "UART_ch1 sent 0x50494e47 at tick 1."

