"""Automatic and operator-stepped execution on one protocol-owner thread."""

from __future__ import annotations

import queue
import runpy
import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from hilrig.api import Test
from hilrig.evaluation import evaluate_assertions
from hilrig.models.execution import CompiledTestIR
from hilrig.protocol import (
    FixedIOProtocolConnection,
    ProtocolWorkflowState,
    UploadAdvanceMode,
)
from hilrig.results import CapturedRunBuilder, CapturedRunIR, CaptureStatus


class DefinitionFileError(ValueError):
    """A Python test-definition file does not satisfy the terminal contract."""


class WorkerState(str, Enum):
    """High-level state exposed by the automatic protocol worker."""

    IDLE = "idle"
    QUEUED = "queued"
    LOADING = "loading"
    CONNECTING = "connecting"
    WAITING_FOR_OPERATOR = "waiting_for_operator"
    UPLOADING = "uploading"
    STARTING = "starting"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ABORTING = "aborting"
    ABORTED = "aborted"
    FAILED = "failed"
    STOPPED = "stopped"


_BUSY_STATES = frozenset(
    {
        WorkerState.QUEUED,
        WorkerState.LOADING,
        WorkerState.CONNECTING,
        WorkerState.WAITING_FOR_OPERATOR,
        WorkerState.UPLOADING,
        WorkerState.STARTING,
        WorkerState.RUNNING,
        WorkerState.FINALIZING,
        WorkerState.ABORTING,
    }
)
_ABORTABLE_STATES = _BUSY_STATES - {WorkerState.FINALIZING}


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """Thread-safe immutable status returned to the terminal."""

    state: WorkerState = WorkerState.IDLE
    detail: str = "No test has been run."
    test_path: Path | None = None
    test_name: str | None = None
    output_directory: Path | None = None
    protocol_state: str | None = None
    received_tick_count: int = 0
    expected_tick_count: int = 0
    verdict: str | None = None
    error: str | None = None
    stepped: bool = False
    next_operation: str | None = None

    @property
    def busy(self) -> bool:
        return self.state in _BUSY_STATES


@dataclass(frozen=True, slots=True)
class _RunCommand:
    path: Path
    stepped: bool


class _AdvanceCommand(str, Enum):
    STEP = "step"
    CONTINUE = "continue"


class _StopWorker:
    pass


class _RunAborted(Exception):
    pass


_Command = _RunCommand | _StopWorker
_ConnectionFactory = Callable[[], Any]
_NotificationCallback = Callable[[str], None]


class ProtocolWorker:
    """Execute terminal runs while owning all protocol objects on one thread.

    The public methods only exchange immutable status and thread-safe signals. The
    worker thread creates, services, and closes ``FixedIOProtocolConnection`` so the
    protocol wrapper's creating-thread ownership rule is never violated. Future
    operation gates can therefore be added as worker commands without changing the
    terminal's threading model.
    """

    def __init__(
        self,
        *,
        connection_factory: _ConnectionFactory | None = None,
        notification_callback: _NotificationCallback | None = None,
        poll_interval_s: float = 0.001,
        abort_timeout_s: float = 3.0,
    ) -> None:
        if poll_interval_s < 0:
            raise ValueError("poll_interval_s must be non-negative")
        if abort_timeout_s <= 0:
            raise ValueError("abort_timeout_s must be positive")
        self._connection_factory = connection_factory or FixedIOProtocolConnection.connect
        self._notification_callback = notification_callback
        self._poll_interval_s = float(poll_interval_s)
        self._abort_timeout_s = float(abort_timeout_s)
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._advance_commands: queue.Queue[_AdvanceCommand] = queue.Queue()
        self._snapshot_lock = threading.Lock()
        self._snapshot = RunSnapshot()
        self._abort_requested = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._started = False
        self._stopping = False
        self._advance_request_pending = False
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="hilrig-protocol-worker",
            daemon=True,
        )

    def start(self) -> None:
        """Start the persistent worker thread once."""
        with self._snapshot_lock:
            if self._started:
                return
            if self._stopping:
                raise RuntimeError("The protocol worker is stopping")
            self._started = True
            self._thread.start()

    def submit(self, path: str | Path, *, stepped: bool = False) -> bool:
        """Queue one run, optionally pausing before each semantic upload operation."""
        if not isinstance(stepped, bool):
            raise TypeError("stepped must be a bool")
        self.start()
        candidate = Path(path).expanduser()
        with self._snapshot_lock:
            if self._stopping or self._snapshot.busy:
                return False
            self._abort_requested.clear()
            self._discard_advance_commands()
            self._advance_request_pending = False
            self._idle.clear()
            self._snapshot = RunSnapshot(
                state=WorkerState.QUEUED,
                detail="Waiting for the protocol worker.",
                test_path=candidate,
                stepped=stepped,
            )
            self._commands.put(_RunCommand(candidate, stepped))
        return True

    def step(self) -> bool:
        """Release one operation when a stepped run is paused at its gate."""
        return self._request_advance(_AdvanceCommand.STEP)

    def continue_run(self) -> bool:
        """Release the gate and make the remainder of the run automatic."""
        return self._request_advance(_AdvanceCommand.CONTINUE)

    def abort(self) -> bool:
        """Request cancellation of the active run without touching protocol objects."""
        with self._snapshot_lock:
            if self._snapshot.state not in _ABORTABLE_STATES:
                return False
            self._abort_requested.set()
            self._snapshot = _updated_snapshot(
                self._snapshot,
                state=WorkerState.ABORTING,
                detail="Abort requested; waiting for the protocol worker.",
            )
        return True

    def _request_advance(self, command: _AdvanceCommand) -> bool:
        with self._snapshot_lock:
            if (
                self._snapshot.state is not WorkerState.WAITING_FOR_OPERATOR
                or self._snapshot.next_operation is None
                or self._advance_request_pending
            ):
                return False
            self._advance_request_pending = True
            self._snapshot = _updated_snapshot(
                self._snapshot,
                detail=f"{command.value.capitalize()} requested for "
                f"{self._snapshot.next_operation}.",
            )
            self._advance_commands.put(command)
        return True

    def snapshot(self) -> RunSnapshot:
        """Return the latest immutable run status."""
        with self._snapshot_lock:
            return self._snapshot

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        """Wait for the current run to reach a terminal state."""
        return self._idle.wait(timeout)

    def shutdown(self, *, timeout: float | None = 10.0) -> None:
        """Abort any active run and stop the worker thread."""
        with self._snapshot_lock:
            if self._stopping:
                thread = self._thread
            else:
                self._stopping = True
                if self._snapshot.busy:
                    self._abort_requested.set()
                self._commands.put(_StopWorker())
                thread = self._thread
        if self._started:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("The protocol worker did not stop in time")

    def _worker_loop(self) -> None:
        while True:
            command = self._commands.get()
            if isinstance(command, _StopWorker):
                break
            try:
                self._execute_run(command.path, stepped=command.stepped)
            finally:
                self._idle.set()
        self._set_snapshot(state=WorkerState.STOPPED, detail="Protocol worker stopped.")

    def _execute_run(self, requested_path: Path, *, stepped: bool) -> None:
        connection: Any | None = None
        builder: CapturedRunBuilder | None = None
        output_directory: Path | None = None
        received_tick_count = 0
        try:
            self._set_snapshot(
                state=WorkerState.LOADING,
                detail="Loading and compiling the test definition.",
                test_path=requested_path,
                stepped=stepped,
                next_operation=None,
            )
            path, compiled = load_test_definition(requested_path)
            if compiled.start_mode == "EXTERNAL_TRIGGER":
                raise DefinitionFileError(
                    "Terminal runs do not yet support EXTERNAL_TRIGGER start mode"
                )
            run_id = secrets.randbits(128)
            output_directory = create_run_directory(path, compiled, run_id=run_id)
            compiled.write_json(output_directory / "test-definition.json")
            compiled.write_excel(output_directory / "test-review.xlsx")
            self._set_snapshot(
                state=WorkerState.CONNECTING,
                detail="Connecting to the RIG and confirming protocol compatibility.",
                test_path=path,
                test_name=compiled.name,
                output_directory=output_directory,
                expected_tick_count=compiled.expected_tick_count,
            )
            self._notify(f"Loaded {compiled.name!r}; connecting to the RIG...")
            self._raise_if_aborted()

            connection = self._connection_factory()
            while not connection.session_confirmed:
                self._raise_if_aborted()
                connection.service()
                self._record_protocol_progress(connection, received_tick_count)
                self._pause()

            info = connection.session_info
            attempt = compiled.new_upload_attempt()
            builder = CapturedRunBuilder.from_compiled_test(
                output_directory / "captured-run.sqlite3",
                compiled,
                upload_attempt=attempt,
                run_id=run_id,
                application_protocol_version=info.protocol_version,
                firmware_version=info.firmware_version,
            )
            connection.bind_result_builder(builder)
            connection.queue_upload(
                compiled,
                upload_attempt=attempt,
                advance_mode=(
                    UploadAdvanceMode.OPERATOR_GATED
                    if stepped
                    else UploadAdvanceMode.AUTOMATIC
                ),
            )
            self._notify(f"Connected to firmware {info.firmware_version}; uploading the test...")

            host_start_requested = False
            while not connection.results_complete:
                self._raise_if_aborted()
                advance_applied = self._apply_operator_command(connection)
                service_report = connection.service()
                received_tick_count += len(service_report.stored_tick_results)
                if (
                    connection.workflow_state is ProtocolWorkflowState.READY_TO_START
                    and compiled.start_mode == "HOST_COMMAND"
                    and not host_start_requested
                ):
                    connection.start()
                    host_start_requested = True
                self._record_protocol_progress(
                    connection,
                    received_tick_count,
                )
                if advance_applied:
                    with self._snapshot_lock:
                        self._advance_request_pending = False
                self._pause()

            self._set_snapshot(
                state=WorkerState.FINALIZING,
                detail="Finalizing captured data and generating reports.",
                protocol_state=connection.workflow_state.value,
                received_tick_count=received_tick_count,
            )
            captured_run = builder.finalize()
            builder = None
            verdict = write_run_artifacts(captured_run, output_directory)
            self._set_snapshot(
                state=WorkerState.COMPLETED,
                detail=f"Run complete: {verdict.upper()}.",
                protocol_state=connection.workflow_state.value,
                received_tick_count=received_tick_count,
                verdict=verdict,
            )
            self._notify(f"Run complete: {verdict.upper()}. Results: {output_directory}")
        except _RunAborted:
            if connection is not None:
                self._attempt_protocol_abort(connection)
            if builder is not None:
                captured_run = builder.abort()
                builder = None
                if output_directory is not None:
                    write_run_artifacts(captured_run, output_directory)
            self._set_snapshot(
                state=WorkerState.ABORTED,
                detail="Run aborted by the user.",
                received_tick_count=received_tick_count,
            )
            self._notify(
                "Run aborted."
                + (f" Partial results: {output_directory}" if output_directory else "")
            )
        except BaseException as error:
            if builder is not None:
                try:
                    captured_run = builder.finalize(status=CaptureStatus.PROTOCOL_ERROR)
                    builder = None
                    if output_directory is not None:
                        write_run_artifacts(captured_run, output_directory)
                except BaseException:
                    pass
            if output_directory is not None:
                _write_failure_file(output_directory, error)
            self._set_snapshot(
                state=WorkerState.FAILED,
                detail="Run failed.",
                received_tick_count=received_tick_count,
                error=f"{type(error).__name__}: {error}",
            )
            self._notify(f"Run failed: {type(error).__name__}: {error}")
        finally:
            if connection is not None:
                with suppress(BaseException):
                    connection.close()
            self._abort_requested.clear()
            with self._snapshot_lock:
                self._advance_request_pending = False
            self._discard_advance_commands()

    def _record_protocol_progress(
        self,
        connection: Any,
        received_tick_count: int,
    ) -> None:
        protocol_state = connection.workflow_state
        previous = self.snapshot()
        next_operation = None
        if connection.waiting_for_operator:
            operation = connection.next_upload_operation
            next_operation = operation.label if operation is not None else None
            state = WorkerState.WAITING_FOR_OPERATOR
            detail = f"Paused before {next_operation}; enter 'step' or 'continue'."
        else:
            state, detail = _worker_state_for_protocol(protocol_state, received_tick_count)
        self._set_snapshot(
            state=state,
            detail=detail,
            protocol_state=protocol_state.value,
            received_tick_count=received_tick_count,
            next_operation=next_operation,
        )
        if state is WorkerState.WAITING_FOR_OPERATOR and (
            previous.state is not WorkerState.WAITING_FOR_OPERATOR
            or previous.next_operation != next_operation
        ):
            self._notify(
                f"Paused before {next_operation}. Enter 'step' to release it or "
                "'continue' to finish automatically."
            )

    def _apply_operator_command(self, connection: Any) -> bool:
        if not connection.waiting_for_operator:
            return False
        try:
            command = self._advance_commands.get_nowait()
        except queue.Empty:
            return False
        if command is _AdvanceCommand.STEP:
            operation = connection.release_next_operation()
            self._notify(f"Released {operation.label}.")
        else:
            operation = connection.continue_upload()
            self._notify(
                f"Released {operation.label}; the remainder of the run is automatic."
            )
        return True

    def _discard_advance_commands(self) -> None:
        while True:
            try:
                self._advance_commands.get_nowait()
            except queue.Empty:
                return

    def _attempt_protocol_abort(self, connection: Any) -> None:
        if connection.active_upload is None:
            return
        try:
            connection.abort()
        except BaseException:
            return
        deadline = time.monotonic() + self._abort_timeout_s
        while time.monotonic() < deadline:
            try:
                connection.service()
            except BaseException:
                return
            if connection.workflow_state is ProtocolWorkflowState.ABORTED:
                return
            self._pause(ignore_abort=True)

    def _raise_if_aborted(self) -> None:
        if self._abort_requested.is_set():
            raise _RunAborted

    def _pause(self, *, ignore_abort: bool = False) -> None:
        if self._poll_interval_s:
            self._abort_requested.wait(self._poll_interval_s)
        if not ignore_abort:
            self._raise_if_aborted()

    def _set_snapshot(self, **changes: object) -> None:
        with self._snapshot_lock:
            self._snapshot = _updated_snapshot(self._snapshot, **changes)

    def _notify(self, message: str) -> None:
        if self._notification_callback is not None:
            # Terminal rendering must never terminate a hardware run.
            with suppress(BaseException):
                self._notification_callback(message)


def load_test_definition(path: str | Path) -> tuple[Path, CompiledTestIR]:
    """Load trusted Python code containing ``build_test() -> Test`` and compile it."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise DefinitionFileError(f"Test file does not exist: {resolved}")
    if resolved.suffix.lower() != ".py":
        raise DefinitionFileError("Test definition path must end in .py")
    try:
        namespace = runpy.run_path(
            str(resolved),
            run_name=f"__hilrig_test_{secrets.token_hex(8)}__",
        )
    except BaseException as error:
        raise DefinitionFileError(f"Could not load {resolved}: {error}") from error
    factory = namespace.get("build_test")
    if not callable(factory):
        raise DefinitionFileError("Test file must define a callable build_test() function")
    try:
        test = factory()
    except BaseException as error:
        raise DefinitionFileError(f"build_test() failed: {error}") from error
    if not isinstance(test, Test):
        raise DefinitionFileError("build_test() must return a hilrig.Test instance")
    try:
        return resolved, test.compile()
    except BaseException as error:
        raise DefinitionFileError(f"Test compilation failed: {error}") from error


def create_run_directory(
    test_path: Path,
    compiled: CompiledTestIR,
    *,
    run_id: int,
) -> Path:
    """Create one unique run directory beside the test file."""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    slug = _slug(compiled.name)
    output = test_path.parent / "runs" / f"{timestamp}-{slug}-{run_id:032x}"
    output.mkdir(parents=True, exist_ok=False)
    return output.resolve()


def write_run_artifacts(captured_run: CapturedRunIR, output_directory: Path) -> str:
    """Write all standard capture exports and evaluation reports."""
    captured_run.write_manifest_json(output_directory / "run-manifest.json")
    captured_run.write_fixed_results_csv(output_directory / "fixed-results.csv")
    captured_run.write_communication_results_csv(output_directory / "communication-results.csv")
    captured_run.write_application_errors_csv(output_directory / "application-errors.csv")
    report = evaluate_assertions(captured_run)
    report.write_json(output_directory / "evaluation-report.json")
    report.write_markdown(output_directory / "evaluation-report.md")
    return report.verdict.value


def _worker_state_for_protocol(
    protocol_state: ProtocolWorkflowState,
    received_tick_count: int,
) -> tuple[WorkerState, str]:
    if protocol_state in {
        ProtocolWorkflowState.CONNECTING,
        ProtocolWorkflowState.DISCOVERING,
        ProtocolWorkflowState.READY,
    }:
        return WorkerState.CONNECTING, "Confirming the protocol session."
    if protocol_state in {
        ProtocolWorkflowState.CONFIGURING,
        ProtocolWorkflowState.UPLOADING,
        ProtocolWorkflowState.VALIDATING,
        ProtocolWorkflowState.READY_TO_START,
    }:
        return WorkerState.UPLOADING, "Uploading and validating the test."
    if protocol_state is ProtocolWorkflowState.STARTING:
        return WorkerState.STARTING, "Starting test execution."
    if protocol_state in {
        ProtocolWorkflowState.RUNNING,
        ProtocolWorkflowState.RESULTS_COMPLETE,
    }:
        return WorkerState.RUNNING, f"Receiving test results ({received_tick_count} received)."
    if protocol_state is ProtocolWorkflowState.ABORTING:
        return WorkerState.ABORTING, "Waiting for firmware to complete ABORT."
    if protocol_state is ProtocolWorkflowState.ABORTED:
        return WorkerState.ABORTED, "Firmware completed ABORT."
    if protocol_state is ProtocolWorkflowState.FAILED:
        return WorkerState.FAILED, "The protocol workflow failed."
    return WorkerState.RUNNING, protocol_state.value


def _updated_snapshot(snapshot: RunSnapshot, **changes: object) -> RunSnapshot:
    values = {
        "state": snapshot.state,
        "detail": snapshot.detail,
        "test_path": snapshot.test_path,
        "test_name": snapshot.test_name,
        "output_directory": snapshot.output_directory,
        "protocol_state": snapshot.protocol_state,
        "received_tick_count": snapshot.received_tick_count,
        "expected_tick_count": snapshot.expected_tick_count,
        "verdict": snapshot.verdict,
        "error": snapshot.error,
        "stepped": snapshot.stepped,
        "next_operation": snapshot.next_operation,
    }
    values.update(changes)
    return RunSnapshot(**values)  # type: ignore[arg-type]


def _slug(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    collapsed = "-".join(part for part in slug.split("-") if part)
    return collapsed[:60] or "test"


def _write_failure_file(output_directory: Path, error: BaseException) -> None:
    with suppress(OSError):
        (output_directory / "run-error.txt").write_text(
            f"{type(error).__name__}: {error}\n",
            encoding="utf-8",
        )


__all__ = [
    "ProtocolWorker",
    "RunSnapshot",
    "DefinitionFileError",
    "WorkerState",
    "create_run_directory",
    "load_test_definition",
    "write_run_artifacts",
]
