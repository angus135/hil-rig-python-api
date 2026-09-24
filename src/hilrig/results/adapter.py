"""Translate decoded protocol result messages into the captured-run IR."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from hilrig.exceptions import ProtocolDependencyError, ProtocolSessionError
from hilrig.models.identifiers import application_test_id_from_bytes
from hilrig.models.protocol import ProtocolFamily
from hilrig.results.builder import CapturedRunBuilder
from hilrig.results.models import (
    ApplicationErrorRecord,
    CommunicationPeripheral,
    CommunicationResult,
    PWMMeasurement,
    TickCondition,
    TickResult,
)


def _load_protocol_module() -> ModuleType:
    try:
        return import_module("hil_rig_protocol")
    except ImportError as error:
        raise ProtocolDependencyError(
            "Incoming protocol results require the hil-rig-protocol package"
        ) from error


class IncomingResultAdapter:
    """Convert fixed and variable Application Test Results and queue them for SQLite storage."""

    def __init__(
        self,
        builder: CapturedRunBuilder,
        *,
        protocol_module: ModuleType | Any | None = None,
        expected_family: ProtocolFamily | str = ProtocolFamily.VARIABLE,
    ) -> None:
        if not isinstance(builder, CapturedRunBuilder):
            raise TypeError("builder must be a CapturedRunBuilder")
        if isinstance(expected_family, str):
            try:
                expected_family = ProtocolFamily(expected_family.lower())
            except ValueError:
                raise ValueError(f"Unknown protocol family: {expected_family!r}")
        elif not isinstance(expected_family, ProtocolFamily):
            raise TypeError("expected_family must be a ProtocolFamily or str")
        self.expected_family = expected_family
        self.builder = builder
        self.protocol = protocol_module or _load_protocol_module()
        self._latched_digital_inputs = [False] * 10
        self._latched_analogue_inputs = [0] * 2
        self._latched_pwm_inputs = [PWMMeasurement(period_ns=0, duty_permyriad=0)] * 2

    def receive_usb_bytes(self, data: bytes) -> None:
        """Reject unframed bytes; the serial Transport connection owns this step."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        raise ProtocolSessionError(
            "Raw USB bytes must be supplied to FixedIOProtocolConnection.service()"
        )

    def ingest_application_message(self, application_message: object) -> TickResult:
        """Validate and persist one decoded Test Result or Variable Test Result message."""
        p = self.protocol
        is_fixed = hasattr(p, "TestResult") and type(application_message) is p.TestResult
        is_variable = (
            hasattr(p, "VariableTestResult")
            and type(application_message) is p.VariableTestResult
        )

        if not is_fixed and not is_variable:
            raise ProtocolSessionError(
                f"Expected TestResult or VariableTestResult, received {type(application_message).__name__}"
            )

        if self.expected_family is ProtocolFamily.VARIABLE and is_fixed:
            raise ProtocolSessionError(
                "Received legacy TestResult (Type 33) when variable message family was expected"
            )
        if self.expected_family is ProtocolFamily.LEGACY and is_variable:
            raise ProtocolSessionError(
                "Received VariableTestResult (Type 34) when legacy message family was expected"
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

        if is_fixed:
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

        tick = application_message.tick_number
        if condition is TickCondition.EXECUTION_PROBLEM:
            result = TickResult.execution_problem(
                tick=tick,
                problem_detail=application_message.problem_detail,
            )
            self.builder.add_tick_result(result)
            return result

        for record in application_message.records:
            periph_type = record.peripheral_type
            if periph_type == p.PeripheralType.DIGITAL_INPUT:
                pin_mask = int.from_bytes(record.data[:2], "little")
                for pin in range(10):
                    self._latched_digital_inputs[pin] = bool((pin_mask >> pin) & 1)
            elif periph_type == p.PeripheralType.ANALOG_INPUT:
                if 0 <= record.channel < len(self._latched_analogue_inputs):
                    self._latched_analogue_inputs[record.channel] = int.from_bytes(
                        record.data[:4], "little"
                    )
            elif periph_type == p.PeripheralType.PWM_INPUT:
                if 0 <= record.channel < len(self._latched_pwm_inputs):
                    period_ns = int.from_bytes(record.data[:4], "little")
                    duty_permyriad = int.from_bytes(record.data[4:6], "little")
                    self._latched_pwm_inputs[record.channel] = PWMMeasurement(
                        period_ns=period_ns,
                        duty_permyriad=duty_permyriad,
                    )
            elif periph_type == p.PeripheralType.UART:
                comm_res = CommunicationResult(
                    tick=tick,
                    peripheral=CommunicationPeripheral.UART,
                    channel=record.channel,
                    payload=bytes(record.data),
                )
                self.builder.add_communication_result(comm_res)
            elif periph_type == p.PeripheralType.SPI:
                comm_res = CommunicationResult(
                    tick=tick,
                    peripheral=CommunicationPeripheral.SPI,
                    channel=record.channel,
                    payload=bytes(record.data),
                )
                self.builder.add_communication_result(comm_res)
            elif periph_type == p.PeripheralType.CAN:
                comm_res = CommunicationResult(
                    tick=tick,
                    peripheral=CommunicationPeripheral.CAN,
                    channel=record.channel,
                    payload=bytes(record.data),
                )
                self.builder.add_communication_result(comm_res)

        result = TickResult(
            tick=tick,
            digital_inputs=tuple(self._latched_digital_inputs),
            analogue_inputs_uv=tuple(self._latched_analogue_inputs),
            pwm_inputs=tuple(self._latched_pwm_inputs),
            condition=condition,
            problem_detail=application_message.problem_detail,
        )
        self.builder.add_tick_result(result)
        return result

    def ingest_application_error(self, application_message: object) -> ApplicationErrorRecord:
        """Validate and persist one decoded Application Error message."""
        p = self.protocol
        if type(application_message) is not p.ApplicationErrorMessage:
            raise ProtocolSessionError(
                f"Expected ApplicationErrorMessage, received {type(application_message).__name__}"
            )

        if application_message.test_id is not None:
            received_test_id = application_test_id_from_bytes(application_message.test_id.bytes)
            if received_test_id != self.builder.application_test_id:
                raise ProtocolSessionError(
                    "Received Application Error has an Application Test ID that does not "
                    "match the active captured run"
                )

        record = ApplicationErrorRecord(
            category=application_message.category.name.lower(),
            detail=str(application_message.detail),
            recoverable=application_message.recoverable,
            tick=application_message.tick_number,
            diagnostic_data=application_message.diagnostic_data,
        )
        self.builder.add_application_error(record)
        return record


__all__ = ["IncomingResultAdapter"]
