import json
from pathlib import Path

import pytest

from hilrig import (
    ApplicationErrorRecord,
    CapturedRunBuilder,
    CapturedRunIR,
    CaptureStatus,
    EvaluationError,
    EvaluationVerdict,
    PWMMeasurement,
    TickResult,
    UnsupportedAssertionError,
    evaluate_assertions,
)
from hilrig import Test as HilRigTest
from hilrig.models.execution import CompiledAssertion, CompiledTestIR, immutable_fields

_DEFAULT_PWM = PWMMeasurement(period_ns=20_000, duty_permyriad=5_000)


def _tick(
    tick: int,
    *,
    digital_0: bool = True,
    analogue_0_uv: int = 5_000_000,
    pwm_0: PWMMeasurement = _DEFAULT_PWM,
) -> TickResult:
    digital = [False] * 10
    digital[0] = digital_0
    return TickResult(
        tick=tick,
        digital_inputs=tuple(digital),
        analogue_inputs_uv=(analogue_0_uv, 0),
        pwm_inputs=(pwm_0, PWMMeasurement(period_ns=10_000, duty_permyriad=2_500)),
    )


def _capture(
    path: Path,
    compiled: CompiledTestIR,
    ticks: list[TickResult],
    *,
    expected_tick_count: int,
    errors: tuple[ApplicationErrorRecord, ...] = (),
) -> CapturedRunIR:
    builder = CapturedRunBuilder(
        path,
        test_id=compiled.test_id,
        run_id=0xCAFE,
        test_name=compiled.name,
        tick_period_ns=compiled.tick_period_ns,
        expected_tick_count=expected_tick_count,
        compiled_ir_version=compiled.schema_version,
        compiled_assertions=compiled.assertions,
    )
    for tick in ticks:
        builder.add_tick_result(tick)
    for error in errors:
        builder.add_application_error(error)
    return builder.finalize()


def _all_passing_assertions() -> CompiledTestIR:
    test = HilRigTest(name="All evaluator operations")
    digital_0 = test.digital_input(channel=0)
    digital_1 = test.digital_input(channel=1)
    pwm = test.pwm_input(channel=0)
    analogue = test.analogue_input(channel=0)

    test.expect(digital_0).low(at_tick=0)
    test.expect(digital_0).high(at_tick=1)
    test.expect(digital_0).remain_high(from_tick=1, until_tick=4)
    test.expect(digital_1).remain_low(from_tick=0, until_tick=4)
    test.expect(digital_0).to_transition(
        from_state=False,
        to_state=True,
        between_ticks=(0, 2),
    )

    test.expect(pwm).period_near(period_ns=20_000, tolerance_ns=10, at_tick=1)
    test.expect(pwm).frequency_near(frequency_hz=50_000, tolerance_hz=10, at_tick=1)
    test.expect(pwm).duty_cycle_near(
        duty_cycle=0.5,
        duty_cycle_tolerance=0.001,
        at_tick=1,
    )
    test.expect(pwm).waveform_near(
        frequency_hz=50_000,
        frequency_tolerance_hz=10,
        duty_cycle=0.5,
        duty_cycle_tolerance=0.001,
        at_tick=1,
    )
    test.expect(pwm).frequency_remain_within(
        minimum_hz=49_999,
        maximum_hz=50_001,
        from_tick=0,
        until_tick=4,
    )
    test.expect(pwm).duty_cycle_remain_within(
        minimum_duty_cycle=0.499,
        maximum_duty_cycle=0.501,
        from_tick=0,
        until_tick=4,
    )

    test.expect(analogue).near(target_v=5, tolerance_v=0.001, at_tick=1)
    test.expect(analogue).within(minimum_v=4.9, maximum_v=5.1, at_tick=1)
    test.expect(analogue).remain_within(
        minimum_v=4.9,
        maximum_v=5.1,
        from_tick=0,
        until_tick=4,
    )
    test.expect(analogue).remain_above(
        threshold_v=4.9,
        from_tick=0,
        until_tick=4,
    )
    test.expect(analogue).remain_below(
        threshold_v=5.1,
        from_tick=0,
        until_tick=4,
    )
    return test.compile()


def test_evaluator_dispatches_every_current_assertion_and_passes(tmp_path: Path) -> None:
    compiled = _all_passing_assertions()
    ticks = [_tick(0, digital_0=False), *[_tick(tick) for tick in range(1, 5)]]
    run = _capture(
        tmp_path / "passing.sqlite3",
        compiled,
        ticks,
        expected_tick_count=5,
    )

    report = evaluate_assertions(run)

    assert report.verdict is EvaluationVerdict.PASS
    assert report.passed_count == 16
    assert report.failed_count == 0
    assert report.inconclusive_count == 0
    assert [result.assertion_id for result in report.assertion_results] == list(range(16))
    assert not report.warnings


def test_point_failures_include_observed_values_and_failure_ticks(tmp_path: Path) -> None:
    test = HilRigTest(name="Point failures")
    test.expect(test.digital_input(channel=0)).low(at_tick=0)
    test.expect(test.analogue_input(channel=0)).near(
        target_v=3.3,
        tolerance_v=0.01,
        at_tick=0,
    )
    test.expect(test.pwm_input(channel=0)).frequency_near(
        frequency_hz=50_000,
        tolerance_hz=10,
        at_tick=0,
    )
    compiled = test.compile()
    run = _capture(
        tmp_path / "failures.sqlite3",
        compiled,
        [
            _tick(
                0,
                digital_0=True,
                analogue_0_uv=5_000_000,
                pwm_0=PWMMeasurement(period_ns=0, duty_permyriad=0),
            )
        ],
        expected_tick_count=1,
    )

    report = evaluate_assertions(run)

    assert report.verdict is EvaluationVerdict.FAIL
    assert report.failed_count == 3
    assert all(result.first_failure_tick == 0 for result in report.assertion_results)
    assert report.assertion_results[0].observed["state"] == "HIGH"
    assert report.assertion_results[1].observed["value_uv"] == 5_000_000
    assert report.assertion_results[2].observed["frequency_hz"] is None


def test_range_without_violations_is_inconclusive_when_evidence_has_gaps(
    tmp_path: Path,
) -> None:
    test = HilRigTest(name="Incomplete range")
    test.expect(test.analogue_input(channel=0)).remain_within(
        minimum_v=4.9,
        maximum_v=5.1,
        from_tick=0,
        until_tick=2,
    )
    compiled = test.compile()
    builder = CapturedRunBuilder(
        tmp_path / "incomplete.sqlite3",
        test_id=compiled.test_id,
        run_id=1,
        test_name=compiled.name,
        tick_period_ns=compiled.tick_period_ns,
        expected_tick_count=3,
        compiled_ir_version=compiled.schema_version,
        compiled_assertions=compiled.assertions,
    )
    builder.add_tick_result(_tick(0))
    builder.add_tick_result(TickResult.execution_problem(tick=2, problem_detail=7))
    run = builder.finalize()

    report = evaluate_assertions(run)
    result = report.assertion_results[0]

    assert report.verdict is EvaluationVerdict.INCONCLUSIVE
    assert result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert result.valid_sample_count == 1
    assert result.missing_sample_count == 1
    assert result.invalid_sample_count == 1
    assert result.violation_count == 0


def test_known_range_violation_fails_even_when_other_ticks_are_missing(tmp_path: Path) -> None:
    test = HilRigTest(name="Violation beats gap")
    test.expect(test.digital_input(channel=0)).remain_high(from_tick=0, until_tick=2)
    compiled = test.compile()
    run = _capture(
        tmp_path / "violation.sqlite3",
        compiled,
        [_tick(0, digital_0=False), _tick(2)],
        expected_tick_count=3,
    )

    result = evaluate_assertions(run).assertion_results[0]

    assert result.verdict is EvaluationVerdict.FAIL
    assert result.violation_count == 1
    assert result.missing_sample_count == 1
    assert result.first_failure_tick == 0


def test_transition_requires_adjacent_valid_ticks(tmp_path: Path) -> None:
    test = HilRigTest(name="Transition gap")
    test.expect(test.digital_input(channel=0)).to_transition(
        from_state=False,
        to_state=True,
        between_ticks=(0, 2),
    )
    compiled = test.compile()
    run = _capture(
        tmp_path / "transition-gap.sqlite3",
        compiled,
        [_tick(0, digital_0=False), _tick(2, digital_0=True)],
        expected_tick_count=3,
    )

    result = evaluate_assertions(run).assertion_results[0]

    assert result.verdict is EvaluationVerdict.INCONCLUSIVE
    assert result.observed["transition_from_tick"] is None
    assert result.missing_sample_count == 1


def test_incomplete_capture_keeps_passing_assertion_but_not_overall_pass(tmp_path: Path) -> None:
    test = HilRigTest(name="Early assertion")
    test.expect(test.digital_input(channel=0)).high(at_tick=0)
    compiled = test.compile()
    run = _capture(
        tmp_path / "incomplete-overall.sqlite3",
        compiled,
        [_tick(0)],
        expected_tick_count=2,
    )

    report = evaluate_assertions(run)

    assert report.assertion_results[0].verdict is EvaluationVerdict.PASS
    assert report.verdict is EvaluationVerdict.INCONCLUSIVE
    assert report.capture_status is CaptureStatus.INCOMPLETE
    assert "received 1 of 2" in report.warnings[0]


def test_non_recoverable_application_error_prevents_overall_pass(tmp_path: Path) -> None:
    test = HilRigTest(name="Application error")
    test.expect(test.digital_input(channel=0)).high(at_tick=0)
    compiled = test.compile()
    run = _capture(
        tmp_path / "application-error.sqlite3",
        compiled,
        [_tick(0)],
        expected_tick_count=1,
        errors=(
            ApplicationErrorRecord(
                category="execution",
                detail="RIG reported an execution problem",
                recoverable=False,
                tick=0,
            ),
        ),
    )

    report = evaluate_assertions(run)

    assert report.assertion_results[0].verdict is EvaluationVerdict.PASS
    assert report.verdict is EvaluationVerdict.INCONCLUSIVE
    assert "including 1 non-recoverable" in report.warnings[0]


def test_empty_assertion_set_is_inconclusive(tmp_path: Path) -> None:
    compiled = HilRigTest(name="No assertions").compile()
    run = _capture(
        tmp_path / "no-assertions.sqlite3",
        compiled,
        [_tick(0)],
        expected_tick_count=1,
    )

    report = evaluate_assertions(run)

    assert report.verdict is EvaluationVerdict.INCONCLUSIVE
    assert report.assertion_results == ()
    assert "contains no assertions" in report.warnings[0]


def test_evaluator_refuses_live_captures(tmp_path: Path) -> None:
    compiled = HilRigTest(name="Live capture").compile()
    builder = CapturedRunBuilder(
        tmp_path / "live.sqlite3",
        test_id=compiled.test_id,
        run_id=2,
        test_name=compiled.name,
        tick_period_ns=compiled.tick_period_ns,
        expected_tick_count=1,
        compiled_ir_version=compiled.schema_version,
        compiled_assertions=compiled.assertions,
    )
    builder.flush()
    live_run = CapturedRunIR.open(builder.database_path)

    with pytest.raises(EvaluationError, match="finalized"):
        evaluate_assertions(live_run)

    builder.abort()


def test_unknown_stored_assertion_requires_a_registered_handler(tmp_path: Path) -> None:
    future_assertion = CompiledAssertion(
        assertion_id=0,
        peripheral="digital_input",
        channel=0,
        assertion="future_operation",
        arguments=immutable_fields({"tick": 0}),
    )
    compiled = HilRigTest(name="Unknown operation").compile()
    builder = CapturedRunBuilder(
        tmp_path / "unknown.sqlite3",
        test_id=compiled.test_id,
        run_id=3,
        test_name=compiled.name,
        tick_period_ns=compiled.tick_period_ns,
        expected_tick_count=1,
        compiled_ir_version=compiled.schema_version,
        compiled_assertions=(future_assertion,),
    )
    builder.add_tick_result(_tick(0))
    run = builder.finalize()

    with pytest.raises(UnsupportedAssertionError, match="future_operation"):
        evaluate_assertions(run)


def test_evaluation_report_exports_json_and_markdown(tmp_path: Path) -> None:
    test = HilRigTest(name="Report export")
    test.expect(test.analogue_input(channel=0)).near(
        target_v=5,
        tolerance_v=0.005,
        at_tick=0,
    )
    compiled = test.compile()
    run = _capture(
        tmp_path / "report.sqlite3",
        compiled,
        [_tick(0)],
        expected_tick_count=1,
    )
    report = evaluate_assertions(run)

    json_path = report.write_json(tmp_path / "report.json")
    markdown_path = report.write_markdown(tmp_path / "report.md")
    document = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert document["evaluation_report_version"] == "1.0"
    assert document["evaluation"]["verdict"] == "pass"
    assert document["assertions"][0]["expected"]["target_uv"] == 5_000_000
    assert "# HIL-RIG Test Report: Report export" in markdown
    assert "**Overall verdict:** `PASS`" in markdown
    assert "5 V (5000000 µV)" in markdown
    assert report.to_dict() == document
    assert report.to_json().endswith("\n")

    with pytest.raises(ValueError, match="must end in .json"):
        report.write_json(tmp_path / "wrong.txt")
