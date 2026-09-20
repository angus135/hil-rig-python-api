from __future__ import annotations

import time
from collections import deque
from dataclasses import replace

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
    configured_initial_instruction,
    execution_control_response,
    global_control_response,
    instruction_semantic_digest,
    make_application_codec,
    representative_configuration,
    representative_instructions,
    response_fixtures,
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


_APPLICATION_MESSAGE_TYPES = {
    protocol.SystemInfoRequest: 1,
    protocol.TestConfiguration: 16,
    protocol.TestInstruction: 17,
    protocol.ExecutionControl: 19,
    protocol.GlobalControl: 20,
    protocol.ApplicationResponse: 48,
    protocol.ApplicationErrorMessage: 49,
}


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
            if self.pending_response is None and hasattr(self, "behavior"):
                follow_on = self.behavior.poll_output()
                if follow_on is not None:
                    self.pending_response = follow_on
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
        self.protocol_version_confirmed = False
        self.active_configuration = None
        self.state = ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        self.next_tick = 0
        self.active_expected_tick_count = 0
        self.v03_mode = False
        self.v03_instructions: dict[int, protocol.TestInstruction] = {}
        self.v03_updates: dict[int, list[protocol.LogicalOperation]] = {}
        self.current_update_tick: int | None = None
        self.v03_result_queue: deque[bytes] = deque()
        self.selected_instruction_family = 0
        self.selected_result_family = 0
        self.completed_instruction_ticks = 0
        self.current_chunk_count = 0
        self.maximum_chunk_count = 0
        self.finalization_requests = 0
        self.accepted_finalizations = 0
        self.variable_operations_accepted = 0
        self.result_records_emitted = 0
        self.selected_test_profile = 0
        self.selected_fault_mode = 0

    def _reset_transaction(self) -> None:
        self.active_configuration = None
        self.state = ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        self.next_tick = 0
        self.active_expected_tick_count = 0
        self.v03_mode = False
        self.v03_instructions.clear()
        self.v03_updates.clear()
        self.current_update_tick = None
        self.v03_result_queue.clear()
        self.selected_instruction_family = 0
        self.selected_result_family = 0
        self.current_chunk_count = 0
        self.selected_test_profile = 0
        self.selected_fault_mode = 0

    def reset_session(self) -> None:
        self.protocol_version_confirmed = False
        self._reset_transaction()

    @staticmethod
    def _application_response(
        test_id: protocol.TestId | None,
        scope: protocol.ResponseScope,
        outcome: protocol.ResponseOutcome,
        *,
        tick_number: int = 0,
        control_command: protocol.ControlCommand = protocol.ControlCommand.INVALID,
        reason: protocol.ResponseReason = protocol.ResponseReason.NONE,
        global_control_command: protocol.GlobalControlCommand = (
            protocol.GlobalControlCommand.INVALID
        ),
    ) -> protocol.ApplicationResponse:
        return protocol.ApplicationResponse(
            test_id,
            scope,
            outcome,
            reason,
            tick_number=tick_number,
            control_command=control_command,
            global_control_command=global_control_command,
        )

    def _reject(
        self,
        message: object,
        scope: protocol.ResponseScope,
        reason: protocol.ResponseReason,
        *,
        tick_number: int = 0,
    ) -> bytes:
        self.semantic_rejections += 1
        self.last_application_status = int(protocol.ApplicationStatus.VALIDATION_FAILED)
        self.current_update_tick = None
        self.current_chunk_count = 0
        if self.state in {
            ApplicationHarnessState.ACCEPTING_INSTRUCTIONS,
            ApplicationHarnessState.READY_TO_START,
        }:
            self.state = ApplicationHarnessState.UPLOAD_INVALID
        test_id = getattr(message, "test_id", None)
        control_command = (
            message.command
            if isinstance(message, protocol.ExecutionControl)
            else protocol.ControlCommand.INVALID
        )
        global_control_command = (
            message.command
            if isinstance(message, protocol.GlobalControl)
            else protocol.GlobalControlCommand.INVALID
        )
        return self.codec.encode(
            self._application_response(
                None if isinstance(message, protocol.GlobalControl) else test_id,
                scope,
                protocol.ResponseOutcome.REJECTED,
                tick_number=tick_number,
                control_command=control_command,
                reason=reason,
                global_control_command=global_control_command,
            )
        )

    def _unconfirmed_response(self, message: object) -> bytes | None:
        if isinstance(message, protocol.TestConfiguration):
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.TEST_CONFIGURATION,
                protocol.ResponseOutcome.REJECTED,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            )
        elif isinstance(message, (protocol.TestInstruction, protocol.UpdateInstruction)):
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.TICK,
                protocol.ResponseOutcome.REJECTED,
                tick_number=message.tick_number,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            )
        elif isinstance(message, protocol.FinalizeTestUpload):
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.COMPLETE_TEST,
                protocol.ResponseOutcome.REJECTED,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            )
        elif isinstance(message, protocol.ExecutionControl):
            response = self._application_response(
                message.test_id,
                protocol.ResponseScope.EXECUTION_CONTROL,
                protocol.ResponseOutcome.REJECTED,
                control_command=message.command,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            )
        elif isinstance(message, protocol.GlobalControl):
            response = self._application_response(
                None,
                protocol.ResponseScope.GLOBAL_CONTROL,
                protocol.ResponseOutcome.REJECTED,
                global_control_command=message.command,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            )
        else:
            return None
        return self.codec.encode(response)

    @staticmethod
    def _operation_enabled(
        configuration: protocol.TestConfiguration, operation: protocol.LogicalOperation
    ) -> bool:
        channel = operation.channel
        if operation.peripheral_type is protocol.PeripheralType.DIGITAL_OUTPUT:
            return (
                channel < len(configuration.digital_out)
                and configuration.digital_out[channel].enabled
                and channel < len(configuration.digital_in)
                and configuration.digital_in[channel].enabled
            )
        if operation.peripheral_type is protocol.PeripheralType.ANALOG_OUTPUT:
            return (
                channel < len(configuration.analog_out)
                and configuration.analog_out[channel].enabled
                and channel < len(configuration.analog_in)
                and configuration.analog_in[channel].enabled
            )
        if operation.peripheral_type is protocol.PeripheralType.PWM_OUTPUT:
            return (
                channel < len(configuration.pwm_out)
                and configuration.pwm_out[channel].enabled
                and channel < len(configuration.pwm_in)
                and configuration.pwm_in[channel].enabled
            )
        if operation.peripheral_type is protocol.PeripheralType.UART:
            return channel < len(configuration.uart) and configuration.uart[channel].enabled
        if operation.peripheral_type is protocol.PeripheralType.SPI:
            return channel < len(configuration.spi) and configuration.spi[channel].enabled
        if operation.peripheral_type is protocol.PeripheralType.CAN:
            return channel < len(configuration.can) and configuration.can[channel].enabled
        return False

    def status_payload(self) -> bytes:
        import struct

        values = [0] * 48
        values[0] = 3
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
        values[32] = self.selected_instruction_family
        values[33] = self.selected_result_family
        values[34] = self.completed_instruction_ticks
        values[35] = self.current_chunk_count
        values[36] = self.maximum_chunk_count
        values[37] = self.finalization_requests
        values[38] = self.accepted_finalizations
        values[39] = self.variable_operations_accepted
        values[40] = self.result_records_emitted
        values[45] = self.selected_test_profile
        values[46] = self.selected_fault_mode
        return struct.pack("<48I", *values)

    def poll_output(self) -> bytes | None:
        if self.v03_result_queue:
            return self.v03_result_queue.popleft()
        if self.state is ApplicationHarnessState.EMITTING_RESULTS:
            self.state = ApplicationHarnessState.COMPLETE
        return None

    def _queue_v03_results(self) -> None:
        assert self.active_configuration is not None
        test_id = self.active_configuration.test_id
        variable = (
            self.active_configuration.extension_data.startswith(b"HTV3")
            and self.active_configuration.extension_data[4] == 1
        )
        for tick in range(self.active_expected_tick_count):
            if variable:
                operations = self.v03_updates.get(tick, [])
                records: list[protocol.CapturedRecord] = []
                for operation in operations:
                    result_type = {
                        protocol.PeripheralType.DIGITAL_OUTPUT: (
                            protocol.PeripheralType.DIGITAL_INPUT
                        ),
                        protocol.PeripheralType.ANALOG_OUTPUT: (
                            protocol.PeripheralType.ANALOG_INPUT
                        ),
                        protocol.PeripheralType.PWM_OUTPUT: protocol.PeripheralType.PWM_INPUT,
                    }.get(operation.peripheral_type, operation.peripheral_type)
                    payload = operation.payload
                    if operation.peripheral_type is protocol.PeripheralType.SPI:
                        packet_count = payload[0]
                        payload = payload[1 + packet_count :]
                    records.append(protocol.CapturedRecord(result_type, operation.channel, payload))
                result = protocol.VariableTestResult(
                    test_id, tick, protocol.ResultCondition.OK, 0, 0, tuple(records)
                )
                chunks: list[bytes] = []
                current: list[protocol.CapturedRecord] = []
                for record in result.records:
                    candidate = replace(result, records=tuple(current + [record]), flags=0)
                    pair = (record.peripheral_type, record.channel)
                    if any(
                        (existing.peripheral_type, existing.channel) == pair for existing in current
                    ):
                        encoded = b""
                    else:
                        try:
                            encoded = self.codec.encode(candidate)
                        except protocol.ApplicationEncodeError:
                            encoded = b""
                    if current and not encoded:
                        chunks.append(
                            self.codec.encode(replace(result, records=tuple(current), flags=1))
                        )
                        current = []
                    current.append(record)
                if current:
                    chunks.append(self.codec.encode(replace(result, records=tuple(current))))
                if not chunks:
                    chunks.append(self.codec.encode(result))
                self.v03_result_queue.extend(chunks)
                self.result_records_emitted += len(records)
            else:
                result = firmware_result(
                    self.active_configuration,
                    self.v03_instructions.get(
                        tick,
                        configured_initial_instruction(self.active_configuration, tick),
                    ),
                )
                self.v03_result_queue.append(self.codec.encode(result))
            self.results_encoded += 1

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
        self.last_message_type = _APPLICATION_MESSAGE_TYPES.get(type(message), 0)
        if type(message) is protocol.SystemInfoRequest:
            self.protocol_version_confirmed = message.protocol_version == protocol.PROTOCOL_VERSION
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            try:
                return self.codec.encode(
                    protocol.SystemInfoResponse(
                        protocol.PROTOCOL_VERSION,
                        protocol.ProtocolVersion(63, 0, 0),
                        b"firmware-pr-63",
                        b"63",
                    )
                )
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return None
        if not self.protocol_version_confirmed:
            self.semantic_rejections += 1
            self.last_application_status = int(protocol.ApplicationStatus.VERSION_MISMATCH)
            return self._unconfirmed_response(message)
        if type(message) is protocol.TestConfiguration:
            if self.state not in {
                ApplicationHarnessState.WAITING_FOR_CONFIGURATION,
                ApplicationHarnessState.COMPLETE,
                ApplicationHarnessState.UPLOAD_INVALID,
            }:
                return self._reject(
                    message,
                    protocol.ResponseScope.TEST_CONFIGURATION,
                    protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                )
            if message.extension_data.startswith(b"HTV3") and (
                len(message.extension_data) < 8
                or message.extension_data[4] > 1
                or message.extension_data[5] > 2
            ):
                return self._reject(
                    message,
                    protocol.ResponseScope.TEST_CONFIGURATION,
                    protocol.ResponseReason.VALIDATION_FAILED,
                )
            if message.expected_tick_count > 4:
                return self._reject(
                    message,
                    protocol.ResponseScope.TEST_CONFIGURATION,
                    protocol.ResponseReason.STORAGE_UNAVAILABLE,
                )
            self._reset_transaction()
            self.v03_mode = True
            self.active_configuration = message
            if message.extension_data.startswith(b"HTV3") and len(message.extension_data) >= 8:
                self.selected_result_family = message.extension_data[4]
                self.selected_fault_mode = message.extension_data[5]
                self.selected_test_profile = self.selected_result_family + (
                    self.selected_fault_mode << 8
                )
            self.configurations_accepted += 1
            self.configuration_digest = configuration_semantic_digest(message)
            self.state = ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
            self.next_tick = 0
            self.active_expected_tick_count = message.expected_tick_count
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return self.codec.encode(
                self._application_response(
                    message.test_id,
                    protocol.ResponseScope.TEST_CONFIGURATION,
                    protocol.ResponseOutcome.ACCEPTED,
                )
            )
        if type(message) is protocol.TestInstruction:
            if self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                    tick_number=message.tick_number,
                )
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.INCONSISTENT_TEST_ID,
                    tick_number=message.tick_number,
                )
            if message.tick_number != self.next_tick:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.INVALID_TICK,
                    tick_number=message.tick_number,
                )
            self.v03_instructions[message.tick_number] = message
            self.instructions_accepted += 1
            self.selected_instruction_family = 1
            self.completed_instruction_ticks += 1
            self.last_instruction_digest = instruction_semantic_digest(message)
            self.next_tick = message.tick_number + 1
            self.last_application_status = int(protocol.ApplicationStatus.OK)
            return self.codec.encode(
                self._application_response(
                    message.test_id,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseOutcome.ACCEPTED,
                    tick_number=message.tick_number,
                )
            )
        if type(message) is protocol.UpdateInstruction:
            if self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                    tick_number=message.tick_number,
                )
            assert self.active_configuration is not None
            if message.test_id != self.active_configuration.test_id:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.INCONSISTENT_TEST_ID,
                    tick_number=message.tick_number,
                )
            if message.tick_number >= self.active_expected_tick_count:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.INVALID_TICK,
                    tick_number=message.tick_number,
                )
            if self.selected_instruction_family == 1:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                    tick_number=message.tick_number,
                )
            if self.current_update_tick is None:
                if self.v03_updates and message.tick_number <= max(self.v03_updates):
                    return self._reject(
                        message,
                        protocol.ResponseScope.TICK,
                        protocol.ResponseReason.INVALID_TICK,
                        tick_number=message.tick_number,
                    )
                self.current_update_tick = message.tick_number
            elif message.tick_number != self.current_update_tick:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.INVALID_TICK,
                    tick_number=message.tick_number,
                )
            if self.current_chunk_count >= 8:
                return self._reject(
                    message,
                    protocol.ResponseScope.TICK,
                    protocol.ResponseReason.STORAGE_UNAVAILABLE,
                    tick_number=message.tick_number,
                )
            existing = self.v03_updates.get(message.tick_number, [])
            for operation in message.operations:
                if not self._operation_enabled(self.active_configuration, operation):
                    return self._reject(
                        message,
                        protocol.ResponseScope.TICK,
                        protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                        tick_number=message.tick_number,
                    )
                if operation.peripheral_type not in {
                    protocol.PeripheralType.UART,
                    protocol.PeripheralType.SPI,
                    protocol.PeripheralType.CAN,
                } and any(
                    prior.peripheral_type is operation.peripheral_type
                    and prior.channel == operation.channel
                    for prior in existing
                ):
                    return self._reject(
                        message,
                        protocol.ResponseScope.TICK,
                        protocol.ResponseReason.VALIDATION_FAILED,
                        tick_number=message.tick_number,
                    )
            self.v03_updates.setdefault(message.tick_number, []).extend(message.operations)
            self.selected_instruction_family = 2
            self.current_chunk_count += 1
            self.maximum_chunk_count = max(self.maximum_chunk_count, self.current_chunk_count)
            self.variable_operations_accepted += len(message.operations)
            self.instructions_accepted += 1 if message.flags == 0 else 0
            if message.flags == 0:
                self.completed_instruction_ticks += 1
                self.current_chunk_count = 0
                self.current_update_tick = None
                return self.codec.encode(
                    self._application_response(
                        message.test_id,
                        protocol.ResponseScope.TICK,
                        protocol.ResponseOutcome.ACCEPTED,
                        tick_number=message.tick_number,
                    )
                )
            return None
        if type(message) is protocol.FinalizeTestUpload:
            self.finalization_requests += 1
            if (
                self.state is not ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
                or self.active_configuration is None
                or message.test_id != self.active_configuration.test_id
            ):
                return self._reject(
                    message,
                    protocol.ResponseScope.COMPLETE_TEST,
                    protocol.ResponseReason.INCONSISTENT_TEST_ID
                    if self.active_configuration is not None
                    and message.test_id != self.active_configuration.test_id
                    else protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                )
            self.state = ApplicationHarnessState.READY_TO_START
            self.accepted_finalizations += 1
            return self.codec.encode(
                self._application_response(
                    self.active_configuration.test_id,
                    protocol.ResponseScope.COMPLETE_TEST,
                    protocol.ResponseOutcome.ACCEPTED,
                )
            )
        if type(message) is protocol.ExecutionControl:
            if (
                self.active_configuration is None
                and message.command is not protocol.ControlCommand.ABORT
            ):
                return self._reject(
                    message,
                    protocol.ResponseScope.EXECUTION_CONTROL,
                    protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                )
            if (
                self.active_configuration is not None
                and message.test_id != self.active_configuration.test_id
            ):
                return self._reject(
                    message,
                    protocol.ResponseScope.EXECUTION_CONTROL,
                    protocol.ResponseReason.INCONSISTENT_TEST_ID,
                )
            if message.command is protocol.ControlCommand.START:
                if self.state is not ApplicationHarnessState.READY_TO_START:
                    return self._reject(
                        message,
                        protocol.ResponseScope.EXECUTION_CONTROL,
                        protocol.ResponseReason.OPERATION_NOT_ALLOWED,
                    )
                self.state = ApplicationHarnessState.EMITTING_RESULTS
                self._queue_v03_results()
                return self.codec.encode(execution_control_response(message))
            response = self.codec.encode(execution_control_response(message))
            self._reset_transaction()
            return response
        if type(message) is protocol.GlobalControl:
            if message.command is protocol.GlobalControlCommand.RESET_APPLICATION:
                self._reset_transaction()
            try:
                return self.codec.encode(global_control_response(message))
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return None
        if type(message) in {protocol.ApplicationResponse, protocol.ApplicationErrorMessage}:
            try:
                return self.codec.encode(message)
            except protocol.ApplicationEncodeError:
                self.encode_failures += 1
                return None
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

    def close(self) -> None:
        self.behavior.reset_session()
        super().close()


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


def test_real_transport_fake_version_gate_and_message_type_diagnostics() -> None:
    behavior = ApplicationRigBehavior()
    test_id = protocol.TestId(bytes(range(16)))
    configuration = representative_configuration(test_id)

    pre_discovery_wire = behavior._application(behavior.codec.encode(configuration))
    assert pre_discovery_wire is not None
    pre_discovery_response = behavior.codec.decode(pre_discovery_wire)
    assert type(pre_discovery_response) is protocol.ApplicationResponse
    assert pre_discovery_response.outcome is protocol.ResponseOutcome.REJECTED
    assert behavior.protocol_version_confirmed is False
    assert behavior.last_message_type == 16
    assert behavior.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)

    discovery_wire = behavior._application(behavior.codec.encode(protocol.SystemInfoRequest()))
    assert discovery_wire is not None
    discovery_response = behavior.codec.decode(discovery_wire)
    assert type(discovery_response) is protocol.SystemInfoResponse
    assert discovery_response.protocol_version == protocol.PROTOCOL_VERSION
    assert behavior.protocol_version_confirmed is True
    assert behavior.last_message_type == 1

    configuration_response = behavior._application(behavior.codec.encode(configuration))
    assert configuration_response is not None
    assert (
        behavior.codec.decode(configuration_response).outcome is protocol.ResponseOutcome.ACCEPTED
    )
    assert behavior.last_message_type == 16
    tick_wire = behavior._application(
        behavior.codec.encode(representative_instructions(test_id)[0])
    )
    assert tick_wire is not None
    assert behavior.codec.decode(tick_wire).outcome is protocol.ResponseOutcome.ACCEPTED
    assert behavior.last_message_type == 17

    finalize_wire = behavior._application(
        behavior.codec.encode(protocol.FinalizeTestUpload(test_id))
    )
    assert finalize_wire is not None
    assert behavior.codec.decode(finalize_wire).outcome is protocol.ResponseOutcome.ACCEPTED

    for command in (protocol.ControlCommand.START, protocol.ControlCommand.ABORT):
        control_wire = behavior._application(
            behavior.codec.encode(protocol.ExecutionControl(test_id, command))
        )
        assert control_wire is not None
        control_response = behavior.codec.decode(control_wire)
        assert type(control_response) is protocol.ApplicationResponse
        assert control_response.outcome is protocol.ResponseOutcome.COMPLETED
        if command is protocol.ControlCommand.START:
            assert behavior.poll_output() is not None
        assert behavior.last_message_type == 19

    reset = protocol.GlobalControl(protocol.GlobalControlCommand.RESET_APPLICATION)
    reset_wire = behavior._application(behavior.codec.encode(reset))
    assert reset_wire is not None
    reset_response = behavior.codec.decode(reset_wire)
    assert type(reset_response) is protocol.ApplicationResponse
    assert reset_response.outcome is protocol.ResponseOutcome.COMPLETED
    assert behavior.last_message_type == 20

    response_wire = behavior._application(behavior.codec.encode(response_fixtures(test_id)[0]))
    assert response_wire is not None
    assert behavior.last_message_type == 48
    error_wire = behavior._application(
        behavior.codec.encode(
            protocol.ApplicationErrorMessage(None, protocol.ErrorCategory.PROTOCOL, True)
        )
    )
    assert error_wire is not None
    assert behavior.last_message_type == 49

    behavior.reset_session()
    assert behavior.protocol_version_confirmed is False
    pre_discovery_wire = behavior._application(behavior.codec.encode(configuration))
    assert pre_discovery_wire is not None
    assert behavior.codec.decode(pre_discovery_wire).outcome is protocol.ResponseOutcome.REJECTED
    assert behavior.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)

    foreign_request = bytearray(behavior.codec.encode(protocol.SystemInfoRequest()))
    foreign_request[1] = 3
    foreign_request[29] = 3
    foreign_wire = behavior._application(bytes(foreign_request))
    assert foreign_wire is not None
    foreign_response = behavior.codec.decode(foreign_wire)
    assert type(foreign_response) is protocol.SystemInfoResponse
    assert foreign_response.protocol_version == protocol.PROTOCOL_VERSION
    assert behavior.protocol_version_confirmed is False
    reset_wire = behavior._application(behavior.codec.encode(reset))
    assert reset_wire is not None
    assert behavior.codec.decode(reset_wire).outcome is protocol.ResponseOutcome.REJECTED
    assert behavior.last_message_type == 20
    assert behavior.last_application_status == int(protocol.ApplicationStatus.VERSION_MISMATCH)


@pytest.mark.parametrize(
    "scenario",
    [
        "application-smoke",
        "application-boundaries",
        "application-negative",
        "application-repeat",
        "application-v03",
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
        source_evidence={"protocol_declared_version": "0.3.0"},
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
            assert result["encoded_sizes"] == [194, 210, 449, 73, 62]
        elif scenario == "application-negative":
            result = runner.run_application_negative()
            assert result["decode_failures_delta"] == 1
            assert result["semantic_rejections_delta"] == 1
        elif scenario == "application-v03":
            result = runner.run_application_v03()
            assert result["application_scenario"] == "application-v03"
            assert [case["result_ticks"] for case in result["cases"]] == [
                [0, 1, 2],
                [0, 1, 2],
                [0, 1, 2],
                [0],
                [0],
            ]
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
