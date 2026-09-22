from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from protocol_fakes import FakeProtocol, FinalizeTestUpload

from hilrig import FixedIOProtocolAdapter, ManualSendResult, PWMMeasurement, TickResult
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
    def __init__(self, *, block_results: bool = False) -> None:
        self.block_results = block_results
        self.session_confirmed = False
        self.session_info = None
        self.workflow_state = ProtocolWorkflowState.CONNECTING
        self.active_upload = None
        self.results_complete = False
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

    def service(self):
        stored = ()
        if not self.session_confirmed:
            self.session_confirmed = True
            self.session_info = SimpleNamespace(
                protocol_version="0.3.0",
                firmware_version="1.2.3",
            )
            self.workflow_state = ProtocolWorkflowState.READY
        elif self.workflow_state is ProtocolWorkflowState.CONFIGURING:
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
            stored = tuple(_tick_result(tick) for tick in range(self.compiled.expected_tick_count))
            for result in stored:
                self.builder.add_tick_result(result)
            self.workflow_state = ProtocolWorkflowState.RESULTS_COMPLETE
            self.results_complete = True
        elif self.workflow_state is ProtocolWorkflowState.ABORTING:
            self.workflow_state = ProtocolWorkflowState.ABORTED
        return SimpleNamespace(stored_tick_results=stored)

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
                    protocol_version="0.3.0",
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
                    "Transport delivery confirmed; Application response was not required."
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
        assert first.received_tick_count == first.expected_tick_count == 101
        assert first.verdict == "inconclusive"
        assert first.output_directory is not None
        assert connections[0].start_called
        assert connections[0].closed
        for filename in (
            "test-definition.json",
            "test-review.xlsx",
            "captured-run.sqlite3",
            "run-manifest.json",
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
