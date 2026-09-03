import sqlite3
from pathlib import Path

import pytest

from hilrig import (
    CapturedRunBuilder,
    CapturedRunIR,
    CaptureSchemaError,
    FrequencyMode,
    StartMode,
)
from hilrig import Test as HilRigTest
from hilrig.models.execution import IR_SCHEMA_VERSION
from hilrig.results import ORIGINAL_ASSERTION_SET_ID, RESULT_IR_SCHEMA_VERSION


def test_compiled_assertions_are_snapshotted_and_restored_for_evaluation(
    tmp_path: Path,
) -> None:
    test = HilRigTest(name="Reusable assertion snapshot")
    test.configure(frequency_mode=FrequencyMode.HZ_10K, start_mode=StartMode.IMMEDIATE)
    digital_input = test.digital_input(channel=0)
    pwm_input = test.pwm_input(channel=1)
    analogue_input = test.analogue_input(channel=0)
    test.expect(digital_input).remain_high(from_tick=10, until_tick=20)
    test.expect(pwm_input).frequency_near(
        frequency_hz=50_000,
        tolerance_hz=500,
        at_tick=25,
    )
    test.expect(analogue_input).remain_within(
        minimum_v=4.9,
        maximum_v=5.1,
        from_tick=30,
        until_tick=40,
    )
    compiled = test.compile()

    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "run.sqlite3",
        compiled,
        run_id=0x1234,
    )
    builder.abort()
    reopened = CapturedRunIR.open(builder.database_path)
    snapshot = reopened.original_assertion_set

    assert snapshot.assertion_set_id == ORIGINAL_ASSERTION_SET_ID
    assert snapshot.name == "Original compiled assertions"
    assert snapshot.compiled_ir_version == compiled.schema_version == IR_SCHEMA_VERSION
    assert snapshot.assertion_count == 3
    assert snapshot.assertions == compiled.assertions
    assert snapshot.assertions is not compiled.assertions
    assert dict(snapshot.assertions[2].arguments) == {
        "from_tick": 30,
        "until_tick": 40,
        "minimum_uv": 4_900_000,
        "maximum_uv": 5_100_000,
    }
    assert tuple(reopened.iter_assertion_sets()) == (snapshot,)

    with pytest.raises(TypeError):
        snapshot.assertions[0].arguments["from_tick"] = 99  # type: ignore[index]


def test_direct_builder_creates_an_empty_original_assertion_set(tmp_path: Path) -> None:
    builder = CapturedRunBuilder(
        tmp_path / "run.sqlite3",
        test_id=1,
        run_id=2,
        test_name="Manual capture",
        tick_period_ns=1_000_000,
        expected_tick_count=1,
    )

    run = builder.abort()

    assert run.metadata.schema_version == RESULT_IR_SCHEMA_VERSION
    assert run.original_assertion_set.compiled_ir_version == IR_SCHEMA_VERSION
    assert run.original_assertion_set.assertions == ()


def test_unknown_assertion_set_is_reported_clearly(tmp_path: Path) -> None:
    builder = CapturedRunBuilder(
        tmp_path / "run.sqlite3",
        test_id=1,
        run_id=2,
        test_name="Unknown set",
        tick_period_ns=1_000_000,
        expected_tick_count=1,
    )
    run = builder.abort()

    with pytest.raises(KeyError, match="Unknown assertion set"):
        run.assertion_set("not-present")


def test_corrupt_assertion_arguments_are_rejected_when_loaded(tmp_path: Path) -> None:
    test = HilRigTest(name="Corrupt assertion snapshot")
    test.expect(test.digital_input(channel=0)).high(at_tick=0)
    builder = CapturedRunBuilder.from_compiled_test(tmp_path / "run.sqlite3", test.compile())
    builder.abort()

    with sqlite3.connect(builder.database_path) as connection:
        connection.execute(
            "UPDATE assertion_definitions SET arguments_json = '[]' WHERE assertion_id = 0"
        )

    run = CapturedRunIR.open(builder.database_path)
    with pytest.raises(CaptureSchemaError, match="must be a JSON object"):
        _ = run.original_assertion_set


def test_capture_open_rejects_a_missing_original_assertion_set(tmp_path: Path) -> None:
    builder = CapturedRunBuilder(
        tmp_path / "run.sqlite3",
        test_id=1,
        run_id=2,
        test_name="Missing assertion set",
        tick_period_ns=1_000_000,
        expected_tick_count=1,
    )
    builder.abort()

    with sqlite3.connect(builder.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM assertion_sets")

    with pytest.raises(CaptureSchemaError, match="no original assertion set"):
        CapturedRunIR.open(builder.database_path)


def test_pre_snapshot_capture_schema_is_rejected_explicitly(tmp_path: Path) -> None:
    builder = CapturedRunBuilder(
        tmp_path / "run.sqlite3",
        test_id=1,
        run_id=2,
        test_name="Old schema marker",
        tick_period_ns=1_000_000,
        expected_tick_count=1,
    )
    builder.abort()

    with sqlite3.connect(builder.database_path) as connection:
        connection.execute("UPDATE result_ir_schema SET schema_version = '1.0'")

    with pytest.raises(CaptureSchemaError, match="Unsupported capture schema '1.0'"):
        CapturedRunIR.open(builder.database_path)
