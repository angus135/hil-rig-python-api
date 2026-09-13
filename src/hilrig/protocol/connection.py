"""Caller-driven composition of pySerial, Transport, and Application codecs."""

from __future__ import annotations

import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from hilrig.exceptions import ProtocolSessionError
from hilrig.models.execution import CompiledTestIR
from hilrig.models.identifiers import UploadAttempt
from hilrig.protocol.application import FixedIOProtocolAdapter, FixedIOUploadMessages
from hilrig.protocol.serial import SerialConnectionSettings, open_serial_port
from hilrig.results.adapter import IncomingResultAdapter
from hilrig.results.builder import CapturedRunBuilder
from hilrig.results.models import TickResult

_UINT32_MASK = (1 << 32) - 1


def monotonic_now_ms() -> int:
    """Return monotonic milliseconds in Transport's wrapped uint32 domain."""
    return int(time.monotonic() * 1_000) & _UINT32_MASK


@dataclass(frozen=True, slots=True)
class ProtocolServiceReport:
    """Work completed by one non-blocking connection service pass."""

    events: tuple[object, ...]
    application_messages: tuple[object, ...]
    stored_tick_results: tuple[TickResult, ...]
    serial_bytes_read: int
    serial_bytes_written: int
    application_message_submitted: bool


@dataclass(frozen=True, slots=True)
class _QueuedApplicationMessage:
    """Encoded message plus correlation retained for future Application Responses."""

    encoded: bytes
    operation: str
    tick: int | None = None


class FixedIOProtocolConnection:
    """Own one host Transport session over a discovered USB CDC serial port.

    All methods must be called on the thread that created the connection, matching
    the protocol wrapper's ownership rule. ``service`` is intentionally non-blocking;
    a CLI, GUI, or later asyncio coordinator decides how often to call it.
    """

    def __init__(
        self,
        *,
        serial_port: Any,
        application: FixedIOProtocolAdapter,
        transport: Any,
        result_adapter: IncomingResultAdapter | None = None,
    ) -> None:
        self.serial_port = serial_port
        self.application = application
        self.transport = transport
        self.result_adapter = result_adapter
        self.protocol = application.protocol

        self._incoming = bytearray()
        self._pending_output: bytes | None = None
        self._pending_output_offset = 0
        self._outgoing_application: deque[_QueuedApplicationMessage] = deque()
        self._transport_delivery_pending = False
        self._active_upload: FixedIOUploadMessages | None = None
        self._closed = False

    @classmethod
    def connect(
        cls,
        *,
        serial_settings: SerialConnectionSettings | None = None,
        transport_config: object | None = None,
        application_config: object | None = None,
        result_builder: CapturedRunBuilder | None = None,
        protocol_module: ModuleType | Any | None = None,
        serial_factory: Any | None = None,
        comports: Any | None = None,
    ) -> FixedIOProtocolConnection:
        """Discover the exact device name, open it, and begin a host session."""
        application = FixedIOProtocolAdapter(
            protocol_module=protocol_module,
            application_config=application_config,
        )
        p = application.protocol
        if transport_config is None:
            transport_config = p.TransportConfig()
        serial_port = open_serial_port(
            serial_settings,
            serial_factory=serial_factory,
            comports=comports,
        )
        try:
            transport = p.Transport(p.Role.HOST, transport_config)
            result_adapter = (
                IncomingResultAdapter(result_builder, protocol_module=p)
                if result_builder is not None
                else None
            )
            connection = cls(
                serial_port=serial_port,
                application=application,
                transport=transport,
                result_adapter=result_adapter,
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
    def upload_delivery_complete(self) -> bool:
        """Whether all queued messages reached the peer Transport endpoint."""
        return (
            self._active_upload is not None
            and not self._outgoing_application
            and not self._transport_delivery_pending
        )

    @property
    def queued_application_message_count(self) -> int:
        return len(self._outgoing_application)

    def queue_upload(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
    ) -> UploadAttempt:
        """Queue configuration plus sparse fixed-I/O states for reliable delivery."""
        self._require_open()
        if self._outgoing_application or self._transport_delivery_pending:
            raise ProtocolSessionError("An Application upload is already in progress")

        if upload_attempt is None and self.result_adapter is not None:
            upload_attempt = UploadAttempt(
                definition_test_id=compiled_test.test_id,
                application_test_id=self.result_adapter.builder.application_test_id,
            )
        upload = self.application.build_upload(
            compiled_test,
            upload_attempt=upload_attempt,
        )
        if (
            self.result_adapter is not None
            and upload.upload_attempt.application_test_id
            != self.result_adapter.builder.application_test_id
        ):
            raise ValueError(
                "upload_attempt Application Test ID does not match the captured-run builder"
            )

        encoded = self.application.encode_upload(upload)
        self._outgoing_application.append(
            _QueuedApplicationMessage(encoded=encoded[0], operation="configuration")
        )
        self._outgoing_application.extend(
            _QueuedApplicationMessage(
                encoded=wire,
                operation="tick",
                tick=message.tick_number,
            )
            for message, wire in zip(upload.instructions, encoded[1:], strict=True)
        )
        self._active_upload = upload
        # Scaffold for public Execution Control: after the complete-test ACCEPTED
        # response, self._active_upload.start_mode determines whether START is queued
        # immediately or left for the host-command UI. The external-trigger policy can
        # be added at this same boundary without changing fixed state expansion.
        return upload.upload_attempt

    def send_upload(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
        timeout_s: float = 10.0,
        poll_interval_s: float = 0.001,
    ) -> UploadAttempt:
        """Service synchronously until every upload message is Transport-delivered.

        This does not claim Application acceptance. Public Response and Execution
        Control support is still required before the host can await configuration,
        tick, complete-test, or START responses.
        """
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
        while not self.upload_delivery_complete:
            self.service()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Timed out before the fixed-I/O upload was delivered by Transport"
                )
            if poll_interval_s:
                time.sleep(float(poll_interval_s))
        return attempt

    def service(self, *, operating_mode: object | None = None) -> ProtocolServiceReport:
        """Run one bounded read/process/drain/submit/write service pass."""
        self._require_open()
        p = self.protocol
        now_ms = monotonic_now_ms()
        bytes_read = self._read_serial()
        self._offer_received()

        if operating_mode is None:
            operating_mode = (
                p.OperatingMode.BULK_TRANSFER
                if self._outgoing_application or self._transport_delivery_pending
                else p.OperatingMode.NORMAL
            )
        process_status = self.transport.process(now_ms, operating_mode)
        if process_status is p.TransportStatus.DELIVERY_FAILED:
            raise ProtocolSessionError("Transport reported reliable delivery failure")

        events, messages, results = self._drain()
        submitted = self._submit_next_application_message()
        if submitted:
            process_status = self.transport.process(now_ms, operating_mode)
            if process_status is p.TransportStatus.DELIVERY_FAILED:
                raise ProtocolSessionError("Transport reported reliable delivery failure")

        bytes_written = self._service_output(now_ms)
        more_events, more_messages, more_results = self._drain()
        return ProtocolServiceReport(
            events=(*events, *more_events),
            application_messages=(*messages, *more_messages),
            stored_tick_results=(*results, *more_results),
            serial_bytes_read=bytes_read,
            serial_bytes_written=bytes_written,
            application_message_submitted=submitted,
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
        self._outgoing_application.clear()
        self._transport_delivery_pending = False
        with suppress(Exception):
            self.transport.notify_link_state(self.protocol.LinkState.DISCONNECTED, now_ms)
        self.transport.close()
        self._closed = True

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

    def _drain(self) -> tuple[tuple[object, ...], tuple[object, ...], tuple[TickResult, ...]]:
        p = self.protocol
        events: list[object] = []
        while (event := self.transport.read_event()) is not None:
            events.append(event)
            if event.type is p.EventType.DELIVERY_CONFIRMED:
                self._transport_delivery_pending = False
            elif event.type is p.EventType.DELIVERY_FAILED:
                self._transport_delivery_pending = False
                raise ProtocolSessionError("Transport delivery failed")
            elif event.type is p.EventType.PROTOCOL_ERROR:
                raise ProtocolSessionError("Transport reported a protocol error")

        messages: list[object] = []
        stored_results: list[TickResult] = []
        while (encoded := self.transport.read_application_data()) is not None:
            message = self.application.decode(encoded)
            messages.append(message)
            if type(message) is p.TestResult and self.result_adapter is not None:
                stored_results.append(self.result_adapter.ingest_application_message(message))
        return tuple(events), tuple(messages), tuple(stored_results)

    def _submit_next_application_message(self) -> bool:
        p = self.protocol
        if (
            not self._outgoing_application
            or self._transport_delivery_pending
            or self._application_response_pending()
        ):
            return False
        snapshot = self.transport.get_status()
        if snapshot.session_state is not p.SessionState.ESTABLISHED:
            return False
        status = self.transport.submit_application_data(self._outgoing_application[0].encoded)
        if status is p.TransportStatus.OK:
            self._outgoing_application.popleft()
            self._transport_delivery_pending = True
            return True
        if status in (p.TransportStatus.NOT_READY, p.TransportStatus.CAPACITY_EXHAUSTED):
            return False
        raise ProtocolSessionError(
            f"Could not submit Application message: Transport returned {status.name}"
        )

    def _application_response_pending(self) -> bool:
        # Scaffold for the forthcoming public Response wrapper: once it exists, this
        # becomes true after configuration or a tick is Transport-delivered and remains
        # true until the correlated Application ACCEPTED response is decoded. Each queue
        # item already retains its operation and tick for that correlation, so this will
        # make the upload strict Application-level stop-and-wait without changing the
        # state expander or serial pump.
        return False

    def _service_output(self, now_ms: int) -> int:
        p = self.protocol
        if self._pending_output is None:
            self._pending_output = self.transport.peek_output()
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
                commit_status = self.transport.commit_output(now_ms)
            finally:
                self._pending_output = None
                self._pending_output_offset = 0
            if commit_status not in (p.TransportStatus.OK, p.TransportStatus.NOT_READY):
                raise ProtocolSessionError(f"Transport output commit returned {commit_status.name}")
        return accepted

    def _require_open(self) -> None:
        if self._closed:
            raise ProtocolSessionError("The fixed-I/O protocol connection is closed")

    def __enter__(self) -> FixedIOProtocolConnection:
        self._require_open()
        return self

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        self.close()


__all__ = [
    "FixedIOProtocolConnection",
    "ProtocolServiceReport",
    "monotonic_now_ms",
]
