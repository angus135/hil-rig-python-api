"""Asynchronous, batched construction of a persistent captured-run IR."""

from __future__ import annotations

import queue
import secrets
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from hilrig.exceptions import CaptureStateError, CaptureStorageError
from hilrig.models.execution import CompiledTestIR
from hilrig.results.models import (
    ApplicationErrorRecord,
    CaptureStatus,
    CommunicationResult,
    TickResult,
    validate_uint128,
)
from hilrig.results.sqlite_store import SQLiteCaptureWriter, initialize_capture_database

if TYPE_CHECKING:
    from hilrig.results.ir import CapturedRunIR


@dataclass(slots=True)
class _ControlRequest:
    """A synchronous barrier sent through the writer queue."""

    action: str
    event: threading.Event
    status: CaptureStatus | None = None
    error: BaseException | None = None


_QueueItem = TickResult | CommunicationResult | ApplicationErrorRecord | _ControlRequest


class CapturedRunBuilder:
    """Accept typed result records and persist them on a background writer thread.

    Producers only enqueue validated Python records. One dedicated thread owns the
    SQLite connection, collects records until ``batch_size`` or ``flush_interval_s``
    is reached, and commits the complete mixed batch in one transaction. The queue is
    bounded and blocks rather than silently discarding test evidence.
    """

    @classmethod
    def from_compiled_test(
        cls,
        database_path: str | Path,
        compiled_test: CompiledTestIR,
        *,
        run_id: int | None = None,
        application_protocol_version: str | None = None,
        firmware_version: str | None = None,
        batch_size: int = 2_000,
        flush_interval_s: float = 0.025,
        queue_capacity: int = 20_000,
    ) -> CapturedRunBuilder:
        """Create a capture using timing and identity from one compiled outgoing IR."""
        if not isinstance(compiled_test, CompiledTestIR):
            raise TypeError("compiled_test must be a CompiledTestIR")
        return cls(
            database_path,
            test_id=compiled_test.test_id,
            test_name=compiled_test.name,
            tick_period_ns=compiled_test.tick_period_ns,
            expected_tick_count=compiled_test.expected_tick_count,
            run_id=run_id,
            application_protocol_version=application_protocol_version,
            firmware_version=firmware_version,
            batch_size=batch_size,
            flush_interval_s=flush_interval_s,
            queue_capacity=queue_capacity,
        )

    def __init__(
        self,
        database_path: str | Path,
        *,
        test_id: int,
        test_name: str,
        tick_period_ns: int,
        expected_tick_count: int,
        run_id: int | None = None,
        application_protocol_version: str | None = None,
        firmware_version: str | None = None,
        batch_size: int = 2_000,
        flush_interval_s: float = 0.025,
        queue_capacity: int = 20_000,
    ) -> None:
        self._database_path = Path(database_path).expanduser().resolve()
        self._test_id = validate_uint128(test_id, name="test_id")
        self._run_id = validate_uint128(
            secrets.randbits(128) if run_id is None else run_id,
            name="run_id",
        )
        if not isinstance(test_name, str) or not test_name.strip():
            raise ValueError("test_name must be a non-empty string")
        self._test_name = test_name
        self._tick_period_ns = _positive_int(tick_period_ns, name="tick_period_ns")
        self._expected_tick_count = _positive_int(
            expected_tick_count,
            name="expected_tick_count",
        )
        self._batch_size = _positive_int(batch_size, name="batch_size")
        if (
            not isinstance(flush_interval_s, (int, float))
            or isinstance(flush_interval_s, bool)
            or flush_interval_s <= 0
        ):
            raise ValueError("flush_interval_s must be a positive number")
        self._flush_interval_s = float(flush_interval_s)
        capacity = _positive_int(queue_capacity, name="queue_capacity")

        initialize_capture_database(
            self._database_path,
            test_id=self._test_id,
            run_id=self._run_id,
            test_name=self._test_name,
            tick_period_ns=self._tick_period_ns,
            expected_tick_count=self._expected_tick_count,
            application_protocol_version=_optional_text(
                application_protocol_version,
                name="application_protocol_version",
            ),
            firmware_version=_optional_text(firmware_version, name="firmware_version"),
        )

        self._queue: queue.Queue[_QueueItem] = queue.Queue(maxsize=capacity)
        self._state_lock = threading.Lock()
        self._accepting = True
        self._worker_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._writer_loop,
            name=f"hilrig-capture-{self._run_id:032x}",
            daemon=True,
        )
        self._thread.start()

    @property
    def database_path(self) -> Path:
        """Return the absolute SQLite database path."""
        return self._database_path

    @property
    def test_id(self) -> int:
        return self._test_id

    @property
    def run_id(self) -> int:
        return self._run_id

    @property
    def queued_record_count(self) -> int:
        """Return an approximate queue depth for monitoring backpressure."""
        return self._queue.qsize()

    def add_tick_result(self, result: TickResult) -> None:
        """Queue one complete fixed-size result for a tick."""
        if not isinstance(result, TickResult):
            raise TypeError("result must be a TickResult")
        self._validate_tick_in_run(result.tick)
        self._submit(result)

    def add_communication_result(self, result: CommunicationResult) -> None:
        """Queue one raw variable-length communication capture."""
        if not isinstance(result, CommunicationResult):
            raise TypeError("result must be a CommunicationResult")
        self._validate_tick_in_run(result.tick)
        self._submit(result)

    def add_application_error(self, error: ApplicationErrorRecord) -> None:
        """Queue one application diagnostic without interpreting it."""
        if not isinstance(error, ApplicationErrorRecord):
            raise TypeError("error must be an ApplicationErrorRecord")
        if error.tick is not None:
            self._validate_tick_in_run(error.tick)
        self._submit(error)

    def flush(self) -> None:
        """Wait until all records submitted before this call are committed."""
        request = _ControlRequest(action="flush", event=threading.Event())
        self._submit(request)
        self._wait_for(request)

    def finalize(self, *, status: CaptureStatus | None = None) -> CapturedRunIR:
        """Flush, mark the run terminal, stop the writer, and open the read-only IR.

        With no explicit status, a run is ``COMPLETE`` only when it contains every
        expected fixed tick from zero through ``expected_tick_count - 1``. Otherwise
        it becomes ``INCOMPLETE``. Protocol/session failures can supply a more precise
        terminal status later.
        """
        if status is not None and not isinstance(status, CaptureStatus):
            raise TypeError("status must be a CaptureStatus value or None")
        request = _ControlRequest(action="finalize", event=threading.Event(), status=status)
        with self._state_lock:
            self._raise_worker_error()
            if not self._accepting:
                raise CaptureStateError("CapturedRunBuilder has already been finalized")
            self._accepting = False
        self._put(request, allow_closing=True)
        self._wait_for(request)
        self._thread.join()

        from hilrig.results.ir import CapturedRunIR

        return CapturedRunIR.open(self._database_path)

    def abort(self) -> CapturedRunIR:
        """Finalize a deliberately stopped run with ``ABORTED`` status."""
        return self.finalize(status=CaptureStatus.ABORTED)

    def __enter__(self) -> CapturedRunBuilder:
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        if not self._accepting:
            return
        if exception_type is None:
            self.finalize()
        else:
            # Do not hide the exception that caused the caller's context to exit.
            with suppress(CaptureStorageError):
                self.abort()

    def _validate_tick_in_run(self, tick: int) -> None:
        if tick >= self._expected_tick_count:
            raise ValueError(
                f"tick {tick} is outside expected range 0..{self._expected_tick_count - 1}"
            )

    def _submit(self, item: _QueueItem) -> None:
        with self._state_lock:
            self._raise_worker_error()
            if not self._accepting:
                raise CaptureStateError("CapturedRunBuilder is no longer accepting records")
        self._put(item)

    def _put(self, item: _QueueItem, *, allow_closing: bool = False) -> None:
        while True:
            self._raise_worker_error()
            if not allow_closing and not self._accepting:
                raise CaptureStateError("CapturedRunBuilder is no longer accepting records")
            try:
                self._queue.put(item, timeout=0.05)
                return
            except queue.Full:
                # Backpressure is intentional. Recheck worker health before waiting again.
                continue

    def _wait_for(self, request: _ControlRequest) -> None:
        while not request.event.wait(timeout=0.05):
            self._raise_worker_error()
        if request.error is not None:
            raise CaptureStorageError(
                "The capture writer could not complete the request"
            ) from request.error
        self._raise_worker_error()

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise CaptureStorageError(
                "The background capture writer failed"
            ) from self._worker_error

    def _writer_loop(self) -> None:
        writer: SQLiteCaptureWriter | None = None
        pending_ticks: list[TickResult] = []
        pending_communications: list[CommunicationResult] = []
        pending_errors: list[ApplicationErrorRecord] = []
        next_ordinal: dict[tuple[int, object, int], int] = {}
        batch_started_at: float | None = None
        active_request: _ControlRequest | None = None

        def record_count() -> int:
            return len(pending_ticks) + len(pending_communications) + len(pending_errors)

        def flush_pending() -> None:
            nonlocal batch_started_at
            if writer is None:
                raise RuntimeError("Capture writer was not initialized")
            writer.write_batch(pending_ticks, pending_communications, pending_errors)
            pending_ticks.clear()
            pending_communications.clear()
            pending_errors.clear()
            batch_started_at = None

        try:
            writer = SQLiteCaptureWriter(self._database_path)
            while True:
                timeout: float | None = None
                if batch_started_at is not None:
                    timeout = max(
                        0.0,
                        self._flush_interval_s - (time.monotonic() - batch_started_at),
                    )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    flush_pending()
                    continue

                try:
                    if isinstance(item, TickResult):
                        pending_ticks.append(item)
                    elif isinstance(item, CommunicationResult):
                        key = (item.tick, item.peripheral, item.channel)
                        ordinal = item.ordinal
                        if ordinal is None:
                            ordinal = next_ordinal.get(key, 0)
                        next_ordinal[key] = max(next_ordinal.get(key, 0), ordinal + 1)
                        pending_communications.append(replace(item, ordinal=ordinal))
                    elif isinstance(item, ApplicationErrorRecord):
                        pending_errors.append(item)
                    elif isinstance(item, _ControlRequest):
                        active_request = item
                        flush_pending()
                        if item.action == "finalize":
                            writer.finalize(item.status)
                            item.event.set()
                            active_request = None
                            return
                        if item.action != "flush":
                            raise RuntimeError(f"Unknown writer action: {item.action}")
                        item.event.set()
                        active_request = None
                    else:
                        raise TypeError(f"Unsupported queued record: {type(item).__name__}")

                    if record_count() and batch_started_at is None:
                        batch_started_at = time.monotonic()
                    if record_count() >= self._batch_size:
                        flush_pending()
                finally:
                    self._queue.task_done()
        except BaseException as error:
            self._worker_error = error
            if active_request is not None:
                active_request.error = error
                active_request.event.set()
            self._fail_queued_control_requests(error)
        finally:
            if writer is not None:
                writer.close()

    def _fail_queued_control_requests(self, error: BaseException) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _ControlRequest):
                    item.error = error
                    item.event.set()
            finally:
                self._queue.task_done()


def _positive_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")
    if not value.strip():
        raise ValueError(f"{name} must not be blank")
    return value
