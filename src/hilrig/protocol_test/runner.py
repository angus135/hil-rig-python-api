"""Synchronous one-request-at-a-time Transport hardware-test scenarios."""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict

from hil_rig_protocol import EventType, SessionState, TransportStatus

from .connection import LinkDisconnectedError, ProtocolTestConnection
from .harness_codec import (
    HarnessCodecError,
    HarnessMessage,
    Opcode,
    RequestIdAllocator,
    StatusPayloadV1,
    decode_message,
    decode_status_payload,
    encode_echo_request,
    encode_status_request,
    maximum_payload_size,
)
from .trace import TraceWriter, payload_hash


class ScenarioFailure(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details


class ProtocolTestRunner:
    """Drive one connection synchronously and correlate one HRTP request at a time."""

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
        self._active_request_id: int | None = None
        self._opened = False

    @property
    def max_application_message_size(self) -> int:
        return self.connection.transport_config.max_application_message_size

    @property
    def max_payload_size(self) -> int:
        return maximum_payload_size(self.max_application_message_size)

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
            self.trace.record("transport_event", **fields)
            if self._active_request_id is not None and event.type in {
                EventType.DELIVERY_FAILED,
                EventType.PROTOCOL_ERROR,
                EventType.SESSION_RESET,
            }:
                raise ScenarioFailure(
                    f"unexpected Transport event during request: {event.type.name}"
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
            )
            if status is TransportStatus.OK:
                return
            if status not in {TransportStatus.NOT_READY, TransportStatus.CAPACITY_EXHAUSTED}:
                raise ScenarioFailure(f"submit_application_data returned {status.name}")
            self._service()
            self._pause()
        raise ScenarioFailure("timed out waiting for Transport Application submission capacity")

    def _next_current_message(self, deadline: float) -> HarnessMessage:
        while self._monotonic() <= deadline:
            self._service()
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
                try:
                    decoded = decode_message(
                        received.data,
                        max_application_message_size=self.max_application_message_size,
                    )
                except HarnessCodecError as exc:
                    self.trace.record(
                        "invalid_application_message",
                        reason=str(exc),
                        payload_size=len(received.data),
                        payload_sha256=payload_hash(received.data),
                    )
                    raise ScenarioFailure(f"invalid HRTP response: {exc}") from exc
                if decoded.request_id in self._completed_ids:
                    raise ScenarioFailure(
                        f"duplicate Application delivery for completed request {decoded.request_id}"
                    )
                return decoded
            self._pause()
        raise ScenarioFailure("request response deadline expired")

    def _exchange(self, encoded: bytes, request_id: int, expected_opcode: Opcode) -> HarnessMessage:
        self._active_request_id = request_id
        submit_started = self._monotonic()
        try:
            self._submit_until_ready(encoded, self._deadline(self.request_timeout_ms))
            submitted_at = self._monotonic()
            self.trace.record(
                "request_submitted",
                request_id=request_id,
                opcode=Opcode(encoded[5]).name,
                link_generation=self.connection.link_generation,
                payload_size=len(encoded) - 16,
                payload_sha256=payload_hash(encoded[16:]),
            )
            deadline = submitted_at + self.request_timeout_ms / 1000
            response = self._next_current_message(deadline)
            received_at = self._monotonic()
            if received_at > deadline:
                raise ScenarioFailure(f"response for request {request_id} arrived after deadline")
            if response.request_id != request_id:
                raise ScenarioFailure(
                    f"wrong response request ID: expected {request_id}, got {response.request_id}"
                )
            if response.opcode is not expected_opcode:
                raise ScenarioFailure(
                    "wrong response opcode: "
                    f"expected {expected_opcode.name}, got {response.opcode.name}"
                )
            self._completed_ids.append(request_id)
            self.trace.record(
                "response_received",
                request_id=request_id,
                opcode=response.opcode.name,
                link_generation=self.connection.link_generation,
                latency_ms=(received_at - submitted_at) * 1000,
                submission_wait_ms=(submitted_at - submit_started) * 1000,
                payload_size=len(response.payload),
                payload_sha256=payload_hash(response.payload),
            )
            # One bounded extra service catches immediately duplicated deliveries without
            # introducing an unbounded post-response wait.
            self._service()
            while (extra := self.connection.pop_application_message()) is not None:
                decoded = decode_message(
                    extra.data,
                    max_application_message_size=self.max_application_message_size,
                )
                if decoded.request_id == request_id:
                    raise ScenarioFailure(
                        f"duplicate Application delivery for request {request_id}"
                    )
                raise ScenarioFailure(
                    f"unexpected queued response {decoded.request_id} while only one request "
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

    def run_status(self) -> StatusPayloadV1:
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
            old_generation = self.connection.link_generation
            prompt(f"Reset the HIL-RIG board for cycle {cycle + 1}, then continue")
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
                    details = {
                        "cycles_requested": cycles,
                        "cycles_completed": cycle,
                        "physical_disconnect_observed": False,
                        "mcu_reset_verified": False,
                        "host_link_fallback_used": False,
                        "old_link_generation": old_generation,
                        "new_link_generation": None,
                        "cycle_results": cycle_results,
                    }
                    raise ScenarioFailure(
                        "MCU reset was not verified because no physical serial disconnect "
                        "was observed",
                        details=details,
                    )
                fallback_used = True
                self.trace.record(
                    "reset_host_link_fallback",
                    link_generation=old_generation,
                    classification="host-link recycle; MCU reset not verified",
                )
                self.connection.close_link()
            reconnect_deadline = self._deadline(self.reconnect_timeout_ms)
            new_generation: int | None = None
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
                try:
                    self._wait_for_session(self._deadline(self.request_timeout_ms))
                except ScenarioFailure as exc:
                    details = {
                        "cycles_requested": cycles,
                        "cycles_completed": cycle,
                        "physical_disconnect_observed": disconnected,
                        "mcu_reset_verified": False,
                        "host_link_fallback_used": fallback_used,
                        "old_link_generation": old_generation,
                        "new_link_generation": generation,
                        "cycle_results": cycle_results,
                    }
                    raise ScenarioFailure(
                        "reconnected serial link but Transport"
                        + f" session establishment failed: {exc}",
                        details=details,
                    ) from exc
                new_generation = generation
                break
            else:
                details = {
                    "cycles_requested": cycles,
                    "cycles_completed": cycle,
                    "physical_disconnect_observed": disconnected,
                    "mcu_reset_verified": False,
                    "host_link_fallback_used": fallback_used,
                    "old_link_generation": old_generation,
                    "new_link_generation": None,
                    "cycle_results": cycle_results,
                }
                raise ScenarioFailure(
                    "timed out re-resolving and reopening serial device", details=details
                )
            try:
                self.run_echo(f"after-reset-{cycle}".encode())
            except ScenarioFailure as exc:
                details = {
                    "cycles_requested": cycles,
                    "cycles_completed": cycle,
                    "physical_disconnect_observed": disconnected,
                    "mcu_reset_verified": False,
                    "host_link_fallback_used": fallback_used,
                    "old_link_generation": old_generation,
                    "new_link_generation": new_generation,
                    "cycle_results": cycle_results,
                }
                raise ScenarioFailure(
                    f"post-reconnect ECHO failed: {exc}", details=details
                ) from exc
            cycle_result = {
                "cycle": cycle + 1,
                "physical_disconnect_observed": disconnected,
                "mcu_reset_verified": disconnected,
                "host_link_fallback_used": fallback_used,
                "old_link_generation": old_generation,
                "new_link_generation": new_generation,
            }
            cycle_results.append(cycle_result)
            self.trace.record("reset_cycle_complete", **cycle_result)
        return {
            "cycles": cycles,
            "physical_disconnect_observed": all(
                bool(result["physical_disconnect_observed"]) for result in cycle_results
            ),
            "mcu_reset_verified": all(
                bool(result["mcu_reset_verified"]) for result in cycle_results
            ),
            "host_link_fallback_used": any(
                bool(result["host_link_fallback_used"]) for result in cycle_results
            ),
            "old_link_generation": cycle_results[0]["old_link_generation"],
            "new_link_generation": cycle_results[-1]["new_link_generation"],
            "cycle_results": cycle_results,
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
