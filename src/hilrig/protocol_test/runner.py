"""Synchronous Transport and Application hardware-test scenarios."""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict

import hil_rig_protocol as protocol
from hil_rig_protocol import EventType, SessionState, TransportStatus

from .application_hardware import (
    ALL_DISABLED_CONFIGURATION_DIGEST,
    ALL_DISABLED_CONFIGURATION_SIZE,
    APPLICATION_CODEC_CONFIG,
    COMPATIBILITY_PROFILE_ID,
    FIXED_INSTRUCTION_SIZE,
    FIXED_RESULT_SIZE,
    MAX_EXTENSION_CONFIGURATION_DIGEST,
    MAX_EXTENSION_CONFIGURATION_SIZE,
    PROTOCOL_VERSION,
    REPRESENTATIVE_CONFIGURATION_DIGEST,
    REPRESENTATIVE_CONFIGURATION_SIZE,
    all_disabled_configuration,
    application_message_name,
    configuration_semantic_digest,
    expected_result,
    instruction_semantic_digest,
    make_application_codec,
    maximum_extension_configuration,
    new_test_id,
    representative_configuration,
    representative_instructions,
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
        self._seen_results: set[tuple[bytes, int]] = set()
        self._active_request_id: int | None = None
        self._outbound_active = False
        self._delivery_confirmations = 0
        self._opened = False
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
            self.trace.record(
                "payload_submitted",
                payload_kind=kind,
                link_generation=self.connection.link_generation,
                payload_size=len(payload),
                payload_sha256=payload_hash(payload),
                submission_wait_ms=(submitted - started) * 1000,
                **(evidence or {}),
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
                        payload_size=len(payload),
                        payload_sha256=payload_hash(payload),
                        **(evidence or {}),
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
        evidence: dict[str, object] = {
            "application_scenario": self.trace.scenario,
            "test_id_hex": message.test_id.bytes.hex(),
            "encoded_message_type": application_message_name(message),
            "encoded_message_size": len(encoded),
            "payload_sha256": payload_hash(encoded),
        }
        if semantic_digest is not None:
            evidence["semantic_digest"] = semantic_digest
        self.trace.record("application_message_encoded", **evidence)
        return encoded

    def _send_configuration(
        self,
        configuration: protocol.TestConfiguration,
        baseline: StatusPayloadV2,
        *,
        expected_size: int,
        expected_digest: int,
    ) -> StatusPayloadV2:
        digest = configuration_semantic_digest(configuration)
        if digest != expected_digest:
            raise ScenarioFailure(
                f"configuration semantic digest mismatch: expected 0x{expected_digest:08X}, "
                f"computed 0x{digest:08X}"
            )
        encoded = self._encode_application_message(configuration, semantic_digest=digest)
        if len(encoded) != expected_size:
            raise ScenarioFailure(
                f"configuration encoded size mismatch: expected {expected_size}, got {len(encoded)}"
            )
        self._submit_and_confirm(
            encoded,
            kind="APPLICATION_TEST_CONFIGURATION",
            evidence={
                "test_id_hex": configuration.test_id.bytes.hex(),
                "configuration_digest": digest,
            },
        )
        status = self.run_status()
        if status.configurations_accepted != baseline.configurations_accepted + 1:
            raise ScenarioFailure("configuration acceptance counter did not increase by one")
        if status.configuration_digest != digest:
            raise ScenarioFailure(
                f"firmware configuration digest mismatch: expected 0x{digest:08X}, "
                f"got 0x{status.configuration_digest:08X}"
            )
        if status.application_harness_state != int(ApplicationHarnessState.ACCEPTING_INSTRUCTIONS):
            raise ScenarioFailure("firmware did not enter ACCEPTING_INSTRUCTIONS")
        if status.next_expected_tick != 0:
            raise ScenarioFailure("firmware next expected tick is not zero after configuration")
        if status.active_expected_tick_count != configuration.expected_tick_count:
            raise ScenarioFailure(
                "firmware active expected tick count does not match configuration"
            )
        return status

    def _wait_expected_result(
        self,
        expected: protocol.TestResult,
        *,
        deadline: float,
    ) -> float:
        started = self._monotonic()
        while self._monotonic() <= deadline:
            self._service()
            received = self._take_pending(hrtp=False)
            if received is not None:
                try:
                    decoded = self.application_codec.decode(received.data)
                except protocol.ApplicationDecodeError as exc:
                    self.trace.record(
                        "unexpected_message",
                        expected="TestResult",
                        actual="undecodable Application message",
                        reason=str(exc),
                        payload_size=len(received.data),
                        payload_sha256=payload_hash(received.data),
                    )
                    raise ScenarioFailure(
                        f"failed to decode firmware Application result: {exc}"
                    ) from exc
                if type(decoded) is not protocol.TestResult:
                    self.trace.record(
                        "unexpected_message",
                        expected="TestResult",
                        actual=application_message_name(decoded),
                        decoded_message=decoded,
                    )
                    raise ScenarioFailure(
                        f"unexpected Application message family {application_message_name(decoded)}"
                    )
                key = (decoded.test_id.bytes, decoded.tick_number)
                if key in self._seen_results:
                    raise ScenarioFailure(
                        f"duplicate TestResult for Test ID {decoded.test_id.bytes.hex()} "
                        f"tick {decoded.tick_number}"
                    )
                self._seen_results.add(key)
                self.trace.record(
                    "application_result_decoded",
                    test_id_hex=decoded.test_id.bytes.hex(),
                    tick=decoded.tick_number,
                    condition=decoded.condition,
                    payload_size=len(received.data),
                    payload_sha256=payload_hash(received.data),
                )
                if decoded != expected:
                    details = {"expected_result": expected, "actual_result": decoded}
                    self.trace.record("application_result_mismatch", **details)
                    raise ScenarioFailure(
                        "decoded TestResult did not match deterministic oracle", details=details
                    )
                self._service()
                extra = self._take_pending(hrtp=False)
                if extra is not None:
                    try:
                        duplicate = self.application_codec.decode(extra.data)
                    except protocol.ApplicationDecodeError as exc:
                        raise ScenarioFailure(
                            f"unexpected extra Application payload after TestResult: {exc}"
                        ) from exc
                    if type(duplicate) is protocol.TestResult:
                        duplicate_key = (duplicate.test_id.bytes, duplicate.tick_number)
                        if duplicate_key == key or duplicate_key in self._seen_results:
                            raise ScenarioFailure(
                                f"duplicate TestResult for Test ID {duplicate.test_id.bytes.hex()} "
                                f"tick {duplicate.tick_number}"
                            )
                    raise ScenarioFailure(
                        "more than one non-HRTP Application message followed instruction"
                    )
                return (self._monotonic() - started) * 1000
            if any(item.data.startswith(MAGIC) for item in self._pending_messages):
                unexpected = next(
                    item for item in self._pending_messages if item.data.startswith(MAGIC)
                )
                self.trace.record(
                    "unexpected_message",
                    expected="TestResult",
                    actual="HRTP response",
                    payload_size=len(unexpected.data),
                    payload_sha256=payload_hash(unexpected.data),
                )
                raise ScenarioFailure("unexpected HRTP message while waiting for TestResult")
            self._pause()
        raise ScenarioFailure("timed out waiting for TestResult")

    def _send_instruction(
        self,
        configuration: protocol.TestConfiguration,
        instruction: protocol.TestInstruction,
        baseline: StatusPayloadV2,
    ) -> tuple[StatusPayloadV2, float]:
        digest = instruction_semantic_digest(instruction)
        encoded = self._encode_application_message(instruction, semantic_digest=digest)
        if len(encoded) != FIXED_INSTRUCTION_SIZE:
            raise ScenarioFailure(
                "instruction encoded size mismatch: "
                f"expected {FIXED_INSTRUCTION_SIZE}, got {len(encoded)}"
            )
        delivery_ms = self._submit_and_confirm(
            encoded,
            kind="APPLICATION_TEST_INSTRUCTION",
            evidence={
                "test_id_hex": instruction.test_id.bytes.hex(),
                "tick": instruction.tick_number,
                "instruction_digest": digest,
            },
        )
        expected = expected_result(configuration, instruction)
        result_latency_ms = self._wait_expected_result(
            expected, deadline=self._deadline(self.request_timeout_ms)
        )
        result_wire = self.application_codec.encode(expected)
        if len(result_wire) != FIXED_RESULT_SIZE:
            raise ScenarioFailure(
                "result encoded size mismatch: "
                f"expected {FIXED_RESULT_SIZE}, got {len(result_wire)}"
            )
        status = self.run_status()
        if status.instructions_accepted != baseline.instructions_accepted + 1:
            raise ScenarioFailure("instruction acceptance counter did not increase by one")
        if status.results_encoded != baseline.results_encoded + 1:
            raise ScenarioFailure("results encoded counter did not increase by one")
        if status.last_instruction_digest != digest:
            raise ScenarioFailure(
                f"firmware instruction digest mismatch: expected 0x{digest:08X}, "
                f"got 0x{status.last_instruction_digest:08X}"
            )
        next_tick = instruction.tick_number + 1
        if status.next_expected_tick != next_tick:
            raise ScenarioFailure(
                "next expected tick mismatch: "
                f"expected {next_tick}, got {status.next_expected_tick}"
            )
        expected_state = (
            ApplicationHarnessState.COMPLETE
            if next_tick == configuration.expected_tick_count
            else ApplicationHarnessState.ACCEPTING_INSTRUCTIONS
        )
        if status.application_harness_state != int(expected_state):
            raise ScenarioFailure(
                f"firmware harness state mismatch after tick {instruction.tick_number}: "
                f"expected {expected_state.name}, got {status.application_harness_state}"
            )
        self.trace.record(
            "application_tick_complete",
            test_id_hex=instruction.test_id.bytes.hex(),
            tick=instruction.tick_number,
            instruction_digest=digest,
            delivery_confirmation_latency_ms=delivery_ms,
            result_latency_ms=result_latency_ms,
            status=status,
        )
        return status, result_latency_ms

    def run_application_smoke(self) -> dict[str, object]:
        baseline = self.run_status()
        test_id = new_test_id()
        configuration = representative_configuration(test_id)
        status = self._send_configuration(
            configuration,
            baseline,
            expected_size=REPRESENTATIVE_CONFIGURATION_SIZE,
            expected_digest=REPRESENTATIVE_CONFIGURATION_DIGEST,
        )
        latencies: list[float] = []
        for instruction in representative_instructions(test_id):
            status, latency = self._send_instruction(configuration, instruction, status)
            latencies.append(latency)
        if status.application_harness_state != int(ApplicationHarnessState.COMPLETE):
            raise ScenarioFailure("Application smoke did not finish in COMPLETE state")
        return {
            "application_scenario": "application-smoke",
            "test_id_hex": test_id.bytes.hex(),
            "configuration_digest": REPRESENTATIVE_CONFIGURATION_DIGEST,
            "instruction_digests": [
                instruction_semantic_digest(item) for item in representative_instructions(test_id)
            ],
            "result_latencies_ms": latencies,
            "final_status": status,
        }

    def run_application_boundaries(self) -> dict[str, object]:
        baseline = self.run_status()
        first_id = new_test_id()
        disabled = all_disabled_configuration(first_id)
        status = self._send_configuration(
            disabled,
            baseline,
            expected_size=ALL_DISABLED_CONFIGURATION_SIZE,
            expected_digest=ALL_DISABLED_CONFIGURATION_DIGEST,
        )
        status, _ = self._send_instruction(disabled, zero_instruction(first_id), status)

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

        second_id = new_test_id()
        maximum = maximum_extension_configuration(second_id)
        status = self._send_configuration(
            maximum,
            status,
            expected_size=MAX_EXTENSION_CONFIGURATION_SIZE,
            expected_digest=MAX_EXTENSION_CONFIGURATION_DIGEST,
        )
        instruction = representative_instructions(second_id)[0]
        status, _ = self._send_instruction(maximum, instruction, status)

        restricted = protocol.ApplicationCodec(
            protocol.ApplicationConfig(
                max_encoded_message_size=MAX_EXTENSION_CONFIGURATION_SIZE - 1,
                max_variable_data_size=APPLICATION_CODEC_CONFIG.max_variable_data_size,
                max_variable_transfers_per_tick=(
                    APPLICATION_CODEC_CONFIG.max_variable_transfers_per_tick
                ),
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

        status = self._send_configuration(
            configuration,
            after_malformed,
            expected_size=REPRESENTATIVE_CONFIGURATION_SIZE,
            expected_digest=REPRESENTATIVE_CONFIGURATION_DIGEST,
        )
        wrong_id = new_test_id()
        while wrong_id == test_id:
            wrong_id = new_test_id()
        wrong_instruction = representative_instructions(wrong_id)[0]
        wrong_digest = instruction_semantic_digest(wrong_instruction)
        wrong_wire = self._encode_application_message(
            wrong_instruction, semantic_digest=wrong_digest
        )
        self._submit_and_confirm(
            wrong_wire,
            kind="APPLICATION_SEMANTIC_REJECTION",
            evidence={
                "test_id_hex": wrong_id.bytes.hex(),
                "tick": 0,
                "instruction_digest": wrong_digest,
                "reason_expected": "wrong_test_id",
            },
        )
        rejected = self.run_status()
        if rejected.application_semantic_rejections != status.application_semantic_rejections + 1:
            raise ScenarioFailure("Application semantic-rejection counter did not increase")
        if rejected.instructions_accepted != status.instructions_accepted:
            raise ScenarioFailure("wrong-Test-ID instruction was incorrectly accepted")
        if rejected.next_expected_tick != 0:
            raise ScenarioFailure("semantic rejection corrupted next expected tick")

        instructions = representative_instructions(test_id)
        for instruction in instructions:
            rejected, _ = self._send_instruction(configuration, instruction, rejected)
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
        status = self.run_status()
        latencies: list[float] = []
        test_ids: list[str] = []
        for _ in range(count):
            test_id = new_test_id()
            test_ids.append(test_id.bytes.hex())
            configuration = all_disabled_configuration(test_id)
            status = self._send_configuration(
                configuration,
                status,
                expected_size=ALL_DISABLED_CONFIGURATION_SIZE,
                expected_digest=ALL_DISABLED_CONFIGURATION_DIGEST,
            )
            status, latency = self._send_instruction(
                configuration, zero_instruction(test_id), status
            )
            latencies.append(latency)
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
        old_id = new_test_id()
        configuration = representative_configuration(old_id)
        status = self._send_configuration(
            configuration,
            status,
            expected_size=REPRESENTATIVE_CONFIGURATION_SIZE,
            expected_digest=REPRESENTATIVE_CONFIGURATION_DIGEST,
        )
        instructions = representative_instructions(old_id)
        status, _ = self._send_instruction(configuration, instructions[0], status)
        reconnect = self._reset_and_reconnect(
            prompt=prompt,
            allow_unobserved_reset=allow_unobserved_reset,
            label="Reset the HIL-RIG board after Application tick 0, then continue",
        )
        reset_status = self.run_status()
        if reset_status.application_harness_state != int(
            ApplicationHarnessState.WAITING_FOR_CONFIGURATION
        ):
            raise ScenarioFailure("Application harness did not reset to WAITING_FOR_CONFIGURATION")
        if reset_status.next_expected_tick != 0 or reset_status.active_expected_tick_count != 0:
            raise ScenarioFailure("Application transaction state was not cleared after reconnect")

        old_wire = self._encode_application_message(
            instructions[1], semantic_digest=instruction_semantic_digest(instructions[1])
        )
        self._submit_and_confirm(
            old_wire,
            kind="APPLICATION_STALE_TEST_INSTRUCTION",
            evidence={"test_id_hex": old_id.bytes.hex(), "tick": 1},
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
        status = self._send_configuration(
            new_configuration,
            rejected,
            expected_size=ALL_DISABLED_CONFIGURATION_SIZE,
            expected_digest=ALL_DISABLED_CONFIGURATION_DIGEST,
        )
        status, _ = self._send_instruction(new_configuration, zero_instruction(new_id), status)
        return {
            "application_scenario": "application-reset-reconnect",
            "old_test_id_hex": old_id.bytes.hex(),
            "new_test_id_hex": new_id.bytes.hex(),
            "old_configuration_digest": REPRESENTATIVE_CONFIGURATION_DIGEST,
            "old_instruction_digest": instruction_semantic_digest(instructions[0]),
            "new_configuration_digest": ALL_DISABLED_CONFIGURATION_DIGEST,
            "new_instruction_digest": instruction_semantic_digest(zero_instruction(new_id)),
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
