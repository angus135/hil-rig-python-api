"""Translate the protocol-neutral IR into fixed-I/O Application messages."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from importlib import import_module
from itertools import groupby
from types import ModuleType
from typing import Any

from hilrig.exceptions import ProtocolDependencyError, ProtocolIntegrationError
from hilrig.models.execution import CompiledConfiguration, CompiledInstruction, CompiledTestIR
from hilrig.models.identifiers import (
    UploadAttempt,
    application_test_id_from_bytes,
    application_test_id_to_bytes,
)

_UINT32_MAX = (1 << 32) - 1
_FIXED_OUTPUT_PERIPHERALS = frozenset({"digital_output", "analogue_output", "pwm_output"})
_CONTROL_FLOW_API = (
    "PROTOCOL_VERSION",
    "SystemInfoRequest",
    "SystemInfoResponse",
    "ApplicationResponse",
    "ApplicationErrorMessage",
    "ExecutionControl",
    "GlobalControl",
    "ControlCommand",
    "GlobalControlCommand",
    "ResponseScope",
    "ResponseOutcome",
    "ResponseReason",
    "check_protocol_version",
)


def _load_protocol_module() -> ModuleType:
    try:
        return import_module("hil_rig_protocol")
    except ImportError as error:
        raise ProtocolDependencyError(
            "Fixed-I/O protocol support requires the hil-rig-protocol package"
        ) from error


@dataclass(frozen=True, slots=True)
class FixedIOUploadMessages:
    """Application values for one configuration and its sparse fixed-I/O states."""

    upload_attempt: UploadAttempt
    start_mode: str
    configuration: object
    instructions: tuple[object, ...]

    @property
    def messages(self) -> tuple[object, ...]:
        """Return the configuration followed by every sparse instruction state."""
        return (self.configuration, *self.instructions)


@dataclass(slots=True)
class _PWMState:
    period_nanoseconds: int = 0
    duty_cycle_permyriad: int = 0
    enabled: bool = False


class FixedIOProtocolAdapter:
    """Build and encode protocol messages for Digital, Analogue, and PWM I/O.

    Communication configurations and instructions remain in the compiled IR but are
    intentionally left disabled/omitted at this boundary until their public protocol
    messages are available.
    """

    def __init__(
        self,
        *,
        protocol_module: ModuleType | Any | None = None,
        application_config: object | None = None,
    ) -> None:
        self.protocol = protocol_module or _load_protocol_module()
        missing = tuple(name for name in _CONTROL_FLOW_API if not hasattr(self.protocol, name))
        if missing:
            raise ProtocolDependencyError(
                "Fixed-I/O protocol control flow requires hil-rig-protocol 0.2.0 or newer; "
                f"missing public API: {', '.join(missing)}"
            )
        if application_config is None:
            application_config = self.protocol.ApplicationConfig()
        self.codec = self.protocol.ApplicationCodec(application_config)

    def build_upload(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
    ) -> FixedIOUploadMessages:
        """Translate a compiled definition into one fixed-I/O upload attempt."""
        if not isinstance(compiled_test, CompiledTestIR):
            raise TypeError("compiled_test must be a CompiledTestIR")
        if upload_attempt is None:
            upload_attempt = compiled_test.new_upload_attempt()
        elif not isinstance(upload_attempt, UploadAttempt):
            raise TypeError("upload_attempt must be an UploadAttempt or None")
        if upload_attempt.definition_test_id != compiled_test.test_id:
            raise ValueError("upload_attempt belongs to a different compiled test")

        test_id = self.protocol.TestId(
            application_test_id_to_bytes(upload_attempt.application_test_id)
        )
        configurations = _configuration_index(compiled_test.configurations)
        configuration = self._build_configuration(compiled_test, test_id, configurations)
        instructions = self._build_sparse_instructions(
            compiled_test,
            test_id,
            configurations,
        )
        return FixedIOUploadMessages(
            upload_attempt=upload_attempt,
            start_mode=compiled_test.start_mode,
            configuration=configuration,
            instructions=instructions,
        )

    def encode_upload(self, upload: FixedIOUploadMessages) -> tuple[bytes, ...]:
        """Encode every complete Application message using the native codec."""
        if not isinstance(upload, FixedIOUploadMessages):
            raise TypeError("upload must be FixedIOUploadMessages")
        return tuple(self.codec.encode(message) for message in upload.messages)

    def decode(self, data: bytes) -> object:
        """Decode one complete Application message received from Transport."""
        return self.codec.decode(data)

    def encode(self, message: object) -> bytes:
        """Encode one public Application value with the native protocol codec."""
        return self.codec.encode(message)

    def build_system_info_request(self, *, request_firmware_git_hash: bool = True) -> object:
        """Build the discovery request required for each new Transport session."""
        if not isinstance(request_firmware_git_hash, bool):
            raise TypeError("request_firmware_git_hash must be a bool")
        return self.protocol.SystemInfoRequest(request_firmware_git_hash=request_firmware_git_hash)

    def build_start(self, upload_attempt: UploadAttempt) -> object:
        """Build a START request for an accepted upload attempt."""
        return self._build_execution_control(upload_attempt, self.protocol.ControlCommand.START)

    def build_abort(self, upload_attempt: UploadAttempt) -> object:
        """Build an ABORT request for an active upload attempt."""
        return self._build_execution_control(upload_attempt, self.protocol.ControlCommand.ABORT)

    def build_reset_application(self) -> object:
        """Build a test-independent Application reset request."""
        return self.protocol.GlobalControl(
            command=self.protocol.GlobalControlCommand.RESET_APPLICATION,
            flags=0,
        )

    def _build_execution_control(self, upload_attempt: UploadAttempt, command: object) -> object:
        if not isinstance(upload_attempt, UploadAttempt):
            raise TypeError("upload_attempt must be an UploadAttempt")
        return self.protocol.ExecutionControl(
            test_id=self.protocol.TestId(
                application_test_id_to_bytes(upload_attempt.application_test_id)
            ),
            command=command,
            flags=0,
        )

    def _build_configuration(
        self,
        compiled_test: CompiledTestIR,
        test_id: object,
        configurations: dict[tuple[str, int], CompiledConfiguration],
    ) -> object:
        p = self.protocol
        digital_in = [p.DigitalInputConfig() for _ in range(10)]
        digital_out = [p.DigitalOutputConfig() for _ in range(10)]
        analogue_in = [p.AnalogInputConfig() for _ in range(2)]
        analogue_out = [p.AnalogOutputConfig() for _ in range(6)]
        pwm_in = [p.PWMInputConfig() for _ in range(2)]
        pwm_out = [p.PWMOutputConfig() for _ in range(2)]

        for (peripheral, channel), item in configurations.items():
            parameters = item.parameters
            if peripheral == "digital_input":
                digital_in[channel] = p.DigitalInputConfig(
                    enabled=True,
                    voltage_level=_peripheral_voltage(p, parameters["voltage"]),
                )
            elif peripheral == "digital_output":
                digital_out[channel] = p.DigitalOutputConfig(
                    enabled=True,
                    voltage_level=_peripheral_voltage(p, parameters["voltage"]),
                    initial_high=_digital_state(parameters["initial_state"]),
                )
            elif peripheral == "analogue_input":
                analogue_in[channel] = p.AnalogInputConfig(enabled=True)
            elif peripheral == "analogue_output":
                analogue_out[channel] = p.AnalogOutputConfig(enabled=True)
            elif peripheral == "pwm_input":
                pwm_in[channel] = p.PWMInputConfig(
                    enabled=True,
                    voltage_level=_peripheral_voltage(p, parameters["voltage"]),
                )
            elif peripheral == "pwm_output":
                enabled = _require_bool(parameters["initially_enabled"], "initially_enabled")
                period = _frequency_to_period_ns(
                    parameters["initial_frequency_hz"],
                    "initial_frequency_hz",
                )
                duty = _duty_to_permyriad(
                    parameters["initial_duty_cycle"],
                    "initial_duty_cycle",
                )
                pwm_out[channel] = p.PWMOutputConfig(
                    enabled=True,
                    voltage_level=_peripheral_voltage(p, parameters["voltage"]),
                    initial_period_nanoseconds=period if enabled else 0,
                    initial_duty_cycle_permyriad=duty if enabled else 0,
                )

        tick_duration_us, remainder = divmod(compiled_test.tick_period_ns, 1_000)
        if remainder:
            raise ProtocolIntegrationError(
                "Compiled tick period cannot be represented as whole protocol microseconds"
            )
        return p.TestConfiguration(
            test_id=test_id,
            tick_duration_us=p.TickDuration(microseconds=tick_duration_us),
            expected_tick_count=compiled_test.expected_tick_count,
            flags=0,
            digital_in=tuple(digital_in),
            digital_out=tuple(digital_out),
            analog_in=tuple(analogue_in),
            analog_out=tuple(analogue_out),
            pwm_in=tuple(pwm_in),
            pwm_out=tuple(pwm_out),
        )

    def _build_sparse_instructions(
        self,
        compiled_test: CompiledTestIR,
        test_id: object,
        configurations: dict[tuple[str, int], CompiledConfiguration],
    ) -> tuple[object, ...]:
        p = self.protocol
        digital_outputs = [False] * 10
        analogue_outputs = [0] * 6
        pwm_outputs = [_PWMState() for _ in range(2)]
        has_analogue_output = False

        for (peripheral, channel), item in configurations.items():
            parameters = item.parameters
            if peripheral == "digital_output":
                digital_outputs[channel] = _digital_state(parameters["initial_state"])
            elif peripheral == "analogue_output":
                analogue_outputs[channel] = _volts_to_microvolts(
                    parameters["initial_voltage"],
                    "initial_voltage",
                )
                has_analogue_output = True
            elif peripheral == "pwm_output":
                pwm_outputs[channel] = _PWMState(
                    period_nanoseconds=_frequency_to_period_ns(
                        parameters["initial_frequency_hz"],
                        "initial_frequency_hz",
                    ),
                    duty_cycle_permyriad=_duty_to_permyriad(
                        parameters["initial_duty_cycle"],
                        "initial_duty_cycle",
                    ),
                    enabled=_require_bool(
                        parameters["initially_enabled"],
                        "initially_enabled",
                    ),
                )

        fixed_instructions = tuple(
            instruction
            for instruction in compiled_test.instructions
            if instruction.peripheral in _FIXED_OUTPUT_PERIPHERALS
        )
        grouped = {
            tick: tuple(group)
            for tick, group in groupby(fixed_instructions, key=lambda item: item.tick)
        }
        ticks = set(grouped)
        if has_analogue_output:
            # Analogue initial voltage is not present in Test Configuration. Tick zero
            # therefore carries the initial expanded state. A real tick-zero stimulus is
            # applied to that state before the single tick-zero message is emitted.
            ticks.add(0)

        messages: list[object] = []
        for tick in sorted(ticks):
            for instruction in grouped.get(tick, ()):
                _apply_instruction(
                    instruction,
                    digital_outputs=digital_outputs,
                    analogue_outputs=analogue_outputs,
                    pwm_outputs=pwm_outputs,
                )
            messages.append(
                p.TestInstruction(
                    test_id=test_id,
                    tick_number=tick,
                    digital_outputs=tuple(
                        p.DigitalOutputValue(high=value) for value in digital_outputs
                    ),
                    analog_outputs=tuple(
                        p.AnalogOutputValue(microvolts=value) for value in analogue_outputs
                    ),
                    pwm_outputs=tuple(
                        p.PWMOutputValue(
                            period_nanoseconds=(state.period_nanoseconds if state.enabled else 0),
                            duty_cycle_permyriad=(
                                state.duty_cycle_permyriad if state.enabled else 0
                            ),
                        )
                        for state in pwm_outputs
                    ),
                )
            )
        return tuple(messages)


def _configuration_index(
    configurations: tuple[CompiledConfiguration, ...],
) -> dict[tuple[str, int], CompiledConfiguration]:
    return {(item.peripheral, item.channel): item for item in configurations}


def _peripheral_voltage(protocol: Any, value: object) -> object:
    names = {
        "V3_3": "V_3V3",
        "V5": "V_5V",
        "V12": "V_12V",
        "V24": "V_24V",
    }
    if not isinstance(value, str) or value not in names:
        raise ProtocolIntegrationError(f"Unsupported fixed-I/O voltage value: {value!r}")
    return getattr(protocol.PeripheralVoltage, names[value])


def _digital_state(value: object) -> bool:
    if value == "LOW":
        return False
    if value == "HIGH":
        return True
    raise ProtocolIntegrationError(f"Unsupported digital state: {value!r}")


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolIntegrationError(f"{name} must be a bool in the compiled IR")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolIntegrationError(f"{name} must be numeric in the compiled IR")
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise ProtocolIntegrationError(f"{name} must be finite in the compiled IR")
    return converted


def _volts_to_microvolts(value: object, name: str) -> int:
    microvolts = _decimal(value, name) * 1_000_000
    if microvolts != microvolts.to_integral_value():
        raise ProtocolIntegrationError(f"{name} does not align with one microvolt")
    converted = int(microvolts)
    if not 0 <= converted <= _UINT32_MAX:
        raise ProtocolIntegrationError(f"{name} is outside the protocol microvolt range")
    return converted


def _duty_to_permyriad(value: object, name: str) -> int:
    permyriad = _decimal(value, name) * 10_000
    if permyriad != permyriad.to_integral_value():
        raise ProtocolIntegrationError(f"{name} does not align with one permyriad")
    converted = int(permyriad)
    if not 0 <= converted <= 10_000:
        raise ProtocolIntegrationError(f"{name} is outside the protocol duty-cycle range")
    return converted


def _frequency_to_period_ns(value: object, name: str) -> int:
    frequency = _decimal(value, name)
    if frequency <= 0:
        raise ProtocolIntegrationError(f"{name} must be positive in the compiled IR")
    period_ns = Decimal(1_000_000_000) / frequency
    if period_ns != period_ns.to_integral_value():
        raise ProtocolIntegrationError(f"{name} does not produce a whole-nanosecond period")
    converted = int(period_ns)
    if not 0 < converted <= _UINT32_MAX:
        raise ProtocolIntegrationError(f"{name} produces a period outside uint32")
    return converted


def _apply_instruction(
    instruction: CompiledInstruction,
    *,
    digital_outputs: list[bool],
    analogue_outputs: list[int],
    pwm_outputs: list[_PWMState],
) -> None:
    arguments = instruction.arguments
    if instruction.peripheral == "digital_output":
        action = arguments.get("action")
        if action == "HIGH":
            digital_outputs[instruction.channel] = True
        elif action == "LOW":
            digital_outputs[instruction.channel] = False
        elif action == "TOGGLE":
            digital_outputs[instruction.channel] = not digital_outputs[instruction.channel]
        else:
            raise ProtocolIntegrationError(f"Unsupported digital output action: {action!r}")
        return

    if instruction.peripheral == "analogue_output":
        if instruction.operation != "set_voltage":
            raise ProtocolIntegrationError(
                f"Unsupported analogue output operation: {instruction.operation}"
            )
        analogue_outputs[instruction.channel] = _volts_to_microvolts(
            arguments.get("voltage"),
            "voltage",
        )
        return

    if instruction.peripheral == "pwm_output":
        state = pwm_outputs[instruction.channel]
        if instruction.operation == "set_enabled":
            state.enabled = _require_bool(arguments.get("enabled"), "enabled")
        elif instruction.operation == "set":
            state.period_nanoseconds = _frequency_to_period_ns(
                arguments.get("frequency_hz"),
                "frequency_hz",
            )
            state.duty_cycle_permyriad = _duty_to_permyriad(
                arguments.get("duty_cycle"),
                "duty_cycle",
            )
        elif instruction.operation == "set_frequency":
            state.period_nanoseconds = _frequency_to_period_ns(
                arguments.get("frequency_hz"),
                "frequency_hz",
            )
        elif instruction.operation == "set_duty_cycle":
            state.duty_cycle_permyriad = _duty_to_permyriad(
                arguments.get("duty_cycle"),
                "duty_cycle",
            )
        else:
            raise ProtocolIntegrationError(
                f"Unsupported PWM output operation: {instruction.operation}"
            )
        return

    raise ProtocolIntegrationError(f"Unsupported fixed-output peripheral: {instruction.peripheral}")


__all__ = [
    "FixedIOProtocolAdapter",
    "FixedIOUploadMessages",
    "application_test_id_from_bytes",
    "application_test_id_to_bytes",
]
