from __future__ import annotations

import json
import struct
from collections import deque
from dataclasses import replace
from pathlib import Path

import hil_rig_protocol as protocol
import pytest
from firmware_results import firmware_result
from hil_rig_protocol import (
    EventType,
    Failure,
    LinkState,
    OperatingMode,
    Role,
    SessionState,
    TransportEvent,
    TransportSnapshot,
    TransportStatus,
)

from hilrig.protocol_test.application_hardware import (
    COMPATIBILITY_PROFILE_ID,
    INSTRUCTION_DIGESTS,
    PROTOCOL_VERSION,
    REPRESENTATIVE_CONFIGURATION_DIGEST,
    configuration_semantic_digest,
    execution_control_response,
    global_control_response,
    instruction_semantic_digest,
    make_application_codec,
    representative_configuration,
    representative_instructions,
    response_fixtures,
    variable_result_oracle,
    zero_instruction,
)
from hilrig.protocol_test.connection import LinkDisconnectedError, hardware_test_transport_config
from hilrig.protocol_test.harness_codec import (
    ApplicationHarnessState,
    Opcode,
    decode_message,
    encode_message,
)
from hilrig.protocol_test.models import (
    ConnectionEvent,
    ReceivedApplicationMessage,
    SerialDevice,
    ServiceResult,
)
from hilrig.protocol_test.runner import ProtocolTestRunner, ScenarioFailure
from hilrig.protocol_test.trace import TraceWriter

_APPLICATION_MESSAGE_TYPES = {
    protocol.SystemInfoRequest: 1,
    protocol.TestConfiguration: 16,
    protocol.TestInstruction: 17,
    protocol.ExecutionControl: 19,
    protocol.GlobalControl: 20,
    protocol.ApplicationResponse: 48,
    protocol.ApplicationErrorMessage: 49,
}


class FakeTime:
    def __init__(self) -> None:
        self.now = 10.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


class ApplicationFirmwareConnection:
    """Scenario double for firmware transaction semantics; Application bytes use public codec."""

    def __init__(self, clock: FakeTime) -> None:
        self.clock = clock
        self.transport_config = hardware_test_transport_config()
        self.link_generation: int | None = None
        self.serial_identity = SerialDevice("fake", "Application firmware", 1, 2, "APP")
        self.closed = False
        self.link_open = False
        self.maximum_acceptable_service_gap_ms = 10
        self._generation = 0
        self.messages: deque[ReceivedApplicationMessage] = deque()
        self.events: deque[ConnectionEvent] = deque()
        self.disconnect_on_service = False
        self.codec = make_application_codec()
        self.compatibility_profile_id = COMPATIBILITY_PROFILE_ID
        self.protocol_version = PROTOCOL_VERSION
        self.codec_initialized = 1
        self.initialization_status = int(protocol.ApplicationStatus.OK)
        self.duplicate_results = False
        self.duplicate_application_responses = False
        self.protocol_version_confirmed = False
        self.invalid_hrtp_messages = 0
        self.transport_event_count = 0
        self.usb_rx_bytes = 0
        self.usb_tx_bytes = 0
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
        self.active_configuration: protocol.TestConfiguration | None = None
        self.state = ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        self.next_tick = 0
        self.active_expected_tick_count = 0
        self.service_calls = 0
        self.v03_mode = False
        self.v03_instructions: dict[int, protocol.TestInstruction] = {}
        self.v03_updates: dict[int, list[protocol.LogicalOperation]] = {}
        self.v03_result_queue: deque[bytes] = deque()

    def _reset_transaction(self) -> None:
        self.protocol_version_confirmed = False
        self.active_configuration = None
        self.state = ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        self.next_tick = 0
        self.active_expected_tick_count = 0
        self.v03_mode = False
        self.v03_instructions.clear()
        self.v03_updates.clear()
        self.v03_result_queue.clear()

    def open_link(self) -> int:
        self._generation += 1
        self.link_generation = self._generation
        self.link_open = True
        self._reset_transaction()
        return self._generation

    def close_link(self) -> None:
        self.link_open = False
        self.link_generation = None
        self.messages.clear()
        self._reset_transaction()

    def close(self) -> None:
        self.closed = True
        self.link_open = False
        self._reset_transaction()

    def get_status(self) -> TransportSnapshot:
        return TransportSnapshot(
            Role.HOST,
            LinkState.CONNECTED if self.link_open else LinkState.DISCONNECTED,
            SessionState.ESTABLISHED if self.link_open else SessionState.DISCONNECTED,
            OperatingMode.NORMAL if self.link_open else None,
            False,
            bool(self.messages),
            bool(self.events),
            False,
            Failure.NONE,
        )

    def service_once(self) -> ServiceResult:
        self.service_calls += 1
        if self.disconnect_on_service:
            self.disconnect_on_service = False
            self.link_open = False
            self.link_generation = None
            self._reset_transaction()
            raise LinkDisconnectedError("simulated physical reset")
        if self.v03_result_queue and not self.messages:
            self._queue_message(self.v03_result_queue.popleft())
            if not self.v03_result_queue:
                self.state = ApplicationHarnessState.COMPLETE
        return ServiceResult(False, False, 1, 1)

    def pop_event(self) -> ConnectionEvent | None:
        return self.events.popleft() if self.events else None

    def pop_application_message(self) -> ReceivedApplicationMessage | None:
        return self.messages.popleft() if self.messages else None

    def get_diagnostics(self) -> dict[str, object]:
        return {"service_calls": self.service_calls, "generation": self.link_generation}

    def _queue_event(self, event_type: EventType) -> None:
        self.transport_event_count += 1
        self.events.append(
            ConnectionEvent(
                TransportEvent(event_type, TransportStatus.OK, Failure.NONE, 0),
                self.link_generation,
                0,
            )
        )

    def _queue_message(self, data: bytes) -> None:
        self.responses_results_submitted += 1
        self.usb_tx_bytes += len(data)
        self.messages.append(ReceivedApplicationMessage(data, self.link_generation or 0, 0))

    @staticmethod
    def _application_response(
        test_id: protocol.TestId,
        scope: protocol.ResponseScope,
        outcome: protocol.ResponseOutcome,
        *,
        tick_number: int = 0,
        control_command: protocol.ControlCommand = protocol.ControlCommand.INVALID,
    ) -> protocol.ApplicationResponse:
        return protocol.ApplicationResponse(
            test_id,
            scope,
            outcome,
            protocol.ResponseReason.NONE,
            tick_number=tick_number,
            control_command=control_command,
        )

    def _status_payload(self) -> bytes:
        values = [0] * 48
        values[0] = 3
        values[1] = 1
        values[2] = self.link_generation or 0
        values[3] = self.transport_event_count
        values[4] = self.usb_rx_bytes
        values[5] = self.usb_tx_bytes
        values[6] = self.total_application_received
        values[7] = self.responses_results_submitted
        values[9] = self.invalid_hrtp_messages
        values[11] = int(SessionState.ESTABLISHED)
        values[12] = self.compatibility_profile_id
        values[13:16] = list(self.protocol_version)
        values[16] = self.codec_initialized
        values[17] = self.initialization_status
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
        values[32] = 1 if self.v03_mode else 0
        values[33] = (
            1
            if self.v03_mode
            and self.active_configuration
            and self.active_configuration.extension_data.startswith(b"HTV3")
            else 0
        )
        values[34] = len(self.v03_instructions) + len(self.v03_updates)
        values[35] = 0
        values[36] = 8
        values[37] = (
            1 if self.v03_mode and self.state is ApplicationHarnessState.READY_TO_START else 0
        )
        values[38] = values[37]
        values[39] = sum(len(items) for items in self.v03_updates.values())
        values[40] = self.results_encoded
        return struct.pack("<48I", *values)

    def _handle_hrtp(self, data: bytes) -> None:
        request = decode_message(data, max_application_message_size=512)
        if request.opcode is Opcode.ECHO_REQUEST:
            response = encode_message(
                Opcode.ECHO_RESPONSE,
                request.request_id,
                request.payload,
                max_application_message_size=512,
            )
        else:
            assert request.opcode is Opcode.STATUS_REQUEST
            response = encode_message(
                Opcode.STATUS_RESPONSE,
                request.request_id,
                self._status_payload(),
                max_application_message_size=512,
            )
        self._queue_message(response)

    def _queue_v03_results(self) -> None:
        assert self.active_configuration is not None
        test_id = self.active_configuration.test_id
        variable = (
            self.active_configuration.extension_data.startswith(b"HTV3")
            and self.active_configuration.extension_data[4] == 1
        )
        for tick in range(self.active_expected_tick_count):
            if variable:
                result = variable_result_oracle(test_id, tick)
                if tick not in self.v03_updates:
                    result = replace(result, records=())
            else:
                instruction = self.v03_instructions.get(tick, zero_instruction(test_id))
                result = firmware_result(self.active_configuration, instruction)
            self.results_encoded += 1
            self.v03_result_queue.append(self.codec.encode(result))

    def _handle_application(self, data: bytes) -> None:
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
            return
        self.last_message_type = _APPLICATION_MESSAGE_TYPES.get(type(message), 0)
        if type(message) is protocol.SystemInfoRequest:
            self.protocol_version_confirmed = message.protocol_version == protocol.PROTOCOL_VERSION
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            response = protocol.SystemInfoResponse(
                protocol.PROTOCOL_VERSION,
                protocol.ProtocolVersion(63, 0, 0),
                b"firmware-pr-63",
                b"63",
            )
            try:
                self._queue_message(self.codec.encode(response))
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
            return
        if not self.protocol_version_confirmed:
            self.semantic_rejections += 1
            self.last_application_status = int(protocol.ApplicationStatus.VERSION_MISMATCH)
            return
        if self.v03_mode and type(message) is protocol.TestConfiguration:
            if self.state not in {
                ApplicationHarnessState.WAITING_FOR_CONFIGURATION,
                ApplicationHarnessState.COMPLETE,
            }:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return
            self.active_configuration = message
            self.configurations_accepted += 1
            self.configuration_digest = configuration_semantic_digest(message)
            self.state = ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
            self.next_tick = 0
            self.active_expected_tick_count = message.expected_tick_count
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.TEST_CONFIGURATION,
                protocol.ResponseOutcome.ACCEPTED,
            )
            self._queue_message(self.codec.encode(response))
            return
        if (
            type(message) is protocol.TestConfiguration
            and message.expected_tick_count == 3
            and (message.extension_data == b"" or message.extension_data.startswith(b"HTV3"))
        ):
            self.v03_mode = True
            self._handle_application(data)
            return
        if self.v03_mode and type(message) is protocol.TestInstruction:
            if self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TEST_ID)
                return
            if message.tick_number != self.next_tick:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TICK)
                return
            self.v03_instructions[message.tick_number] = message
            self.instructions_accepted += 1
            self.last_instruction_digest = instruction_semantic_digest(message)
            self.next_tick += 1
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.TICK,
                protocol.ResponseOutcome.ACCEPTED,
                tick_number=message.tick_number,
            )
            self._queue_message(self.codec.encode(response))
            return
        if self.v03_mode and type(message) is protocol.UpdateInstruction:
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TEST_ID)
                return
            self.v03_updates.setdefault(message.tick_number, []).extend(message.operations)
            self.instructions_accepted += 1 if message.flags == 0 else 0
            if message.flags == 0:
                response = self._application_response(
                    message.test_id,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseOutcome.ACCEPTED,
                    tick_number=message.tick_number,
                )
                self._queue_message(self.codec.encode(response))
            return
        if self.v03_mode and type(message) is protocol.FinalizeTestUpload:
            assert self.active_configuration is not None
            self.state = ApplicationHarnessState.READY_TO_START
            response = self._application_response(
                self.active_configuration.test_id,
                protocol.ResponseScope.COMPLETE_TEST,
                protocol.ResponseOutcome.ACCEPTED,
            )
            self._queue_message(self.codec.encode(response))
            return
        if self.v03_mode and type(message) is protocol.ExecutionControl:
            if message.command is protocol.ControlCommand.START:
                self.state = ApplicationHarnessState.EMITTING_RESULTS
                response = execution_control_response(message)
                self._queue_message(self.codec.encode(response))
                self._queue_v03_results()
            else:
                response = execution_control_response(message)
                self._queue_message(self.codec.encode(response))
            return
        if type(message) is protocol.ExecutionControl:
            try:
                self._queue_message(self.codec.encode(execution_control_response(message)))
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
            return
        if type(message) is protocol.GlobalControl:
            try:
                self._queue_message(self.codec.encode(global_control_response(message)))
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
            return
        if type(message) is protocol.TestConfiguration:
            if self.state not in {
                ApplicationHarnessState.WAITING_FOR_CONFIGURATION,
                ApplicationHarnessState.COMPLETE,
            }:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return
            self.active_configuration = message
            self.configurations_accepted += 1
            self.configuration_digest = configuration_semantic_digest(message)
            self.state = ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
            self.next_tick = 0
            self.active_expected_tick_count = message.expected_tick_count
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return
        if type(message) is protocol.TestInstruction:
            if self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
                return
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TEST_ID)
                return
            if message.tick_number != self.next_tick:
                self.semantic_rejections += 1
                self.last_application_status = int(protocol.ApplicationStatus.INCONSISTENT_TICK)
                return
            self.instructions_accepted += 1
            self.last_instruction_digest = instruction_semantic_digest(message)
            result = firmware_result(self.active_configuration, message)
            try:
                encoded = self.codec.encode(result)
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return
            self.results_encoded += 1
            self._queue_message(encoded)
            if self.duplicate_results:
                self._queue_message(encoded)
            self.next_tick += 1
            if self.next_tick == self.active_expected_tick_count:
                self.state = ApplicationHarnessState.COMPLETE
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return
        if type(message) in {protocol.ApplicationResponse, protocol.ApplicationErrorMessage}:
            try:
                encoded = self.codec.encode(message)
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return
            self._queue_message(encoded)
            if self.duplicate_application_responses:
                self._queue_message(encoded)
            return
        self.semantic_rejections += 1

    def submit_application_data(self, data: bytes) -> TransportStatus:
        self.total_application_received += 1
        self.usb_rx_bytes += len(data)
        self._queue_event(EventType.DELIVERY_CONFIRMED)
        if data.startswith(b"HRTP"):
            self._handle_hrtp(data)
        else:
            self._handle_application(data)
        return TransportStatus.OK


def make_runner(
    tmp_path: Path,
) -> tuple[ProtocolTestRunner, ApplicationFirmwareConnection, TraceWriter]:
    fake_time = FakeTime()
    connection = ApplicationFirmwareConnection(fake_time)
    trace = TraceWriter(
        tmp_path,
        "application-unit",
        seed=1,
        source_evidence={"protocol_declared_version": "0.3.0"},
    )
    runner = ProtocolTestRunner(
        connection,  # type: ignore[arg-type]
        trace,
        request_timeout_ms=20,
        reconnect_timeout_ms=20,
        sleep=fake_time.sleep,
        monotonic=fake_time.monotonic,
    )
    runner.open()
    return runner, connection, trace


def close(
    runner: ProtocolTestRunner,
    trace: TraceWriter,
    connection: ApplicationFirmwareConnection,
) -> None:
    runner.close()
    trace.finish(passed=True, failure_reason=None, diagnostics=connection.get_diagnostics())


def _foreign_system_info_request(codec: protocol.ApplicationCodec) -> bytes:
    wire = bytearray(codec.encode(protocol.SystemInfoRequest()))
    wire[1] = 3
    wire[29] = 3  # Public v0.3.0 SystemInfoRequest application_protocol_patch byte.
    return bytes(wire)


def test_fake_version_confirmation_gate_and_message_type_diagnostics(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    test_id = protocol.TestId(bytes(range(16)))
    configuration = representative_configuration(test_id)

    connection._handle_application(connection.codec.encode(configuration))
    assert connection.protocol_version_confirmed is False
    assert connection.last_message_type == 16
    assert connection.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)
    assert connection.configurations_accepted == 0

    connection._handle_application(connection.codec.encode(protocol.SystemInfoRequest()))
    discovered = connection.pop_application_message()
    assert discovered is not None
    response = connection.codec.decode(discovered.data)
    assert type(response) is protocol.SystemInfoResponse
    assert response.protocol_version == protocol.PROTOCOL_VERSION
    assert connection.protocol_version_confirmed is True
    assert connection.last_message_type == 1

    connection._handle_application(connection.codec.encode(configuration))
    assert connection.configurations_accepted == 1
    assert connection.last_message_type == 16
    instruction = representative_instructions(test_id)[0]
    connection._handle_application(connection.codec.encode(instruction))
    assert connection.last_message_type == 17
    assert connection.pop_application_message() is not None

    for command in (protocol.ControlCommand.START, protocol.ControlCommand.ABORT):
        control = protocol.ExecutionControl(test_id, command)
        connection._handle_application(connection.codec.encode(control))
        received = connection.pop_application_message()
        assert received is not None
        control_response = connection.codec.decode(received.data)
        assert type(control_response) is protocol.ApplicationResponse
        assert control_response.outcome is protocol.ResponseOutcome.COMPLETED
        assert connection.last_message_type == 19

    reset = protocol.GlobalControl(protocol.GlobalControlCommand.RESET_APPLICATION)
    connection._handle_application(connection.codec.encode(reset))
    received = connection.pop_application_message()
    assert received is not None
    reset_response = connection.codec.decode(received.data)
    assert type(reset_response) is protocol.ApplicationResponse
    assert reset_response.outcome is protocol.ResponseOutcome.COMPLETED
    assert connection.last_message_type == 20

    response = response_fixtures(test_id)[0]
    connection._handle_application(connection.codec.encode(response))
    assert connection.last_message_type == 48
    assert connection.pop_application_message() is not None
    error = protocol.ApplicationErrorMessage(None, protocol.ErrorCategory.PROTOCOL, True)
    connection._handle_application(connection.codec.encode(error))
    assert connection.last_message_type == 49
    assert connection.pop_application_message() is not None

    connection.close_link()
    connection.open_link()
    connection._handle_application(connection.codec.encode(configuration))
    assert connection.protocol_version_confirmed is False
    assert connection.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)

    connection._handle_application(_foreign_system_info_request(connection.codec))
    received = connection.pop_application_message()
    assert received is not None
    foreign_discovery_response = connection.codec.decode(received.data)
    assert type(foreign_discovery_response) is protocol.SystemInfoResponse
    assert foreign_discovery_response.protocol_version == protocol.PROTOCOL_VERSION
    assert connection.protocol_version_confirmed is False
    connection._handle_application(connection.codec.encode(reset))
    assert connection.last_message_type == 20
    assert connection.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)
    close(runner, trace, connection)


def test_application_smoke_checks_configuration_three_results_and_complete_state(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_smoke()
    assert result["final_status"].application_harness_state == int(ApplicationHarnessState.COMPLETE)
    assert connection.configurations_accepted == 1
    assert connection.instructions_accepted == 3
    assert connection.results_encoded == 3
    close(runner, trace, connection)


def test_application_v03_runs_fixed_and_sparse_variable_transactions(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_v03()
    assert result["application_scenario"] == "application-v03"
    assert [case["result_ticks"] for case in result["cases"]] == [[0, 1, 2], [0, 1, 2]]
    assert result["cases"][0]["instruction_family"] == "FIXED"
    assert result["cases"][1]["instruction_family"] == "VARIABLE"
    assert connection.results_encoded == 6
    close(runner, trace, connection)


def test_application_boundaries_complete_independent_transactions_and_local_oversize(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_boundaries()
    assert result["encoded_sizes"] == [194, 210, 449, 73, 62]
    assert connection.configurations_accepted == 2
    assert connection.instructions_accepted == 2
    close(runner, trace, connection)


def test_application_negative_keeps_transaction_usable(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_negative()
    assert result["decode_failures_delta"] == 1
    assert result["semantic_rejections_delta"] == 1
    assert connection.invalid_hrtp_messages == 0
    assert result["final_status"].application_harness_state == int(ApplicationHarnessState.COMPLETE)
    close(runner, trace, connection)


def test_application_repeat_uses_fresh_test_ids_and_detects_no_stale_results(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_repeat(4)
    assert result["completed"] == 4
    assert len(set(result["test_ids_hex"])) == 4
    assert connection.configurations_accepted == 4
    assert connection.instructions_accepted == 4
    close(runner, trace, connection)


def test_application_message_fixtures_round_trip_controls_responses_and_errors(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_message_fixtures()
    assert result["case_counts"] == {
        "system_information": 1,
        "execution_control": 2,
        "global_control": 1,
        "response_scopes": 5,
        "error_forms": 3,
        "total": 12,
    }
    assert len(set(result["test_ids_hex"].values())) == 3
    assert result["discovery"].protocol_version == protocol.PROTOCOL_VERSION
    assert [item["size"] for item in result["sizes"][3:8]] == [36] * 5
    assert [item["size"] for item in result["sizes"][-3:]] == [35, 47, 290]
    assert result["failure_deltas"] == {
        "decode_failures": 0,
        "semantic_rejections": 0,
        "encode_failures": 0,
    }
    assert connection.semantic_rejections == 0
    close(runner, trace, connection)


def test_application_discovery_rejects_mismatched_patch_version(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    wire = bytearray(
        runner.application_codec.encode(
            protocol.SystemInfoResponse(
                protocol.PROTOCOL_VERSION,
                protocol.ProtocolVersion(63, 0, 0),
            )
        )
    )
    wire[27] = 1  # SystemInfoResponse protocol-version patch in the public v0.3.0 wire form.
    connection.messages.append(
        ReceivedApplicationMessage(bytes(wire), connection.link_generation or 0, 0)
    )
    with pytest.raises(ScenarioFailure, match="discovery protocol version mismatch"):
        runner.discover_application()
    runner.close()
    trace.finish(
        passed=False, failure_reason="version mismatch", diagnostics=connection.get_diagnostics()
    )


def test_application_exchange_rejects_mismatched_and_duplicate_responses(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    runner.discover_application()
    expected = response_fixtures(protocol.TestId(bytes(range(16))))[0]
    mismatch = replace(expected, detail=1)
    connection.messages.append(
        ReceivedApplicationMessage(
            runner.application_codec.encode(mismatch), connection.link_generation or 0, 0
        )
    )
    with pytest.raises(ScenarioFailure, match="did not match the expected value"):
        runner._exchange_application(expected, expected, kind="APPLICATION_RESPONSE_TEST")
    runner.close()
    trace.finish(passed=False, failure_reason="mismatch", diagnostics=connection.get_diagnostics())

    runner, connection, trace = make_runner(tmp_path)
    connection.duplicate_application_responses = True
    with pytest.raises(ScenarioFailure, match="duplicate Application response"):
        runner.run_application_message_fixtures()
    runner.close()
    trace.finish(passed=False, failure_reason="duplicate", diagnostics=connection.get_diagnostics())


def test_duplicate_fixed_result_is_rejected(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    connection.duplicate_results = True
    with pytest.raises(ScenarioFailure, match="duplicate TestResult"):
        runner.run_application_smoke()
    runner.close()
    trace.finish(passed=False, failure_reason="duplicate", diagnostics=connection.get_diagnostics())


def test_raw_hrtp_and_application_classification_preserves_other_message(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    test_id = protocol.TestId(bytes(range(16)))
    application_wire = runner.application_codec.encode(representative_configuration(test_id))
    hrtp_wire = encode_message(Opcode.ECHO_RESPONSE, 99, b"x", max_application_message_size=512)
    connection.messages.extend(
        [
            ReceivedApplicationMessage(application_wire, connection.link_generation or 0, 1),
            ReceivedApplicationMessage(hrtp_wire, connection.link_generation or 0, 2),
        ]
    )
    runner._service()
    raw = runner.wait_for_raw_application_message()
    assert raw.data == application_wire
    raw = runner.wait_for_raw_application_message()
    assert raw.data == hrtp_wire
    close(runner, trace, connection)


@pytest.mark.parametrize(
    ("attribute", "value", "match"),
    [
        ("compatibility_profile_id", 0xDEADBEEF, "compatibility profile mismatch"),
        ("protocol_version", (0, 2, 1), "protocol version mismatch"),
        ("codec_initialized", 0, "not initialized"),
        (
            "initialization_status",
            int(protocol.ApplicationStatus.INTERNAL_ERROR),
            "initialization failed",
        ),
    ],
)
def test_status_compatibility_rejections(
    tmp_path: Path, attribute: str, value: object, match: str
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    setattr(connection, attribute, value)
    with pytest.raises(ScenarioFailure, match=match):
        runner.run_status()
    runner.close()
    trace.finish(passed=False, failure_reason=match, diagnostics=connection.get_diagnostics())


def test_application_reset_reconnect_clears_old_transaction_and_completes_new_one(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)

    def prompt(_: str) -> None:
        connection.disconnect_on_service = True

    result = runner.run_application_reset_reconnect(prompt=prompt)
    assert result["physical_disconnect_observed"] is True
    assert "mcu_reset_verified" not in result
    assert result["new_transport_session_established"] is True
    assert result["post_reconnect_transaction_succeeded"] is True
    assert result["old_test_id_hex"] != result["new_test_id_hex"]
    assert result["final_status"].application_harness_state == int(ApplicationHarnessState.COMPLETE)
    records = [
        json.loads(line) for line in trace.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    discoveries = [
        record for record in records if record["kind"] == "application_discovery_complete"
    ]
    assert [record["link_generation"] for record in discoveries] == [1, 2]
    close(runner, trace, connection)


def test_application_trace_contains_message_result_delivery_and_status_evidence(
    tmp_path: Path,
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    runner.run_application_smoke()
    records = [
        json.loads(line) for line in trace.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    kinds = {record["kind"] for record in records}
    assert "application_message_encoded" in kinds
    assert "delivery_confirmed" in kinds
    assert "application_result_decoded" in kinds
    assert "status_decoded" in kinds
    encoded = next(record for record in records if record["kind"] == "application_message_encoded")
    assert "test_id_hex" in encoded
    assert "payload_sha256" in encoded
    assert "encoded_message_type" in encoded
    close(runner, trace, connection)


def test_delivery_confirmation_count_tracks_each_reliable_submission(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    baseline = runner.delivery_confirmation_count
    runner.run_echo(b"delivery")
    assert runner.delivery_confirmation_count == baseline + 1
    status_baseline = runner.delivery_confirmation_count
    runner.run_status()
    assert runner.delivery_confirmation_count == status_baseline + 1
    close(runner, trace, connection)


def test_stale_application_payload_is_rejected_before_raw_delivery(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    test_id = protocol.TestId(bytes(range(16)))
    application_wire = runner.application_codec.encode(representative_configuration(test_id))
    generation = connection.link_generation or 0
    connection.messages.extend(
        [
            ReceivedApplicationMessage(application_wire, generation - 1, 1),
            ReceivedApplicationMessage(application_wire, generation, 2),
        ]
    )
    runner._service()
    received = runner.wait_for_raw_application_message()
    assert received.link_generation == generation
    assert received.data == application_wire
    records = [
        json.loads(line) for line in trace.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(record["kind"] == "stale_application_message" for record in records)
    close(runner, trace, connection)


def test_application_summary_contains_compatibility_and_semantic_evidence(tmp_path: Path) -> None:
    runner, connection, trace = make_runner(tmp_path)
    result = runner.run_application_smoke()
    runner.close()
    trace.finish(
        passed=True,
        failure_reason=None,
        diagnostics=connection.get_diagnostics(),
        extra={"result": result},
    )
    summary = json.loads(trace.summary_path.read_text(encoding="utf-8"))
    assert summary["scenario"] == "application-unit"
    assert summary["protocol_version"] == [0, 3, 0]
    assert summary["compatibility_profile_id"] == COMPATIBILITY_PROFILE_ID
    assert summary["application_codec_config"]["max_encoded_message_size"] == 512
    assert summary["result"]["test_id_hex"]
    assert summary["result"]["configuration_digest"] == REPRESENTATIVE_CONFIGURATION_DIGEST
    assert summary["result"]["instruction_digests"] == list(INSTRUCTION_DIGESTS)


def test_firmware_response_detects_production_oracle_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, connection, trace = make_runner(tmp_path)
    monkeypatch.setattr(
        "hilrig.protocol_test.runner.expected_result",
        lambda configuration, instruction: protocol.TestResult(
            test_id=instruction.test_id, tick_number=instruction.tick_number
        ),
    )
    try:
        with pytest.raises(ScenarioFailure, match="did not match deterministic oracle"):
            runner.run_application_smoke()
    finally:
        close(runner, trace, connection)
