"""Caller-driven composition of pySerial, Transport, and Application workflows."""

from __future__ import annotations

import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Any

from hilrig.exceptions import ProtocolSessionError
from hilrig.models.execution import CompiledTestIR
from hilrig.models.identifiers import UploadAttempt, application_test_id_from_bytes
from hilrig.protocol.application import (
    FixedIOProtocolAdapter,
    FixedIOUploadMessages,
    ProtocolFamily,
    VariableIOProtocolAdapter,
    VariableIOUploadMessages,
    ResponseCorrelation,
    UploadOperation,
    UploadOperationKind,
    UploadPlan,
)
from hilrig.protocol.manual import ManualApplicationMessage
from hilrig.protocol.serial import SerialConnectionSettings, open_serial_port
from hilrig.results.adapter import IncomingResultAdapter
from hilrig.results.builder import CapturedRunBuilder
from hilrig.results.models import ApplicationErrorRecord, TickResult

_UINT32_MASK = (1 << 32) - 1
_DEFAULT_RETRANSMIT_TIMEOUT_MS = 250
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_APPLICATION_RESPONSE_TIMEOUT_S = 30.0


def monotonic_now_ms() -> int:
    """Return monotonic milliseconds in Transport's wrapped uint32 domain."""
    return int(time.monotonic() * 1_000) & _UINT32_MASK


class ProtocolWorkflowState(str, Enum):
    """Host-visible state of the Application transaction layered over Transport."""

    CONNECTING = "connecting"
    DISCOVERING = "discovering"
    READY = "ready"
    MANUAL_READY = "manual_ready"
    MANUAL_SENDING = "manual_sending"
    WAITING_FOR_OPERATOR = "waiting_for_operator"
    CONFIGURING = "configuring"
    UPLOADING = "uploading"
    VALIDATING = "validating"
    READY_TO_START = "ready_to_start"
    STARTING = "starting"
    RUNNING = "running"
    ABORTING = "aborting"
    RESETTING = "resetting"
    RESULTS_COMPLETE = "results_complete"
    ABORTED = "aborted"
    FAILED = "failed"


class UploadAdvanceMode(str, Enum):
    """How semantic upload operations are released to Transport."""

    AUTOMATIC = "automatic"
    OPERATOR_GATED = "operator_gated"


@dataclass(frozen=True, slots=True)
class RigSystemInfo:
    """Version and diagnostic information confirmed for one Transport session."""

    protocol_version: str
    firmware_version: str
    diagnostic_data: bytes
    firmware_git_hash: bytes


@dataclass(frozen=True, slots=True)
class ProtocolServiceReport:
    """Work completed by one non-blocking connection service pass."""

    events: tuple[object, ...]
    application_messages: tuple[object, ...]
    stored_tick_results: tuple[TickResult, ...]
    stored_application_errors: tuple[ApplicationErrorRecord, ...]
    serial_bytes_read: int
    serial_bytes_written: int
    application_message_submitted: bool
    workflow_state: ProtocolWorkflowState


@dataclass(frozen=True, slots=True)
class ManualSendResult:
    """Terminal result of one standalone manual Application-message send."""

    sequence: int
    label: str
    transport_delivered: bool
    application_response_required: bool
    application_response: object | None
    success: bool
    detail: str


@dataclass(slots=True)
class _ApplicationOperation:
    """One response-gated operation, containing one or more Transport messages."""

    kind: str
    encoded_messages: tuple[bytes, ...]
    response: ResponseCorrelation | None = None
    upload_operation: UploadOperation | None = None
    next_message_index: int = 0
    response_received: bool = False
    response_deadline: float | None = None
    response_error: str | None = None
    response_outcome: object | None = None
    previous_workflow_state: ProtocolWorkflowState | None = None
    manual_sequence: int | None = None
    manual_label: str | None = None
    application_response_required: bool = True

    @property
    def every_message_submitted(self) -> bool:
        return self.next_message_index == len(self.encoded_messages)


class FixedIOProtocolConnection:
    """Own one response-gated host Application session over USB CDC serial.

    All methods must be called on the thread that created the connection, matching
    the protocol wrapper's ownership rule. ``service`` is intentionally non-blocking;
    a CLI, GUI, or later asyncio coordinator decides how often to call it.
    """

    def __init__(
        self,
        *,
        serial_port: Any,
        application: FixedIOProtocolAdapter | VariableIOProtocolAdapter,
        transport: Any,
        result_adapter: IncomingResultAdapter | None = None,
        protocol_family: ProtocolFamily | str = ProtocolFamily.VARIABLE,
        application_response_timeout_s: float = _DEFAULT_APPLICATION_RESPONSE_TIMEOUT_S,
        manual_mode: bool = False,
        skip_system_info: bool = False,
    ) -> None:
        if (
            not isinstance(application_response_timeout_s, (int, float))
            or isinstance(application_response_timeout_s, bool)
            or application_response_timeout_s <= 0
        ):
            raise ValueError("application_response_timeout_s must be a positive number")

        if isinstance(protocol_family, str):
            try:
                protocol_family = ProtocolFamily(protocol_family.lower())
            except ValueError:
                raise ValueError(f"Unknown protocol family: {protocol_family!r}")
        elif not isinstance(protocol_family, ProtocolFamily):
            raise TypeError("protocol_family must be a ProtocolFamily or str")

        self.serial_port = serial_port
        self.application = application
        self.transport = transport
        self.result_adapter = result_adapter
        self.protocol_family = protocol_family
        self.protocol = application.protocol
        self.application_response_timeout_s = float(application_response_timeout_s)
        if not isinstance(manual_mode, bool):
            raise TypeError("manual_mode must be a bool")
        if not isinstance(skip_system_info, bool):
            raise TypeError("skip_system_info must be a bool")
        if skip_system_info and not manual_mode:
            raise ValueError("skip_system_info is only available in manual mode")
        self._manual_mode = manual_mode
        self._skip_system_info = skip_system_info

        self._incoming = bytearray()
        self._pending_output: bytes | None = None
        self._pending_output_offset = 0
        self._upload_operations: deque[UploadOperation] = deque()
        self._pending_operation: _ApplicationOperation | None = None
        self._gated_operation: UploadOperation | None = None
        self._transport_delivery_pending = False
        self._active_upload: FixedIOUploadMessages | VariableIOUploadMessages | None = None
        self._active_upload_plan: UploadPlan | None = None
        self._advance_mode = UploadAdvanceMode.AUTOMATIC
        self._upload_message_count = 0
        self._upload_messages_delivered = 0
        self._upload_accepted = False
        self._execution_started = False
        self._next_result_tick = 0
        self._session_confirmed = False
        self._session_info: RigSystemInfo | None = None
        self._last_application_response: object | None = None
        self._manual_send_sequence = 0
        self._last_manual_send_result: ManualSendResult | None = None
        self._retired_application_test_ids: set[int] = set()
        self._workflow_state = ProtocolWorkflowState.CONNECTING
        self._closed = False

    @classmethod
    def connect(
        cls,
        *,
        serial_settings: SerialConnectionSettings | None = None,
        transport_config: object | None = None,
        application_config: object | None = None,
        protocol_family: ProtocolFamily | str = ProtocolFamily.VARIABLE,
        result_builder: CapturedRunBuilder | None = None,
        application_response_timeout_s: float = _DEFAULT_APPLICATION_RESPONSE_TIMEOUT_S,
        protocol_module: ModuleType | Any | None = None,
        serial_factory: Any | None = None,
        comports: Any | None = None,
    ) -> FixedIOProtocolConnection:
        """Discover the exact device name, open it, and begin a host session."""
        if isinstance(protocol_family, str):
            try:
                protocol_family = ProtocolFamily(protocol_family.lower())
            except ValueError:
                raise ValueError(f"Unknown protocol family: {protocol_family!r}")
        elif not isinstance(protocol_family, ProtocolFamily):
            raise TypeError("protocol_family must be a ProtocolFamily or str")

        if protocol_family is ProtocolFamily.VARIABLE:
            application = VariableIOProtocolAdapter(
                protocol_module=protocol_module,
                application_config=application_config,
            )
        else:
            application = FixedIOProtocolAdapter(
                protocol_module=protocol_module,
                application_config=application_config,
            )
        p = application.protocol
        if transport_config is None:
            transport_config = p.TransportConfig(
                retransmit_timeout_ms=_DEFAULT_RETRANSMIT_TIMEOUT_MS,
                max_retries=_DEFAULT_MAX_RETRIES,
            )
        serial_port = open_serial_port(
            serial_settings,
            serial_factory=serial_factory,
            comports=comports,
        )
        try:
            transport = p.Transport(p.Role.HOST, transport_config)
            result_adapter = (
                IncomingResultAdapter(
                    result_builder,
                    protocol_module=p,
                    expected_family=protocol_family,
                )
                if result_builder is not None
                else None
            )
            connection = cls(
                serial_port=serial_port,
                application=application,
                transport=transport,
                result_adapter=result_adapter,
                protocol_family=protocol_family,
                application_response_timeout_s=application_response_timeout_s,
            )
            connection.transport.notify_link_state(p.LinkState.CONNECTED, monotonic_now_ms())
            return connection
        except Exception:
            with suppress(Exception):
                serial_port.close()
            raise

    @classmethod
    def connect_manual(
        cls,
        *,
        serial_settings: SerialConnectionSettings | None = None,
        transport_config: object | None = None,
        application_config: object | None = None,
        application_response_timeout_s: float = _DEFAULT_APPLICATION_RESPONSE_TIMEOUT_S,
        skip_system_info: bool = False,
        protocol_module: ModuleType | Any | None = None,
        serial_factory: Any | None = None,
        comports: Any | None = None,
    ) -> FixedIOProtocolConnection:
        """Open a persistent standalone Application-message debugging session."""
        application = VariableIOProtocolAdapter(
            protocol_module=protocol_module,
            application_config=application_config,
        )
        p = application.protocol
        if transport_config is None:
            transport_config = p.TransportConfig(
                retransmit_timeout_ms=_DEFAULT_RETRANSMIT_TIMEOUT_MS,
                max_retries=_DEFAULT_MAX_RETRIES,
            )
        serial_port = open_serial_port(
            serial_settings,
            serial_factory=serial_factory,
            comports=comports,
        )
        try:
            transport = p.Transport(p.Role.HOST, transport_config)
            connection = cls(
                serial_port=serial_port,
                application=application,
                transport=transport,
                application_response_timeout_s=application_response_timeout_s,
                manual_mode=True,
                skip_system_info=skip_system_info,
            )
            connection.transport.notify_link_state(p.LinkState.CONNECTED, monotonic_now_ms())
            return connection
        except Exception:
            with suppress(Exception):
                serial_port.close()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def workflow_state(self) -> ProtocolWorkflowState:
        return self._workflow_state

    @property
    def session_confirmed(self) -> bool:
        """Whether exact Application compatibility was confirmed for this session."""
        return self._session_confirmed

    @property
    def session_info(self) -> RigSystemInfo | None:
        return self._session_info

    @property
    def active_upload(self) -> FixedIOUploadMessages | VariableIOUploadMessages | None:
        return self._active_upload

    @property
    def active_upload_plan(self) -> UploadPlan | None:
        return self._active_upload_plan

    @property
    def advance_mode(self) -> UploadAdvanceMode:
        return self._advance_mode

    @property
    def waiting_for_operator(self) -> bool:
        return self._gated_operation is not None

    @property
    def next_upload_operation(self) -> UploadOperation | None:
        """Return the semantic operation currently waiting at the operator gate."""
        return self._gated_operation

    @property
    def last_application_response(self) -> object | None:
        return self._last_application_response

    @property
    def manual_mode(self) -> bool:
        return self._manual_mode

    @property
    def manual_send_pending(self) -> bool:
        return self._pending_operation is not None and self._pending_operation.kind == "manual"

    @property
    def last_manual_send_result(self) -> ManualSendResult | None:
        return self._last_manual_send_result

    @property
    def upload_delivery_complete(self) -> bool:
        """Whether every upload message, including finalization, was Transport-confirmed."""
        return (
            self._active_upload is not None
            and self._upload_message_count > 0
            and self._upload_messages_delivered == self._upload_message_count
        )

    @property
    def upload_accepted(self) -> bool:
        """Whether firmware accepted and completed validation of the sparse upload."""
        return self._upload_accepted

    @property
    def execution_started(self) -> bool:
        return self._execution_started

    @property
    def results_complete(self) -> bool:
        return self._workflow_state is ProtocolWorkflowState.RESULTS_COMPLETE

    @property
    def queued_application_message_count(self) -> int:
        pending = 0
        if self._pending_operation is not None:
            pending = len(self._pending_operation.encoded_messages) - (
                self._pending_operation.next_message_index
            )
        gated = (
            len(self._gated_operation.encoded_messages) if self._gated_operation is not None else 0
        )
        return (
            pending
            + gated
            + sum(len(operation.encoded_messages) for operation in self._upload_operations)
        )

    def bind_result_builder(
        self,
        builder: CapturedRunBuilder,
        *,
        replace: bool = False,
    ) -> None:
        """Bind result/error storage, optionally replacing an abandoned attempt."""
        self._require_open()
        if not isinstance(builder, CapturedRunBuilder):
            raise TypeError("builder must be a CapturedRunBuilder")
        if not isinstance(replace, bool):
            raise TypeError("replace must be a bool")
        if self.result_adapter is not None and not replace:
            raise ProtocolSessionError(
                "A captured-run builder is already bound; pass replace=True after "
                "finalizing an abandoned attempt"
            )
        if self.result_adapter is not None and self._active_upload is not None:
            raise ProtocolSessionError(
                "Cannot replace the captured-run builder while an upload is active"
            )
        if (
            self._active_upload is not None
            and builder.application_test_id
            != self._active_upload.upload_attempt.application_test_id
        ):
            raise ValueError("builder Application Test ID does not match the active upload")
        self.result_adapter = IncomingResultAdapter(
            builder,
            protocol_module=self.protocol,
            expected_family=self.protocol_family,
        )

    def queue_upload(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
        advance_mode: UploadAdvanceMode = UploadAdvanceMode.AUTOMATIC,
    ) -> UploadAttempt:
        """Queue configuration, sparse fixed-I/O states, and upload finalization."""
        self._require_open()
        if self._manual_mode:
            raise ProtocolSessionError("Test uploads are unavailable in a manual session")
        if not isinstance(advance_mode, UploadAdvanceMode):
            raise TypeError("advance_mode must be an UploadAdvanceMode")
        if (
            self._active_upload is not None
            or self._upload_operations
            or self._gated_operation is not None
        ):
            raise ProtocolSessionError("An Application upload is already active")
        if self._pending_operation is not None and self._pending_operation.kind != "discovery":
            raise ProtocolSessionError("Another Application operation is already in progress")

        if upload_attempt is None and self.result_adapter is not None:
            upload_attempt = UploadAttempt(
                definition_test_id=compiled_test.test_id,
                application_test_id=self.result_adapter.builder.application_test_id,
            )
        plan = self.application.build_upload_plan(
            compiled_test,
            upload_attempt=upload_attempt,
        )
        upload = plan.upload
        if upload.upload_attempt.application_test_id in self._retired_application_test_ids:
            raise ValueError(
                "upload_attempt reuses an abandoned Application Test ID; restart the "
                "UploadAttempt and bind a builder for the fresh ID"
            )
        if self.result_adapter is not None and (
            upload.upload_attempt.application_test_id
            != self.result_adapter.builder.application_test_id
            or compiled_test.test_id != self.result_adapter.builder.test_id
        ):
            raise ValueError(
                "upload attempt does not match the captured-run builder's definition and "
                "Application Test IDs"
            )

        self._upload_operations.extend(plan.transfer_operations)
        self._active_upload = upload
        self._active_upload_plan = plan
        self._advance_mode = advance_mode
        self._upload_message_count = sum(
            len(operation.encoded_messages) for operation in plan.transfer_operations
        )
        self._upload_messages_delivered = 0
        self._upload_accepted = False
        self._execution_started = False
        self._next_result_tick = 0
        self._advance_workflow()
        return upload.upload_attempt

    def queue_manual_message(
        self,
        message: ManualApplicationMessage,
        *,
        transport_only: bool = False,
    ) -> int:
        """Queue one standalone Application message in a manual session."""
        self._require_open()
        if not self._manual_mode:
            raise ProtocolSessionError("Manual messages require a manual connection")
        if not isinstance(message, ManualApplicationMessage):
            raise TypeError("message must be a ManualApplicationMessage")
        if not isinstance(transport_only, bool):
            raise TypeError("transport_only must be a bool")
        if not self._session_confirmed:
            raise ProtocolSessionError("The manual protocol session is not ready")
        self._require_no_pending_operation()
        self._manual_send_sequence += 1
        sequence = self._manual_send_sequence
        self._last_manual_send_result = None
        self._last_application_response = None
        self._activate_operation(
            _ApplicationOperation(
                kind="manual",
                encoded_messages=(message.encoded_message,),
                response=None if transport_only else message.response,
                manual_sequence=sequence,
                manual_label=message.label,
                application_response_required=not transport_only,
            )
        )
        return sequence

    def release_next_operation(self) -> UploadOperation:
        """Release exactly one semantic operation waiting at the operator gate."""
        self._require_open()
        if self._advance_mode is not UploadAdvanceMode.OPERATOR_GATED:
            raise ProtocolSessionError("The active upload is not operator-gated")
        operation = self._take_gated_operation()
        self._activate_upload_operation(operation)
        return operation

    def continue_upload(self) -> UploadOperation:
        """Release the waiting operation and run the remainder automatically."""
        self._require_open()
        if self._advance_mode is not UploadAdvanceMode.OPERATOR_GATED:
            raise ProtocolSessionError("The active upload is not operator-gated")
        operation = self._take_gated_operation()
        self._advance_mode = UploadAdvanceMode.AUTOMATIC
        self._activate_upload_operation(operation)
        return operation

    def send_upload(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
        timeout_s: float = 10.0,
        poll_interval_s: float = 0.001,
    ) -> UploadAttempt:
        """Service until firmware semantically accepts the complete sparse upload."""
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
            raise ValueError("timeout_s must be a positive number")
        if (
            not isinstance(poll_interval_s, (int, float))
            or isinstance(poll_interval_s, bool)
            or poll_interval_s < 0
        ):
            raise ValueError("poll_interval_s must be a non-negative number")

        attempt = self.queue_upload(compiled_test, upload_attempt=upload_attempt)
        deadline = time.monotonic() + float(timeout_s)
        while not self.upload_accepted:
            report = self.service()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Timed out before firmware accepted the complete fixed-I/O upload"
                )
            if poll_interval_s and not (
                report.serial_bytes_read
                or report.serial_bytes_written
                or report.application_message_submitted
            ):
                time.sleep(float(poll_interval_s))
        return attempt

    def start(self) -> None:
        """Queue START after Complete Test acceptance, normally for HOST_COMMAND."""
        self._require_open()
        if (
            self._active_upload is None
            or not self._upload_accepted
            or self._workflow_state is not ProtocolWorkflowState.READY_TO_START
        ):
            raise ProtocolSessionError("No accepted upload is ready to start")
        if self._active_upload.start_mode == "EXTERNAL_TRIGGER":
            raise ProtocolSessionError("EXTERNAL_TRIGGER has no protocol implementation")
        self._require_no_pending_operation()
        self._queue_start()

    def abort(self) -> None:
        """Queue a test-scoped ABORT request for the active upload attempt."""
        self._require_open()
        if self._active_upload is None:
            raise ProtocolSessionError("There is no active upload to abort")
        self._require_no_pending_operation()
        message = self.application.build_abort(self._active_upload.upload_attempt)
        self._activate_operation(
            _ApplicationOperation(
                kind="abort",
                encoded_messages=(self.application.encode(message),),
                response=ResponseCorrelation(
                    scope=self.protocol.ResponseScope.EXECUTION_CONTROL,
                    successful_outcome=self.protocol.ResponseOutcome.COMPLETED,
                    application_test_id=(self._active_upload.upload_attempt.application_test_id),
                    control_command=self.protocol.ControlCommand.ABORT,
                ),
                previous_workflow_state=self._workflow_state,
            )
        )

    def reset_application(self) -> None:
        """Queue a test-independent Application reset without resetting Transport."""
        self._require_open()
        if not self._session_confirmed:
            raise ProtocolSessionError("Application compatibility has not been confirmed")
        self._require_no_pending_operation()
        message = self.application.build_reset_application()
        self._activate_operation(
            _ApplicationOperation(
                kind="reset",
                encoded_messages=(self.application.encode(message),),
                response=ResponseCorrelation(
                    scope=self.protocol.ResponseScope.GLOBAL_CONTROL,
                    successful_outcome=self.protocol.ResponseOutcome.COMPLETED,
                    application_test_id=None,
                    global_control_command=(self.protocol.GlobalControlCommand.RESET_APPLICATION),
                ),
                previous_workflow_state=self._workflow_state,
            )
        )

    def service(self, *, operating_mode: object | None = None) -> ProtocolServiceReport:
        """Run one bounded read/process/drain/submit/write service pass."""
        self._require_open()
        p = self.protocol
        now_ms = monotonic_now_ms()
        bytes_read = self._read_serial()
        # self._offer_received()

        if operating_mode is None:
            operating_mode = (
                p.OperatingMode.BULK_TRANSFER
                if self._upload_operations
                or (
                    self._gated_operation is not None
                    and self._gated_operation.kind
                    in {
                        UploadOperationKind.CONFIGURATION,
                        UploadOperationKind.TICK,
                        UploadOperationKind.FINALIZE,
                    }
                )
                or self._transport_delivery_pending
                or (
                    self._pending_operation is not None
                    and self._pending_operation.kind in {"configuration", "tick", "finalize"}
                )
                else p.OperatingMode.NORMAL
            )
        # self._process_transport(now_ms, operating_mode)

        events, messages, results, errors = self._drain()
        self._advance_workflow()
        self._check_response_timeout()
        submitted = self._submit_next_application_message()
        if submitted:
            # self._process_transport(now_ms, operating_mode)
            pass

        bytes_written = self._service_output(now_ms)
        more_events, more_messages, more_results, more_errors = self._drain()
        self._advance_workflow()
        self._check_response_timeout()
        return ProtocolServiceReport(
            events=(*events, *more_events),
            application_messages=(*messages, *more_messages),
            stored_tick_results=(*results, *more_results),
            stored_application_errors=(*errors, *more_errors),
            serial_bytes_read=bytes_read,
            serial_bytes_written=bytes_written,
            application_message_submitted=submitted,
            workflow_state=self._workflow_state,
        )

    def close(self) -> None:
        """Quiesce serial state, notify Transport, and release both lifetimes."""
        if self._closed:
            return
        now_ms = monotonic_now_ms()
        with suppress(Exception):
            self.serial_port.reset_input_buffer()
        with suppress(Exception):
            self.serial_port.reset_output_buffer()
        with suppress(Exception):
            self.serial_port.close()

        self._incoming.clear()
        self._pending_output = None
        self._pending_output_offset = 0
        self._upload_operations.clear()
        self._pending_operation = None
        self._gated_operation = None
        self._transport_delivery_pending = False
        with suppress(Exception):
            self.transport.notify_link_state(self.protocol.LinkState.DISCONNECTED, now_ms)
        self.transport.close()
        self._closed = True

    def _process_transport(self, now_ms: int, operating_mode: object) -> None:
        status = self.transport.process(now_ms, operating_mode)
        if status is self.protocol.TransportStatus.DELIVERY_FAILED:
            self._fail_workflow()
            raise ProtocolSessionError("Transport reported reliable delivery failure")

    def _read_serial(self) -> int:
        waiting = self.serial_port.in_waiting
        if not isinstance(waiting, int) or isinstance(waiting, bool) or waiting < 0:
            raise ProtocolSessionError("Serial in_waiting returned an invalid byte count")
        if waiting == 0:
            return 0
        try:
            data = self.serial_port.read(waiting)
        except Exception as error:
            raise ProtocolSessionError("Could not read from the HIL-RIG serial port") from error
        if not isinstance(data, bytes):
            data = bytes(data)
        self._incoming.extend(data)
        return len(data)

    def _offer_received(self) -> None:
        p = self.protocol
        first_offer = True
        while self._incoming or first_offer:
            first_offer = False
            offered: bytes | bytearray = self._incoming if self._incoming else b""
            result = self.transport.receive_bytes(offered)
            del self._incoming[: result.bytes_consumed]
            if result.status in (p.TransportStatus.NOT_READY, p.TransportStatus.CAPACITY_EXHAUSTED):
                return
            if result.status is not p.TransportStatus.OK:
                raise ProtocolSessionError(
                    f"Transport receive returned unexpected status {result.status.name}"
                )
            if not self._incoming:
                return

    def _drain(
        self,
    ) -> tuple[
        tuple[object, ...],
        tuple[object, ...],
        tuple[TickResult, ...],
        tuple[ApplicationErrorRecord, ...],
    ]:
        p = self.protocol
        events: list[object] = []
        while False and (event := self.transport.read_event()) is not None:
            events.append(event)
            if event.type is p.EventType.DELIVERY_CONFIRMED:
                self._handle_delivery_confirmed()
            elif event.type is p.EventType.DELIVERY_FAILED:
                self._fail_workflow()
                raise ProtocolSessionError("Transport delivery failed")
            elif event.type is p.EventType.PROTOCOL_ERROR:
                self._fail_workflow()
                raise ProtocolSessionError("Transport reported a protocol error")
            elif event.type is p.EventType.SESSION_RESET:
                self._invalidate_session()
                raise ProtocolSessionError(
                    "Transport session reset; the Application transaction was abandoned"
                )
            elif event.type is p.EventType.SESSION_ESTABLISHED:
                self._session_confirmed = False
                self._session_info = None
                self._workflow_state = ProtocolWorkflowState.CONNECTING

        messages: list[object] = []
        stored_results: list[TickResult] = []
        stored_errors: list[ApplicationErrorRecord] = []
        # Direct USB Framing: 2-byte Little Endian length header
        while len(self._incoming) >= 2:
            msg_len = int.from_bytes(self._incoming[:2], "little")
            if len(self._incoming) < 2 + msg_len:
                break
            encoded = bytes(self._incoming[2 : 2 + msg_len])
            del self._incoming[: 2 + msg_len]
            message = self.application.decode(encoded)
            messages.append(message)
            if type(message) is p.SystemInfoResponse:
                if (
                    self._pending_operation is not None
                    and self._pending_operation.kind == "discovery"
                ) or not self._manual_mode:
                    self._handle_system_info_response(message)
            elif type(message) is p.ApplicationResponse:
                if not self._manual_mode or (
                    self._pending_operation is not None
                    and self._pending_operation.kind == "manual"
                    and self._pending_operation.application_response_required
                ):
                    self._handle_application_response(message)
            elif type(message) is p.ApplicationErrorMessage:
                if (
                    not self._manual_mode
                    and self.result_adapter is not None
                    and self._error_belongs_to_active_upload(message)
                ):
                    stored_errors.append(self.result_adapter.ingest_application_error(message))
                self._handle_application_error(message)
            elif type(message) in (p.TestResult, getattr(p, "VariableTestResult", None)):
                if not self._manual_mode:
                    if (
                        self.protocol_family is ProtocolFamily.VARIABLE
                        and type(message) is p.TestResult
                    ):
                        self._fail_workflow()
                        raise ProtocolSessionError(
                            "Received legacy TestResult (Type 33) when variable message family was expected"
                        )
                    if self.protocol_family is ProtocolFamily.LEGACY and type(message) is getattr(
                        p, "VariableTestResult", None
                    ):
                        self._fail_workflow()
                        raise ProtocolSessionError(
                            "Received VariableTestResult (Type 34) when legacy message family was expected"
                        )
                    self._validate_result_sequence(message)
                    if self.result_adapter is not None:
                        stored_results.append(
                            self.result_adapter.ingest_application_message(message)
                        )
                    self._next_result_tick += 1
                    if self._active_upload is not None and (
                        self._next_result_tick
                        == self._active_upload.configuration.expected_tick_count
                    ):
                        self._workflow_state = ProtocolWorkflowState.RESULTS_COMPLETE
            else:
                if not self._manual_mode:
                    self._fail_workflow()
                    raise ProtocolSessionError(
                        f"Unexpected inbound Application message: {type(message).__name__}"
                    )
        return tuple(events), tuple(messages), tuple(stored_results), tuple(stored_errors)

    def _handle_delivery_confirmed(self) -> None:
        operation = self._pending_operation
        if operation is None or not self._transport_delivery_pending:
            self._fail_workflow()
            raise ProtocolSessionError(
                "Transport confirmed delivery without a pending Application message"
            )
        self._transport_delivery_pending = False
        if operation.kind in {"configuration", "tick", "finalize"}:
            self._upload_messages_delivered += 1
        if operation.every_message_submitted:
            if (operation.kind == "manual" and not operation.application_response_required) or (
                operation.kind == "tick" and operation.response is None
            ):
                operation.response_received = True
            else:
                operation.response_deadline = time.monotonic() + self.application_response_timeout_s
        self._finish_pending_operation_if_ready()

    def _handle_system_info_response(self, message: object) -> None:
        operation = self._pending_operation
        if operation is None or operation.kind != "discovery":
            self._fail_workflow()
            raise ProtocolSessionError("Received an unexpected System Information response")
        try:
            self.protocol.check_protocol_version(message.protocol_version)
        except Exception as error:
            self._fail_workflow()
            raise ProtocolSessionError(
                "The RIG Application protocol version is incompatible with this host"
            ) from error
        self._session_info = RigSystemInfo(
            protocol_version=_version_text(message.protocol_version),
            firmware_version=_version_text(message.firmware_version),
            diagnostic_data=message.diagnostic_data,
            firmware_git_hash=message.firmware_git_hash,
        )
        operation.response_received = True
        self._finish_pending_operation_if_ready()

    def _handle_application_response(self, message: object) -> None:
        operation = self._pending_operation
        if operation is None or operation.response is None:
            self._fail_workflow()
            fields = [
                f"scope={message.scope.name}",
                f"outcome={message.outcome.name}",
                f"reason={message.reason.name}",
            ]
            if message.tick_number is not None:
                fields.append(f"tick={message.tick_number}")
            fields.append(f"detail={message.detail}")
            raise ProtocolSessionError(
                "Received an Application Response with no matching response-bearing request: "
                f"ApplicationResponse({', '.join(fields)})"
            )

        correlation = operation.response
        if message.scope is not correlation.scope:
            if self._manual_mode and operation.kind == "manual":
                return
            self._response_mismatch(
                f"expected scope {correlation.scope.name}, received {message.scope.name}"
            )

        received_test_id = (
            None
            if message.test_id is None
            else application_test_id_from_bytes(message.test_id.bytes)
        )
        if received_test_id != correlation.application_test_id:
            if self._manual_mode and operation.kind == "manual":
                return
            self._response_mismatch("Application Test ID does not match the pending operation")
        if correlation.tick is not None and message.tick_number != correlation.tick:
            if self._manual_mode and operation.kind == "manual":
                return
            self._response_mismatch(
                f"expected tick {correlation.tick}, received tick {message.tick_number}"
            )
        if correlation.control_command is not None and (
            message.control_command is not correlation.control_command
        ):
            if self._manual_mode and operation.kind == "manual":
                return
            self._response_mismatch("Execution Control command does not match the request")
        if correlation.global_control_command is not None and (
            message.global_control_command is not correlation.global_control_command
        ):
            if self._manual_mode and operation.kind == "manual":
                return
            self._response_mismatch("Global Control command does not match the request")

        self._last_application_response = message
        if message.outcome is not correlation.successful_outcome:
            operation.response_error = (
                f"{operation.kind} was {message.outcome.name.lower()}: "
                f"{message.reason.name} (detail {message.detail})"
            )
            operation.response_outcome = message.outcome

        operation.response_received = True
        self._finish_pending_operation_if_ready()

    def _handle_application_error(self, message: object) -> None:
        """Terminate the single pending operation when an Application Error applies to it."""
        operation = self._pending_operation
        if operation is None or not self._application_error_matches_operation(message, operation):
            return

        fields = [
            f"category={message.category.name.lower()}",
            f"recoverable={'true' if message.recoverable else 'false'}",
        ]
        if message.tick_number is not None:
            fields.append(f"tick={message.tick_number}")
        fields.append(f"detail={message.detail}")
        if message.diagnostic_data:
            fields.append(f"diagnostic_data=0x{message.diagnostic_data.hex()}")

        operation.response_error = (
            f"RIG Application Error while waiting for the {operation.kind} response: "
            f"ApplicationError({', '.join(fields)})"
        )
        operation.response_received = True
        self._finish_pending_operation_if_ready()

    @staticmethod
    def _application_error_matches_operation(
        message: object,
        operation: _ApplicationOperation,
    ) -> bool:
        """Correlate an error using existing fields and the one-operation-at-a-time rule."""
        if message.test_id is None:
            return True
        if operation.response is None or operation.response.application_test_id is None:
            return False
        return (
            application_test_id_from_bytes(message.test_id.bytes)
            == operation.response.application_test_id
        )

    def _response_mismatch(self, detail: str) -> None:
        self._fail_workflow()
        raise ProtocolSessionError(f"Application Response correlation failed: {detail}")

    def _finish_pending_operation_if_ready(self) -> None:
        operation = self._pending_operation
        if (
            operation is None
            or not operation.response_received
            or self._transport_delivery_pending
            or not operation.every_message_submitted
        ):
            return

        self._pending_operation = None
        if operation.kind == "manual":
            success = operation.response_error is None
            if operation.application_response_required:
                detail = (
                    "Application response accepted."
                    if success
                    else operation.response_error or "Application response failed."
                )
            else:
                detail = "Transport delivery confirmed; Application response was not required."
            self._last_manual_send_result = ManualSendResult(
                sequence=operation.manual_sequence or 0,
                label=operation.manual_label or "Application message",
                transport_delivered=True,
                application_response_required=operation.application_response_required,
                application_response=(
                    self._last_application_response
                    if operation.application_response_required
                    else None
                ),
                success=success,
                detail=detail,
            )
            self._workflow_state = ProtocolWorkflowState.MANUAL_READY
            return
        if operation.response_error is not None:
            self._handle_negative_response(operation)
            raise ProtocolSessionError(operation.response_error)
        if operation.kind == "discovery":
            self._session_confirmed = True
            self._workflow_state = (
                ProtocolWorkflowState.MANUAL_READY
                if self._manual_mode
                else ProtocolWorkflowState.READY
            )
        elif operation.kind in {"configuration", "tick"}:
            if self._upload_operations:
                self._advance_or_gate(self._upload_operations.popleft())
        elif operation.kind == "finalize":
            self._upload_accepted = True
            self._workflow_state = ProtocolWorkflowState.READY_TO_START
            if self._advance_mode is UploadAdvanceMode.OPERATOR_GATED:
                self._gate_start()
            elif self._active_upload is not None and self._active_upload.start_mode == "IMMEDIATE":
                self._queue_start()
        elif operation.kind == "start":
            self._execution_started = True
            self._workflow_state = ProtocolWorkflowState.RUNNING
        elif operation.kind == "abort":
            self._retire_active_upload()
            self._workflow_state = ProtocolWorkflowState.ABORTED
        elif operation.kind == "reset":
            self._retire_active_upload()
            self._workflow_state = ProtocolWorkflowState.READY

    def _handle_negative_response(self, operation: _ApplicationOperation) -> None:
        """Apply the protocol's scope-specific recovery semantics."""
        p = self.protocol
        if operation.kind in {"configuration", "tick", "finalize"}:
            self._retire_active_upload()
            self._workflow_state = ProtocolWorkflowState.FAILED
            return
        if operation.kind == "start" and operation.response_outcome is p.ResponseOutcome.REJECTED:
            self._execution_started = False
            self._upload_accepted = True
            self._workflow_state = ProtocolWorkflowState.READY_TO_START
            return
        if operation.kind in {"abort", "reset"} and (
            operation.response_outcome is p.ResponseOutcome.REJECTED
        ):
            self._workflow_state = operation.previous_workflow_state or ProtocolWorkflowState.FAILED
            return
        self._fail_workflow()

    def _advance_workflow(self) -> None:
        if self._pending_operation is not None or self._gated_operation is not None:
            return
        # snapshot = self.transport.get_status()
        # if snapshot.session_state is not self.protocol.SessionState.ESTABLISHED:
        #     return
        if self._manual_mode and self._skip_system_info and not self._session_confirmed:
            self._session_confirmed = True
            self._workflow_state = ProtocolWorkflowState.MANUAL_READY
            return
        if not self._session_confirmed:
            request = self.application.build_system_info_request(request_firmware_git_hash=True)
            self._activate_operation(
                _ApplicationOperation(
                    kind="discovery",
                    encoded_messages=(self.application.encode(request),),
                )
            )
            return
        if self._manual_mode:
            return
        if self._upload_operations:
            self._advance_or_gate(self._upload_operations.popleft())

    def _activate_operation(self, operation: _ApplicationOperation) -> None:
        if self._pending_operation is not None:
            raise ProtocolSessionError("Cannot activate two Application operations at once")
        self._pending_operation = operation
        self._workflow_state = {
            "discovery": ProtocolWorkflowState.DISCOVERING,
            "configuration": ProtocolWorkflowState.CONFIGURING,
            "tick": ProtocolWorkflowState.UPLOADING,
            "finalize": ProtocolWorkflowState.VALIDATING,
            "start": ProtocolWorkflowState.STARTING,
            "abort": ProtocolWorkflowState.ABORTING,
            "reset": ProtocolWorkflowState.RESETTING,
            "manual": ProtocolWorkflowState.MANUAL_SENDING,
        }[operation.kind]

    def _activate_upload_operation(self, operation: UploadOperation) -> None:
        self._activate_operation(
            _ApplicationOperation(
                kind=operation.kind.value,
                encoded_messages=operation.encoded_messages,
                response=operation.response,
                upload_operation=operation,
            )
        )

    def _advance_or_gate(self, operation: UploadOperation) -> None:
        if operation.kind is UploadOperationKind.FINALIZE:
            self._activate_upload_operation(operation)
            return
        if self._advance_mode is UploadAdvanceMode.OPERATOR_GATED:
            if self._gated_operation is not None:
                raise ProtocolSessionError("Cannot gate two upload operations at once")
            self._gated_operation = operation
            self._workflow_state = ProtocolWorkflowState.WAITING_FOR_OPERATOR
            return
        self._activate_upload_operation(operation)

    def _gate_start(self) -> None:
        if self._active_upload_plan is None:
            raise ProtocolSessionError("There is no upload plan ready to start")
        operation = self._active_upload_plan.start_operation
        if operation is None:
            return
        self._advance_or_gate(operation)

    def _take_gated_operation(self) -> UploadOperation:
        operation = self._gated_operation
        if operation is None:
            raise ProtocolSessionError("No upload operation is waiting for the operator")
        if self._pending_operation is not None or self._transport_delivery_pending:
            raise ProtocolSessionError("Another Application operation is already pending")
        self._gated_operation = None
        return operation

    def _queue_start(self) -> None:
        if self._active_upload_plan is None:
            raise ProtocolSessionError("There is no accepted upload to start")
        operation = self._active_upload_plan.start_operation
        if operation is None:
            raise ProtocolSessionError("EXTERNAL_TRIGGER has no protocol START operation")
        self._activate_upload_operation(operation)

    def _submit_next_application_message(self) -> bool:
        p = self.protocol
        operation = self._pending_operation
        if (
            operation is None
            or operation.every_message_submitted
            or self._transport_delivery_pending
        ):
            return False
        # snapshot = self.transport.get_status()
        # if snapshot.session_state is not p.SessionState.ESTABLISHED:
        #     return False
        if operation.kind != "discovery" and not self._session_confirmed:
            return False

        encoded = operation.encoded_messages[operation.next_message_index]
        # status = self.transport.submit_application_data(encoded)
        framed = len(encoded).to_bytes(2, "little") + encoded
        if self._pending_output is None:
            self._pending_output = framed
            self._pending_output_offset = 0
        else:
            self._pending_output = self._pending_output[self._pending_output_offset :] + framed
            self._pending_output_offset = 0

        operation.next_message_index += 1
        if operation.kind in {"configuration", "tick", "finalize"}:
            self._upload_messages_delivered += 1
        if operation.every_message_submitted:
            if (operation.kind == "manual" and not operation.application_response_required) or (
                operation.kind == "tick" and operation.response is None
            ):
                operation.response_received = True
            else:
                operation.response_deadline = time.monotonic() + self.application_response_timeout_s
        self._finish_pending_operation_if_ready()
        return True
        # if status is p.TransportStatus.OK:
        #     operation.next_message_index += 1
        #     self._transport_delivery_pending = True
        #     return True
        # if status in (p.TransportStatus.NOT_READY, p.TransportStatus.CAPACITY_EXHAUSTED):
        #     return False
        # self._fail_workflow()
        # raise ProtocolSessionError(
        #     f"Could not submit Application message: Transport returned {status.name}"
        # )

    def _check_response_timeout(self) -> None:
        operation = self._pending_operation
        if (
            operation is None
            or operation.response_received
            or operation.response_deadline is None
            or time.monotonic() < operation.response_deadline
        ):
            return
        kind = operation.kind
        if kind == "manual":
            self._pending_operation = None
            self._last_manual_send_result = ManualSendResult(
                sequence=operation.manual_sequence or 0,
                label=operation.manual_label or "Application message",
                transport_delivered=True,
                application_response_required=True,
                application_response=None,
                success=False,
                detail="Timed out waiting for the Application response.",
            )
            self._workflow_state = ProtocolWorkflowState.MANUAL_READY
            return
        self._fail_workflow()
        raise ProtocolSessionError(f"Timed out waiting for the {kind} Application response")

    def _validate_result_sequence(self, message: object) -> None:
        if self._active_upload is None or not self._execution_started:
            self._fail_workflow()
            raise ProtocolSessionError("Received a TestResult before START completed")
        received_test_id = application_test_id_from_bytes(message.test_id.bytes)
        if received_test_id != self._active_upload.upload_attempt.application_test_id:
            self._fail_workflow()
            raise ProtocolSessionError("Received TestResult for a different Application Test ID")
        expected_tick_count = self._active_upload.configuration.expected_tick_count
        if message.tick_number >= expected_tick_count:
            self._fail_workflow()
            raise ProtocolSessionError(
                "RIG emitted TestResult tick "
                f"{message.tick_number} outside the configured range "
                f"0..{expected_tick_count - 1} ({expected_tick_count} ticks)"
            )
        if message.tick_number != self._next_result_tick:
            self._fail_workflow()
            raise ProtocolSessionError(
                f"Expected TestResult tick {self._next_result_tick}, received {message.tick_number}"
            )

    def _require_no_pending_operation(self) -> None:
        if self._pending_operation is not None or self._transport_delivery_pending:
            raise ProtocolSessionError("Another response-requiring operation is already pending")

    def _clear_active_transaction(self) -> None:
        self._upload_operations.clear()
        self._gated_operation = None
        self._active_upload = None
        self._active_upload_plan = None
        self._advance_mode = UploadAdvanceMode.AUTOMATIC
        self._upload_message_count = 0
        self._upload_messages_delivered = 0
        self._upload_accepted = False
        self._execution_started = False
        self._next_result_tick = 0

    def _retire_active_upload(self) -> None:
        if self._active_upload is not None:
            self._retired_application_test_ids.add(
                self._active_upload.upload_attempt.application_test_id
            )
        self._clear_active_transaction()

    def _error_belongs_to_active_upload(self, message: object) -> bool:
        if self._active_upload is None:
            return False
        if message.test_id is None:
            return True
        return (
            application_test_id_from_bytes(message.test_id.bytes)
            == self._active_upload.upload_attempt.application_test_id
        )

    def _fail_workflow(self) -> None:
        self._upload_operations.clear()
        self._gated_operation = None
        self._pending_operation = None
        self._transport_delivery_pending = False
        self._upload_accepted = False
        self._execution_started = False
        self._workflow_state = ProtocolWorkflowState.FAILED

    def _invalidate_session(self) -> None:
        self._session_confirmed = False
        self._session_info = None
        self._pending_output = None
        self._pending_output_offset = 0
        self._fail_workflow()

    def _service_output(self, now_ms: int) -> int:
        p = self.protocol
        if self._pending_output is None:
            # self._pending_output = self.transport.peek_output()
            pass
            self._pending_output_offset = 0
        if self._pending_output is None:
            return 0

        remaining = self._pending_output[self._pending_output_offset :]
        try:
            accepted = self.serial_port.write(remaining)
        except Exception as error:
            raise ProtocolSessionError("Could not write to the HIL-RIG serial port") from error
        if not isinstance(accepted, int) or isinstance(accepted, bool):
            raise ProtocolSessionError("Serial write did not return an integer byte count")
        if not 0 <= accepted <= len(remaining):
            raise ProtocolSessionError("Serial write accepted an invalid byte count")

        self._pending_output_offset += accepted
        if self._pending_output_offset == len(self._pending_output):
            try:
                # commit_status = self.transport.commit_output(now_ms)
                pass
            finally:
                self._pending_output = None
                self._pending_output_offset = 0
                # if commit_status not in (p.TransportStatus.OK, p.TransportStatus.NOT_READY):
                # raise ProtocolSessionError(f"Transport output commit returned {commit_status.name}")
                pass
        return accepted

    def _require_open(self) -> None:
        if self._closed:
            raise ProtocolSessionError("The fixed-I/O protocol connection is closed")

    def __enter__(self) -> FixedIOProtocolConnection:
        self._require_open()
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()


def _version_text(version: object) -> str:
    return f"{version.major}.{version.minor}.{version.patch}"


__all__ = [
    "FixedIOProtocolConnection",
    "ManualSendResult",
    "ProtocolServiceReport",
    "ProtocolWorkflowState",
    "RigSystemInfo",
    "UploadAdvanceMode",
    "monotonic_now_ms",
]
