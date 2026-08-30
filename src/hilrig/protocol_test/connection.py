"""Reusable synchronous ownership boundary between serial I/O and Transport."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from hil_rig_protocol import (
    LinkState,
    OperatingMode,
    Role,
    Transport,
    TransportConfig,
)

from .models import (
    UINT32_MASK,
    ConnectionCounters,
    ConnectionEvent,
    ReceivedApplicationMessage,
    SerialSelector,
    ServiceBudgets,
    ServiceResult,
)
from .serial_port import SerialIOError, SerialPort, SerialProvider, SerialWriteTimeout


class ConnectionError(RuntimeError):
    """Base error for the caller-owned Transport/serial integration."""


class ConnectionClosedError(ConnectionError):
    pass


class ConnectionOwnershipError(ConnectionError):
    pass


class LinkDisconnectedError(ConnectionError):
    pass


class CommitOutputError(ConnectionError):
    def __init__(self, status: object | None = None) -> None:
        detail = "exception" if status is None else getattr(status, "name", str(status))
        super().__init__(f"commit_output failed after serial accepted the complete item: {detail}")
        self.status = status


def monotonic_transport_ms() -> int:
    """Return the caller-owned wrapped uint32 Transport time."""
    return int(time.monotonic() * 1000) & UINT32_MASK


def hardware_test_transport_config() -> TransportConfig:
    """Return the HOST Transport configuration paired with the firmware test harness."""
    return TransportConfig(
        max_application_message_size=512,
        max_encoded_frame_size=640,
        session_seed=None,
        initial_reliable_sequence=0,
        connection_timeout_ms=0,
        retransmit_timeout_ms=100,
        max_retries=5,
    )


class ProtocolTestConnection:
    """Own one HOST Transport and one serial link on one creating thread.

    The class knows nothing about HRTP, ECHO, STATUS, request IDs, or scenarios.
    It only moves complete opaque Application messages and public Transport events.
    """

    def __init__(
        self,
        provider: SerialProvider,
        selector: SerialSelector,
        *,
        baud: int = 115200,
        transport: Any | None = None,
        transport_config: TransportConfig | None = None,
        budgets: ServiceBudgets | None = None,
        application_queue_capacity: int = 1,
        event_queue_capacity: int = 64,
        caller_input_limit: int | None = None,
        maximum_service_gap_ms: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(baud) is not int or baud <= 0:
            raise ValueError("baud must be a positive integer")
        if application_queue_capacity < 1 or event_queue_capacity < 1:
            raise ValueError("queue capacities must be positive")
        if maximum_service_gap_ms < 1:
            raise ValueError("maximum_service_gap_ms must be positive")
        self._owner_thread = threading.current_thread()
        self._provider = provider
        self._selector = selector
        self._baud = baud
        self._clock = clock
        self._budgets = budgets or ServiceBudgets()
        self._maximum_service_gap_ms = maximum_service_gap_ms
        requested_config = transport_config or hardware_test_transport_config()
        self._transport = transport or Transport(Role.HOST, requested_config)
        self._transport_config = self._transport.config
        default_input_limit = self._transport_config.max_encoded_frame_size * 4
        self._caller_input_limit = caller_input_limit or default_input_limit
        if self._caller_input_limit < self._transport_config.max_encoded_frame_size:
            raise ValueError("caller_input_limit must fit at least one encoded Transport frame")
        self._application_queue_capacity = application_queue_capacity
        self._event_queue_capacity = event_queue_capacity
        self._serial: SerialPort | None = None
        self._last_serial_identity: object | None = None
        self._retained_input = bytearray()
        self._staged_output: bytes | None = None
        self._staged_offset = 0
        self._staged_generation: int | None = None
        self._staged_started_ms: int | None = None
        self._application_messages: deque[ReceivedApplicationMessage] = deque()
        self._events: deque[ConnectionEvent] = deque()
        self._generation_counter = 0
        self._active_generation: int | None = None
        self._last_service_ms: int | None = None
        self._max_service_gap_ms = 0
        self._current_service_gap_ms = 0
        self._zero_receive_needed = True
        self._commit_faulted = False
        self._closed = False
        self._counters = ConnectionCounters()

    @property
    def transport_config(self) -> TransportConfig:
        return self._transport_config

    @property
    def link_generation(self) -> int | None:
        return self._active_generation

    @property
    def serial_identity(self) -> object | None:
        return self._last_serial_identity if self._serial is None else self._serial.identity

    @property
    def link_open(self) -> bool:
        return (
            self._serial is not None
            and self._serial.is_open
            and self._active_generation is not None
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def maximum_acceptable_service_gap_ms(self) -> int:
        return self._maximum_service_gap_ms

    def _require_owner(self) -> None:
        if threading.current_thread() is not self._owner_thread:
            raise ConnectionOwnershipError("connection may only be used by its creating thread")

    def _require_live(self) -> None:
        self._require_owner()
        if self._closed:
            raise ConnectionClosedError("connection is permanently closed")

    def _clock_ms(self) -> int:
        return int(self._clock() * 1000)

    @staticmethod
    def _transport_ms(clock_ms: int) -> int:
        return clock_ms & UINT32_MASK

    @staticmethod
    def _allowed(status: object, *names: str) -> bool:
        return getattr(status, "name", None) in names

    def _clear_external_state(self) -> None:
        self._retained_input.clear()
        self._staged_output = None
        self._staged_offset = 0
        self._staged_generation = None
        self._staged_started_ms = None
        self._application_messages.clear()
        self._zero_receive_needed = True
        self._commit_faulted = False

    def open_link(self) -> int:
        self._require_live()
        if self.link_open:
            raise ConnectionError("serial link is already open")
        device = self._provider.resolve(self._selector)
        serial_port = self._provider.open(device, self._baud)
        now = self._clock_ms()
        try:
            serial_port.reset_input_buffer()
            serial_port.reset_output_buffer()
            self._clear_external_state()
            self._events.clear()
            self._generation_counter += 1
            generation = self._generation_counter
            status = self._transport.notify_link_state(LinkState.CONNECTED, self._transport_ms(now))
            if not self._allowed(status, "OK", "CAPACITY_EXHAUSTED"):
                raise ConnectionError(
                    f"CONNECTED notification returned {getattr(status, 'name', status)}"
                )
        except Exception:
            try:
                serial_port.close()
            finally:
                self._active_generation = None
            raise
        self._serial = serial_port
        self._last_serial_identity = serial_port.identity
        self._active_generation = generation
        self._last_service_ms = None
        return generation

    def _close_physical(self) -> None:
        serial_port = self._serial
        self._serial = None
        if serial_port is not None:
            with contextlib.suppress(SerialIOError):
                serial_port.close()

    def close_link(self) -> None:
        self._require_live()
        if self._serial is None and self._active_generation is None:
            self._clear_external_state()
            return
        now = self._clock_ms()
        self._clear_external_state()
        self._active_generation = None
        try:
            status = self._transport.notify_link_state(
                LinkState.DISCONNECTED, self._transport_ms(now)
            )
            if not self._allowed(status, "OK", "CAPACITY_EXHAUSTED"):
                raise ConnectionError(
                    f"DISCONNECTED notification returned {getattr(status, 'name', status)}"
                )
        finally:
            self._close_physical()

    def _disconnect_after_serial_error(self) -> None:
        if self._serial is None and self._active_generation is None:
            return
        now = self._clock_ms()
        self._clear_external_state()
        self._active_generation = None
        try:
            self._transport.notify_link_state(LinkState.DISCONNECTED, self._transport_ms(now))
        finally:
            self._close_physical()

    def reset_transport(self) -> object:
        self._require_live()
        self._clear_external_state()
        self._events.clear()
        return self._transport.reset()

    def submit_application_data(self, data: bytes) -> object:
        self._require_live()
        if not self.link_open:
            raise LinkDisconnectedError("cannot submit while the serial link is disconnected")
        return self._transport.submit_application_data(data)

    def pop_application_message(self) -> ReceivedApplicationMessage | None:
        self._require_live()
        if not self._application_messages:
            return None
        message = self._application_messages.popleft()
        self._zero_receive_needed = True
        return message

    def pop_event(self) -> ConnectionEvent | None:
        self._require_live()
        if not self._events:
            return None
        event = self._events.popleft()
        self._zero_receive_needed = True
        return event

    def get_status(self) -> object:
        self._require_live()
        return self._transport.get_status()

    def _read_serial(self) -> bool:
        serial_port = self._serial
        if serial_port is None:
            return False
        remaining_capacity = self._caller_input_limit - len(self._retained_input)
        if remaining_capacity <= 0:
            self._counters.input_capacity_deferrals += 1
            return False
        try:
            waiting = serial_port.in_waiting
            if waiting <= 0:
                return False
            size = min(waiting, remaining_capacity, self._budgets.serial_read_bytes)
            data = serial_port.read(size)
        except SerialIOError as exc:
            self._counters.serial_read_failures += 1
            self._disconnect_after_serial_error()
            raise LinkDisconnectedError(str(exc)) from exc
        if not data:
            return False
        if len(data) > remaining_capacity:
            raise ConnectionError("serial abstraction returned more bytes than requested capacity")
        self._retained_input.extend(data)
        self._counters.serial_bytes_read += len(data)
        self._counters.retained_input_high_water = max(
            self._counters.retained_input_high_water, len(self._retained_input)
        )
        return True

    def _offer_receive(self, *, force_zero: bool = False) -> tuple[bool, bool]:
        if not self._retained_input and not force_zero:
            return False, False
        offered = b"" if force_zero else bytes(self._retained_input)
        if force_zero:
            self._counters.zero_byte_receive_calls += 1
        self._counters.bytes_offered += len(offered)
        result = self._transport.receive_bytes(offered)
        self._counters.count_status(self._counters.receive_statuses, result.status)
        consumed = result.bytes_consumed
        if consumed < 0 or consumed > len(offered):
            raise ConnectionError("Transport reported an invalid receive consumed count")
        self._counters.bytes_consumed += consumed
        if 0 < consumed < len(offered):
            self._counters.partial_consumption_calls += 1
        if consumed == 0:
            self._counters.zero_progress_receive_calls += 1
        if consumed:
            del self._retained_input[:consumed]
        partial = bool(self._retained_input)
        if partial:
            self._zero_receive_needed = True
        return consumed > 0, partial

    def _drain_events(self, now_ms: int) -> tuple[bool, bool]:
        progress = False
        exhausted = False
        for index in range(self._budgets.event_reads):
            if len(self._events) >= self._event_queue_capacity:
                exhausted = True
                break
            event = self._transport.read_event()
            if event is None:
                break
            self._events.append(
                ConnectionEvent(event, self._active_generation, self._transport_ms(now_ms))
            )
            self._counters.events_read += 1
            progress = True
            self._zero_receive_needed = True
            if index + 1 == self._budgets.event_reads:
                exhausted = True
        return progress, exhausted

    def _drain_application(self, now_ms: int) -> tuple[bool, bool]:
        if len(self._application_messages) >= self._application_queue_capacity:
            return False, True
        progress = False
        exhausted = False
        for index in range(self._budgets.application_reads):
            if len(self._application_messages) >= self._application_queue_capacity:
                exhausted = True
                break
            message = self._transport.read_application_data()
            if message is None:
                break
            if self._active_generation is None:
                # A disconnected generation cannot deliver to a future request.
                continue
            self._application_messages.append(
                ReceivedApplicationMessage(
                    message, self._active_generation, self._transport_ms(now_ms)
                )
            )
            self._counters.application_messages_read += 1
            progress = True
            self._zero_receive_needed = True
            if index + 1 == self._budgets.application_reads:
                exhausted = True
        return progress, exhausted

    def _service_output(self, now_ms: int) -> tuple[bool, bool]:
        serial_port = self._serial
        if serial_port is None or self._active_generation is None:
            return False, False
        progress = False
        exhausted = False
        for index in range(self._budgets.output_attempts):
            if self._staged_output is None:
                item = self._transport.peek_output()
                if item is None:
                    break
                self._staged_output = bytes(item)
                self._staged_offset = 0
                self._staged_generation = self._active_generation
                self._staged_started_ms = now_ms
                self._counters.output_items_peeked += 1
            if self._staged_generation != self._active_generation:
                self._staged_output = None
                self._staged_offset = 0
                self._staged_generation = None
                self._staged_started_ms = None
                break
            remaining = self._staged_output[self._staged_offset :]
            try:
                written = serial_port.write(remaining)
            except SerialWriteTimeout as exc:
                self._counters.write_timeouts += 1
                self._disconnect_after_serial_error()
                raise LinkDisconnectedError(str(exc)) from exc
            except SerialIOError as exc:
                self._counters.serial_write_failures += 1
                self._disconnect_after_serial_error()
                raise LinkDisconnectedError(str(exc)) from exc
            accepted = 0 if written is None else written
            if type(accepted) is not int or not 0 <= accepted <= len(remaining):
                raise ConnectionError("serial write returned an invalid accepted byte count")
            if accepted == 0:
                self._counters.zero_byte_writes += 1
                break
            self._staged_offset += accepted
            self._counters.output_bytes_accepted += accepted
            self._counters.serial_bytes_written += accepted
            progress = True
            if accepted < len(remaining):
                self._counters.partial_writes += 1
                if index + 1 == self._budgets.output_attempts:
                    exhausted = True
                continue
            if self._staged_offset != len(self._staged_output):
                continue
            if self._staged_started_ms is not None:
                self._counters.max_staged_duration_ms = max(
                    self._counters.max_staged_duration_ms, now_ms - self._staged_started_ms
                )
            commit_status: object | None = None
            try:
                commit_status = self._transport.commit_output(self._transport_ms(now_ms))
                if not self._allowed(commit_status, "OK"):
                    self._counters.commit_failures += 1
                    self._commit_faulted = True
                    raise CommitOutputError(commit_status)
                self._counters.output_items_committed += 1
                self._zero_receive_needed = True
            except CommitOutputError:
                raise
            except Exception as exc:
                self._counters.commit_failures += 1
                self._commit_faulted = True
                raise CommitOutputError() from exc
            finally:
                self._staged_output = None
                self._staged_offset = 0
                self._staged_generation = None
                self._staged_started_ms = None
            if index + 1 == self._budgets.output_attempts:
                exhausted = True
        return progress, exhausted

    def service_once(self) -> ServiceResult:
        self._require_live()
        if self._commit_faulted:
            raise CommitOutputError()
        if not self.link_open:
            if self._serial is not None or self._active_generation is not None:
                self._disconnect_after_serial_error()
            raise LinkDisconnectedError("serial link is disconnected")
        now_ms = self._clock_ms()
        gap = 0 if self._last_service_ms is None else max(0, now_ms - self._last_service_ms)
        self._last_service_ms = now_ms
        self._current_service_gap_ms = gap
        self._max_service_gap_ms = max(self._max_service_gap_ms, gap)
        self._counters.service_loops += 1
        if gap > self._maximum_service_gap_ms:
            self._counters.late_loops += 1

        progress = False
        budget_exhausted = False

        output_progress, output_exhausted = self._service_output(now_ms)
        progress |= output_progress
        budget_exhausted |= output_exhausted

        progress |= self._read_serial()

        receive_calls = 0
        if self._retained_input:
            while receive_calls < self._budgets.receive_calls and self._retained_input:
                received, partial = self._offer_receive()
                receive_calls += 1
                progress |= received
                if not received:
                    break
                if not partial:
                    break
            if receive_calls == self._budgets.receive_calls and self._retained_input:
                budget_exhausted = True

        process_status = self._transport.process(self._transport_ms(now_ms), OperatingMode.NORMAL)
        self._zero_receive_needed = True
        self._counters.count_status(self._counters.process_statuses, process_status)
        process_progress = self._allowed(
            process_status, "OK", "CAPACITY_EXHAUSTED", "DELIVERY_FAILED"
        )

        event_progress, event_exhausted = self._drain_events(now_ms)
        application_progress, application_exhausted = self._drain_application(now_ms)
        progress |= event_progress or application_progress
        budget_exhausted |= event_exhausted or application_exhausted

        if self._zero_receive_needed and receive_calls < self._budgets.receive_calls:
            zero_progress, _ = self._offer_receive(force_zero=True)
            receive_calls += 1
            progress |= zero_progress
            self._zero_receive_needed = False

        if (
            self._budgets.process_calls > 1
            and (progress or process_progress)
            and (
                event_progress
                or application_progress
                or output_progress
                or bool(self._retained_input)
            )
        ):
            second_status = self._transport.process(
                self._transport_ms(now_ms), OperatingMode.NORMAL
            )
            self._counters.count_status(self._counters.process_statuses, second_status)

        output_progress, output_exhausted = self._service_output(now_ms)
        progress |= output_progress
        budget_exhausted |= output_exhausted

        if budget_exhausted:
            self._counters.operation_budget_exhaustions += 1
        return ServiceResult(progress, budget_exhausted, gap, self._max_service_gap_ms)

    def get_diagnostics(self) -> dict[str, object]:
        self._require_live()
        return {
            "link_open": self.link_open,
            "link_generation": self._active_generation,
            "retained_input_length": len(self._retained_input),
            "staged_output_length": 0 if self._staged_output is None else len(self._staged_output),
            "staged_output_offset": self._staged_offset,
            "application_queue_length": len(self._application_messages),
            "event_queue_length": len(self._events),
            "current_service_gap_ms": self._current_service_gap_ms,
            "max_service_gap_ms": self._max_service_gap_ms,
            "maximum_acceptable_service_gap_ms": self._maximum_service_gap_ms,
            "caller_input_limit": self._caller_input_limit,
            "budgets": asdict(self._budgets),
            "counters": asdict(self._counters),
        }

    def close(self) -> None:
        self._require_owner()
        if self._closed:
            return
        try:
            if self._serial is not None or self._active_generation is not None:
                self.close_link()
        finally:
            try:
                self._transport.close()
            finally:
                self._closed = True
