from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from protocol_fakes import (
    ApplicationResponse,
    FakeProtocol,
    FinalizeTestUpload,
    GlobalControlCommand,
    ResponseOutcome,
    ResponseReason,
    ResponseScope,
)

from hilrig import (
    ApplicationErrorRecord,
    FixedIOProtocolAdapter,
    ManualSendResult,
    ProtocolSessionError,
    PWMMeasurement,
    RunReportRecord,
    TickResult,
)
from hilrig.protocol import ProtocolWorkflowState, UploadAdvanceMode, UploadOperationKind
from hilrig.runner import (
    DefinitionFileError,
    ManualSessionState,
    ProtocolWorker,
    WorkerState,
    load_test_definition,
)


def _write_test_file(path: Path, *, start_mode: str = "HOST_COMMAND") -> Path:
    path.write_text(
        "\n".join(
            (
                "from hilrig import FrequencyMode, StartMode, Test",
                "",
                "def build_test():",
                "    test = Test(name='Worker example')",
                "    test.configure(",
                "        frequency_mode=FrequencyMode.HZ_100,",
                f"        start_mode=StartMode.{start_mode},",
                "    )",
                "    return test",
                "",
            )
        ),
        encoding="utf-8",
    )
    return path


class _AutomaticConnection:
    def __init__(
        self,
        *,
        block_results: bool = False,
        application_errors: tuple[ApplicationErrorRecord, ...] = (),
        fail_during_upload: bool = False,
        reset_responses: tuple[tuple[ResponseOutcome, ResponseReason], ...] = (),
        reported_outcome: str = "SUCCESS",
    ) -> None:
        self.block_results = block_results
        self.application_errors = application_errors
        self.fail_during_upload = fail_during_upload
        self.reset_responses = list(reset_responses)
        self.reset_attempts = 0
        self.reported_outcome = reported_outcome
        self._accumulated_errors: list[ApplicationErrorRecord] = []
        self.last_error = None
        self.session_confirmed = False
        self.session_info = None
        self.workflow_state = ProtocolWorkflowState.CONNECTING
        self.active_upload = None
        self.results_complete = False
        self.report_received = False
        self.report_timed_out = False
        self.run_report = None
        self.lifecycle_responses = ()
        self.status_events = ()
        self.ready_for_upload = True
        self.protocol = FakeProtocol
        self.last_application_response = None
        self.builder = None
        self.compiled = None
        self.start_called = False
        self.abort_called = False
        self.closed = False
        self.advance_mode = UploadAdvanceMode.AUTOMATIC
        self.next_upload_operation = None

    @property
    def waiting_for_operator(self) -> bool:
        return self.next_upload_operation is not None

    def drain_accumulated_errors(self) -> tuple[ApplicationErrorRecord, ...]:
        errors = tuple(self._accumulated_errors)
        self._accumulated_errors.clear()
        return errors

    def service(self):
        stored = ()
        errors = ()
        if not self.session_confirmed:
            self.session_confirmed = True
            self.session_info = SimpleNamespace(
                protocol_version="0.4.0",
                firmware_version="1.2.3",
            )
            self.workflow_state = ProtocolWorkflowState.READY
        elif self.workflow_state is ProtocolWorkflowState.CONFIGURING:
            if self.fail_during_upload:
                self.workflow_state = ProtocolWorkflowState.FAILED
                self.last_error = "Instruction upload rejected on tick 0: INVALID_TICK (detail 42)"
                err = ApplicationErrorRecord(
                    category="tick_rejected",
                    detail="INVALID_TICK (detail 42)",
                    recoverable=False,
                    tick=0,
                )
                self._accumulated_errors.append(err)
                raise ProtocolSessionError(self.last_error)
            if self.reported_outcome == "REJECTED":
                self.run_report = self._report(
                    run_outcome="REJECTED",
                    execution_outcome="NOT_STARTED",
                    result_status="UNAVAILABLE",
                    emitted=0,
                )
                self.workflow_state = ProtocolWorkflowState.REPORT_RECEIVED
                self.report_received = True
                return self._service_report()
            if self.advance_mode is UploadAdvanceMode.OPERATOR_GATED:
                self.next_upload_operation = SimpleNamespace(
                    kind=UploadOperationKind.START,
                    label="START",
                )
                self.workflow_state = ProtocolWorkflowState.WAITING_FOR_OPERATOR
            else:
                self.workflow_state = ProtocolWorkflowState.READY_TO_START
        elif self.workflow_state is ProtocolWorkflowState.STARTING:
            self.workflow_state = ProtocolWorkflowState.RUNNING
        elif self.workflow_state is ProtocolWorkflowState.RUNNING and not self.block_results:
            errors = self.application_errors
            self.application_errors = ()
            stored = tuple(_tick_result(tick) for tick in range(self.compiled.expected_tick_count))
            for result in stored:
                self.builder.add_tick_result(result)
            self.run_report = self._report(
                run_outcome="SUCCESS",
                execution_outcome="COMPLETE",
                result_status="COMPLETE",
                emitted=self.compiled.expected_tick_count,
            )
            self.workflow_state = ProtocolWorkflowState.REPORT_RECEIVED
            self.results_complete = True
            self.report_received = True
        elif self.workflow_state is ProtocolWorkflowState.RESETTING:
            outcome, reason = (
                self.reset_responses.pop(0)
                if self.reset_responses
                else (ResponseOutcome.COMPLETED, ResponseReason.NONE)
            )
            self.last_application_response = ApplicationResponse(
                test_id=None,
                scope=ResponseScope.GLOBAL_CONTROL,
                outcome=outcome,
                reason=reason,
                global_control_command=GlobalControlCommand.RESET_APPLICATION,
            )
            self.workflow_state = ProtocolWorkflowState.REPORT_RECEIVED
            if outcome is ResponseOutcome.COMPLETED:
                self.workflow_state = ProtocolWorkflowState.READY
                self.ready_for_upload = True
        elif self.workflow_state is ProtocolWorkflowState.ABORTING:
            self.workflow_state = ProtocolWorkflowState.ABORTED
        return self._service_report(stored=stored, errors=errors)

    def _report(
        self,
        *,
        run_outcome: str,
        execution_outcome: str,
        result_status: str,
        emitted: int,
    ) -> RunReportRecord:
        return RunReportRecord(
            schema_version=1,
            valid_sections=1,
            run_outcome=run_outcome,
            execution_outcome=execution_outcome,
            result_status=result_status,
            expected_tick_count=self.compiled.expected_tick_count,
            tick_period_us=self.compiled.tick_period_ns // 1000,
            last_completed_boundary=None,
            result_ticks_emitted=emitted,
            failure_source="NONE",
            failure_stage="NONE",
            failure_reason="NONE",
            isr_timing=None,
            instruction_buffer=None,
            result_buffer=None,
            flash=None,
        )

    @staticmethod
    def _service_report(*, stored=(), errors=()):
        return SimpleNamespace(
            stored_tick_results=stored,
            stored_application_errors=errors,
            serial_bytes_read=0,
            serial_bytes_written=0,
            application_message_submitted=False,
        )

    def bind_result_builder(self, builder) -> None:
        self.builder = builder

    def queue_upload(
        self,
        compiled,
        *,
        upload_attempt,
        advance_mode=UploadAdvanceMode.AUTOMATIC,
    ) -> None:
        self.compiled = compiled
        self.active_upload = SimpleNamespace(upload_attempt=upload_attempt)
        self.advance_mode = advance_mode
        if advance_mode is UploadAdvanceMode.OPERATOR_GATED:
            self.next_upload_operation = SimpleNamespace(
                kind=UploadOperationKind.CONFIGURATION,
                label="configuration",
            )
            self.workflow_state = ProtocolWorkflowState.WAITING_FOR_OPERATOR
        else:
            self.workflow_state = ProtocolWorkflowState.CONFIGURING

    def release_next_operation(self):
        operation = self.next_upload_operation
        self.next_upload_operation = None
        if operation.kind is UploadOperationKind.START:
            self.start_called = True
            self.workflow_state = ProtocolWorkflowState.STARTING
        else:
            self.workflow_state = ProtocolWorkflowState.CONFIGURING
        return operation

    def continue_upload(self):
        operation = self.next_upload_operation
        self.advance_mode = UploadAdvanceMode.AUTOMATIC
        self.next_upload_operation = None
        if operation.kind is UploadOperationKind.START:
            self.start_called = True
            self.workflow_state = ProtocolWorkflowState.STARTING
        else:
            self.workflow_state = ProtocolWorkflowState.CONFIGURING
        return operation

    def start(self) -> None:
        self.start_called = True
        self.workflow_state = ProtocolWorkflowState.STARTING

    def abort(self) -> None:
        self.abort_called = True
        self.workflow_state = ProtocolWorkflowState.ABORTING

    def reset_application(self) -> None:
        self.reset_attempts += 1
        self.ready_for_upload = False
        self.workflow_state = ProtocolWorkflowState.RESETTING

    def get_status(self) -> None:
        self.ready_for_upload = True
        self.workflow_state = ProtocolWorkflowState.READY

    def close(self) -> None:
        self.closed = True


class _ManualConnection:
    def __init__(self, *, device: str | None, skip_system_info: bool) -> None:
        self.skip_system_info = skip_system_info
        self.serial_port = SimpleNamespace(port=device or "COM7")
        self.application = FixedIOProtocolAdapter(protocol_module=FakeProtocol)
        self.protocol = FakeProtocol
        self.session_confirmed = False
        self.session_info = None
        self.last_manual_send_result = None
        self.pending = None
        self.sequence = 0
        self.queued_messages = []
        self.closed = False

    def service(self):
        if not self.session_confirmed:
            self.session_confirmed = True
            if not self.skip_system_info:
                self.session_info = SimpleNamespace(
                    protocol_version="0.4.0",
                    firmware_version="1.2.3",
                )
        elif self.pending is not None:
            message, transport_only = self.pending
            self.pending = None
            self.last_manual_send_result = ManualSendResult(
                sequence=self.sequence,
                label=message.label,
                transport_delivered=True,
                application_response_required=not transport_only,
                application_response=None,
                success=True,
                detail=(
                    "Application message submitted; a response was not required."
                    if transport_only
                    else "Application response accepted."
                ),
            )
        return SimpleNamespace(application_messages=())

    def queue_manual_message(self, message, *, transport_only: bool = False) -> int:
        self.sequence += 1
        self.last_manual_send_result = None
        self.queued_messages.append(message)
        self.pending = (message, transport_only)
        return self.sequence

    def close(self) -> None:
        self.closed = True


def _tick_result(tick: int) -> TickResult:
    return TickResult(
        tick=tick,
        digital_inputs=(False,) * 10,
        analogue_inputs_uv=(0, 0),
        pwm_inputs=(
            PWMMeasurement(period_ns=0, duty_permyriad=0),
            PWMMeasurement(period_ns=0, duty_permyriad=0),
        ),
    )


def test_load_test_definition_requires_build_test_contract(tmp_path: Path) -> None:
    valid = _write_test_file(tmp_path / "valid.py")
    resolved, compiled = load_test_definition(valid)

    assert resolved == valid.resolve()
    assert compiled.name == "Worker example"

    missing = tmp_path / "missing.py"
    missing.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(DefinitionFileError, match=r"build_test\(\)"):
        load_test_definition(missing)

    wrong = tmp_path / "wrong.py"
    wrong.write_text("def build_test():\n    return object()\n", encoding="utf-8")
    with pytest.raises(DefinitionFileError, match="hilrig.Test"):
        load_test_definition(wrong)


def test_basic_digital_example_satisfies_terminal_contract() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "basic_digital_test.py"

    _, compiled = load_test_definition(example)

    assert compiled.name == "Digital input/output example"
    assert compiled.instructions
    assert compiled.assertions


def test_worker_runs_test_writes_artifacts_and_accepts_another_run(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "automatic.py")
    connections: list[_AutomaticConnection] = []

    def connection_factory() -> _AutomaticConnection:
        connection = _AutomaticConnection()
        connections.append(connection)
        return connection

    worker = ProtocolWorker(connection_factory=connection_factory, poll_interval_s=0)
    try:
        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        first = worker.snapshot()

        assert first.state is WorkerState.COMPLETED
        assert first.received_tick_count == first.expected_tick_count == 100
        assert first.verdict == "inconclusive"
        assert first.output_directory is not None
        assert connections[0].start_called
        assert connections[0].closed
        for filename in (
            "test-definition.json",
            "test-review.xlsx",
            "captured-run.sqlite3",
            "run-manifest.json",
            "run-metadata.md",
            "fixed-results.csv",
            "communication-results.csv",
            "application-errors.csv",
            "evaluation-report.json",
            "evaluation-report.md",
        ):
            assert (first.output_directory / filename).is_file()

        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        second = worker.snapshot()
        assert second.state is WorkerState.COMPLETED
        assert second.output_directory != first.output_directory
        assert len(connections) == 2
    finally:
        worker.shutdown()


def test_worker_retries_reset_while_hardware_cleanup_is_finishing(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "reset-retry.py")
    connection = _AutomaticConnection(
        reset_responses=(
            (ResponseOutcome.REJECTED, ResponseReason.HARDWARE_NOT_READY),
            (ResponseOutcome.COMPLETED, ResponseReason.NONE),
        )
    )
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0)
    try:
        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        assert worker.snapshot().state is WorkerState.COMPLETED
        assert connection.reset_attempts == 2
    finally:
        worker.shutdown()


def test_worker_keeps_reports_when_reset_recovery_is_exhausted(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "reset-fails.py")
    connection = _AutomaticConnection(
        reset_responses=(
            (ResponseOutcome.REJECTED, ResponseReason.HARDWARE_NOT_READY),
            (ResponseOutcome.REJECTED, ResponseReason.HARDWARE_NOT_READY),
            (ResponseOutcome.REJECTED, ResponseReason.HARDWARE_NOT_READY),
            (ResponseOutcome.REJECTED, ResponseReason.HARDWARE_NOT_READY),
        )
    )
    worker = ProtocolWorker(
        connection_factory=lambda: connection,
        poll_interval_s=0,
        reset_retry_delay_s=0,
    )
    try:
        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        snapshot = worker.snapshot()

        assert snapshot.state is WorkerState.FAILED
        assert snapshot.output_directory is not None
        assert (snapshot.output_directory / "captured-run.sqlite3").is_file()
        assert (snapshot.output_directory / "run-metadata.md").is_file()
        assert (snapshot.output_directory / "evaluation-report.md").is_file()
        failure = (snapshot.output_directory / "run-error.txt").read_text(encoding="utf-8")
        assert "RecoveryError" in failure
    finally:
        worker.shutdown()


def test_worker_returns_rejected_report_as_completed_attempt(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "rejected.py")
    connection = _AutomaticConnection(reported_outcome="REJECTED")
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0)
    try:
        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        snapshot = worker.snapshot()
        assert snapshot.state is WorkerState.COMPLETED
        assert "REJECTED" in snapshot.detail
        database = snapshot.output_directory / "captured-run.sqlite3"
        from hilrig import CapturedRunIR

        captured = CapturedRunIR.open(database)
        assert captured.report.run_outcome == "REJECTED"
        assert captured.report.execution_outcome == "NOT_STARTED"
        assert captured.report.result_status == "UNAVAILABLE"
    finally:
        worker.shutdown()


def test_worker_abort_retains_partial_capture(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "blocking.py")
    connection = _AutomaticConnection(block_results=True)
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0.001)
    try:
        assert worker.submit(definition)
        _wait_for_state(worker, WorkerState.RUNNING)
        assert worker.abort()
        assert worker.wait_until_idle(5)
        snapshot = worker.snapshot()

        assert snapshot.state is WorkerState.ABORTED
        assert connection.abort_called
        assert connection.closed
        assert snapshot.output_directory is not None
        assert (snapshot.output_directory / "captured-run.sqlite3").is_file()
        assert (snapshot.output_directory / "run-metadata.md").is_file()
        assert (snapshot.output_directory / "evaluation-report.md").is_file()
    finally:
        worker.shutdown()


def test_worker_steps_configuration_and_start_then_finishes_results(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "stepped.py")
    connection = _AutomaticConnection()
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0.001)
    try:
        assert worker.submit(definition, stepped=True)
        _wait_for_next_operation(worker, "configuration")
        assert worker.step()
        _wait_for_next_operation(worker, "START")
        assert worker.step()
        assert worker.wait_until_idle(5)

        snapshot = worker.snapshot()
        assert snapshot.state is WorkerState.COMPLETED
        assert snapshot.stepped
        assert connection.start_called
    finally:
        worker.shutdown()


def test_worker_continue_makes_remainder_automatic(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "continued.py")
    connection = _AutomaticConnection()
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0.001)
    try:
        assert worker.submit(definition, stepped=True)
        _wait_for_next_operation(worker, "configuration")
        assert worker.continue_run()
        assert worker.wait_until_idle(5)

        assert worker.snapshot().state is WorkerState.COMPLETED
        assert connection.start_called
    finally:
        worker.shutdown()


def test_worker_announces_and_retains_run_application_errors(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "application-error.py")
    error = ApplicationErrorRecord(
        category="execution",
        detail="27",
        recoverable=True,
        tick=3,
        diagnostic_data=b"timing slip",
    )
    connection = _AutomaticConnection(application_errors=(error,))
    notifications: list[str] = []
    worker = ProtocolWorker(
        connection_factory=lambda: connection,
        notification_callback=notifications.append,
        poll_interval_s=0.001,
    )
    try:
        assert worker.submit(definition, stepped=True)
        _wait_for_next_operation(worker, "configuration")
        assert worker.continue_run()
        assert worker.wait_until_idle(5)

        inbox = worker.run_inbox()
        assert worker.snapshot().inbox_count == 1
        assert "category=execution" in inbox[0]
        assert "recoverable=true" in inbox[0]
        assert "tick=3" in inbox[0]
        assert "timing slip" in inbox[0]
        assert any(message.startswith("RIG Application Error:") for message in notifications)

        assert worker.clear_run_inbox()
        assert worker.run_inbox() == ()
        assert worker.snapshot().inbox_count == 0
    finally:
        worker.shutdown()


def test_worker_owns_persistent_manual_session_and_sends_message() -> None:
    created: list[tuple[object, _ManualConnection]] = []

    def manual_factory(*, serial_settings, skip_system_info):
        connection = _ManualConnection(
            device=serial_settings.device,
            skip_system_info=skip_system_info,
        )
        created.append((serial_settings, connection))
        return connection

    worker = ProtocolWorker(
        manual_connection_factory=manual_factory,
        poll_interval_s=0.001,
    )
    example = (
        Path(__file__).resolve().parents[1] / "examples" / "manual_messages" / "instruction.json"
    )
    try:
        assert worker.manual_connect(device="COM2", skip_system_info=True)
        _wait_for_manual_state(worker, ManualSessionState.READY)
        assert created[0][0].device == "COM2"
        assert created[0][1].skip_system_info
        assert not worker.submit(example)

        assert worker.manual_send(example, transport_only=True)
        _wait_for_manual_result(worker)
        snapshot = worker.manual_snapshot()
        assert snapshot.last_result is not None
        assert snapshot.last_result.success
        assert not snapshot.last_result.application_response_required

        application_test_id = 0x00112233445566778899AABBCCDDEEFF
        assert worker.manual_finalize(application_test_id)
        _wait_for_manual_result(worker)
        snapshot = worker.manual_snapshot()
        assert snapshot.last_result is not None
        assert snapshot.last_result.success
        assert snapshot.last_result.application_response_required
        finalize = created[0][1].queued_messages[-1]
        assert type(finalize.message) is FinalizeTestUpload
        assert finalize.message.test_id.bytes == application_test_id.to_bytes(16, "big")
        assert finalize.response.scope is FakeProtocol.ResponseScope.COMPLETE_TEST

        assert worker.manual_disconnect()
        assert worker.wait_until_idle(5)
        assert worker.manual_snapshot().state is ManualSessionState.DISCONNECTED
        assert created[0][1].closed
    finally:
        worker.shutdown()


def _wait_for_state(worker: ProtocolWorker, state: WorkerState) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if worker.snapshot().state is state:
            return
        time.sleep(0.001)
    raise AssertionError(f"worker did not reach {state.value}: {worker.snapshot()}")


def _wait_for_next_operation(worker: ProtocolWorker, label: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = worker.snapshot()
        if snapshot.state is WorkerState.WAITING_FOR_OPERATOR and snapshot.next_operation == label:
            return
        time.sleep(0.001)
    raise AssertionError(f"worker did not pause before {label}: {worker.snapshot()}")


def _wait_for_manual_state(worker: ProtocolWorker, state: ManualSessionState) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if worker.manual_snapshot().state is state:
            return
        time.sleep(0.001)
    raise AssertionError(f"manual session did not reach {state.value}: {worker.manual_snapshot()}")


def _wait_for_manual_result(worker: ProtocolWorker) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = worker.manual_snapshot()
        if snapshot.state is ManualSessionState.READY and snapshot.last_result is not None:
            return
        time.sleep(0.001)
    raise AssertionError(f"manual send did not complete: {worker.manual_snapshot()}")


def test_worker_handles_upload_rejection_retaining_inbox_error(tmp_path: Path) -> None:
    definition = _write_test_file(tmp_path / "failing_upload.py")
    connection = _AutomaticConnection(fail_during_upload=True)
    worker = ProtocolWorker(connection_factory=lambda: connection, poll_interval_s=0)
    try:
        assert worker.submit(definition)
        assert worker.wait_until_idle(5)
        snapshot = worker.snapshot()

        assert snapshot.state is WorkerState.FAILED
        assert snapshot.inbox_count == 1
        assert "ProtocolSessionError" in (snapshot.error or "")

        inbox = worker.run_inbox()
        assert len(inbox) == 1
        assert "tick_rejected" in inbox[0]
        assert "INVALID_TICK" in inbox[0]
    finally:
        worker.shutdown()
