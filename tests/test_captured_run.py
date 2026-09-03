import csv
import json
from pathlib import Path

import pytest

from hilrig import (
    ApplicationErrorRecord,
    CapturedRunBuilder,
    CapturedRunIR,
    CaptureStateError,
    CaptureStatus,
    CaptureStorageError,
    CommunicationPeripheral,
    CommunicationResult,
    FrequencyMode,
    IncomingResultAdapter,
    PWMMeasurement,
    StartMode,
    TickCondition,
    TickResult,
)
from hilrig import Test as HilRigTest


def _tick(tick: int, *, condition: TickCondition = TickCondition.OK) -> TickResult:
    return TickResult(
        tick=tick,
        digital_inputs=tuple((tick + channel) % 2 == 0 for channel in range(10)),
        analogue_inputs_uv=(tick * 1_000, -(tick * 1_000)),
        pwm_inputs=(
            PWMMeasurement(period_ns=20_000 + tick, duty_permyriad=4_000),
            PWMMeasurement(period_ns=10_000 + tick, duty_permyriad=8_000),
        ),
        condition=condition,
    )


def _builder(path: Path, *, expected_tick_count: int = 3, **kwargs: object) -> CapturedRunBuilder:
    return CapturedRunBuilder(
        path,
        test_id=0x1234,
        run_id=0x5678,
        test_name="Captured test",
        tick_period_ns=100_000,
        expected_tick_count=expected_tick_count,
        **kwargs,
    )


def test_builder_persists_complete_run_and_channel_queries(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", batch_size=2)
    builder.add_tick_result(_tick(2))
    builder.add_tick_result(_tick(0))
    builder.add_tick_result(_tick(1, condition=TickCondition.PARTIAL))

    run = builder.finalize()

    assert isinstance(run, CapturedRunIR)
    assert run.metadata.status is CaptureStatus.COMPLETE
    assert run.metadata.test_id == 0x1234
    assert run.metadata.run_id == 0x5678
    assert run.metadata.expected_tick_count == 3
    assert run.metadata.received_tick_count == 3
    assert run.metadata.first_tick == 0
    assert run.metadata.last_tick == 2
    assert [result.tick for result in run.iter_ticks()] == [0, 1, 2]

    digital = run.digital_input(channel=1).sample_at(2)
    assert digital is not None
    assert digital.value is False
    assert digital.time_ns == 200_000

    analogue = run.analogue_input(channel=0).sample_at(1)
    assert analogue is not None
    assert analogue.microvolts == 1_000
    assert analogue.condition is TickCondition.PARTIAL

    pwm = run.pwm_input(channel=1).sample_at(0)
    assert pwm is not None
    assert pwm.measurement == PWMMeasurement(period_ns=10_000, duty_permyriad=8_000)
    assert pwm.measurement.duty_cycle == 0.8


def test_builder_can_reuse_identity_and_timing_from_compiled_test(tmp_path: Path) -> None:
    test = HilRigTest(name="Compiled capture")
    test.configure(frequency_mode=FrequencyMode.HZ_10K, start_mode=StartMode.IMMEDIATE)
    test.digital_output(channel=0).high(at_tick=25)
    compiled = test.compile()

    builder = CapturedRunBuilder.from_compiled_test(
        tmp_path / "run.sqlite3",
        compiled,
        run_id=0xABCD,
    )
    live_run = CapturedRunIR.open(builder.database_path)

    assert live_run.metadata.test_id == compiled.test_id
    assert live_run.metadata.test_name == compiled.name
    assert live_run.metadata.run_id == 0xABCD
    assert live_run.metadata.tick_period_ns == compiled.tick_period_ns == 100_000
    assert live_run.metadata.expected_tick_count == compiled.expected_tick_count == 10_026
    builder.abort()


def test_flush_is_a_barrier_that_makes_pending_rows_visible(tmp_path: Path) -> None:
    builder = _builder(
        tmp_path / "run.sqlite3",
        batch_size=1_000,
        flush_interval_s=60,
    )
    builder.add_tick_result(_tick(0))

    builder.flush()
    live_run = CapturedRunIR.open(builder.database_path)

    assert live_run.metadata.status is CaptureStatus.IN_PROGRESS
    assert live_run.metadata.received_tick_count == 1
    builder.finalize(status=CaptureStatus.ABORTED)


def test_execution_problem_discards_placeholder_fixed_values(tmp_path: Path) -> None:
    placeholder = TickResult(
        tick=0,
        digital_inputs=(False,) * 10,
        analogue_inputs_uv=(0, 0),
        pwm_inputs=(
            PWMMeasurement(period_ns=0, duty_permyriad=0),
            PWMMeasurement(period_ns=0, duty_permyriad=0),
        ),
        condition=TickCondition.EXECUTION_PROBLEM,
        problem_detail=7,
    )
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=1)
    builder.add_tick_result(placeholder)

    run = builder.finalize()
    result = run.tick_at(0)

    assert result is not None
    assert not result.valid
    assert result.digital_inputs == (None,) * 10
    assert result.analogue_inputs_uv == (None, None)
    assert result.pwm_inputs == (None, None)
    assert result.problem_detail == 7
    digital = run.digital_input(channel=0).sample_at(0)
    assert digital is not None and not digital.valid


def test_raw_communication_results_and_errors_are_kept_separately(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=1)
    builder.add_tick_result(_tick(0, condition=TickCondition.PARTIAL))
    builder.add_communication_result(
        CommunicationResult(
            tick=0,
            peripheral=CommunicationPeripheral.UART,
            channel=0,
            payload=b"READY\r\n",
        )
    )
    builder.add_communication_result(
        CommunicationResult(
            tick=0,
            peripheral=CommunicationPeripheral.UART,
            channel=0,
            payload=b"\x00\xff",
        )
    )
    builder.add_application_error(
        ApplicationErrorRecord(
            tick=0,
            category="communication_capture",
            detail="UART capture truncated",
            recoverable=True,
            diagnostic_data=b"\x10",
        )
    )

    run = builder.finalize()
    captures = list(run.iter_communications(peripheral=CommunicationPeripheral.UART, channel=0))
    errors = list(run.iter_application_errors())

    assert [capture.ordinal for capture in captures] == [0, 1]
    assert [capture.payload for capture in captures] == [b"READY\r\n", b"\x00\xff"]
    assert captures[0].payload_size == 7
    assert errors[0].category == "communication_capture"
    assert errors[0].diagnostic_data == b"\x10"


def test_missing_ticks_produce_incomplete_capture(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=3)
    builder.add_tick_result(_tick(0))
    builder.add_tick_result(_tick(2))

    run = builder.finalize()

    assert run.metadata.status is CaptureStatus.INCOMPLETE
    assert run.metadata.missing_tick_count == 1
    assert run.tick_at(1) is None


def test_explicit_complete_status_rejects_missing_ticks(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=2)
    builder.add_tick_result(_tick(0))

    with pytest.raises(CaptureStorageError, match="could not complete"):
        builder.finalize(status=CaptureStatus.COMPLETE)


def test_duplicate_tick_fails_the_whole_pending_transaction(tmp_path: Path) -> None:
    builder = _builder(
        tmp_path / "run.sqlite3",
        expected_tick_count=2,
        batch_size=100,
        flush_interval_s=60,
    )
    builder.add_tick_result(_tick(0))
    builder.add_tick_result(_tick(0))

    with pytest.raises(CaptureStorageError, match="could not complete"):
        builder.flush()

    run = CapturedRunIR.open(builder.database_path)
    assert run.metadata.received_tick_count == 0


def test_builder_refuses_out_of_range_ticks_and_post_finalize_writes(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=1)

    with pytest.raises(ValueError, match="outside expected range"):
        builder.add_tick_result(_tick(1))

    builder.add_tick_result(_tick(0))
    builder.finalize()

    with pytest.raises(CaptureStateError):
        builder.add_tick_result(_tick(0))


def test_existing_database_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "run.sqlite3"
    builder = _builder(path, expected_tick_count=1)
    builder.add_tick_result(_tick(0))
    builder.finalize()

    with pytest.raises(FileExistsError):
        _builder(path, expected_tick_count=1)


def test_manifest_and_csv_exports_are_derived_from_database(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=1)
    builder.add_tick_result(_tick(0))
    builder.add_communication_result(
        CommunicationResult(
            tick=0,
            peripheral=CommunicationPeripheral.SPI,
            channel=1,
            payload=b"\xaa\xbb",
        )
    )
    run = builder.finalize()

    manifest_path = run.write_manifest_json(tmp_path / "manifest.json")
    fixed_path = run.write_fixed_results_csv(tmp_path / "fixed.csv")
    communications_path = run.write_communication_results_csv(tmp_path / "communications.csv")
    errors_path = run.write_application_errors_csv(tmp_path / "errors.csv")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["capture"]["status"] == "complete"
    assert manifest["assertions"] == {
        "original_set_id": "original",
        "compiled_ir_version": "1.1",
        "count": 0,
    }
    assert manifest["authoritative_data"] == "run.sqlite3"
    with fixed_path.open(encoding="utf-8", newline="") as stream:
        fixed_rows = list(csv.DictReader(stream))
    assert fixed_rows[0]["tick"] == "0"
    assert fixed_rows[0]["digital_input_0"] == "1"
    with communications_path.open(encoding="utf-8", newline="") as stream:
        communication_rows = list(csv.DictReader(stream))
    assert communication_rows[0]["payload_hex"] == "0xaabb"
    with errors_path.open(encoding="utf-8", newline="") as stream:
        assert list(csv.DictReader(stream)) == []


def test_future_adapter_is_explicitly_unimplemented(tmp_path: Path) -> None:
    builder = _builder(tmp_path / "run.sqlite3", expected_tick_count=1)
    adapter = IncomingResultAdapter(builder)

    with pytest.raises(NotImplementedError, match="final Python interfaces"):
        adapter.receive_usb_bytes(b"future protocol data")
    with pytest.raises(NotImplementedError, match="field mapping"):
        adapter.ingest_application_message(object())

    builder.abort()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TickResult(
            tick=0,
            digital_inputs=(True,) * 9,
            analogue_inputs_uv=(0, 0),
            pwm_inputs=(
                PWMMeasurement(period_ns=1, duty_permyriad=1),
                PWMMeasurement(period_ns=1, duty_permyriad=1),
            ),
        ),
        lambda: PWMMeasurement(period_ns=1, duty_permyriad=10_001),
        lambda: CommunicationResult(
            tick=0,
            peripheral=CommunicationPeripheral.UART,
            channel=0,
            payload="not bytes",  # type: ignore[arg-type]
        ),
    ],
)
def test_incoming_typed_records_validate_their_shape(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]
