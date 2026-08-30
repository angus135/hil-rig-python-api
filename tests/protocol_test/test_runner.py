from __future__ import annotations

import json
import struct
from collections import deque
from dataclasses import asdict
from pathlib import Path

import pytest
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

from hilrig.protocol_test.connection import LinkDisconnectedError, hardware_test_transport_config
from hilrig.protocol_test.harness_codec import Opcode, decode_message, encode_message
from hilrig.protocol_test.models import (
    ConnectionEvent,
    ReceivedApplicationMessage,
    SerialDevice,
    ServiceResult,
)
from hilrig.protocol_test.runner import ProtocolTestRunner, ScenarioFailure
from hilrig.protocol_test.trace import TraceWriter


class FakeTime:
    def __init__(self) -> None:
        self.now = 10.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


class ScenarioConnection:
    def __init__(self, clock: FakeTime, behavior: str = "success") -> None:
        self.clock = clock
        self.behavior = behavior
        self.transport_config = hardware_test_transport_config()
        self.link_generation: int | None = None
        self.serial_identity = SerialDevice("fake", "Fake", 1, 2, "SER")
        self.closed = False
        self.link_open = False
        self._generation = 0
        self.messages: deque[ReceivedApplicationMessage] = deque()
        self.events = deque()
        self.submit_statuses: deque[TransportStatus] = deque()
        self.submissions = 0
        self.service_calls = 0
        self.close_link_calls = 0
        self.disconnect_on_service = False
        self.service_results: deque[ServiceResult] = deque()
        self.open_failures = 0
        self.session_ready = True
        self.maximum_acceptable_service_gap_ms = 10
        self.late_service_calls = 0
        self.budget_exhaustions = 0
        self.max_service_gap_ms = 0

    def open_link(self) -> int:
        if self.open_failures:
            self.open_failures -= 1
            raise RuntimeError("simulated open failure")
        self._generation += 1
        self.link_generation = self._generation
        self.link_open = True
        return self._generation

    def close_link(self) -> None:
        self.close_link_calls += 1
        self.link_open = False
        self.link_generation = None
        self.messages.clear()

    def close(self) -> None:
        self.closed = True
        self.link_open = False

    def get_status(self) -> TransportSnapshot:
        return TransportSnapshot(
            Role.HOST,
            LinkState.CONNECTED if self.link_open else LinkState.DISCONNECTED,
            (
                SessionState.ESTABLISHED
                if self.link_open and self.session_ready
                else SessionState.CONNECTING
                if self.link_open
                else SessionState.DISCONNECTED
            ),
            OperatingMode.NORMAL if self.link_open else None,
            False,
            bool(self.messages),
            False,
            False,
            Failure.NONE,
        )

    def service_once(self) -> ServiceResult:
        self.service_calls += 1
        if self.disconnect_on_service:
            self.disconnect_on_service = False
            self.link_open = False
            self.link_generation = None
            raise LinkDisconnectedError("simulated disconnect")
        result = (
            self.service_results.popleft()
            if self.service_results
            else ServiceResult(False, False, 1, 1)
        )
        self.max_service_gap_ms = max(self.max_service_gap_ms, result.current_service_gap_ms)
        if result.current_service_gap_ms > self.maximum_acceptable_service_gap_ms:
            self.late_service_calls += 1
        if result.operation_budget_exhausted:
            self.budget_exhaustions += 1
        return result

    def pop_event(self):
        return self.events.popleft() if self.events else None

    def pop_application_message(self):
        return self.messages.popleft() if self.messages else None

    def get_diagnostics(self) -> dict[str, object]:
        return {
            "service_calls": self.service_calls,
            "generation": self.link_generation,
            "max_service_gap_ms": self.max_service_gap_ms,
            "counters": {
                "service_loops": self.service_calls,
                "late_loops": self.late_service_calls,
                "operation_budget_exhaustions": self.budget_exhaustions,
            },
        }

    def submit_application_data(self, data: bytes) -> TransportStatus:
        self.submissions += 1
        if self.submit_statuses:
            status = self.submit_statuses.popleft()
            if status is not TransportStatus.OK:
                return status
        request = decode_message(data, max_application_message_size=512)
        if self.behavior == "no_response":
            return TransportStatus.OK
        if self.behavior == "disconnect":
            self.disconnect_on_service = True
            return TransportStatus.OK
        request_id = request.request_id
        opcode = (
            Opcode.ECHO_RESPONSE
            if request.opcode is Opcode.ECHO_REQUEST
            else Opcode.STATUS_RESPONSE
        )
        payload = request.payload
        if request.opcode is Opcode.STATUS_REQUEST:
            payload = struct.pack("<12I", 1, 1, *range(10))
        if self.behavior == "wrong_id":
            request_id = (request_id + 1) & 0xFFFF_FFFF
        if self.behavior == "wrong_opcode":
            opcode = (
                Opcode.STATUS_RESPONSE if opcode is Opcode.ECHO_RESPONSE else Opcode.ECHO_RESPONSE
            )
        if self.behavior == "mismatch":
            payload = b"wrong"
        encoded = encode_message(
            opcode,
            request_id,
            payload,
            max_application_message_size=512,
        )
        generation = self.link_generation or 0
        if self.behavior == "stale_only":
            generation = max(0, generation - 1)
        self.messages.append(ReceivedApplicationMessage(encoded, generation, 0))
        if self.behavior == "duplicate":
            self.messages.append(ReceivedApplicationMessage(encoded, generation, 0))
        return TransportStatus.OK


def make_runner(
    tmp_path: Path, behavior: str = "success"
) -> tuple[ProtocolTestRunner, ScenarioConnection, TraceWriter, FakeTime]:
    fake_time = FakeTime()
    connection = ScenarioConnection(fake_time, behavior)
    trace = TraceWriter(tmp_path, "unit", seed=123)
    runner = ProtocolTestRunner(
        connection,  # type: ignore[arg-type]
        trace,
        poll_ms=1,
        request_timeout_ms=5,
        reconnect_timeout_ms=5,
        seed=123,
        sleep=fake_time.sleep,
        monotonic=fake_time.monotonic,
    )
    runner.open()
    return runner, connection, trace, fake_time


def finish(trace: TraceWriter, connection: ScenarioConnection, *, passed: bool) -> None:
    trace.finish(
        passed=passed,
        failure_reason=None if passed else "failed",
        diagnostics=connection.get_diagnostics(),
    )


def test_successful_echo(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    runner.run_echo(b"abc\x00def")
    assert connection.submissions == 1
    runner.close()
    finish(trace, connection, passed=True)


def test_not_ready_submission_is_retried_unchanged_then_succeeds(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    connection.submit_statuses.extend([TransportStatus.NOT_READY, TransportStatus.OK])
    runner.run_echo(b"same")
    assert connection.submissions == 2
    runner.close()
    finish(trace, connection, passed=True)


@pytest.mark.parametrize(
    ("behavior", "match"),
    [
        ("wrong_id", "wrong response request ID"),
        ("wrong_opcode", "wrong response opcode"),
        ("duplicate", "duplicate Application delivery"),
        ("mismatch", "payload mismatch"),
    ],
)
def test_response_correlation_failures(tmp_path: Path, behavior: str, match: str) -> None:
    runner, connection, trace, _ = make_runner(tmp_path, behavior)
    with pytest.raises(ScenarioFailure, match=match):
        runner.run_echo(b"payload")
    runner.close()
    finish(trace, connection, passed=False)


def test_request_timeout(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path, "no_response")
    with pytest.raises(ScenarioFailure, match="deadline"):
        runner.run_echo(b"payload")
    runner.close()
    finish(trace, connection, passed=False)


def test_disconnect_during_request(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path, "disconnect")
    with pytest.raises(LinkDisconnectedError):
        runner.run_echo(b"payload")
    runner.close()
    finish(trace, connection, passed=False)


def test_status_request_and_typed_decode(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    status = runner.run_status()
    assert status.schema_version == 1
    assert status.link_generation == 0
    assert status.transport_session_state == 9
    records = [json.loads(line) for line in trace.trace_path.read_text().splitlines()]
    raw = next(record for record in records if record["kind"] == "status_raw")
    assert raw["payload_hex"] == struct.pack("<12I", 1, 1, *range(10)).hex()
    runner.close()
    finish(trace, connection, passed=True)


def test_reset_reconnect_observed_disconnect_is_verified(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)

    def prompt(_: str) -> None:
        connection.disconnect_on_service = True

    result = runner.run_reset_reconnect(1, prompt=prompt)
    assert result["physical_disconnect_observed"] is True
    assert result["mcu_reset_verified"] is True
    assert result["host_link_fallback_used"] is False
    assert result["old_link_generation"] == 1
    assert result["new_link_generation"] == 2
    assert connection._generation == 2
    runner.close()
    finish(trace, connection, passed=True)


def test_reset_reconnect_strict_mode_fails_without_observed_disconnect(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    with pytest.raises(ScenarioFailure, match="no physical serial disconnect") as exc_info:
        runner.run_reset_reconnect(1, prompt=lambda _: None)
    assert exc_info.value.details is not None
    assert exc_info.value.details["physical_disconnect_observed"] is False
    assert exc_info.value.details["mcu_reset_verified"] is False
    assert exc_info.value.details["host_link_fallback_used"] is False
    assert connection._generation == 1
    runner.close()
    finish(trace, connection, passed=False)


def test_reset_reconnect_explicit_host_link_fallback_is_not_reset_verification(
    tmp_path: Path,
) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    result = runner.run_reset_reconnect(
        1,
        prompt=lambda _: None,
        allow_unobserved_reset=True,
    )
    assert result["physical_disconnect_observed"] is False
    assert result["mcu_reset_verified"] is False
    assert result["host_link_fallback_used"] is True
    assert result["old_link_generation"] == 1
    assert result["new_link_generation"] == 2
    runner.close()
    finish(trace, connection, passed=True)


def test_reset_reconnect_fails_when_reopen_never_succeeds(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)

    def prompt(_: str) -> None:
        connection.disconnect_on_service = True
        connection.open_failures = 100

    with pytest.raises(ScenarioFailure, match="timed out re-resolving"):
        runner.run_reset_reconnect(1, prompt=prompt)
    runner.close()
    finish(trace, connection, passed=False)


def test_reset_reconnect_fails_when_new_session_does_not_establish(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)

    def prompt(_: str) -> None:
        connection.disconnect_on_service = True
        connection.session_ready = False

    with pytest.raises(ScenarioFailure, match="session establishment"):
        runner.run_reset_reconnect(1, prompt=prompt)
    runner.close()
    finish(trace, connection, passed=False)


def test_idle_service_calls_do_not_emit_one_trace_record_per_loop(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    before = len(trace.trace_path.read_text().splitlines())
    for _ in range(100):
        runner._service()
    after = len(trace.trace_path.read_text().splitlines())
    assert after == before
    runner.close()
    finish(trace, connection, passed=True)


def test_service_anomalies_are_recorded(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    connection.service_results.extend(
        [
            ServiceResult(False, True, 1, 1),
            ServiceResult(False, False, 11, 11),
        ]
    )
    runner._service()
    runner._service()
    kinds = [json.loads(line)["kind"] for line in trace.trace_path.read_text().splitlines()]
    assert "service_budget_exhausted" in kinds
    assert "service_gap_late" in kinds
    runner.close()
    finish(trace, connection, passed=True)


def test_request_response_event_and_failure_evidence_remains_available(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    connection.events.append(
        ConnectionEvent(
            event=TransportEvent(
                EventType.SESSION_ESTABLISHED,
                TransportStatus.OK,
                Failure.NONE,
                0,
            ),
            link_generation=connection.link_generation,
            monotonic_ms=1000,
        )
    )
    runner.run_echo(b"evidence")
    runner._service()
    runner.close()
    trace.finish(
        passed=False, failure_reason="synthetic failure", diagnostics=connection.get_diagnostics()
    )
    kinds = [json.loads(line)["kind"] for line in trace.trace_path.read_text().splitlines()]
    assert "request_submitted" in kinds
    assert "response_received" in kinds
    assert "transport_event" in kinds
    assert "run_end" in kinds


def test_link_open_records_actual_effective_transport_configuration(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    record = next(
        json.loads(line)
        for line in trace.trace_path.read_text().splitlines()
        if json.loads(line)["kind"] == "link_open"
    )
    assert record["effective_transport_config"] == asdict(connection.transport_config)
    runner.close()
    finish(trace, connection, passed=True)


def test_stale_response_from_old_generation_cannot_satisfy_request(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path, "stale_only")
    with pytest.raises(ScenarioFailure, match="deadline"):
        runner.run_echo(b"payload")
    runner.close()
    finish(trace, connection, passed=False)


def test_json_summary_written_on_success(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path)
    runner.run_echo(b"ok")
    runner.close()
    summary = trace.finish(
        passed=True, failure_reason=None, diagnostics=connection.get_diagnostics()
    )
    loaded = json.loads(trace.summary_path.read_text())
    assert summary["passed"] is True
    assert loaded["passed"] is True
    assert loaded["diagnostics"]["counters"]["service_loops"] == connection.service_calls
    assert "late_loops" in loaded["diagnostics"]["counters"]
    assert "operation_budget_exhaustions" in loaded["diagnostics"]["counters"]
    assert "max_service_gap_ms" in loaded["diagnostics"]
    assert trace.trace_path.exists()


def test_json_summary_written_on_failure(tmp_path: Path) -> None:
    runner, connection, trace, _ = make_runner(tmp_path, "mismatch")
    with pytest.raises(ScenarioFailure):
        runner.run_echo(b"expected")
    runner.close()
    trace.finish(passed=False, failure_reason="mismatch", diagnostics=connection.get_diagnostics())
    loaded = json.loads(trace.summary_path.read_text())
    assert loaded["passed"] is False
    assert loaded["failure_reason"] == "mismatch"
