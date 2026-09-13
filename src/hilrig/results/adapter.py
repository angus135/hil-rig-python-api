"""Translate decoded protocol result messages into the captured-run IR."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from hilrig.exceptions import ProtocolDependencyError, ProtocolSessionError
from hilrig.models.identifiers import application_test_id_from_bytes
from hilrig.results.builder import CapturedRunBuilder
from hilrig.results.models import PWMMeasurement, TickCondition, TickResult


def _load_protocol_module() -> ModuleType:
    try:
        return import_module("hil_rig_protocol")
    except ImportError as error:
        raise ProtocolDependencyError(
            "Incoming protocol results require the hil-rig-protocol package"
        ) from error


class IncomingResultAdapter:
    """Convert fixed Application Test Results and queue them for SQLite storage."""

    def __init__(
        self,
        builder: CapturedRunBuilder,
        *,
        protocol_module: ModuleType | Any | None = None,
    ) -> None:
        if not isinstance(builder, CapturedRunBuilder):
            raise TypeError("builder must be a CapturedRunBuilder")
        self.builder = builder
        self.protocol = protocol_module or _load_protocol_module()

    def receive_usb_bytes(self, data: bytes) -> None:
        """Reject unframed bytes; the serial Transport connection owns this step."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        raise ProtocolSessionError(
            "Raw USB bytes must be supplied to FixedIOProtocolConnection.service()"
        )

    def ingest_application_message(self, application_message: object) -> TickResult:
        """Validate and persist one decoded fixed Test Result message."""
        p = self.protocol
        if type(application_message) is not p.TestResult:
            raise ProtocolSessionError(
                f"Expected TestResult, received {type(application_message).__name__}"
            )

        received_test_id = application_test_id_from_bytes(application_message.test_id.bytes)
        if received_test_id != self.builder.application_test_id:
            raise ProtocolSessionError(
                "Received TestResult has an Application Test ID that does not match "
                "the active captured run"
            )

        condition = {
            p.ResultCondition.OK: TickCondition.OK,
            p.ResultCondition.PARTIAL: TickCondition.PARTIAL,
            p.ResultCondition.EXECUTION_PROBLEM: TickCondition.EXECUTION_PROBLEM,
        }.get(application_message.condition)
        if condition is None:
            raise ProtocolSessionError(
                f"Unsupported TestResult condition: {application_message.condition!r}"
            )

        if condition is TickCondition.EXECUTION_PROBLEM:
            result = TickResult.execution_problem(
                tick=application_message.tick_number,
                problem_detail=application_message.problem_detail,
            )
        else:
            result = TickResult(
                tick=application_message.tick_number,
                digital_inputs=tuple(value.high for value in application_message.digital_inputs),
                analogue_inputs_uv=tuple(
                    value.microvolts for value in application_message.analog_inputs
                ),
                pwm_inputs=tuple(
                    PWMMeasurement(
                        period_ns=value.period_nanoseconds,
                        duty_permyriad=value.duty_cycle_permyriad,
                    )
                    for value in application_message.pwm_inputs
                ),
                condition=condition,
                problem_detail=application_message.problem_detail,
            )
        self.builder.add_tick_result(result)
        return result


__all__ = ["IncomingResultAdapter"]
