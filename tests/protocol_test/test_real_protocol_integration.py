from __future__ import annotations

import time
from collections import deque

import pytest

protocol = pytest.importorskip("hil_rig_protocol")
from hil_rig_protocol import (  # noqa: E402
    EventType,
    LinkState,
    OperatingMode,
    Role,
    SessionState,
    Transport,
    TransportConfig,
    TransportStatus,
)

from hilrig.protocol_test.connection import ProtocolTestConnection  # noqa: E402
from hilrig.protocol_test.models import SerialDevice, SerialSelector  # noqa: E402

pytestmark = pytest.mark.protocol_integration


def now_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFF_FFFF


class InMemoryRigSerial:
    def __init__(
        self, *, max_read_chunk: int | None = None, max_write_accept: int | None = None
    ) -> None:
        self.identity = SerialDevice("memory-rig", "In-memory RIG", 0x135, 0x138, "MEM")
        self.is_open = True
        self.max_read_chunk = max_read_chunk
        self.max_write_accept = max_write_accept
        self.host_to_rig = bytearray()
        self.rig_to_host = bytearray()
        self.rig = Transport(Role.RIG, TransportConfig())
        self.rig.notify_link_state(LinkState.CONNECTED, now_ms())
        self.received_application: deque[bytes] = deque()
        self.pending_response: bytes | None = None
        self.response_factory = lambda data: b"rig-response:" + data

    def _service_rig(self) -> None:
        for _ in range(16):
            progress = False
            if self.host_to_rig:
                offered = bytes(self.host_to_rig)
                result = self.rig.receive_bytes(offered)
                if result.bytes_consumed:
                    del self.host_to_rig[: result.bytes_consumed]
                    progress = True
            status = self.rig.process(now_ms(), OperatingMode.NORMAL)
            if status not in {
                TransportStatus.OK,
                TransportStatus.NOT_READY,
                TransportStatus.CAPACITY_EXHAUSTED,
                TransportStatus.DELIVERY_FAILED,
            }:
                raise AssertionError(status)
            drained = False
            while self.rig.read_event() is not None:
                drained = True
            while (message := self.rig.read_application_data()) is not None:
                self.received_application.append(message)
                if self.pending_response is None:
                    self.pending_response = self.response_factory(message)
                drained = True
            if drained:
                self.rig.receive_bytes(b"")
                progress = True
            if self.pending_response is not None:
                submit = self.rig.submit_application_data(self.pending_response)
                if submit is TransportStatus.OK:
                    self.pending_response = None
                    progress = True
                elif submit not in {TransportStatus.NOT_READY, TransportStatus.CAPACITY_EXHAUSTED}:
                    raise AssertionError(submit)
            output = self.rig.peek_output()
            if output is not None:
                self.rig_to_host.extend(output)
                commit = self.rig.commit_output(now_ms())
                if commit is not TransportStatus.OK:
                    raise AssertionError(commit)
                progress = True
            if not progress:
                break

    @property
    def in_waiting(self) -> int:
        self._service_rig()
        return len(self.rig_to_host)

    def read(self, size: int) -> bytes:
        self._service_rig()
        if self.max_read_chunk is not None:
            size = min(size, self.max_read_chunk)
        data = bytes(self.rig_to_host[:size])
        del self.rig_to_host[:size]
        return data

    def write(self, data: bytes) -> int:
        accepted = len(data)
        if self.max_write_accept is not None:
            accepted = min(accepted, self.max_write_accept)
        self.host_to_rig.extend(data[:accepted])
        return accepted

    def reset_input_buffer(self) -> None:
        self.rig_to_host.clear()

    def reset_output_buffer(self) -> None:
        self.host_to_rig.clear()

    def close(self) -> None:
        if not self.is_open:
            return
        self.rig.notify_link_state(LinkState.DISCONNECTED, now_ms())
        self.rig.close()
        self.is_open = False


class InMemoryProvider:
    def __init__(
        self, *, max_read_chunk: int | None = None, max_write_accept: int | None = None
    ) -> None:
        self.max_read_chunk = max_read_chunk
        self.max_write_accept = max_write_accept
        self.opened: list[InMemoryRigSerial] = []

    def resolve(self, selector: SerialSelector) -> SerialDevice:
        return SerialDevice("memory-rig", "In-memory RIG", 0x135, 0x138, "MEM")

    def open(self, device: SerialDevice, baud: int) -> InMemoryRigSerial:
        serial = InMemoryRigSerial(
            max_read_chunk=self.max_read_chunk,
            max_write_accept=self.max_write_accept,
        )
        self.opened.append(serial)
        return serial


def establish(connection: ProtocolTestConnection) -> None:
    for _ in range(2000):
        connection.service_once()
        if connection.get_status().session_state is SessionState.ESTABLISHED:
            return
    raise AssertionError("HOST/RIG session did not establish")


def exchange(connection: ProtocolTestConnection, request: bytes) -> bytes:
    for _ in range(2000):
        status = connection.submit_application_data(request)
        if status is TransportStatus.OK:
            break
        assert status in {TransportStatus.NOT_READY, TransportStatus.CAPACITY_EXHAUSTED}
        connection.service_once()
    else:
        raise AssertionError("request never submitted")
    for _ in range(2000):
        connection.service_once()
        received = connection.pop_application_message()
        if received is not None:
            return received.data
    raise AssertionError("response never arrived")


@pytest.mark.parametrize("read_chunk", [1, None])
def test_real_protocol_session_and_opaque_request_response(read_chunk: int | None) -> None:
    provider = InMemoryProvider(max_read_chunk=read_chunk)
    connection = ProtocolTestConnection(provider, SerialSelector(port="memory-rig"))
    try:
        assert connection.transport_config.retransmit_timeout_ms == 100
        assert connection.transport_config.max_retries == 5
        connection.open_link()
        establish(connection)
        assert exchange(connection, b"opaque-request") == b"rig-response:opaque-request"
        assert provider.opened[-1].received_application == deque([b"opaque-request"])
    finally:
        connection.close()


def test_real_protocol_partial_output_acceptance_and_event_draining() -> None:
    provider = InMemoryProvider(max_write_accept=1)
    connection = ProtocolTestConnection(provider, SerialSelector(port="memory-rig"))
    try:
        connection.open_link()
        establish(connection)
        events = []
        while (event := connection.pop_event()) is not None:
            events.append(event.event.type)
        assert EventType.SESSION_ESTABLISHED in events
        assert exchange(connection, b"partial-write") == b"rig-response:partial-write"
        diagnostics = connection.get_diagnostics()
        assert diagnostics["counters"]["partial_writes"] > 0
        assert diagnostics["counters"]["output_items_committed"] > 0
    finally:
        connection.close()


def test_real_protocol_disconnect_and_reconnect_uses_new_generation() -> None:
    provider = InMemoryProvider()
    connection = ProtocolTestConnection(provider, SerialSelector(port="memory-rig"))
    try:
        first = connection.open_link()
        establish(connection)
        connection.close_link()
        second = connection.open_link()
        establish(connection)
        assert first == 1
        assert second == 2
        assert len(provider.opened) == 2
        assert exchange(connection, b"after-reconnect") == b"rig-response:after-reconnect"
    finally:
        connection.close()
