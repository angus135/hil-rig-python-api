"""Standalone JSON Application-message construction for the manual terminal mode."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hilrig.protocol.application import FixedIOProtocolAdapter, ResponseCorrelation

_UINT32_MAX = (1 << 32) - 1


class ManualMessageDefinitionError(ValueError):
    """A standalone manual-message document is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ManualApplicationMessage:
    """One independently constructed Application message and expected response."""

    label: str
    message: object
    encoded_message: bytes
    response: ResponseCorrelation


def load_manual_message(
    path: str | Path,
    adapter: FixedIOProtocolAdapter,
) -> ManualApplicationMessage:
    """Load and encode one standalone JSON Application-message definition."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ManualMessageDefinitionError(f"Manual message file does not exist: {resolved}")
    if resolved.suffix.lower() != ".json":
        raise ManualMessageDefinitionError("Manual message path must end in .json")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManualMessageDefinitionError(
            f"Could not read manual message {resolved}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ManualMessageDefinitionError("Manual message JSON must contain one object")

    message_type = document.get("type")
    builders = {
        "test_configuration": _build_test_configuration,
        "test_instruction": _build_test_instruction,
        "execution_control": _build_execution_control,
        "global_control": _build_global_control,
    }
    if message_type not in builders:
        supported = ", ".join(sorted(builders))
        raise ManualMessageDefinitionError(
            f"Unsupported manual message type {message_type!r}; expected one of: {supported}"
        )
    label, message, response = builders[message_type](document, adapter.protocol)
    try:
        encoded = adapter.encode(message)
    except Exception as error:
        raise ManualMessageDefinitionError(
            f"The protocol codec rejected the manual {message_type} message: {error}"
        ) from error
    return ManualApplicationMessage(
        label=label,
        message=message,
        encoded_message=encoded,
        response=response,
    )


def _build_test_instruction(
    document: dict[str, object],
    protocol: Any,
) -> tuple[str, object, ResponseCorrelation]:
    test_id, application_test_id = _test_id(document, protocol)
    tick = _integer(document.get("tick"), "tick", minimum=0, maximum=_UINT32_MAX)

    digital = [protocol.DigitalOutputValue() for _ in range(10)]
    for channel, item in _channel_items(document, "digital_outputs", 10):
        high = item.get("high")
        if not isinstance(high, bool):
            raise ManualMessageDefinitionError("digital_outputs.high must be a bool")
        digital[channel] = protocol.DigitalOutputValue(high=high)

    analog = [protocol.AnalogOutputValue() for _ in range(6)]
    for channel, item in _channel_items(document, "analog_outputs", 6):
        microvolts = _integer(
            item.get("microvolts"),
            "analog_outputs.microvolts",
            minimum=0,
            maximum=_UINT32_MAX,
        )
        analog[channel] = protocol.AnalogOutputValue(microvolts=microvolts)

    pwm = [protocol.PWMOutputValue() for _ in range(2)]
    for channel, item in _channel_items(document, "pwm_outputs", 2):
        period = _integer(
            item.get("period_nanoseconds"),
            "pwm_outputs.period_nanoseconds",
            minimum=0,
            maximum=_UINT32_MAX,
        )
        duty = _integer(
            item.get("duty_cycle_permyriad"),
            "pwm_outputs.duty_cycle_permyriad",
            minimum=0,
            maximum=10_000,
        )
        pwm[channel] = protocol.PWMOutputValue(
            period_nanoseconds=period,
            duty_cycle_permyriad=duty,
        )

    message = protocol.TestInstruction(
        test_id=test_id,
        tick_number=tick,
        digital_outputs=tuple(digital),
        analog_outputs=tuple(analog),
        pwm_outputs=tuple(pwm),
    )
    return (
        f"TestInstruction tick {tick}",
        message,
        ResponseCorrelation(
            scope=protocol.ResponseScope.TICK,
            successful_outcome=protocol.ResponseOutcome.ACCEPTED,
            application_test_id=application_test_id,
            tick=tick,
        ),
    )


def _build_test_configuration(
    document: dict[str, object],
    protocol: Any,
) -> tuple[str, object, ResponseCorrelation]:
    test_id, application_test_id = _test_id(document, protocol)
    tick_duration = _integer(
        document.get("tick_duration_us"),
        "tick_duration_us",
        minimum=1,
        maximum=_UINT32_MAX,
    )
    expected_ticks = _integer(
        document.get("expected_tick_count"),
        "expected_tick_count",
        minimum=1,
        maximum=_UINT32_MAX,
    )
    flags = _integer(document.get("flags", 0), "flags", minimum=0, maximum=255)

    digital_in = [protocol.DigitalInputConfig() for _ in range(10)]
    for channel, item in _channel_items(document, "digital_inputs", 10):
        digital_in[channel] = protocol.DigitalInputConfig(
            enabled=_boolean(item.get("enabled", True), "digital_inputs.enabled"),
            voltage_level=_voltage(item.get("voltage"), protocol),
        )

    digital_out = [protocol.DigitalOutputConfig() for _ in range(10)]
    for channel, item in _channel_items(document, "digital_outputs", 10):
        digital_out[channel] = protocol.DigitalOutputConfig(
            enabled=_boolean(item.get("enabled", True), "digital_outputs.enabled"),
            voltage_level=_voltage(item.get("voltage"), protocol),
            initial_high=_boolean(
                item.get("initial_high", False),
                "digital_outputs.initial_high",
            ),
        )

    analog_in = [protocol.AnalogInputConfig() for _ in range(2)]
    for channel, item in _channel_items(document, "analog_inputs", 2):
        analog_in[channel] = protocol.AnalogInputConfig(
            enabled=_boolean(item.get("enabled", True), "analog_inputs.enabled")
        )

    analog_out = [protocol.AnalogOutputConfig() for _ in range(6)]
    for channel, item in _channel_items(document, "analog_outputs", 6):
        analog_out[channel] = protocol.AnalogOutputConfig(
            enabled=_boolean(item.get("enabled", True), "analog_outputs.enabled")
        )

    pwm_in = [protocol.PWMInputConfig() for _ in range(2)]
    for channel, item in _channel_items(document, "pwm_inputs", 2):
        pwm_in[channel] = protocol.PWMInputConfig(
            enabled=_boolean(item.get("enabled", True), "pwm_inputs.enabled"),
            voltage_level=_voltage(item.get("voltage"), protocol),
        )

    pwm_out = [protocol.PWMOutputConfig() for _ in range(2)]
    for channel, item in _channel_items(document, "pwm_outputs", 2):
        pwm_out[channel] = protocol.PWMOutputConfig(
            enabled=_boolean(item.get("enabled", True), "pwm_outputs.enabled"),
            voltage_level=_voltage(item.get("voltage"), protocol),
            initial_period_nanoseconds=_integer(
                item.get("initial_period_nanoseconds", 0),
                "pwm_outputs.initial_period_nanoseconds",
                minimum=0,
                maximum=_UINT32_MAX,
            ),
            initial_duty_cycle_permyriad=_integer(
                item.get("initial_duty_cycle_permyriad", 0),
                "pwm_outputs.initial_duty_cycle_permyriad",
                minimum=0,
                maximum=10_000,
            ),
        )

    message = protocol.TestConfiguration(
        test_id=test_id,
        tick_duration_us=protocol.TickDuration(microseconds=tick_duration),
        expected_tick_count=expected_ticks,
        flags=flags,
        digital_in=tuple(digital_in),
        digital_out=tuple(digital_out),
        analog_in=tuple(analog_in),
        analog_out=tuple(analog_out),
        pwm_in=tuple(pwm_in),
        pwm_out=tuple(pwm_out),
    )
    return (
        "TestConfiguration",
        message,
        ResponseCorrelation(
            scope=protocol.ResponseScope.TEST_CONFIGURATION,
            successful_outcome=protocol.ResponseOutcome.ACCEPTED,
            application_test_id=application_test_id,
        ),
    )


def _build_execution_control(
    document: dict[str, object],
    protocol: Any,
) -> tuple[str, object, ResponseCorrelation]:
    test_id, application_test_id = _test_id(document, protocol)
    command_name = _string(document.get("command"), "command").upper()
    try:
        command = protocol.ControlCommand[command_name]
    except KeyError as error:
        raise ManualMessageDefinitionError(
            f"Unsupported execution control command: {command_name}"
        ) from error
    flags = _integer(document.get("flags", 0), "flags", minimum=0, maximum=255)
    message = protocol.ExecutionControl(test_id=test_id, command=command, flags=flags)
    return (
        f"ExecutionControl {command.name}",
        message,
        ResponseCorrelation(
            scope=protocol.ResponseScope.EXECUTION_CONTROL,
            successful_outcome=protocol.ResponseOutcome.COMPLETED,
            application_test_id=application_test_id,
            control_command=command,
        ),
    )


def _build_global_control(
    document: dict[str, object],
    protocol: Any,
) -> tuple[str, object, ResponseCorrelation]:
    command_name = _string(document.get("command"), "command").upper()
    try:
        command = protocol.GlobalControlCommand[command_name]
    except KeyError as error:
        raise ManualMessageDefinitionError(
            f"Unsupported global control command: {command_name}"
        ) from error
    flags = _integer(document.get("flags", 0), "flags", minimum=0, maximum=255)
    message = protocol.GlobalControl(command=command, flags=flags)
    return (
        f"GlobalControl {command.name}",
        message,
        ResponseCorrelation(
            scope=protocol.ResponseScope.GLOBAL_CONTROL,
            successful_outcome=protocol.ResponseOutcome.COMPLETED,
            application_test_id=None,
            global_control_command=command,
        ),
    )


def _test_id(document: dict[str, object], protocol: Any) -> tuple[object, int]:
    raw = _string(document.get("test_id"), "test_id").replace("-", "")
    if len(raw) != 32:
        raise ManualMessageDefinitionError("test_id must contain exactly 32 hexadecimal digits")
    try:
        data = bytes.fromhex(raw)
    except ValueError as error:
        raise ManualMessageDefinitionError(
            "test_id must contain only hexadecimal digits"
        ) from error
    return protocol.TestId(data), int.from_bytes(data, "big")


def _channel_items(
    document: dict[str, object],
    name: str,
    channel_count: int,
) -> tuple[tuple[int, dict[str, object]], ...]:
    raw = document.get(name, [])
    if not isinstance(raw, list):
        raise ManualMessageDefinitionError(f"{name} must be a list")
    items: list[tuple[int, dict[str, object]]] = []
    seen: set[int] = set()
    for value in raw:
        if not isinstance(value, dict):
            raise ManualMessageDefinitionError(f"Every {name} entry must be an object")
        channel = _integer(
            value.get("channel"),
            f"{name}.channel",
            minimum=0,
            maximum=channel_count - 1,
        )
        if channel in seen:
            raise ManualMessageDefinitionError(f"{name} repeats channel {channel}")
        seen.add(channel)
        items.append((channel, value))
    return tuple(items)


def _voltage(value: object, protocol: Any) -> object:
    name = _string(value, "voltage").upper().replace(".", "_")
    names = {
        "3V3": "V_3V3",
        "V3_3": "V_3V3",
        "V_3V3": "V_3V3",
        "5V": "V_5V",
        "V5": "V_5V",
        "V_5V": "V_5V",
        "12V": "V_12V",
        "V12": "V_12V",
        "V_12V": "V_12V",
        "24V": "V_24V",
        "V24": "V_24V",
        "V_24V": "V_24V",
    }
    try:
        return getattr(protocol.PeripheralVoltage, names[name])
    except (KeyError, AttributeError) as error:
        raise ManualMessageDefinitionError(f"Unsupported voltage: {value!r}") from error


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManualMessageDefinitionError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ManualMessageDefinitionError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ManualMessageDefinitionError(f"{name} must be a bool")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManualMessageDefinitionError(f"{name} must be a non-empty string")
    return value.strip()


__all__ = [
    "ManualApplicationMessage",
    "ManualMessageDefinitionError",
    "load_manual_message",
]
