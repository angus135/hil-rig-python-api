from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hilrig import PWMMeasurement, TickResult
from hilrig.protocol import ProtocolWorkflowState
from hilrig.runner import DefinitionFileError, ProtocolWorker, WorkerState, load_test_definition


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

    def service(self):
        stored = ()
        if not self.session_confirmed:
            self.session_confirmed = True
            self.session_info = SimpleNamespace(
                protocol_version="0.2.0",
                firmware_version="1.2.3",
            )
            self.workflow_state = ProtocolWorkflowState.READY
        elif self.workflow_state is ProtocolWorkflowState.CONFIGURING:
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

    def queue_upload(self, compiled, *, upload_attempt) -> None:
        self.compiled = compiled
        self.active_upload = SimpleNamespace(upload_attempt=upload_attempt)
        self.workflow_state = ProtocolWorkflowState.CONFIGURING

    def start(self) -> None:
        self.start_called = True
        self.workflow_state = ProtocolWorkflowState.STARTING

    def abort(self) -> None:
        self.abort_called = True
        self.workflow_state = ProtocolWorkflowState.ABORTING

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


def _wait_for_state(worker: ProtocolWorker, state: WorkerState) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if worker.snapshot().state is state:
            return
        time.sleep(0.001)
    raise AssertionError(f"worker did not reach {state.value}: {worker.snapshot()}")
