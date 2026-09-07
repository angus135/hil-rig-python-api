from __future__ import annotations

import time
from collections import deque

import pytest

protocol = pytest.importorskip("hil_rig_protocol")
from firmware_results import firmware_result  # noqa: E402
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

from hilrig.protocol_test.application_hardware import (  # noqa: E402
    COMPATIBILITY_PROFILE_ID,
    PROTOCOL_VERSION,
    configuration_semantic_digest,
    instruction_semantic_digest,
    make_application_codec,
)
from hilrig.protocol_test.connection import ProtocolTestConnection  # noqa: E402
from hilrig.protocol_test.harness_codec import (  # noqa: E402
    ApplicationHarnessState,
    Opcode,
    decode_message,
    encode_message,
)
from hilrig.protocol_test.models import SerialDevice, SerialSelector  # noqa: E402
from hilrig.protocol_test.runner import ProtocolTestRunner, ScenarioFailure  # noqa: E402
from hilrig.protocol_test.trace import TraceWriter  # noqa: E402

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


class ApplicationRigBehavior:
    """Minimal firmware transaction model behind a real RIG Transport endpoint."""

    def __init__(self) -> None:
        self.codec = make_application_codec()
        self.invalid_hrtp_messages = 0
        self.total_application_received = 0
        self.responses_results_submitted = 0
        self.non_hrtp_received = 0
        self.decode_failures = 0
        self.semantic_rejections = 0
        self.encode_failures = 0
        self.configurations_accepted = 0
        self.instructions_accepted = 0
        self.results_encoded = 0
        self.last_application_status = int(protocol.ApplicationStatus.OK)
        self.last_message_type = 0
        self.configuration_digest = 0
        self.last_instruction_digest = 0
        self.active_configuration = None
        self.state = ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        self.next_tick = 0
        self.active_expected_tick_count = 0

    def status_payload(self) -> bytes:
        import struct

        values = [0] * 32
        values[0] = 2
        values[1] = 1
        values[3] = self.total_application_received
        values[6] = self.total_application_received
        values[7] = self.responses_results_submitted
        values[11] = int(SessionState.ESTABLISHED)
        values[12] = COMPATIBILITY_PROFILE_ID
        values[13:16] = list(PROTOCOL_VERSION)
        values[16] = 1
        values[17] = int(protocol.ApplicationStatus.OK)
        values[18] = self.non_hrtp_received
        values[19] = self.decode_failures
        values[20] = self.semantic_rejections
        values[21] = self.encode_failures
        values[22] = self.configurations_accepted
        values[23] = self.instructions_accepted
        values[24] = self.results_encoded
        values[25] = int(self.state)
        values[26] = self.next_tick
        values[27] = self.active_expected_tick_count
        values[28] = self.last_application_status
        values[29] = self.last_message_type
        values[30] = self.configuration_digest
        values[31] = self.last_instruction_digest
        return struct.pack("<32I", *values)

    def _hrtp(self, data: bytes) -> bytes | None:
        try:
            request = decode_message(data, max_application_message_size=512)
        except ValueError:
            self.invalid_hrtp_messages += 1
            return None
        if request.opcode is Opcode.ECHO_REQUEST:
            return encode_message(
                Opcode.ECHO_RESPONSE,
                request.request_id,
                request.payload,
                max_application_message_size=512,
            )
        if request.opcode is Opcode.STATUS_REQUEST:
            return encode_message(
                Opcode.STATUS_RESPONSE,
                request.request_id,
                self.status_payload(),
                max_application_message_size=512,
            )
        self.invalid_hrtp_messages += 1
        return None

    def _application(self, data: bytes) -> bytes | None:
        self.non_hrtp_received += 1
        try:
            message = self.codec.decode(data)
        except protocol.ApplicationDecodeError as exc:
            self.decode_failures += 1
            self.last_application_status = int(
                exc.status
                if exc.status is not None
                else protocol.ApplicationStatus.MALFORMED_MESSAGE
            )
            return None
        if type(message) is protocol.TestConfiguration:
            self.last_message_type = 16
            if self.state not in {
                ApplicationHarnessState.WAITING_FOR_CONFIGURATION,
                ApplicationHarnessState.COMPLETE,
            }:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return None
            self.active_configuration = message
            self.configurations_accepted += 1
            self.configuration_digest = configuration_semantic_digest(message)
            self.state = ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
            self.next_tick = 0
            self.active_expected_tick_count = message.expected_tick_count
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return None
        if type(message) is protocol.TestInstruction:
            self.last_message_type = 17
            if self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return None
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TEST_ID)
                return None
            if message.tick_number != self.next_tick:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TICK)
                return None
            self.instructions_accepted += 1
            self.last_instruction_digest = instruction_semantic_digest(message)
            result = firmware_result(self.active_configuration, message)
            try:
                encoded = self.codec.encode(result)
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return None
            self.results_encoded += 1
            self.next_tick += 1
            if self.next_tick == self.active_expected_tick_count:
                self.state = ApplicationHarnessState.COMPLETE
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return encoded
        self.semantic_rejections += 1
        return None

    def handle(self, data: bytes) -> bytes | None:
        self.total_application_received += 1
        response = self._hrtp(data) if data.startswith(b"HRTP") else self._application(data)
        if response is not None:
            self.responses_results_submitted += 1
        return response


class ApplicationInMemoryRigSerial(InMemoryRigSerial):
    def __init__(self) -> None:
        super().__init__()
        self.behavior = ApplicationRigBehavior()
        self.response_factory = self.behavior.handle


class InMemoryProvider:
    def __init__(
        self,
        *,
        max_read_chunk: int | None = None,
        max_write_accept: int | None = None,
        application_behavior: bool = False,
    ) -> None:
        self.max_read_chunk = max_read_chunk
        self.max_write_accept = max_write_accept
        self.application_behavior = application_behavior
        self.opened: list[InMemoryRigSerial] = []

    def resolve(self, selector: SerialSelector) -> SerialDevice:
        return SerialDevice("memory-rig", "In-memory RIG", 0x135, 0x138, "MEM")

    def open(self, device: SerialDevice, baud: int) -> InMemoryRigSerial:
        if self.application_behavior:
            serial = ApplicationInMemoryRigSerial()
        else:
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


@pytest.mark.parametrize(
    "scenario",
    [
        "application-smoke",
        "application-boundaries",
        "application-negative",
        "application-repeat",
        "oracle-regression",
    ],
)
def test_real_protocol_application_runner_over_both_transport_endpoints(
    tmp_path, scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = InMemoryProvider(application_behavior=True)
    connection = ProtocolTestConnection(provider, SerialSelector(port="memory-rig"))
    trace = TraceWriter(
        tmp_path,
        scenario,
        seed=1,
        source_evidence={"protocol_declared_version": "0.1.0"},
    )
    runner = ProtocolTestRunner(connection, trace, request_timeout_ms=1000)
    try:
        runner.open()
        if scenario == "oracle-regression":
            monkeypatch.setattr(
                "hilrig.protocol_test.runner.expected_result",
                lambda configuration, instruction: protocol.TestResult(
                    test_id=instruction.test_id, tick_number=instruction.tick_number
                ),
            )
            with pytest.raises(ScenarioFailure, match="did not match deterministic oracle"):
                runner.run_application_smoke()
        elif scenario == "application-smoke":
            result = runner.run_application_smoke()
            assert result["final_status"].application_harness_state == int(
                ApplicationHarnessState.COMPLETE
            )
        elif scenario == "application-boundaries":
            result = runner.run_application_boundaries()
            assert result["encoded_sizes"] == [226, 242, 481, 73, 62]
        elif scenario == "application-negative":
            result = runner.run_application_negative()
            assert result["decode_failures_delta"] == 1
            assert result["semantic_rejections_delta"] == 1
        else:
            result = runner.run_application_repeat(3)
            assert result["completed"] == 3
        behavior = provider.opened[-1].behavior
        assert behavior.invalid_hrtp_messages == 0
    finally:
        runner.close()
        trace.finish(
            passed=True,
            failure_reason=None,
            diagnostics=None if connection.closed else connection.get_diagnostics(),
        )
