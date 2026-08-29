from __future__ import annotations

import threading
from collections import deque

import pytest
from hil_rig_protocol import (
    EventType,
    Failure,
    LinkState,
    OperatingMode,
    ReceiveResult,
    Role,
    SessionState,
    TransportConfig,
    TransportEvent,
    TransportSnapshot,
    TransportStatus,
)

from hilrig.protocol_test.connection import (
    CommitOutputError,
    ConnectionOwnershipError,
    LinkDisconnectedError,
    ProtocolTestConnection,
)
from hilrig.protocol_test.models import SerialDevice, SerialSelector, ServiceBudgets
from hilrig.protocol_test.serial_port import SerialIOError, SerialWriteTimeout


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 1.0

    def __call__(self) -> float:
        return self.seconds

    def advance_ms(self, ms: int) -> None:
        self.seconds += ms / 1000


class FakeSerial:
    def __init__(self) -> None:
        self.identity = SerialDevice("fake", "Fake", 0x1234, 0x5678, "ABC")
        self.is_open = True
        self.incoming = bytearray()
        self.writes: list[bytes] = []
        self.write_results: deque[object] = deque()
        self.raise_on_readiness: Exception | None = None

    @property
    def in_waiting(self) -> int:
        if self.raise_on_readiness is not None:
            raise self.raise_on_readiness
        return len(self.incoming)

    def read(self, size: int) -> bytes:
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data: bytes) -> int | None:
        if self.write_results:
            result = self.write_results.popleft()
            if isinstance(result, Exception):
                raise result
            accepted = result
        else:
            accepted = len(data)
        if accepted:
            self.writes.append(data[:accepted])
        return accepted

    def reset_input_buffer(self) -> None:
        self.incoming.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


class FakeProvider:
    def __init__(self, serial: FakeSerial | None = None) -> None:
        self.serials: deque[FakeSerial] = deque([serial or FakeSerial()])
        self.resolved = SerialDevice("fake", "Fake", 0x1234, 0x5678, "ABC")

    def resolve(self, selector: SerialSelector) -> SerialDevice:
        return self.resolved

    def open(self, device: SerialDevice, baud: int) -> FakeSerial:
        if len(self.serials) > 1:
            return self.serials.popleft()
        return self.serials[0]


class FakeTransport:
    def __init__(self) -> None:
        self.config = TransportConfig(session_seed=1)
        self.notifications: list[tuple[LinkState, int]] = []
        self.receive_offers: list[bytes] = []
        self.receive_results: deque[ReceiveResult] = deque()
        self.process_calls: list[tuple[int, OperatingMode]] = []
        self.events: deque[TransportEvent] = deque()
        self.messages: deque[bytes] = deque()
        self.outputs: deque[bytes] = deque()
        self.current_output: bytes | None = None
        self.commit_calls = 0
        self.commit_result: object = TransportStatus.OK
        self.commit_exception: Exception | None = None
        self.closed = False
        self.reset_calls = 0

    def notify_link_state(self, state: LinkState, now_ms: int) -> TransportStatus:
        self.notifications.append((state, now_ms))
        return TransportStatus.OK

    def receive_bytes(self, data: bytes) -> ReceiveResult:
        self.receive_offers.append(bytes(data))
        if self.receive_results:
            return self.receive_results.popleft()
        return ReceiveResult(TransportStatus.OK, len(data))

    def process(self, now_ms: int, mode: OperatingMode) -> TransportStatus:
        self.process_calls.append((now_ms, mode))
        return TransportStatus.OK

    def read_event(self) -> TransportEvent | None:
        return self.events.popleft() if self.events else None

    def read_application_data(self) -> bytes | None:
        return self.messages.popleft() if self.messages else None

    def peek_output(self) -> bytes | None:
        if self.current_output is None and self.outputs:
            self.current_output = self.outputs.popleft()
        return self.current_output

    def commit_output(self, now_ms: int) -> object:
        self.commit_calls += 1
        if self.commit_exception is not None:
            raise self.commit_exception
        self.current_output = None
        return self.commit_result

    def get_status(self) -> TransportSnapshot:
        return TransportSnapshot(
            Role.HOST,
            LinkState.CONNECTED,
            SessionState.ESTABLISHED,
            OperatingMode.NORMAL,
            self.current_output is not None,
            bool(self.messages),
            bool(self.events),
            False,
            Failure.NONE,
        )

    def reset(self) -> TransportStatus:
        self.reset_calls += 1
        return TransportStatus.OK

    def close(self) -> None:
        self.closed = True


def make_connection(
    *,
    serial: FakeSerial | None = None,
    transport: FakeTransport | None = None,
    clock: FakeClock | None = None,
    budgets: ServiceBudgets | None = None,
    app_capacity: int = 1,
) -> tuple[ProtocolTestConnection, FakeSerial, FakeTransport, FakeClock]:
    serial = serial or FakeSerial()
    transport = transport or FakeTransport()
    clock = clock or FakeClock()
    connection = ProtocolTestConnection(
        FakeProvider(serial),
        SerialSelector(port="fake"),
        transport=transport,
        budgets=budgets or ServiceBudgets(),
        application_queue_capacity=app_capacity,
        clock=clock,
    )
    return connection, serial, transport, clock


def test_serial_open_notifies_connected_once() -> None:
    connection, _, transport, _ = make_connection()
    assert connection.open_link() == 1
    assert [state for state, _ in transport.notifications] == [LinkState.CONNECTED]


def test_link_close_notifies_disconnected_and_final_close_is_idempotent() -> None:
    connection, serial, transport, _ = make_connection()
    connection.open_link()
    connection.close_link()
    assert [state for state, _ in transport.notifications] == [
        LinkState.CONNECTED,
        LinkState.DISCONNECTED,
    ]
    assert not serial.is_open
    connection.close()
    connection.close()
    assert transport.closed


def test_process_runs_even_without_new_input() -> None:
    connection, _, transport, _ = make_connection()
    connection.open_link()
    connection.service_once()
    assert transport.process_calls
    assert transport.process_calls[0][1] is OperatingMode.NORMAL


def test_partial_receive_retains_exact_unconsumed_suffix() -> None:
    transport = FakeTransport()
    transport.receive_results.extend(
        [
            ReceiveResult(TransportStatus.CAPACITY_EXHAUSTED, 2),
            ReceiveResult(TransportStatus.NOT_READY, 0),
        ]
    )
    serial = FakeSerial()
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    connection.open_link()
    serial.incoming.extend(b"abcdef")
    connection.service_once()
    diagnostics = connection.get_diagnostics()
    assert diagnostics["retained_input_length"] == 4
    assert transport.receive_offers[0] == b"abcdef"
    assert transport.receive_offers[1] == b"cdef"
    assert diagnostics["counters"]["partial_consumption_calls"] == 1


def test_zero_byte_receive_after_application_capacity_is_released() -> None:
    transport = FakeTransport()
    transport.messages.append(b"one")
    connection, _, _, _ = make_connection(transport=transport)
    connection.open_link()
    connection.service_once()
    transport.receive_offers.clear()
    assert connection.pop_application_message() is not None
    connection.service_once()
    assert b"" in transport.receive_offers


def test_events_are_drained_to_bounded_caller_queue() -> None:
    transport = FakeTransport()
    transport.events.append(
        TransportEvent(EventType.SESSION_ESTABLISHED, TransportStatus.OK, Failure.NONE, 0)
    )
    connection, _, _, _ = make_connection(transport=transport)
    connection.open_link()
    connection.service_once()
    record = connection.pop_event()
    assert record is not None
    assert record.event.type is EventType.SESSION_ESTABLISHED


def test_application_message_remains_unread_when_consumer_slot_is_full() -> None:
    transport = FakeTransport()
    transport.messages.extend([b"one", b"two"])
    connection, _, _, _ = make_connection(transport=transport, app_capacity=1)
    connection.open_link()
    connection.service_once()
    connection.service_once()
    assert list(transport.messages) == [b"two"]
    assert connection.pop_application_message().data == b"one"


def test_partial_serial_write_advances_only_accepted_suffix_then_commits_once() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abcdef")
    serial = FakeSerial()
    serial.write_results.extend([2, 2, 2])
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    connection.open_link()
    connection.service_once()
    assert serial.writes == [b"ab", b"cd", b"ef"]
    assert transport.commit_calls == 1
    assert connection.get_diagnostics()["counters"]["partial_writes"] == 2


def test_zero_byte_write_retains_staged_item_without_commit() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abc")
    serial = FakeSerial()
    serial.write_results.extend([0, 0, 3])
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    connection.open_link()
    connection.service_once()
    assert transport.commit_calls == 0
    assert connection.get_diagnostics()["staged_output_length"] == 3
    connection.service_once()
    assert transport.commit_calls == 1


def test_commit_status_failure_clears_staged_python_state() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abc")
    transport.commit_result = TransportStatus.NOT_READY
    connection, _, _, _ = make_connection(transport=transport)
    connection.open_link()
    with pytest.raises(CommitOutputError, match="NOT_READY"):
        connection.service_once()
    diagnostics = connection.get_diagnostics()
    assert diagnostics["staged_output_length"] == 0
    assert diagnostics["staged_output_offset"] == 0
    assert transport.commit_calls == 1


def test_commit_exception_clears_staged_python_state() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abc")
    transport.commit_exception = RuntimeError("native commit failed")
    connection, _, _, _ = make_connection(transport=transport)
    connection.open_link()
    with pytest.raises(CommitOutputError):
        connection.service_once()
    assert connection.get_diagnostics()["staged_output_length"] == 0
    assert transport.commit_calls == 1


def test_disconnect_clears_retained_and_staged_data_and_old_generation_messages() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abcdef")
    transport.messages.append(b"old")
    transport.receive_results.append(ReceiveResult(TransportStatus.NOT_READY, 0))
    serial = FakeSerial()
    serial.write_results.append(0)
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    assert connection.open_link() == 1
    serial.incoming.extend(b"rx")
    connection.service_once()
    assert connection.get_diagnostics()["retained_input_length"] == 2
    connection.close_link()
    diagnostics = connection.get_diagnostics()
    assert diagnostics["retained_input_length"] == 0
    assert diagnostics["staged_output_length"] == 0
    assert connection.pop_application_message() is None


def test_serial_exception_triggers_controlled_disconnection() -> None:
    serial = FakeSerial()
    serial.raise_on_readiness = SerialIOError("removed")
    connection, _, transport, _ = make_connection(serial=serial)
    connection.open_link()
    with pytest.raises(LinkDisconnectedError, match="removed"):
        connection.service_once()
    assert not connection.link_open
    assert [state for state, _ in transport.notifications][-1] is LinkState.DISCONNECTED


def test_write_timeout_triggers_controlled_disconnection() -> None:
    serial = FakeSerial()
    serial.write_results.append(SerialWriteTimeout("busy"))
    transport = FakeTransport()
    transport.outputs.append(b"abc")
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    connection.open_link()
    with pytest.raises(LinkDisconnectedError, match="busy"):
        connection.service_once()
    assert not connection.link_open


def test_service_operation_budgets_terminate_and_are_counted() -> None:
    transport = FakeTransport()
    for _ in range(5):
        transport.events.append(
            TransportEvent(
                EventType.CAPACITY_EXHAUSTED,
                TransportStatus.CAPACITY_EXHAUSTED,
                Failure.CAPACITY,
                1,
            )
        )
    connection, _, _, _ = make_connection(
        transport=transport,
        budgets=ServiceBudgets(event_reads=2),
    )
    connection.open_link()
    result = connection.service_once()
    assert result.operation_budget_exhausted
    assert len(transport.events) == 3
    assert connection.get_diagnostics()["counters"]["operation_budget_exhaustions"] == 1


def test_connection_and_transport_calls_never_move_to_another_thread() -> None:
    connection, _, transport, _ = make_connection()
    connection.open_link()
    caught: list[BaseException] = []

    def worker() -> None:
        try:
            connection.service_once()
        except BaseException as exc:
            caught.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert len(caught) == 1
    assert isinstance(caught[0], ConnectionOwnershipError)
    assert transport.process_calls == []


def test_commit_failure_blocks_resend_until_link_boundary() -> None:
    transport = FakeTransport()
    transport.outputs.append(b"abc")
    transport.commit_result = TransportStatus.NOT_READY
    serial = FakeSerial()
    connection, _, _, _ = make_connection(serial=serial, transport=transport)
    connection.open_link()
    with pytest.raises(CommitOutputError):
        connection.service_once()
    writes_after_failure = list(serial.writes)
    with pytest.raises(CommitOutputError):
        connection.service_once()
    assert serial.writes == writes_after_failure
    assert transport.commit_calls == 1


def test_already_closed_serial_handle_causes_disconnect_notification() -> None:
    serial = FakeSerial()
    connection, _, transport, _ = make_connection(serial=serial)
    connection.open_link()
    serial.is_open = False
    with pytest.raises(LinkDisconnectedError):
        connection.service_once()
    assert [state for state, _ in transport.notifications][-1] is LinkState.DISCONNECTED
    assert connection.link_generation is None
