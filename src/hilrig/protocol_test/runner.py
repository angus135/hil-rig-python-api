"""Synchronous Transport and Application hardware-test scenarios."""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace

import hil_rig_protocol as protocol
from hil_rig_protocol import EventType, SessionState, TransportStatus

from .application_hardware import (
    ALL_DISABLED_CONFIGURATION_DIGEST,
    ALL_DISABLED_CONFIGURATION_SIZE,
    APPLICATION_CODEC_CONFIG,
    COMPATIBILITY_PROFILE_ID,
    ERROR_FIXED_SIZE,
    FIXED_INSTRUCTION_SIZE,
    FIXED_RESPONSE_SIZE,
    FIXED_RESULT_SIZE,
    MAX_EXTENSION_CONFIGURATION_DIGEST,
    MAX_EXTENSION_CONFIGURATION_SIZE,
    PROTOCOL_VERSION,
    REPRESENTATIVE_CONFIGURATION_DIGEST,
    REPRESENTATIVE_CONFIGURATION_SIZE,
    all_disabled_configuration,
    application_error_fixtures,
    application_message_name,
    configured_initial_instruction,
    execution_control_response,
    execution_controls,
    expected_result,
    finalize_upload,
    global_control_response,
    instruction_semantic_digest,
    make_application_codec,
    maximum_extension_configuration,
    multi_chunk_variable_result_oracle,
    multi_chunk_variable_upload,
    new_test_id,
    representative_configuration,
    representative_instructions,
    reset_application_control,
    response_fixtures,
    sparse_variable_upload,
    test_profile,
    variable_operations,
    variable_result_oracle,
    zero_instruction,
)
from .connection import LinkDisconnectedError, ProtocolTestConnection
from .harness_codec import (
    MAGIC,
    ApplicationHarnessState,
    HarnessCodecError,
    HarnessMessage,
    Opcode,
    RequestIdAllocator,
    StatusPayloadV2,
    decode_message,
    decode_status_payload,
    encode_echo_request,
    encode_status_request,
    maximum_payload_size,
)
from .models import ReceivedApplicationMessage
from .trace import TraceWriter, payload_hash


class ScenarioFailure(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details


class ProtocolTestRunner:
    """Drive one caller-owned connection with at most one reliable outbound payload active."""

    def __init__(
        self,
        connection: ProtocolTestConnection,
        trace: TraceWriter,
        *,
        poll_ms: float = 1.0,
        request_timeout_ms: int = 3000,
        reconnect_timeout_ms: int = 15000,
        seed: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_ms < 0:
            raise ValueError("poll_ms must be non-negative")
        if request_timeout_ms < 1 or reconnect_timeout_ms < 1:
            raise ValueError("timeouts must be positive")
        self.connection = connection
        self.trace = trace
        self.poll_ms = poll_ms
        self.request_timeout_ms = request_timeout_ms
        self.reconnect_timeout_ms = reconnect_timeout_ms
        self.seed = seed
        self._sleep = sleep
        self._monotonic = monotonic
        self._ids = RequestIdAllocator()
        self._completed_ids: deque[int] = deque(maxlen=1024)
        self._pending_messages: deque[ReceivedApplicationMessage] = deque()
        self._stale_application_messages: deque[ReceivedApplicationMessage] = deque()
        self._seen_results: set[tuple[bytes, int]] = set()
        self._active_request_id: int | None = None
        self._outbound_active = False
        self._delivery_confirmations = 0
        self._opened = False
        self._application_discovery_generation: int | None = None
        self.application_codec = make_application_codec()

    @property
    def max_application_message_size(self) -> int:
        return self.connection.transport_config.max_application_message_size

    @property
    def max_payload_size(self) -> int:
        return maximum_payload_size(self.max_application_message_size)

    @property
    def delivery_confirmation_count(self) -> int:
        return self._delivery_confirmations

    def _deadline(self, timeout_ms: int) -> float:
        return self._monotonic() + timeout_ms / 1000

    def _pause(self) -> None:
        if self.poll_ms:
            self._sleep(self.poll_ms / 1000)

    def open(self) -> None:
        generation = self.connection.open_link()
        self._opened = True
        self.trace.record(
            "link_open",
            link_generation=generation,
            serial_device=self.connection.serial_identity,
            effective_transport_config=asdict(self.connection.transport_config),
            application_codec_config=APPLICATION_CODEC_CONFIG,
            protocol_version=PROTOCOL_VERSION,
            compatibility_profile_id=COMPATIBILITY_PROFILE_ID,
        )
        self._wait_for_session(self._deadline(self.request_timeout_ms))

    def close(self) -> None:
        if self.connection.closed:
            return
        try:
            self.connection.close()
        finally:
            self._opened = False

    def _record_events(self) -> None:
        while (record := self.connection.pop_event()) is not None:
            event = record.event
            fields = {
                "type": getattr(event.type, "name", str(event.type)),
                "status": getattr(event.status, "name", str(event.status)),
                "failure": getattr(event.failure, "name", str(event.failure)),
                "required_capacity": event.required_capacity,
                "link_generation": record.link_generation,
                "monotonic_ms": record.monotonic_ms,
            }
            if event.type is EventType.DELIVERY_CONFIRMED:
                self._delivery_confirmations += 1
                fields["delivery_confirmation_count"] = self._delivery_confirmations
            self.trace.record("transport_event", **fields)
            if self._outbound_active and event.type in {
                EventType.DELIVERY_FAILED,
                EventType.PROTOCOL_ERROR,
                EventType.SESSION_RESET,
            }:
                raise ScenarioFailure(
                    f"unexpected Transport event during delivery: {event.type.name}"
                )

    def _drain_received_messages(self) -> None:
        while (received := self.connection.pop_application_message()) is not None:
            if received.link_generation != self.connection.link_generation:
                self._stale_application_messages.append(received)
                self.trace.record(
                    "stale_application_message",
                    link_generation=received.link_generation,
                    current_generation=self.connection.link_generation,
                    payload_size=len(received.data),
                    payload_sha256=payload_hash(received.data),
                )
                continue
            classification = "HRTP" if received.data.startswith(MAGIC) else "APPLICATION"
            self._pending_messages.append(received)
            self.trace.record(
                "application_payload_received",
                classification=classification,
                link_generation=received.link_generation,
                monotonic_ms=received.monotonic_ms,
                payload_size=len(received.data),
                payload_sha256=payload_hash(received.data),
            )

    def _service(self, *, disconnect_expected: bool = False) -> None:
        try:
            result = self.connection.service_once()
        except LinkDisconnectedError as exc:
            self.trace.record(
                "link_disconnect_observed" if disconnect_expected else "unexpected_disconnect",
                reason=str(exc),
                expected=disconnect_expected,
                diagnostics=self.connection.get_diagnostics(),
            )
            raise
        if result.operation_budget_exhausted:
            self.trace.record(
                "service_budget_exhausted",
                current_service_gap_ms=result.current_service_gap_ms,
                max_service_gap_ms=result.max_service_gap_ms,
                diagnostics=self.connection.get_diagnostics(),
            )
        if result.current_service_gap_ms > self.connection.maximum_acceptable_service_gap_ms:
            self.trace.record(
                "service_gap_late",
                current_service_gap_ms=result.current_service_gap_ms,
                maximum_acceptable_service_gap_ms=(
                    self.connection.maximum_acceptable_service_gap_ms
                ),
                max_service_gap_ms=result.max_service_gap_ms,
            )
        self._record_events()
        self._drain_received_messages()

    def _wait_for_session(self, deadline: float) -> None:
        while self._monotonic() <= deadline:
            self._service()
            snapshot = self.connection.get_status()
            if snapshot.session_state is SessionState.ESTABLISHED:
                self.trace.record(
                    "session_established",
                    link_generation=self.connection.link_generation,
                    status=snapshot,
                )
                return
            if snapshot.session_state is SessionState.FAULT:
                raise ScenarioFailure("Transport entered FAULT while establishing a session")
            self._pause()
        raise ScenarioFailure("timed out waiting for Transport session establishment")

    def _submit_until_ready(self, encoded: bytes, deadline: float) -> None:
        while self._monotonic() <= deadline:
            status = self.connection.submit_application_data(encoded)
            self.trace.record(
                "submit_attempt",
                request_id=self._active_request_id,
                status=status,
                message_size=len(encoded),
                payload_sha256=payload_hash(encoded),
            )
            if status is TransportStatus.OK:
                return
            if status not in {TransportStatus.NOT_READY, TransportStatus.CAPACITY_EXHAUSTED}:
                raise ScenarioFailure(f"submit_application_data returned {status.name}")
            self._service()
            self._pause()
        raise ScenarioFailure("timed out waiting for Transport Application submission capacity")

    def _submit_and_confirm(
        self,
        payload: bytes,
        *,
        kind: str,
        evidence: dict[str, object] | None = None,
    ) -> float:
        """Submit one reliable payload and wait for its Transport delivery confirmation."""
        if self._outbound_active:
            raise ScenarioFailure("only one outbound reliable Application payload may be active")
        baseline = self._delivery_confirmations
        started = self._monotonic()
        deadline = self._deadline(self.request_timeout_ms)
        self._outbound_active = True
        try:
            self._submit_until_ready(payload, deadline)
            submitted = self._monotonic()
            payload_evidence = {
                "payload_size": len(payload),
                "payload_sha256": payload_hash(payload),
                **(evidence or {}),
            }
            self.trace.record(
                "payload_submitted",
                payload_kind=kind,
                link_generation=self.connection.link_generation,
                submission_wait_ms=(submitted - started) * 1000,
                **payload_evidence,
            )
            while self._monotonic() <= deadline:
                self._service()
                if self._delivery_confirmations > baseline:
                    confirmed = self._monotonic()
                    latency_ms = (confirmed - submitted) * 1000
                    self.trace.record(
                        "delivery_confirmed",
                        payload_kind=kind,
                        delivery_confirmation_count=self._delivery_confirmations,
                        delivery_confirmation_latency_ms=latency_ms,
                        **payload_evidence,
                    )
                    return latency_ms
                self._pause()
        finally:
            self._outbound_active = False
        raise ScenarioFailure(f"timed out waiting for Transport delivery confirmation for {kind}")

    def _take_pending(self, *, hrtp: bool) -> ReceivedApplicationMessage | None:
        items = list(self._pending_messages)
        for index, item in enumerate(items):
            if item.data.startswith(MAGIC) is hrtp:
                match = items.pop(index)
                self._pending_messages = deque(items)
                return match
        return None

    def wait_for_raw_application_message(
        self, timeout_ms: int | None = None
    ) -> ReceivedApplicationMessage:
        """Retrieve one raw current-generation payload without selecting a logical decoder."""
        deadline = self._deadline(timeout_ms or self.request_timeout_ms)
        while self._monotonic() <= deadline:
            if self._pending_messages:
                return self._pending_messages.popleft()
            self._service()
            self._pause()
        raise ScenarioFailure("timed out waiting for a raw Transport Application payload")

    def _wait_hrtp_response(
        self, request_id: int, expected_opcode: Opcode, deadline: float
    ) -> HarnessMessage:
        while self._monotonic() <= deadline:
            self._service()
            received = self._take_pending(hrtp=True)
            if received is not None:
                try:
                    decoded = decode_message(
                        received.data,
                        max_application_message_size=self.max_application_message_size,
                    )
                except HarnessCodecError as exc:
                    self.trace.record(
                        "unexpected_message",
                        expected="valid HRTP response",
                        reason=str(exc),
                        payload_size=len(received.data),
                        payload_sha256=payload_hash(received.data),
                    )
                    raise ScenarioFailure(f"invalid HRTP response: {exc}") from exc
                if decoded.request_id in self._completed_ids:
                    raise ScenarioFailure(
                        f"duplicate Application delivery for completed request {decoded.request_id}"
                    )
                if decoded.request_id != request_id:
                    raise ScenarioFailure(
                        "wrong response request ID: "
                        f"expected {request_id}, got {decoded.request_id}"
                    )
                if decoded.opcode is not expected_opcode:
                    raise ScenarioFailure(
                        "wrong response opcode: "
                        f"expected {expected_opcode.name}, got {decoded.opcode.name}"
                    )
                return decoded
            if any(not item.data.startswith(MAGIC) for item in self._pending_messages):
                unexpected = next(
                    item for item in self._pending_messages if not item.data.startswith(MAGIC)
                )
                self.trace.record(
                    "unexpected_message",
                    expected="HRTP response",
                    actual="Application message",
                    payload_size=len(unexpected.data),
                    payload_sha256=payload_hash(unexpected.data),
                )
                raise ScenarioFailure(
                    "unexpected non-HRTP Application message while waiting for HRTP"
                )
            self._pause()
        raise ScenarioFailure("request response deadline expired")

    def _exchange(self, encoded: bytes, request_id: int, expected_opcode: Opcode) -> HarnessMessage:
        self._active_request_id = request_id
        try:
            self._submit_and_confirm(
                encoded,
                kind="HRTP",
                evidence={"request_id": request_id, "opcode": Opcode(encoded[5]).name},
            )
            response = self._wait_hrtp_response(
                request_id, expected_opcode, self._deadline(self.request_timeout_ms)
            )
            self._completed_ids.append(request_id)
            self.trace.record(
                "response_received",
                request_id=request_id,
                opcode=response.opcode.name,
                link_generation=self.connection.link_generation,
                payload_size=len(response.payload),
                payload_sha256=payload_hash(response.payload),
            )
            self._service()
            for item in self._pending_messages:
                if not item.data.startswith(MAGIC):
                    continue
                decoded = decode_message(
                    item.data, max_application_message_size=self.max_application_message_size
                )
                if decoded.request_id == request_id:
                    raise ScenarioFailure(
                        f"duplicate Application delivery for request {request_id}"
                    )
                raise ScenarioFailure(
                    f"unexpected queued HRTP response {decoded.request_id} while only one request "
                    "is allowed"
                )
            return response
        finally:
            self._active_request_id = None
            self._ids.release(request_id)

    def run_echo(self, payload: bytes) -> float:
        request_id = self._ids.allocate()
        encoded = encode_echo_request(
            request_id,
            payload,
            max_application_message_size=self.max_application_message_size,
        )
        started = self._monotonic()
        response = self._exchange(encoded, request_id, Opcode.ECHO_RESPONSE)
        if response.payload != payload:
            self.trace.record(
                "payload_mismatch",
                request_id=request_id,
                expected_size=len(payload),
                expected_sha256=payload_hash(payload),
                actual_size=len(response.payload),
                actual_sha256=payload_hash(response.payload),
            )
            raise ScenarioFailure(f"ECHO payload mismatch for request {request_id}")
        return (self._monotonic() - started) * 1000

    def _require_status_compatibility(self, status: StatusPayloadV2) -> None:
        if status.compatibility_profile_id != COMPATIBILITY_PROFILE_ID:
            raise ScenarioFailure(
                "firmware compatibility profile mismatch: "
                f"expected 0x{COMPATIBILITY_PROFILE_ID:08X}, "
                f"got 0x{status.compatibility_profile_id:08X}"
            )
        actual_version = (
            status.protocol_version_major,
            status.protocol_version_minor,
            status.protocol_version_patch,
        )
        if actual_version != PROTOCOL_VERSION:
            expected = ".".join(map(str, PROTOCOL_VERSION))
            actual = ".".join(map(str, actual_version))
            raise ScenarioFailure(
                f"firmware protocol version mismatch: expected {expected}, got {actual}"
            )
        if status.application_codec_initialized != 1:
            raise ScenarioFailure("firmware Application codec is not initialized")
        if status.application_initialization_status != int(protocol.ApplicationStatus.OK):
            raise ScenarioFailure(
                "firmware Application codec initialization failed with status "
                f"{status.application_initialization_status}"
            )

    def run_status(self) -> StatusPayloadV2:
        request_id = self._ids.allocate()
        encoded = encode_status_request(
            request_id, max_application_message_size=self.max_application_message_size
        )
        response = self._exchange(encoded, request_id, Opcode.STATUS_RESPONSE)
        self.trace.record(
            "status_raw",
            request_id=request_id,
            payload_size=len(response.payload),
            payload_sha256=payload_hash(response.payload),
            payload_hex=response.payload.hex(),
        )
        try:
            status = decode_status_payload(response.payload)
        except HarnessCodecError as exc:
            self.trace.record("status_decode_failure", request_id=request_id, reason=str(exc))
            raise ScenarioFailure(f"invalid STATUS response: {exc}") from exc
        self._require_status_compatibility(status)
        self.trace.record("status_decoded", request_id=request_id, status=status)
        return status

    def run_smoke(self) -> dict[str, object]:
        self.run_status()
        rng = random.Random(self.seed)
        payloads = [
            b"",
            b"HIL-RIG transport echo",
            b"\x00A\x00B\x00\xff",
            bytes((0x00, 0x01, 0x00, 0xFE, 0xFF, 0x7E, 0xC0, 0xDB)),
            bytes(rng.getrandbits(8) for _ in range(min(64, self.max_payload_size))),
            bytes((index * 37) & 0xFF for index in range(self.max_payload_size)),
        ]
        latencies = [self.run_echo(payload) for payload in payloads]
        return {"echo_count": len(payloads), "latencies_ms": latencies}

    def run_boundaries(self) -> dict[str, object]:
        maximum = self.max_payload_size
        near_encoded_boundary = min(maximum, max(0, 254 - 16))
        sizes = [0, 1, 15, 16, near_encoded_boundary, max(0, maximum - 1), maximum]
        unique_sizes = list(dict.fromkeys(size for size in sizes if size <= maximum))
        for size in unique_sizes:
            payload = bytes((index * 17) & 0xFF for index in range(size))
            self.run_echo(payload)
        oversized = bytes(maximum + 1)
        request_id = self._ids.allocate()
        try:
            try:
                encode_echo_request(
                    request_id,
                    oversized,
                    max_application_message_size=self.max_application_message_size,
                )
            except HarnessCodecError:
                self.trace.record("local_oversize_rejected", payload_size=len(oversized))
            else:
                raise ScenarioFailure("maximum-plus-one payload was not rejected locally")
        finally:
            self._ids.release(request_id)
        return {"payload_sizes": unique_sizes, "local_reject_size": len(oversized)}

    def run_repeat(self, count: int) -> dict[str, object]:
        if count < 1:
            raise ValueError("repeat count must be positive")
        rng = random.Random(self.seed)
        latencies: list[float] = []
        for index in range(count):
            size = min(self.max_payload_size, 1 + (index % max(1, min(128, self.max_payload_size))))
            payload = bytes(rng.getrandbits(8) for _ in range(size))
            latencies.append(self.run_echo(payload))
        return {
            "completed": count,
            "latency_max_ms": max(latencies),
            "latency_average_ms": sum(latencies) / len(latencies),
        }

    def _encode_application_message(
        self,
        message: protocol.ApplicationMessage,
        *,
        semantic_digest: int | None = None,
    ) -> bytes:
        encoded = self.application_codec.encode(message)
        evidence = self._application_message_evidence(message, encoded)
        evidence["application_scenario"] = self.trace.scenario
        evidence["encoded_message_type"] = application_message_name(message)
        evidence["encoded_message_size"] = len(encoded)
        if semantic_digest is not None:
            evidence["semantic_digest"] = semantic_digest
        self.trace.record("application_message_encoded", **evidence)
        return encoded

    def _application_message_form(self, message: protocol.ApplicationMessage) -> str | None:
        if type(message) is protocol.ApplicationResponse:
            return message.scope.name
        if type(message) is protocol.ApplicationErrorMessage:
            if message.test_id is None:
                return "GLOBAL"
            if message.tick_number is None:
                return "TEST_WIDE"
            return "TICK_SPECIFIC"
        if type(message) is protocol.ExecutionControl:
            return message.command.name
        if type(message) is protocol.GlobalControl:
            return message.command.name
        if type(message) is protocol.SystemInfoRequest:
            return message.query.name
        if type(message) is protocol.SystemInfoResponse:
            return "BASIC"
        return None

    def _application_message_evidence(
        self, message: protocol.ApplicationMessage, encoded: bytes
    ) -> dict[str, object]:
        """Return common trace evidence without inventing a Test ID for global messages."""
        test_id = getattr(message, "test_id", None)
        return {
            "message_family": application_message_name(message),
            "message_scope": (
                message.scope.name if type(message) is protocol.ApplicationResponse else None
            ),
            "message_form": self._application_message_form(message),
            "test_id_hex": test_id.bytes.hex() if test_id is not None else None,
            "tick": getattr(message, "tick_number", None),
            "payload_size": len(encoded),
            "payload_sha256": payload_hash(encoded),
        }

    def _require_application_discovery(self) -> None:
        if self._application_discovery_generation != self.connection.link_generation:
            raise ScenarioFailure(
                "Application System Information discovery is required for the current "
                "link generation"
            )

    def _raise_for_stale_application_message(self) -> None:
        if not self._stale_application_messages:
            return
        received = self._stale_application_messages.popleft()
        raise ScenarioFailure(
            "stale-generation Application response received",
            details={
                "received_generation": received.link_generation,
                "current_generation": self.connection.link_generation,
                "payload_size": len(received.data),
                "payload_sha256": payload_hash(received.data),
            },
        )

    def _raise_for_unexpected_application_queue(
        self, *, expected_name: str, expected_value: protocol.ApplicationMessage | None
    ) -> None:
        self._raise_for_stale_application_message()
        if not self._pending_messages:
            return
        unexpected = self._pending_messages.popleft()
        if unexpected.link_generation != self.connection.link_generation:
            raise ScenarioFailure("stale-generation Application response received")
        if unexpected.data.startswith(MAGIC):
            self.trace.record(
                "unexpected_message",
                expected=expected_name,
                actual="HRTP response",
                payload_size=len(unexpected.data),
                payload_sha256=payload_hash(unexpected.data),
            )
            raise ScenarioFailure("unexpected HRTP response while waiting for Application response")
        try:
            decoded = self.application_codec.decode(unexpected.data)
        except protocol.ApplicationDecodeError as exc:
            raise ScenarioFailure(f"malformed extra Application response: {exc}") from exc
        if (
            expected_value is not None
            and type(decoded) is type(expected_value)
            and decoded == expected_value
        ):
            raise ScenarioFailure("duplicate Application response")
        raise ScenarioFailure(
            f"unexpected additional Application response {application_message_name(decoded)}"
        )

    def _wait_for_application_response(
        self,
        *,
        expected_type: type[object],
        expected_value: protocol.ApplicationMessage | None,
        deadline: float,
        allow_follow_on_results: bool = False,
    ) -> tuple[protocol.ApplicationMessage, float]:
        """Wait for exactly one current-generation non-HRTP Application response."""
        started = self._monotonic()
        while self._monotonic() <= deadline:
            self._service()
            self._raise_for_stale_application_message()
            if any(item.data.startswith(MAGIC) for item in self._pending_messages):
                self._raise_for_unexpected_application_queue(
                    expected_name=expected_type.__name__, expected_value=expected_value
                )
            received = self._take_pending(hrtp=False)
            if received is not None:
                if received.link_generation != self.connection.link_generation:
                    raise ScenarioFailure("stale-generation Application response received")
                try:
                    decoded = self.application_codec.decode(received.data)
                except protocol.ApplicationDecodeError as exc:
                    self.trace.record(
                        "unexpected_message",
                        expected=expected_type.__name__,
                        actual="malformed Application response",
                        reason=str(exc),
                        payload_size=len(received.data),
                        payload_sha256=payload_hash(received.data),
                    )
                    raise ScenarioFailure(f"malformed Application response: {exc}") from exc
                if type(decoded) is not expected_type:
                    raise ScenarioFailure(
                        "unexpected Application response family: "
                        f"expected {expected_type.__name__}, got "
                        f"{application_message_name(decoded)}"
                    )
                if expected_value is not None and decoded != expected_value:
                    details = {"expected": expected_value, "actual": decoded}
                    self.trace.record("application_response_mismatch", **details)
                    raise ScenarioFailure(
                        "Application response did not match the expected value", details=details
                    )
                self.trace.record(
                    "application_response_decoded",
                    response_latency_ms=(self._monotonic() - started) * 1000,
                    **self._application_message_evidence(decoded, received.data),
                )
                self._service()
                if expected_value is not None and not allow_follow_on_results:
                    self._raise_for_unexpected_application_queue(
                        expected_name=expected_type.__name__, expected_value=expected_value
                    )
                return decoded, (self._monotonic() - started) * 1000
            self._pause()
        raise ScenarioFailure("timed out waiting for Application response")

    def _exchange_application(
        self,
        message: protocol.ApplicationMessage,
        expected: protocol.ApplicationMessage,
        *,
        kind: str,
        allow_follow_on_results: bool = False,
    ) -> tuple[float, float]:
        """Send one Application value and require one exact public-codec response value."""
        self._require_application_discovery()
        encoded = self._encode_application_message(message)
        delivery_latency_ms = self._submit_and_confirm(
            encoded,
            kind=kind,
            evidence=self._application_message_evidence(message, encoded),
        )
        decoded, response_latency_ms = self._wait_for_application_response(
            expected_type=type(expected),
            expected_value=expected,
            deadline=self._deadline(self.request_timeout_ms),
            allow_follow_on_results=allow_follow_on_results,
        )
        if decoded != expected:  # Defensive: the wait enforces exact equality above.
            raise AssertionError("Application response equality was not enforced")
        return delivery_latency_ms, response_latency_ms

    def discover_application(self) -> protocol.SystemInfoResponse:
        """Discover the current Application protocol and require the public exact-version gate."""
        request = protocol.SystemInfoRequest(
            protocol.SystemInfoQuery.BASIC,
            request_firmware_git_hash=True,
        )
        encoded = self._encode_application_message(request)
        delivery_latency_ms = self._submit_and_confirm(
            encoded,
            kind="APPLICATION_SYSTEM_INFO_REQUEST",
            evidence=self._application_message_evidence(request, encoded),
        )
        decoded, response_latency_ms = self._wait_for_application_response(
            expected_type=protocol.SystemInfoResponse,
            expected_value=None,
            deadline=self._deadline(self.request_timeout_ms),
        )
        assert type(decoded) is protocol.SystemInfoResponse
        try:
            protocol.check_protocol_version(decoded.protocol_version)
        except protocol.ApplicationVersionMismatchError as exc:
            self.trace.record(
                "application_discovery_version_mismatch",
                protocol_version=decoded.protocol_version,
                reason=str(exc),
            )
            raise ScenarioFailure(
                f"Application discovery protocol version mismatch: {exc}"
            ) from exc
        self._application_discovery_generation = self.connection.link_generation
        self.trace.record(
            "application_discovery_complete",
            link_generation=self.connection.link_generation,
            protocol_version=decoded.protocol_version,
            firmware_version=decoded.firmware_version,
            delivery_confirmation_latency_ms=delivery_latency_ms,
            response_latency_ms=response_latency_ms,
            **self._application_message_evidence(decoded, self.application_codec.encode(decoded)),
        )
        return decoded

    def _application_response(
        self,
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
        detail: int = 0,
    ) -> protocol.ApplicationResponse:
        return protocol.ApplicationResponse(
            test_id,
            scope,
            outcome,
            reason,
            tick_number=tick_number,
            control_command=control_command,
            global_control_command=global_control_command,
            detail=detail,
        )

    def _send_and_expect_response(
        self,
        message: protocol.ApplicationMessage,
        expected: protocol.ApplicationResponse,
        *,
        kind: str,
        allow_follow_on_results: bool = False,
    ) -> float:
        _, latency = self._exchange_application(
            message,
            expected,
            kind=kind,
            allow_follow_on_results=allow_follow_on_results,
        )
        return latency

    def _wait_application_results(
        self,
        test_id: protocol.TestId,
        expected: dict[int, protocol.ApplicationMessage],
        *,
        variable: bool,
    ) -> dict[int, protocol.ApplicationMessage]:
        """Collect a contiguous, non-interleaved result stream without ACKs."""
        expected_ticks = sorted(expected)
        if expected_ticks != list(range(len(expected_ticks))):
            raise ScenarioFailure(f"expected result ticks are not contiguous: {expected_ticks}")
        started = self._monotonic()
        chunks: dict[int, list[protocol.CapturedRecord]] = {}
        conditions: dict[int, tuple[protocol.ResultCondition, int]] = {}
        chunk_counts: dict[int, int] = {}
        complete: dict[int, protocol.ApplicationMessage] = {}
        next_tick = 0
        deadline = self._deadline(self.request_timeout_ms)
        while self._monotonic() <= deadline and next_tick < len(expected_ticks):
            self._service()
            received = self._take_pending(hrtp=False)
            if received is None:
                self._pause()
                continue
            if len(received.data) > APPLICATION_CODEC_CONFIG.max_encoded_message_size:
                raise ScenarioFailure("Application result exceeded the configured message limit")
            decoded = self.application_codec.decode(received.data)
            if getattr(decoded, "test_id", None) != test_id:
                raise ScenarioFailure("result Test ID mismatch")
            tick = getattr(decoded, "tick_number", None)
            if tick is not None and tick < next_tick:
                raise ScenarioFailure("duplicate TestResult received")
            if tick != next_tick:
                raise ScenarioFailure(
                    f"result ticks were not contiguous/in order: expected {next_tick}, got {tick}"
                )
            if variable:
                if type(decoded) is not protocol.VariableTestResult:
                    raise ScenarioFailure("fixed result received for variable result profile")
                chunk_counts[tick] = chunk_counts.get(tick, 0) + 1
                if chunk_counts[tick] > 8:
                    raise ScenarioFailure("firmware emitted more than eight result chunks")
                marker = (decoded.condition, decoded.problem_detail)
                if tick in conditions and conditions[tick] != marker:
                    raise ScenarioFailure("Type 34 condition/detail changed across chunks")
                conditions[tick] = marker
                chunks.setdefault(tick, []).extend(decoded.records)
                if decoded.flags == 1:
                    pass
                elif decoded.flags == 0:
                    assembled = replace(decoded, records=tuple(chunks[tick]), flags=0)
                    if assembled != expected[tick]:
                        raise ScenarioFailure(
                            f"variable result did not match deterministic oracle at tick {tick}"
                        )
                    complete[tick] = assembled
                    next_tick += 1
                else:
                    raise ScenarioFailure(f"invalid Type 34 flags for tick {tick}")
            else:
                if type(decoded) is not protocol.TestResult:
                    raise ScenarioFailure("variable result received for fixed result profile")
                if decoded != expected[tick]:
                    raise ScenarioFailure(
                        f"fixed result did not match deterministic oracle at tick {tick}"
                    )
                complete[tick] = decoded
                next_tick += 1
            self.trace.record(
                "application_result_tick",
                elapsed_ms=(self._monotonic() - started) * 1000,
                tick=tick,
                result_family="VARIABLE" if variable else "FIXED",
                chunk_index=chunk_counts.get(tick, 1),
                records=len(getattr(decoded, "records", ())),
                condition=getattr(getattr(decoded, "condition", None), "name", None),
                problem_detail=getattr(decoded, "problem_detail", 0),
            )
            self.trace.record(
                "application_result_decoded",
                tick=tick,
                chunk_index=chunk_counts.get(tick, 1),
                result_family="VARIABLE" if variable else "FIXED",
            )
        if next_tick != len(expected_ticks):
            raise ScenarioFailure(
                "timed out waiting for results; "
                f"expected ticks {sorted(expected)}, received {sorted(complete)}"
            )
        self._service()
        extra = self._take_pending(hrtp=False)
        if extra is not None:
            try:
                extra_message = self.application_codec.decode(extra.data)
            except protocol.ApplicationDecodeError as exc:
                raise ScenarioFailure("malformed trailing Application result") from exc
            if type(extra_message) in {
                protocol.TestResult,
                protocol.VariableTestResult,
            }:
                raise ScenarioFailure("duplicate TestResult received")
            raise ScenarioFailure("unexpected trailing Application message after results")
        return complete

    def _run_application_transaction(
        self,
        configuration: protocol.TestConfiguration,
        instructions: tuple[protocol.ApplicationMessage, ...],
        expected_results: dict[int, protocol.ApplicationMessage],
        *,
        variable_instruction: bool,
        variable_result: bool,
    ) -> dict[str, object]:
        self._require_application_discovery()
        test_id = configuration.test_id
        response_latencies = [
            self._send_and_expect_response(
                configuration,
                self._application_response(
                    test_id,
                    protocol.ResponseScope.TEST_CONFIGURATION,
                    protocol.ResponseOutcome.ACCEPTED,
                ),
                kind="APPLICATION_TEST_CONFIGURATION",
            )
        ]
        for message in instructions:
            if variable_instruction and type(message) is protocol.UpdateInstruction:
                if message.flags == 1:
                    encoded = self._encode_application_message(message)
                    self._submit_and_confirm(
                        encoded,
                        kind="APPLICATION_UPDATE_INSTRUCTION_CONTINUATION",
                        evidence=self._application_message_evidence(message, encoded),
                    )
                else:
                    response_latencies.append(
                        self._send_and_expect_response(
                            message,
                            self._application_response(
                                test_id,
                                protocol.ResponseScope.TICK,
                                protocol.ResponseOutcome.ACCEPTED,
                                tick_number=message.tick_number,
                            ),
                            kind="APPLICATION_UPDATE_INSTRUCTION_FINAL",
                        ),
                    )
            else:
                response_latencies.append(
                    self._send_and_expect_response(
                        message,
                        self._application_response(
                            test_id,
                            protocol.ResponseScope.TICK,
                            protocol.ResponseOutcome.ACCEPTED,
                            tick_number=message.tick_number,
                        ),
                        kind="APPLICATION_TEST_INSTRUCTION",
                    ),
                )
        finalizer = finalize_upload(test_id)
        response_latencies.append(
            self._send_and_expect_response(
                finalizer,
                self._application_response(
                    test_id, protocol.ResponseScope.COMPLETE_TEST, protocol.ResponseOutcome.ACCEPTED
                ),
                kind="APPLICATION_FINALIZE_TEST_UPLOAD",
            ),
        )
        start = protocol.ExecutionControl(test_id, protocol.ControlCommand.START)
        response_latencies.append(
            self._send_and_expect_response(
                start,
                self._application_response(
                    test_id,
                    protocol.ResponseScope.EXECUTION_CONTROL,
                    protocol.ResponseOutcome.COMPLETED,
                    control_command=protocol.ControlCommand.START,
                ),
                kind="APPLICATION_EXECUTION_CONTROL_START",
                allow_follow_on_results=True,
            ),
        )
        results = self._wait_application_results(
            test_id, expected_results, variable=variable_result
        )
        return {
            "test_id_hex": test_id.bytes.hex(),
            "instruction_family": "VARIABLE" if variable_instruction else "FIXED",
            "result_family": "VARIABLE" if variable_result else "FIXED",
            "result_ticks": sorted(results),
            "result_count": len(results),
            "response_latencies_ms": response_latencies,
        }

    def run_application_v03(self) -> dict[str, object]:
        """Run the bounded v0.3 lifecycle and family-independence smoke matrix."""
        self.run_status()
        self.discover_application()
        cases: list[dict[str, object]] = []

        fixed_id = new_test_id()
        fixed_config = replace(all_disabled_configuration(fixed_id), expected_tick_count=3)
        fixed_instructions = representative_instructions(fixed_id)
        cases.append(
            self._run_application_transaction(
                fixed_config,
                fixed_instructions,
                {
                    item.tick_number: expected_result(fixed_config, item)
                    for item in fixed_instructions
                },
                variable_instruction=False,
                variable_result=False,
            )
        )

        fixed_variable_id = new_test_id()
        fixed_variable_config = replace(
            representative_configuration(fixed_variable_id),
            expected_tick_count=3,
            extension_data=test_profile(result_family=1),
        )
        fixed_variable_instructions = representative_instructions(fixed_variable_id)
        cases.append(
            self._run_application_transaction(
                fixed_variable_config,
                fixed_variable_instructions,
                {
                    tick: replace(variable_result_oracle(fixed_variable_id, tick), records=())
                    for tick in range(3)
                },
                variable_instruction=False,
                variable_result=True,
            )
        )

        variable_id = new_test_id()
        variable_config = replace(
            representative_configuration(variable_id),
            expected_tick_count=3,
            extension_data=test_profile(result_family=1),
        )
        variable_messages = sparse_variable_upload(variable_id)
        variable_expected = {tick: variable_result_oracle(variable_id, tick) for tick in range(3)}
        # Tick 1 is intentionally omitted from upload and must still have a zero-record result.
        variable_expected[1] = replace(variable_result_oracle(variable_id, 1), records=())
        cases.append(
            self._run_application_transaction(
                variable_config,
                variable_messages,
                variable_expected,
                variable_instruction=True,
                variable_result=True,
            )
        )

        multi_chunk_id = new_test_id()
        multi_chunk_config = replace(
            representative_configuration(multi_chunk_id),
            expected_tick_count=1,
            extension_data=test_profile(result_family=1),
        )
        multi_chunk_messages = multi_chunk_variable_upload(multi_chunk_id)
        cases.append(
            self._run_application_transaction(
                multi_chunk_config,
                multi_chunk_messages,
                {0: multi_chunk_variable_result_oracle(multi_chunk_id)},
                variable_instruction=True,
                variable_result=True,
            )
        )

        variable_fixed_id = new_test_id()
        variable_fixed_config = replace(
            representative_configuration(variable_fixed_id),
            expected_tick_count=1,
            extension_data=test_profile(result_family=0),
        )
        variable_fixed_messages = variable_operations(variable_fixed_id)
        variable_fixed_initial = configured_initial_instruction(variable_fixed_config)
        cases.append(
            self._run_application_transaction(
                variable_fixed_config,
                variable_fixed_messages,
                {0: expected_result(variable_fixed_config, variable_fixed_initial)},
                variable_instruction=True,
                variable_result=False,
            )
        )

        return {"application_scenario": "application-v03", "cases": cases}

    def run_application_smoke(self) -> dict[str, object]:
        self.run_status()
        self.discover_application()
        test_id = new_test_id()
        configuration = representative_configuration(test_id)
        instructions = representative_instructions(test_id)
        transaction = self._run_application_transaction(
            configuration,
            instructions,
            {item.tick_number: expected_result(configuration, item) for item in instructions},
            variable_instruction=False,
            variable_result=False,
        )
        status = self.run_status()
        if status.application_harness_state != int(ApplicationHarnessState.COMPLETE):
            raise ScenarioFailure("Application smoke did not finish in COMPLETE state")
        return {
            "application_scenario": "application-smoke",
            "test_id_hex": test_id.bytes.hex(),
            "configuration_digest": REPRESENTATIVE_CONFIGURATION_DIGEST,
            "instruction_digests": [instruction_semantic_digest(item) for item in instructions],
            "result_latencies_ms": transaction["response_latencies_ms"],
            "final_status": status,
        }

    def run_application_boundaries(self) -> dict[str, object]:
        self.run_status()
        self.discover_application()
        first_id = new_test_id()
        disabled = all_disabled_configuration(first_id)
        first_instruction = zero_instruction(first_id)
        self._run_application_transaction(
            disabled,
            (first_instruction,),
            {0: expected_result(disabled, first_instruction)},
            variable_instruction=False,
            variable_result=False,
        )

        representative_id = new_test_id()
        representative = representative_configuration(representative_id)
        representative_wire = self._encode_application_message(
            representative, semantic_digest=REPRESENTATIVE_CONFIGURATION_DIGEST
        )
        if len(representative_wire) != REPRESENTATIVE_CONFIGURATION_SIZE:
            raise ScenarioFailure(
                "representative configuration encoded size mismatch: "
                f"expected {REPRESENTATIVE_CONFIGURATION_SIZE}, "
                f"got {len(representative_wire)}"
            )
        representative_instructions_for_transaction = representative_instructions(representative_id)
        self._run_application_transaction(
            representative,
            representative_instructions_for_transaction,
            {
                item.tick_number: expected_result(representative, item)
                for item in representative_instructions_for_transaction
            },
            variable_instruction=False,
            variable_result=False,
        )

        second_id = new_test_id()
        maximum = maximum_extension_configuration(second_id)
        instruction = representative_instructions(second_id)[0]
        self._run_application_transaction(
            maximum,
            (instruction,),
            {0: expected_result(maximum, instruction)},
            variable_instruction=False,
            variable_result=False,
        )
        status = self.run_status()

        restricted = protocol.ApplicationCodec(
            protocol.ApplicationConfig(
                max_encoded_message_size=MAX_EXTENSION_CONFIGURATION_SIZE - 1,
                max_variable_data_size=APPLICATION_CODEC_CONFIG.max_variable_data_size,
                max_expected_tick_count=APPLICATION_CODEC_CONFIG.max_expected_tick_count,
            )
        )
        try:
            restricted.encode(maximum)
        except protocol.ApplicationEncodeError as exc:
            self.trace.record(
                "application_local_oversize_rejected",
                configured_maximum=MAX_EXTENSION_CONFIGURATION_SIZE - 1,
                attempted_encoded_size=MAX_EXTENSION_CONFIGURATION_SIZE,
                status=exc.status,
            )
        else:
            raise ScenarioFailure(
                "Application message beyond configured maximum was not rejected locally"
            )
        return {
            "application_scenario": "application-boundaries",
            "test_ids_hex": [
                first_id.bytes.hex(),
                representative_id.bytes.hex(),
                second_id.bytes.hex(),
            ],
            "configuration_digests": [
                ALL_DISABLED_CONFIGURATION_DIGEST,
                REPRESENTATIVE_CONFIGURATION_DIGEST,
                MAX_EXTENSION_CONFIGURATION_DIGEST,
            ],
            "instruction_digests": [
                instruction_semantic_digest(zero_instruction(first_id)),
                instruction_semantic_digest(instruction),
            ],
            "encoded_sizes": [
                ALL_DISABLED_CONFIGURATION_SIZE,
                REPRESENTATIVE_CONFIGURATION_SIZE,
                MAX_EXTENSION_CONFIGURATION_SIZE,
                FIXED_INSTRUCTION_SIZE,
                FIXED_RESULT_SIZE,
            ],
            "final_status": status,
        }

    def run_application_negative(self) -> dict[str, object]:
        baseline = self.run_status()
        self.discover_application()
        test_id = new_test_id()
        configuration = representative_configuration(test_id)
        valid_wire = self._encode_application_message(
            configuration, semantic_digest=REPRESENTATIVE_CONFIGURATION_DIGEST
        )
        malformed = valid_wire[:-1]
        self._submit_and_confirm(
            malformed,
            kind="APPLICATION_MALFORMED",
            evidence={"test_id_hex": test_id.bytes.hex(), "mutation": "truncate_last_byte"},
        )
        after_malformed = self.run_status()
        if after_malformed.application_decode_failures != baseline.application_decode_failures + 1:
            raise ScenarioFailure("Application decode-failure counter did not increase")
        if after_malformed.invalid_hrtp_messages != baseline.invalid_hrtp_messages:
            raise ScenarioFailure(
                "malformed Application message incorrectly incremented invalid HRTP counter"
            )
        if after_malformed.configurations_accepted != baseline.configurations_accepted:
            raise ScenarioFailure("malformed Application message was accepted as a configuration")

        self._send_and_expect_response(
            configuration,
            self._application_response(
                test_id,
                protocol.ResponseScope.TEST_CONFIGURATION,
                protocol.ResponseOutcome.ACCEPTED,
            ),
            kind="APPLICATION_TEST_CONFIGURATION",
        )
        wrong_id = new_test_id()
        while wrong_id == test_id:
            wrong_id = new_test_id()
        wrong_instruction = representative_instructions(wrong_id)[0]
        wrong_digest = instruction_semantic_digest(wrong_instruction)
        self._send_and_expect_response(
            wrong_instruction,
            self._application_response(
                wrong_id,
                protocol.ResponseScope.TICK,
                protocol.ResponseOutcome.REJECTED,
                tick_number=0,
                reason=protocol.ResponseReason.INCONSISTENT_TEST_ID,
            ),
            kind="APPLICATION_SEMANTIC_REJECTION",
        )
        rejected = self.run_status()
        if (
            rejected.application_semantic_rejections
            != after_malformed.application_semantic_rejections + 1
        ):
            raise ScenarioFailure("Application semantic-rejection counter did not increase")
        if rejected.instructions_accepted != after_malformed.instructions_accepted:
            raise ScenarioFailure("wrong-Test-ID instruction was incorrectly accepted")
        if rejected.next_expected_tick != 0:
            raise ScenarioFailure("semantic rejection corrupted next expected tick")

        reset = reset_application_control()
        self._send_and_expect_response(
            reset,
            self._application_response(
                None,
                protocol.ResponseScope.GLOBAL_CONTROL,
                protocol.ResponseOutcome.COMPLETED,
                global_control_command=protocol.GlobalControlCommand.RESET_APPLICATION,
            ),
            kind="APPLICATION_GLOBAL_CONTROL_RESET_APPLICATION",
        )
        instructions = representative_instructions(test_id)
        self._run_application_transaction(
            configuration,
            instructions,
            {item.tick_number: expected_result(configuration, item) for item in instructions},
            variable_instruction=False,
            variable_result=False,
        )
        rejected = self.run_status()
        return {
            "application_scenario": "application-negative",
            "test_id_hex": test_id.bytes.hex(),
            "rejected_test_id_hex": wrong_id.bytes.hex(),
            "configuration_digest": REPRESENTATIVE_CONFIGURATION_DIGEST,
            "rejected_instruction_digest": wrong_digest,
            "accepted_instruction_digests": [
                instruction_semantic_digest(item) for item in instructions
            ],
            "decode_failures_delta": (
                rejected.application_decode_failures - baseline.application_decode_failures
            ),
            "semantic_rejections_delta": (
                rejected.application_semantic_rejections - baseline.application_semantic_rejections
            ),
            "final_status": rejected,
        }

    def run_application_repeat(self, count: int) -> dict[str, object]:
        if count < 1:
            raise ValueError("application repeat count must be positive")
        self.run_status()
        self.discover_application()
        latencies: list[float] = []
        test_ids: list[str] = []
        for _ in range(count):
            test_id = new_test_id()
            test_ids.append(test_id.bytes.hex())
            configuration = all_disabled_configuration(test_id)
            instruction = zero_instruction(test_id)
            transaction = self._run_application_transaction(
                configuration,
                (instruction,),
                {0: expected_result(configuration, instruction)},
                variable_instruction=False,
                variable_result=False,
            )
            latencies.extend(transaction["response_latencies_ms"])
        status = self.run_status()
        return {
            "application_scenario": "application-repeat",
            "completed": count,
            "test_ids_hex": test_ids,
            "configuration_digest": ALL_DISABLED_CONFIGURATION_DIGEST,
            "instruction_digest": instruction_semantic_digest(zero_instruction(bytes(16))),
            "latency_max_ms": max(latencies),
            "latency_average_ms": sum(latencies) / len(latencies),
            "final_status": status,
        }

    def run_application_message_fixtures(self) -> dict[str, object]:
        """Exercise control, response, and error message fixtures over v0.3.0."""
        baseline = self.run_status()
        discovery = self.discover_application()
        control_test_id = new_test_id()
        response_test_id = new_test_id()
        error_test_id = new_test_id()
        while len({control_test_id, response_test_id, error_test_id}) != 3:
            response_test_id = new_test_id()
            error_test_id = new_test_id()

        sizes: list[dict[str, object]] = []
        latencies: list[dict[str, object]] = []

        def exchange(
            message: protocol.ApplicationMessage,
            expected: protocol.ApplicationMessage,
            *,
            kind: str,
            expected_size: int | None = None,
        ) -> None:
            encoded = self.application_codec.encode(message)
            if expected_size is not None and len(encoded) != expected_size:
                raise ScenarioFailure(
                    f"{application_message_name(message)} encoded size mismatch: "
                    f"expected {expected_size}, got {len(encoded)}"
                )
            delivery_latency_ms, response_latency_ms = self._exchange_application(
                message, expected, kind=kind
            )
            evidence = self._application_message_evidence(message, encoded)
            sizes.append(
                {
                    "message_family": evidence["message_family"],
                    "message_scope": evidence["message_scope"],
                    "message_form": evidence["message_form"],
                    "size": len(encoded),
                }
            )
            latencies.append(
                {
                    "message_family": evidence["message_family"],
                    "message_scope": evidence["message_scope"],
                    "message_form": evidence["message_form"],
                    "delivery_confirmation_ms": delivery_latency_ms,
                    "response_ms": response_latency_ms,
                }
            )

        controls = execution_controls(control_test_id)
        control_expectations = (
            self._application_response(
                control_test_id,
                protocol.ResponseScope.EXECUTION_CONTROL,
                protocol.ResponseOutcome.REJECTED,
                control_command=protocol.ControlCommand.START,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            ),
            execution_control_response(controls[1]),
        )
        for control, expected_control in zip(controls, control_expectations, strict=True):
            exchange(
                control,
                expected_control,
                kind=f"APPLICATION_EXECUTION_CONTROL_{control.command.name}",
            )
        reset = reset_application_control()
        exchange(
            reset,
            global_control_response(reset),
            kind="APPLICATION_GLOBAL_CONTROL_RESET_APPLICATION",
        )

        for response in response_fixtures(response_test_id):
            exchange(
                response,
                response,
                kind=f"APPLICATION_RESPONSE_{response.scope.name}",
                expected_size=FIXED_RESPONSE_SIZE,
            )
        for error in application_error_fixtures(error_test_id):
            exchange(
                error,
                error,
                kind=f"APPLICATION_ERROR_{self._application_message_form(error)}",
                expected_size=ERROR_FIXED_SIZE + len(error.diagnostic_data),
            )

        final_status = self.run_status()
        failure_deltas = {
            "decode_failures": (
                final_status.application_decode_failures - baseline.application_decode_failures
            ),
            "semantic_rejections": (
                final_status.application_semantic_rejections
                - baseline.application_semantic_rejections
            ),
            "encode_failures": (
                final_status.application_encode_failures - baseline.application_encode_failures
            ),
        }
        if failure_deltas["decode_failures"] or failure_deltas["encode_failures"]:
            raise ScenarioFailure(
                "Application message-fixture scenario changed failure counters",
                details={"failure_deltas": failure_deltas, "final_status": final_status},
            )
        return {
            "application_scenario": "application-message-fixtures",
            "case_counts": {
                "system_information": 1,
                "execution_control": 2,
                "global_control": 1,
                "response_scopes": len(response_fixtures(response_test_id)),
                "error_forms": len(application_error_fixtures(error_test_id)),
                "total": 12,
            },
            "test_ids_hex": {
                "execution_control": control_test_id.bytes.hex(),
                "response_round_trip": response_test_id.bytes.hex(),
                "error_round_trip": error_test_id.bytes.hex(),
            },
            "discovery": discovery,
            "sizes": sizes,
            "latencies_ms": latencies,
            "failure_deltas": failure_deltas,
            "final_status": final_status,
        }

    def _reset_and_reconnect(
        self,
        *,
        prompt: Callable[[str], None],
        allow_unobserved_reset: bool,
        label: str,
    ) -> dict[str, object]:
        old_generation = self.connection.link_generation
        prompt(label)
        observe_deadline = self._deadline(self.reconnect_timeout_ms)
        disconnected = False
        while self._monotonic() <= observe_deadline:
            try:
                self._service(disconnect_expected=True)
            except LinkDisconnectedError:
                disconnected = True
                break
            self._pause()
        fallback_used = False
        if not disconnected:
            self.trace.record(
                "reset_disconnect_not_observed",
                link_generation=old_generation,
                allow_unobserved_reset=allow_unobserved_reset,
            )
            if not allow_unobserved_reset:
                raise ScenarioFailure(
                    "no physical serial disconnect was observed during the reset window",
                    details={
                        "physical_disconnect_observed": False,
                        "new_transport_session_established": False,
                        "post_reconnect_transaction_succeeded": False,
                        "host_link_fallback_used": False,
                        "old_link_generation": old_generation,
                        "new_link_generation": None,
                    },
                )
            fallback_used = True
            self.trace.record(
                "reset_host_link_fallback",
                link_generation=old_generation,
                classification="host-link recycle; serial disconnect not observed",
            )
            self.connection.close_link()
            self._pending_messages.clear()
            self._stale_application_messages.clear()
            self._application_discovery_generation = None
        reconnect_deadline = self._deadline(self.reconnect_timeout_ms)
        while self._monotonic() <= reconnect_deadline:
            try:
                generation = self.connection.open_link()
            except Exception as exc:
                self.trace.record("reconnect_attempt_failed", reason=str(exc))
                self._pause()
                continue
            self.trace.record(
                "link_reopened",
                link_generation=generation,
                serial_device=self.connection.serial_identity,
            )
            if generation == old_generation:
                raise ScenarioFailure("reconnect did not allocate a new link generation")
            self._pending_messages.clear()
            self._stale_application_messages.clear()
            self._application_discovery_generation = None
            self._wait_for_session(self._deadline(self.request_timeout_ms))
            return {
                "physical_disconnect_observed": disconnected,
                "new_transport_session_established": True,
                "post_reconnect_transaction_succeeded": False,
                "host_link_fallback_used": fallback_used,
                "old_link_generation": old_generation,
                "new_link_generation": generation,
            }
        raise ScenarioFailure("timed out re-resolving and reopening serial device")

    def run_reset_reconnect(
        self,
        cycles: int,
        *,
        prompt: Callable[[str], None],
        allow_unobserved_reset: bool = False,
    ) -> dict[str, object]:
        if cycles < 1:
            raise ValueError("cycles must be positive")
        cycle_results: list[dict[str, object]] = []
        for cycle in range(cycles):
            self.run_echo(f"before-reset-{cycle}".encode())
            result = self._reset_and_reconnect(
                prompt=prompt,
                allow_unobserved_reset=allow_unobserved_reset,
                label=f"Reset the HIL-RIG board for cycle {cycle + 1}, then continue",
            )
            try:
                self.run_echo(f"after-reset-{cycle}".encode())
            except ScenarioFailure as exc:
                raise ScenarioFailure(f"post-reconnect ECHO failed: {exc}", details=result) from exc
            cycle_result = {
                "cycle": cycle + 1,
                **result,
                "post_reconnect_transaction_succeeded": True,
            }
            cycle_results.append(cycle_result)
            self.trace.record("reset_cycle_complete", **cycle_result)
        return {
            "cycles": cycles,
            "physical_disconnect_observed": all(
                bool(result["physical_disconnect_observed"]) for result in cycle_results
            ),
            "new_transport_session_established": all(
                bool(result["new_transport_session_established"]) for result in cycle_results
            ),
            "post_reconnect_transaction_succeeded": all(
                bool(result["post_reconnect_transaction_succeeded"]) for result in cycle_results
            ),
            "host_link_fallback_used": any(
                bool(result["host_link_fallback_used"]) for result in cycle_results
            ),
            "old_link_generation": cycle_results[0]["old_link_generation"],
            "new_link_generation": cycle_results[-1]["new_link_generation"],
            "cycle_results": cycle_results,
        }

    def run_application_reset_reconnect(
        self,
        *,
        prompt: Callable[[str], None],
        allow_unobserved_reset: bool = False,
    ) -> dict[str, object]:
        status = self.run_status()
        self.discover_application()
        old_id = new_test_id()
        configuration = representative_configuration(old_id)
        self._send_and_expect_response(
            configuration,
            self._application_response(
                old_id,
                protocol.ResponseScope.TEST_CONFIGURATION,
                protocol.ResponseOutcome.ACCEPTED,
            ),
            kind="APPLICATION_TEST_CONFIGURATION",
        )
        instructions = representative_instructions(old_id)
        self._send_and_expect_response(
            instructions[0],
            self._application_response(
                old_id,
                protocol.ResponseScope.TICK,
                protocol.ResponseOutcome.ACCEPTED,
                tick_number=0,
            ),
            kind="APPLICATION_TEST_INSTRUCTION",
        )
        reconnect = self._reset_and_reconnect(
            prompt=prompt,
            allow_unobserved_reset=allow_unobserved_reset,
            label="Reset the HIL-RIG board after Application tick 0, then continue",
        )
        reset_status = self.run_status()
        self.discover_application()
        if reset_status.application_harness_state != int(
            ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        ):
            raise ScenarioFailure("Application harness did not reset to WAITING_FOR_CONFIGURATION")
        if reset_status.next_expected_tick != 0 or reset_status.active_expected_tick_count != 0:
            raise ScenarioFailure("Application transaction state was not cleared after reconnect")

        self._send_and_expect_response(
            instructions[1],
            self._application_response(
                old_id,
                protocol.ResponseScope.TICK,
                protocol.ResponseOutcome.REJECTED,
                tick_number=1,
                reason=protocol.ResponseReason.OPERATION_NOT_ALLOWED,
            ),
            kind="APPLICATION_STALE_TEST_INSTRUCTION",
        )
        rejected = self.run_status()
        if (
            rejected.application_semantic_rejections
            != reset_status.application_semantic_rejections + 1
        ):
            raise ScenarioFailure("old-test instruction was not rejected after reconnect")
        if rejected.application_harness_state != int(
            ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        ):
            raise ScenarioFailure("old-test rejection changed reset Application state")

        new_id = new_test_id()
        new_configuration = all_disabled_configuration(new_id)
        new_instruction = zero_instruction(new_id)
        self._run_application_transaction(
            new_configuration,
            (new_instruction,),
            {0: expected_result(new_configuration, new_instruction)},
            variable_instruction=False,
            variable_result=False,
        )
        status = self.run_status()
        return {
            "application_scenario": "application-reset-reconnect",
            "old_test_id_hex": old_id.bytes.hex(),
            "new_test_id_hex": new_id.bytes.hex(),
            "old_configuration_digest": REPRESENTATIVE_CONFIGURATION_DIGEST,
            "old_instruction_digest": instruction_semantic_digest(instructions[0]),
            "new_configuration_digest": ALL_DISABLED_CONFIGURATION_DIGEST,
            "new_instruction_digest": instruction_semantic_digest(new_instruction),
            **reconnect,
            "post_reconnect_transaction_succeeded": True,
            "final_status": status,
        }

    def run_soak(
        self,
        *,
        duration_seconds: float | None,
        count: int | None,
        status_every: int = 100,
    ) -> dict[str, object]:
        if duration_seconds is None and count is None:
            raise ValueError("soak requires a duration or transfer-count limit")
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if count is not None and count <= 0:
            raise ValueError("count must be positive")
        rng = random.Random(self.seed)
        started = self._monotonic()
        completed = 0
        while True:
            if count is not None and completed >= count:
                break
            if duration_seconds is not None and self._monotonic() - started >= duration_seconds:
                break
            size = min(
                self.max_payload_size,
                1 + (completed % max(1, min(256, self.max_payload_size))),
            )
            payload = bytes(rng.getrandbits(8) for _ in range(size))
            self.run_echo(payload)
            completed += 1
            if status_every > 0 and completed % status_every == 0:
                self.run_status()
                elapsed = self._monotonic() - started
                self.trace.record(
                    "soak_progress",
                    completed=completed,
                    elapsed_seconds=elapsed,
                    diagnostics=self.connection.get_diagnostics(),
                )
                logging.getLogger(__name__).info(
                    "soak progress: %d transfers in %.1f seconds", completed, elapsed
                )
        return {"completed": completed, "elapsed_seconds": self._monotonic() - started}
