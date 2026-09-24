"""Translate the protocol-neutral IR into fixed-I/O Application messages."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
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
    "FinalizeTestUpload",
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


class ProtocolFamily(str, Enum):
    """Supported HIL-RIG message framing and data structure families."""

    VARIABLE = "variable"
    LEGACY = "legacy"


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


class UploadOperationKind(str, Enum):
    """Semantic host operations that may be released through an operator gate."""

    CONFIGURATION = "configuration"
    TICK = "tick"
    FINALIZE = "finalize"
    START = "start"


@dataclass(frozen=True, slots=True)
class ResponseCorrelation:
    """Application Response fields expected for one semantic operation."""

    scope: object
    successful_outcome: object
    application_test_id: int | None
    tick: int | None = None
    control_command: object | None = None
    global_control_command: object | None = None


@dataclass(frozen=True, slots=True)
class UploadOperation:
    """One semantic upload action and every wire message needed to perform it."""

    kind: UploadOperationKind
    encoded_messages: tuple[bytes, ...]
    response: ResponseCorrelation | None = None
    tick: int | None = None

    @property
    def label(self) -> str:
        """Return a concise operator-facing description."""
        if self.kind is UploadOperationKind.TICK:
            return f"tick {self.tick}"
        if self.kind is UploadOperationKind.START:
            return "START"
        if self.kind is UploadOperationKind.FINALIZE:
            return "upload finalization"
        return "configuration"


@dataclass(frozen=True, slots=True)
class UploadPlan:
    """Ordered semantic operations for one fixed-I/O upload and execution start."""

    upload: FixedIOUploadMessages
    operations: tuple[UploadOperation, ...]

    @property
    def transfer_operations(self) -> tuple[UploadOperation, ...]:
        """Return configuration, tick, and finalization operations, excluding START."""
        return tuple(
            operation
            for operation in self.operations
            if operation.kind is not UploadOperationKind.START
        )

    @property
    def start_operation(self) -> UploadOperation | None:
        """Return the planned START operation, when this start mode uses one."""
        return next(
            (
                operation
                for operation in self.operations
                if operation.kind is UploadOperationKind.START
            ),
            None,
        )


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
                "Fixed-I/O protocol control flow requires hil-rig-protocol 0.3.0 or newer; "
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
        comm_peripherals = {"uart", "spi", "i2c", "can"}
        for periph, _ in configurations.keys():
            if periph in comm_peripherals:
                raise ProtocolIntegrationError(
                    f"Test contains {periph.upper()} configuration which is not supported in the legacy fixed-I/O message family"
                )
        for inst in compiled_test.instructions:
            if inst.peripheral in comm_peripherals:
                raise ProtocolIntegrationError(
                    f"Test contains {inst.peripheral.upper()} instruction which is not supported in the legacy fixed-I/O message family"
                )
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

    def build_upload_plan(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
    ) -> UploadPlan:
        """Build response-correlated semantic operations for one test attempt.

        Each operation currently contains one encoded message. Keeping that grouping
        here means a future tick can contain several wire messages without changing
        connection gating or terminal stepping.
        """
        upload = self.build_upload(compiled_test, upload_attempt=upload_attempt)
        encoded = self.encode_upload(upload)
        p = self.protocol
        application_test_id = upload.upload_attempt.application_test_id
        operations = [
            UploadOperation(
                kind=UploadOperationKind.CONFIGURATION,
                encoded_messages=(encoded[0],),
                response=ResponseCorrelation(
                    scope=p.ResponseScope.TEST_CONFIGURATION,
                    successful_outcome=p.ResponseOutcome.ACCEPTED,
                    application_test_id=application_test_id,
                ),
            )
        ]
        operations.extend(
            UploadOperation(
                kind=UploadOperationKind.TICK,
                encoded_messages=(wire,),
                response=None,
                tick=message.tick_number,
            )
            for message, wire in zip(upload.instructions, encoded[1:], strict=True)
        )
        finalize = self.build_finalize_test_upload(application_test_id)
        operations.append(
            UploadOperation(
                kind=UploadOperationKind.FINALIZE,
                encoded_messages=(self.encode(finalize),),
                response=ResponseCorrelation(
                    scope=p.ResponseScope.COMPLETE_TEST,
                    successful_outcome=p.ResponseOutcome.ACCEPTED,
                    application_test_id=application_test_id,
                ),
            )
        )
        if upload.start_mode != "EXTERNAL_TRIGGER":
            start = self.build_start(upload.upload_attempt)
            operations.append(
                UploadOperation(
                    kind=UploadOperationKind.START,
                    encoded_messages=(self.encode(start),),
                    response=ResponseCorrelation(
                        scope=p.ResponseScope.EXECUTION_CONTROL,
                        successful_outcome=p.ResponseOutcome.COMPLETED,
                        application_test_id=application_test_id,
                        control_command=p.ControlCommand.START,
                    ),
                )
            )
        return UploadPlan(upload=upload, operations=tuple(operations))

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

    def build_finalize_test_upload(self, application_test_id: int) -> object:
        """Build the request declaring that no further upload instructions follow."""
        return self.protocol.FinalizeTestUpload(
            test_id=self.protocol.TestId(application_test_id_to_bytes(application_test_id)),
            flags=0,
        )

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
        can = [p.CANConfig() for _ in range(2)]
        spi = [p.SPIConfig() for _ in range(2)]
        uart = [p.UARTConfig() for _ in range(2)]
        i2c = [p.I2CConfig() for _ in range(2)]

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
            elif peripheral == "spi":
                clock_polarity, clock_phase = _spi_clock_mode(p, parameters["mode"])
                spi[channel] = p.SPIConfig(
                    enabled=True,
                    bit_rate=_spi_bit_rate(parameters["baud"]),
                    role=_protocol_enum(p.BusRole, parameters["role"]),
                    data_width=_protocol_enum(
                        p.SPIDataWidth,
                        {"SIZE_8BIT": "BITS_8", "SIZE_16BIT": "BITS_16"}.get(
                            parameters["data_size"]
                        ),
                    ),
                    bit_order=_protocol_enum(
                        p.SPIBitOrder,
                        {"MSB": "MSB_FIRST", "LSB": "LSB_FIRST"}.get(parameters["first_bit"]),
                    ),
                    clock_polarity=clock_polarity,
                    clock_phase=clock_phase,
                )
            elif peripheral == "uart":
                uart[channel] = p.UARTConfig(
                    enabled=True,
                    baud_rate=_require_positive_integer(parameters["baud_hz"], "baud_hz"),
                    electrical_mode=_protocol_enum(
                        p.UARTElectricalMode,
                        {"TTL_3V3": "TTL_3V3", "TTL_5V0": "TTL_5V", "RS232": "RS232"}.get(
                            parameters["mode"]
                        ),
                    ),
                    word_length=_protocol_enum(
                        p.UARTWordLength,
                        {"EIGHT": "BITS_8", "NINE": "BITS_9"}.get(parameters["length"]),
                    ),
                    parity=_protocol_enum(p.UARTParity, parameters["parity"]),
                    stop_bits=_protocol_enum(
                        p.UARTStopBits,
                        {"ONE": "BITS_1", "TWO": "BITS_2"}.get(parameters["stop"]),
                    ),
                    rx_enabled=True,
                    tx_enabled=True,
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
            can=tuple(can),
            spi=tuple(spi),
            uart=tuple(uart),
            i2c=tuple(i2c),
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


def _protocol_enum(enum_type: Any, name: object) -> object:
    if not isinstance(name, str) or not hasattr(enum_type, name):
        raise ProtocolIntegrationError(f"Unsupported {enum_type.__name__} value: {name!r}")
    return getattr(enum_type, name)


def _spi_bit_rate(value: object) -> int:
    rates = {
        "BAUD_45MBIT": 45_000_000,
        "BAUD_22M5BIT": 22_500_000,
        "BAUD_11M25BIT": 11_250_000,
        "BAUD_5M625BIT": 5_625_000,
        "BAUD_2M813BIT": 2_813_000,
        "BAUD_1M406BIT": 1_406_000,
        "BAUD_703KBIT": 703_000,
        "BAUD_352KBIT": 352_000,
    }
    if not isinstance(value, str) or value not in rates:
        raise ProtocolIntegrationError(f"Unsupported SPI bit rate: {value!r}")
    return rates[value]


def _spi_clock_mode(protocol: Any, value: object) -> tuple[object, object]:
    modes = {
        "MODE_0": ("IDLE_LOW", "FIRST_EDGE"),
        "MODE_1": ("IDLE_LOW", "SECOND_EDGE"),
        "MODE_2": ("IDLE_HIGH", "FIRST_EDGE"),
        "MODE_3": ("IDLE_HIGH", "SECOND_EDGE"),
    }
    if not isinstance(value, str) or value not in modes:
        raise ProtocolIntegrationError(f"Unsupported SPI mode: {value!r}")
    polarity, phase = modes[value]
    return protocol.SPIClockPolarity[polarity], protocol.SPIClockPhase[phase]


def _require_positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ProtocolIntegrationError(f"{name} must be a positive integer in the compiled IR")
    return value


def _digital_state(value: object) -> bool:
    if value == "LOW":
        return False
    if value == "HIGH":
        return True
    raise ProtocolIntegrationError(f"Unsupported digital state: {value!r}")


def _extract_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        if value.startswith(("0x", "0X")):
            return bytes.fromhex(value[2:])
        return value.encode("utf-8")
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise ProtocolIntegrationError(
        f"Cannot extract payload bytes from {type(value).__name__}: {value!r}"
    )


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


@dataclass(frozen=True, slots=True)
class VariableIOUploadMessages:
    """Application values for one configuration and its sparse update instructions."""

    upload_attempt: UploadAttempt
    start_mode: str
    configuration: object
    instructions: tuple[object, ...]

    @property
    def messages(self) -> tuple[object, ...]:
        """Return the configuration followed by every sparse update instruction."""
        return (self.configuration, *self.instructions)


class VariableIOProtocolAdapter:
    """Build and encode sparse variable UpdateInstruction messages and test control."""

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
                "Variable-I/O protocol control flow requires hil-rig-protocol 0.3.0 or newer; "
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
    ) -> VariableIOUploadMessages:
        """Translate a compiled definition into one sparse variable upload attempt."""
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
        if any(peripheral == "i2c" for peripheral, _channel in configurations):
            raise ProtocolIntegrationError(
                "I2C is not supported by the variable message family because the "
                "current RIG hardware path is unavailable"
            )
        fixed_adapter = FixedIOProtocolAdapter(
            protocol_module=self.protocol,
            application_config=self.codec.config,
        )
        configuration = fixed_adapter._build_configuration(compiled_test, test_id, configurations)
        instructions = self._build_sparse_update_instructions(
            compiled_test,
            test_id,
            configurations,
        )
        return VariableIOUploadMessages(
            upload_attempt=upload_attempt,
            start_mode=compiled_test.start_mode,
            configuration=configuration,
            instructions=instructions,
        )

    def encode_upload(
        self, upload: VariableIOUploadMessages | FixedIOUploadMessages
    ) -> tuple[bytes, ...]:
        """Encode every complete Application message using the native codec."""
        if not isinstance(upload, (VariableIOUploadMessages, FixedIOUploadMessages)):
            raise TypeError("upload must be VariableIOUploadMessages or FixedIOUploadMessages")
        return tuple(self.codec.encode(message) for message in upload.messages)

    def build_upload_plan(
        self,
        compiled_test: CompiledTestIR,
        *,
        upload_attempt: UploadAttempt | None = None,
    ) -> UploadPlan:
        """Build response-correlated semantic operations for one test attempt with UpdateInstruction."""
        upload = self.build_upload(compiled_test, upload_attempt=upload_attempt)
        encoded = self.encode_upload(upload)
        p = self.protocol
        application_test_id = upload.upload_attempt.application_test_id
        operations = [
            UploadOperation(
                kind=UploadOperationKind.CONFIGURATION,
                encoded_messages=(encoded[0],),
                response=ResponseCorrelation(
                    scope=p.ResponseScope.TEST_CONFIGURATION,
                    successful_outcome=p.ResponseOutcome.ACCEPTED,
                    application_test_id=application_test_id,
                ),
            )
        ]
        operations.extend(
            UploadOperation(
                kind=UploadOperationKind.TICK,
                encoded_messages=(wire,),
                response=None,
                tick=message.tick_number,
            )
            for message, wire in zip(upload.instructions, encoded[1:], strict=True)
        )
        finalize = self.build_finalize_test_upload(application_test_id)
        operations.append(
            UploadOperation(
                kind=UploadOperationKind.FINALIZE,
                encoded_messages=(self.encode(finalize),),
                response=ResponseCorrelation(
                    scope=p.ResponseScope.COMPLETE_TEST,
                    successful_outcome=p.ResponseOutcome.ACCEPTED,
                    application_test_id=application_test_id,
                ),
            )
        )
        if upload.start_mode != "EXTERNAL_TRIGGER":
            start = self.build_start(upload.upload_attempt)
            operations.append(
                UploadOperation(
                    kind=UploadOperationKind.START,
                    encoded_messages=(self.encode(start),),
                    response=ResponseCorrelation(
                        scope=p.ResponseScope.EXECUTION_CONTROL,
                        successful_outcome=p.ResponseOutcome.COMPLETED,
                        application_test_id=application_test_id,
                        control_command=p.ControlCommand.START,
                    ),
                )
            )
        return UploadPlan(upload=upload, operations=tuple(operations))

    def decode(self, data: bytes) -> object:
        """Decode one complete Application message received from USB CDC."""
        return self.codec.decode(data)

    def encode(self, message: object) -> bytes:
        """Encode one public Application value with the native protocol codec."""
        return self.codec.encode(message)

    def build_system_info_request(self, *, request_firmware_git_hash: bool = True) -> object:
        if not isinstance(request_firmware_git_hash, bool):
            raise TypeError("request_firmware_git_hash must be a bool")
        return self.protocol.SystemInfoRequest(request_firmware_git_hash=request_firmware_git_hash)

    def build_start(self, upload_attempt: UploadAttempt) -> object:
        return self._build_execution_control(upload_attempt, self.protocol.ControlCommand.START)

    def build_finalize_test_upload(self, application_test_id: int) -> object:
        return self.protocol.FinalizeTestUpload(
            test_id=self.protocol.TestId(application_test_id_to_bytes(application_test_id)),
            flags=0,
        )

    def build_abort(self, upload_attempt: UploadAttempt) -> object:
        return self._build_execution_control(upload_attempt, self.protocol.ControlCommand.ABORT)

    def build_reset_application(self) -> object:
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

    def _build_sparse_update_instructions(
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
                if analogue_outputs[channel] != 0:
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

        grouped: dict[int, list[CompiledInstruction]] = {}
        for instruction in compiled_test.instructions:
            grouped.setdefault(instruction.tick, []).append(instruction)

        ticks = set(grouped.keys())
        if has_analogue_output:
            ticks.add(0)

        messages: list[object] = []
        for tick in sorted(ticks):
            instructions_on_tick = grouped.get(tick, [])
            operations: list[object] = []
            digital_changed = False

            if tick == 0 and has_analogue_output:
                tick_0_analogue_channels = {
                    inst.channel
                    for inst in instructions_on_tick
                    if inst.peripheral == "analogue_output"
                }
                for (periph, chan), item in configurations.items():
                    if periph == "analogue_output" and chan not in tick_0_analogue_channels:
                        uv = analogue_outputs[chan]
                        if uv != 0:
                            operations.append(
                                p.LogicalOperation(
                                    peripheral_type=p.PeripheralType.ANALOG_OUTPUT,
                                    channel=chan,
                                    payload=uv.to_bytes(4, "little"),
                                )
                            )

            for instruction in instructions_on_tick:
                arguments = instruction.arguments
                periph = instruction.peripheral

                if periph == "digital_output":
                    action = arguments.get("action")
                    if action == "HIGH":
                        digital_outputs[instruction.channel] = True
                    elif action == "LOW":
                        digital_outputs[instruction.channel] = False
                    elif action == "TOGGLE":
                        digital_outputs[instruction.channel] = not digital_outputs[
                            instruction.channel
                        ]
                    else:
                        raise ProtocolIntegrationError(
                            f"Unsupported digital output action: {action!r}"
                        )
                    digital_changed = True

                elif periph == "analogue_output":
                    if instruction.operation != "set_voltage":
                        raise ProtocolIntegrationError(
                            f"Unsupported analogue output operation: {instruction.operation}"
                        )
                    uv = _volts_to_microvolts(arguments.get("voltage"), "voltage")
                    analogue_outputs[instruction.channel] = uv
                    operations.append(
                        p.LogicalOperation(
                            peripheral_type=p.PeripheralType.ANALOG_OUTPUT,
                            channel=instruction.channel,
                            payload=uv.to_bytes(4, "little"),
                        )
                    )

                elif periph == "pwm_output":
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
                    period_ns = state.period_nanoseconds if state.enabled else 0
                    duty_permyriad = state.duty_cycle_permyriad if state.enabled else 0
                    operations.append(
                        p.LogicalOperation(
                            peripheral_type=p.PeripheralType.PWM_OUTPUT,
                            channel=instruction.channel,
                            payload=period_ns.to_bytes(4, "little")
                            + duty_permyriad.to_bytes(2, "little"),
                        )
                    )

                elif periph == "uart":
                    data = _extract_bytes(arguments.get("data"))
                    operations.append(
                        p.LogicalOperation(
                            peripheral_type=p.PeripheralType.UART,
                            channel=instruction.channel,
                            payload=data,
                        )
                    )

                elif periph == "spi":
                    tx_data = _extract_bytes(arguments.get("tx_data"))
                    spi_payload = bytes([1, len(tx_data)]) + tx_data
                    operations.append(
                        p.LogicalOperation(
                            peripheral_type=p.PeripheralType.SPI,
                            channel=instruction.channel,
                            payload=spi_payload,
                        )
                    )

                elif periph == "can":
                    can_data = _extract_bytes(arguments.get("data"))
                    operations.append(
                        p.LogicalOperation(
                            peripheral_type=p.PeripheralType.CAN,
                            channel=instruction.channel,
                            payload=can_data,
                        )
                    )

            if digital_changed:
                mask = sum((1 << i) for i, state in enumerate(digital_outputs) if state)
                operations.insert(
                    0,
                    p.LogicalOperation(
                        peripheral_type=p.PeripheralType.DIGITAL_OUTPUT,
                        channel=0,
                        payload=mask.to_bytes(2, "little"),
                    ),
                )

            if operations:
                messages.append(
                    p.UpdateInstruction(
                        test_id=test_id,
                        tick_number=tick,
                        flags=0,
                        operations=tuple(operations),
                    )
                )

        return tuple(messages)


__all__ = [
    "FixedIOProtocolAdapter",
    "FixedIOUploadMessages",
    "ProtocolFamily",
    "ResponseCorrelation",
    "UploadOperation",
    "UploadOperationKind",
    "UploadPlan",
    "VariableIOProtocolAdapter",
    "VariableIOUploadMessages",
    "application_test_id_from_bytes",
    "application_test_id_to_bytes",
]
