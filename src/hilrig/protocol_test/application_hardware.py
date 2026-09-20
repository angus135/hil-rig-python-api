"""Reusable fixtures and semantic oracles for Application hardware testing."""

from __future__ import annotations

import secrets
import struct
from dataclasses import replace

import hil_rig_protocol as protocol

APPLICATION_CODEC_CONFIG = protocol.ApplicationConfig(
    max_encoded_message_size=512,
    max_variable_data_size=255,
    max_expected_tick_count=1_000_000,
)

PROTOCOL_VERSION = (0, 3, 0)
COMPATIBILITY_PROFILE_ID = 0x41505032

REPRESENTATIVE_CONFIGURATION_SIZE = 210
ALL_DISABLED_CONFIGURATION_SIZE = 194
MAX_EXTENSION_CONFIGURATION_SIZE = 449
FIXED_INSTRUCTION_SIZE = 73
FIXED_RESULT_SIZE = 62
FIXED_RESPONSE_SIZE = 36
ERROR_FIXED_SIZE = 35
ERROR_DIAGNOSTIC_SIZE_BASE = ERROR_FIXED_SIZE

REPRESENTATIVE_CONFIGURATION_DIGEST = 0xE5B6A67E
ALL_DISABLED_CONFIGURATION_DIGEST = 0xBA7FAE23
MAX_EXTENSION_CONFIGURATION_DIGEST = 0x71EF1F9D
INSTRUCTION_DIGESTS = (0x80089EF8, 0x8DE22BBE, 0x6AC9DD7A)

REPRESENTATIVE_EXTENSION = bytes.fromhex("00 01 7E 7F 80 FE FF 48 52 54 50 00 A5 5A C3 3C")
MAX_EXTENSION = bytes((index * 37) & 0xFF for index in range(255))
BINARY_ERROR_DIAGNOSTIC = bytes.fromhex("00 FF 48 49 4C 00 52 49 47 7E C0 DB")
MAXIMUM_ERROR_DIAGNOSTIC = bytes(range(255))

_FNV_OFFSET = 2_166_136_261
_FNV_PRIME = 16_777_619
_UINT32_MASK = 0xFFFF_FFFF


def make_application_codec() -> protocol.ApplicationCodec:
    """Create the public Application codec with the hardware-test policy."""
    return protocol.ApplicationCodec(APPLICATION_CODEC_CONFIG)


def new_test_id() -> protocol.TestId:
    """Allocate a fresh opaque 16-byte Test ID for a hardware transaction."""
    return protocol.TestId(secrets.token_bytes(16))


def _test_id(value: bytes | protocol.TestId) -> protocol.TestId:
    if type(value) is protocol.TestId:
        return value
    if type(value) is not bytes:
        raise TypeError("test_id must be bytes or TestId")
    return protocol.TestId(value)


def representative_configuration(test_id: bytes | protocol.TestId) -> protocol.TestConfiguration:
    """Build the fixed representative configuration for one caller-provided Test ID."""
    test_id = _test_id(test_id)
    voltages = (
        protocol.PeripheralVoltage.V_3V3,
        protocol.PeripheralVoltage.V_5V,
        protocol.PeripheralVoltage.V_12V,
        protocol.PeripheralVoltage.V_24V,
        protocol.PeripheralVoltage.V_3V3,
        protocol.PeripheralVoltage.V_5V,
        protocol.PeripheralVoltage.V_12V,
        protocol.PeripheralVoltage.V_24V,
        protocol.PeripheralVoltage.V_3V3,
        protocol.PeripheralVoltage.V_24V,
    )
    initial = (False, True, False, True, False, True, False, True, False, True)
    return protocol.TestConfiguration(
        test_id=test_id,
        tick_duration_us=protocol.TickDuration(1_000),
        expected_tick_count=3,
        flags=0,
        digital_in=tuple(protocol.DigitalInputConfig(True, voltage) for voltage in voltages),
        digital_out=tuple(
            protocol.DigitalOutputConfig(True, voltage, high)
            for voltage, high in zip(voltages, initial, strict=True)
        ),
        analog_in=(protocol.AnalogInputConfig(True), protocol.AnalogInputConfig(True)),
        analog_out=(protocol.AnalogOutputConfig(True),) * 6,
        pwm_in=(
            protocol.PWMInputConfig(True, protocol.PeripheralVoltage.V_3V3),
            protocol.PWMInputConfig(True, protocol.PeripheralVoltage.V_24V),
        ),
        pwm_out=(
            protocol.PWMOutputConfig(True, protocol.PeripheralVoltage.V_3V3, 1_000_000, 2_500),
            protocol.PWMOutputConfig(True, protocol.PeripheralVoltage.V_24V, 2_000_000, 7_500),
        ),
        can=(
            protocol.CANConfig(True, 500_000, 0x123, 0x7FF),
            protocol.CANConfig(True, 250_000, 0x400, 0x700),
        ),
        spi=(
            protocol.SPIConfig(
                True,
                5_625_000,
                protocol.BusRole.MASTER,
                protocol.SPIDataWidth.BITS_8,
                protocol.SPIBitOrder.MSB_FIRST,
                protocol.SPIClockPolarity.IDLE_LOW,
                protocol.SPIClockPhase.FIRST_EDGE,
            ),
            protocol.SPIConfig(
                True,
                703_125,
                protocol.BusRole.SLAVE,
                protocol.SPIDataWidth.BITS_16,
                protocol.SPIBitOrder.LSB_FIRST,
                protocol.SPIClockPolarity.IDLE_HIGH,
                protocol.SPIClockPhase.SECOND_EDGE,
            ),
        ),
        uart=(
            protocol.UARTConfig(
                True,
                115_200,
                protocol.UARTElectricalMode.TTL_3V3,
                protocol.UARTWordLength.BITS_8,
                protocol.UARTParity.NONE,
                protocol.UARTStopBits.BITS_1,
                True,
                True,
            ),
            protocol.UARTConfig(
                True,
                57_600,
                protocol.UARTElectricalMode.RS232,
                protocol.UARTWordLength.BITS_9,
                protocol.UARTParity.EVEN,
                protocol.UARTStopBits.BITS_2,
                True,
                True,
            ),
        ),
        # Enabled I2C is intentionally covered only by raw-negative tests;
        # the representative supported fixture keeps both channels disabled.
        i2c=(protocol.I2CConfig(), protocol.I2CConfig()),
        extension_data=REPRESENTATIVE_EXTENSION,
    )


def representative_instructions(
    test_id: bytes | protocol.TestId,
) -> tuple[protocol.TestInstruction, ...]:
    """Build the three fixed representative instructions."""
    test_id = _test_id(test_id)
    digital = (
        (False, True, False, True, False, True, False, True, False, True),
        (True, False, True, False, True, False, True, False, True, False),
        (False,) * 10,
    )
    analog = (
        (0, 1_000, 5_000, 10_000, 15_000, 20_000),
        (20_000, 15_000, 10_000, 5_000, 1_000, 0),
        (3_300, 5_000, 12_000, 18_000, 20_000, 0),
    )
    pwm = (
        ((1_000_000, 2_500), (2_000_000, 7_500)),
        ((500_000, 5_000), (4_000_000, 1_000)),
        ((10_000_000, 0), (1_000_000, 10_000)),
    )
    return tuple(
        protocol.TestInstruction(
            test_id=test_id,
            tick_number=tick,
            digital_outputs=tuple(protocol.DigitalOutputValue(value) for value in digital[tick]),
            analog_outputs=tuple(protocol.AnalogOutputValue(value) for value in analog[tick]),
            pwm_outputs=tuple(protocol.PWMOutputValue(*value) for value in pwm[tick]),
        )
        for tick in range(3)
    )


def all_disabled_configuration(test_id: bytes | protocol.TestId) -> protocol.TestConfiguration:
    """Build the canonical all-disabled boundary configuration."""
    return protocol.TestConfiguration(
        test_id=_test_id(test_id),
        tick_duration_us=protocol.TickDuration(10_000),
        expected_tick_count=1,
        flags=0,
    )


def zero_instruction(test_id: bytes | protocol.TestId) -> protocol.TestInstruction:
    """Build the zero-valued tick used with the all-disabled boundary configuration."""
    return protocol.TestInstruction(test_id=_test_id(test_id), tick_number=0)


def configured_initial_instruction(
    configuration: protocol.TestConfiguration, tick: int = 0
) -> protocol.TestInstruction:
    """Build the fixed result input represented by configured initial output state."""
    return protocol.TestInstruction(
        test_id=configuration.test_id,
        tick_number=tick,
        digital_outputs=tuple(
            protocol.DigitalOutputValue(channel.initial_high)
            for channel in configuration.digital_out
        ),
        analog_outputs=(protocol.AnalogOutputValue(0),) * len(configuration.analog_out),
        pwm_outputs=tuple(
            protocol.PWMOutputValue(
                channel.initial_period_nanoseconds,
                channel.initial_duty_cycle_permyriad,
            )
            for channel in configuration.pwm_out
        ),
    )


def test_profile(*, result_family: int = 0, fault_mode: int = 0, capacity: int = 0) -> bytes:
    """Build the documented test-only HTV3 extension marker.

    The marker is consumed only by the temporary firmware harness. Other
    extension bytes remain opaque to the protocol and are intentionally left
    available for the boundary fixture.
    """
    if result_family not in (0, 1) or fault_mode not in (0, 1, 2):
        raise ValueError("invalid HTV3 profile selector")
    if not 0 <= capacity <= 0xFFFF:
        raise ValueError("capacity must fit the HTV3 uint16 field")
    return b"HTV3" + bytes((result_family, fault_mode)) + struct.pack("<H", capacity)


def variable_operations(
    test_id: bytes | protocol.TestId, tick: int = 0
) -> tuple[protocol.UpdateInstruction, ...]:
    """Build a two-chunk Type 21 sparse upload covering every supported family."""
    test_id = _test_id(test_id)
    operations = (
        protocol.LogicalOperation(
            protocol.PeripheralType.DIGITAL_OUTPUT, 0, struct.pack("<H", tick & 1)
        ),
        protocol.LogicalOperation(
            protocol.PeripheralType.ANALOG_OUTPUT, 0, struct.pack("<I", 3300 + tick)
        ),
        protocol.LogicalOperation(
            protocol.PeripheralType.PWM_OUTPUT, 0, struct.pack("<IH", 1_000_000, 2500)
        ),
        protocol.LogicalOperation(protocol.PeripheralType.UART, 0, b"HT-UART"),
        protocol.LogicalOperation(protocol.PeripheralType.SPI, 0, b"\x01\x02HT"),
        protocol.LogicalOperation(
            protocol.PeripheralType.CAN,
            0,
            struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
        ),
    )
    return (
        protocol.UpdateInstruction(test_id, tick, 1, operations[:3]),
        protocol.UpdateInstruction(test_id, tick, 0, operations[3:]),
    )


def sparse_variable_upload(
    test_id: bytes | protocol.TestId,
) -> tuple[protocol.UpdateInstruction, ...]:
    """Build ticks 0 and 2, deliberately omitting sparse tick 1."""
    return variable_operations(test_id, 0) + variable_operations(test_id, 2)


def multi_chunk_variable_upload(
    test_id: bytes | protocol.TestId, tick: int = 0
) -> tuple[protocol.UpdateInstruction, ...]:
    """Build eight valid Type 21 chunks with repeated communication records."""
    test_id = _test_id(test_id)
    operations = (
        protocol.LogicalOperation(protocol.PeripheralType.UART, 0, b"HT-UART"),
        protocol.LogicalOperation(protocol.PeripheralType.UART, 1, b"HT-UART"),
        protocol.LogicalOperation(protocol.PeripheralType.SPI, 0, b"\x01\x02HT"),
        protocol.LogicalOperation(protocol.PeripheralType.SPI, 1, b"\x01\x02HT"),
        protocol.LogicalOperation(
            protocol.PeripheralType.CAN,
            0,
            struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
        ),
        protocol.LogicalOperation(
            protocol.PeripheralType.CAN,
            1,
            struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
        ),
    )
    return tuple(
        protocol.UpdateInstruction(
            test_id,
            tick,
            1 if chunk < 7 else 0,
            operations,
        )
        for chunk in range(8)
    )


def finalize_upload(
    test_id: bytes | protocol.TestId, flags: int = 0
) -> protocol.FinalizeTestUpload:
    """Build the explicit Type 22 upload finalizer."""
    return protocol.FinalizeTestUpload(_test_id(test_id), flags)


def variable_result_oracle(
    test_id: bytes | protocol.TestId,
    tick: int,
    *,
    condition: protocol.ResultCondition = protocol.ResultCondition.OK,
    problem_detail: int = 0,
) -> protocol.VariableTestResult:
    """Build the synthetic Type 34 capture oracle for one result tick."""
    test_id = _test_id(test_id)
    records = (
        ()
        if condition is not protocol.ResultCondition.OK
        else (
            protocol.CapturedRecord(
                protocol.PeripheralType.DIGITAL_INPUT, 0, struct.pack("<H", tick & 1)
            ),
            protocol.CapturedRecord(
                protocol.PeripheralType.ANALOG_INPUT, 0, struct.pack("<I", 3300 + tick)
            ),
            protocol.CapturedRecord(
                protocol.PeripheralType.PWM_INPUT, 0, struct.pack("<IH", 1_000_000, 2500)
            ),
            protocol.CapturedRecord(protocol.PeripheralType.UART, 0, b"HT-UART"),
            # SPI captures contain the concatenated packet data, not the count
            # and per-packet length prefix used by the update payload.
            protocol.CapturedRecord(protocol.PeripheralType.SPI, 0, b"HT"),
            protocol.CapturedRecord(
                protocol.PeripheralType.CAN,
                0,
                struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
            ),
        )
    )
    return protocol.VariableTestResult(test_id, tick, condition, 0, problem_detail, records)


def multi_chunk_variable_result_oracle(
    test_id: bytes | protocol.TestId, tick: int = 0
) -> protocol.VariableTestResult:
    """Build the ordered Type 34 oracle for the eight-chunk communication fixture."""
    test_id = _test_id(test_id)
    records = tuple(
        protocol.CapturedRecord(peripheral_type, channel, payload)
        for _ in range(8)
        for peripheral_type, channel, payload in (
            (protocol.PeripheralType.UART, 0, b"HT-UART"),
            (protocol.PeripheralType.UART, 1, b"HT-UART"),
            (protocol.PeripheralType.SPI, 0, b"HT"),
            (protocol.PeripheralType.SPI, 1, b"HT"),
            (
                protocol.PeripheralType.CAN,
                0,
                struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
            ),
            (
                protocol.PeripheralType.CAN,
                1,
                struct.pack("<HB", 0x123, 3) + b"CAN" + bytes(5) + b"\x00",
            ),
        )
    )
    return protocol.VariableTestResult(
        test_id,
        tick,
        protocol.ResultCondition.OK,
        0,
        0,
        records,
    )


def maximum_extension_configuration(
    test_id: bytes | protocol.TestId,
) -> protocol.TestConfiguration:
    """Build representative peripherals with the maximum 255-byte extension."""
    return replace(
        representative_configuration(test_id), expected_tick_count=1, extension_data=MAX_EXTENSION
    )


def execution_controls(
    test_id: bytes | protocol.TestId,
) -> tuple[protocol.ExecutionControl, protocol.ExecutionControl]:
    """Build deterministic START and ABORT controls for a caller-owned fresh Test ID."""
    test_id = _test_id(test_id)
    return (
        protocol.ExecutionControl(test_id, protocol.ControlCommand.START),
        protocol.ExecutionControl(test_id, protocol.ControlCommand.ABORT),
    )


def reset_application_control() -> protocol.GlobalControl:
    """Build the test-only RESET_APPLICATION global control fixture."""
    return protocol.GlobalControl(protocol.GlobalControlCommand.RESET_APPLICATION)


def execution_control_response(
    control: protocol.ExecutionControl,
) -> protocol.ApplicationResponse:
    """Build the correlated synthetic response expected for a control fixture."""
    return protocol.ApplicationResponse(
        control.test_id,
        protocol.ResponseScope.EXECUTION_CONTROL,
        protocol.ResponseOutcome.COMPLETED,
        protocol.ResponseReason.NONE,
        control_command=control.command,
    )


def global_control_response(control: protocol.GlobalControl) -> protocol.ApplicationResponse:
    """Build the no-Test-ID synthetic response expected for a global control fixture."""
    return protocol.ApplicationResponse(
        None,
        protocol.ResponseScope.GLOBAL_CONTROL,
        protocol.ResponseOutcome.COMPLETED,
        protocol.ResponseReason.NONE,
        global_control_command=control.command,
    )


def response_fixtures(test_id: bytes | protocol.TestId) -> tuple[protocol.ApplicationResponse, ...]:
    """Build one deterministic Response for every defined non-sentinel Response scope."""
    test_id = _test_id(test_id)
    start, _ = execution_controls(test_id)
    reset = reset_application_control()
    return (
        protocol.ApplicationResponse(
            test_id,
            protocol.ResponseScope.TEST_CONFIGURATION,
            protocol.ResponseOutcome.ACCEPTED,
            protocol.ResponseReason.NONE,
        ),
        protocol.ApplicationResponse(
            test_id,
            protocol.ResponseScope.TICK,
            protocol.ResponseOutcome.COMPLETED,
            protocol.ResponseReason.NONE,
            tick_number=23,
            detail=0x1020_3040,
        ),
        protocol.ApplicationResponse(
            test_id,
            protocol.ResponseScope.COMPLETE_TEST,
            protocol.ResponseOutcome.COMPLETED,
            protocol.ResponseReason.NONE,
            tick_number=24,
            detail=0x5060_7080,
        ),
        execution_control_response(start),
        global_control_response(reset),
    )


def global_application_error() -> protocol.ApplicationErrorMessage:
    """Build the global Error form with an intentionally empty diagnostic."""
    return protocol.ApplicationErrorMessage(
        None,
        protocol.ErrorCategory.PROTOCOL,
        True,
        detail=0x0102_0304,
    )


def test_wide_application_error(
    test_id: bytes | protocol.TestId,
) -> protocol.ApplicationErrorMessage:
    """Build the test-wide Error form with binary diagnostic bytes."""
    return protocol.ApplicationErrorMessage(
        _test_id(test_id),
        protocol.ErrorCategory.HARDWARE,
        False,
        detail=0x1122_3344,
        diagnostic_data=BINARY_ERROR_DIAGNOSTIC,
    )


def tick_application_error(test_id: bytes | protocol.TestId) -> protocol.ApplicationErrorMessage:
    """Build the tick-specific Error form with the maximum 255-byte diagnostic."""
    return protocol.ApplicationErrorMessage(
        _test_id(test_id),
        protocol.ErrorCategory.EXECUTION,
        True,
        tick_number=23,
        detail=0x99AA_BBCC,
        diagnostic_data=MAXIMUM_ERROR_DIAGNOSTIC,
    )


def application_error_fixtures(
    test_id: bytes | protocol.TestId,
) -> tuple[protocol.ApplicationErrorMessage, ...]:
    """Build deterministic global, test-wide, and tick-specific Error fixtures."""
    return (
        global_application_error(),
        test_wide_application_error(test_id),
        tick_application_error(test_id),
    )


def expected_result(
    configuration: protocol.TestConfiguration,
    instruction: protocol.TestInstruction,
) -> protocol.TestResult:
    """Return the deterministic firmware result oracle for a fixed instruction."""
    if configuration.test_id != instruction.test_id:
        raise ValueError("configuration and instruction Test IDs differ")
    tick = instruction.tick_number
    digital_inputs = tuple(
        protocol.DigitalInputValue(
            instruction.digital_outputs[index].high if item.enabled else False
        )
        for index, item in enumerate(configuration.digital_in)
    )
    analog_values = (
        configuration_semantic_digest(configuration) % 20_000_001,
        instruction_semantic_digest(instruction) % 20_000_001,
    )
    analog_inputs = tuple(
        protocol.AnalogInputValue(analog_values[index] if item.enabled else 0)
        for index, item in enumerate(configuration.analog_in)
    )
    pwm_inputs = tuple(
        protocol.PWMInputValue(
            instruction.pwm_outputs[index].period_nanoseconds if item.enabled else 0,
            instruction.pwm_outputs[index].duty_cycle_permyriad if item.enabled else 0,
        )
        for index, item in enumerate(configuration.pwm_in)
    )
    return protocol.TestResult(
        test_id=instruction.test_id,
        tick_number=tick,
        digital_inputs=digital_inputs,
        analog_inputs=analog_inputs,
        pwm_inputs=pwm_inputs,
        condition=protocol.ResultCondition.OK,
        problem_detail=0,
    )


def _fnv1a(parts: list[bytes]) -> int:
    value = _FNV_OFFSET
    for part in parts:
        for byte in part:
            value ^= byte
            value = (value * _FNV_PRIME) & _UINT32_MASK
    return value


def _u8(value: int | bool) -> bytes:
    return struct.pack("<B", int(value))


def _u16(value: int) -> bytes:
    return struct.pack("<H", int(value))


def _u32(value: int) -> bytes:
    return struct.pack("<I", int(value))


def configuration_semantic_digest(configuration: protocol.TestConfiguration) -> int:
    """Digest configuration semantics in the documented stable field order, excluding Test ID."""
    parts = [
        _u32(configuration.tick_duration_us.microseconds),
        _u32(configuration.expected_tick_count),
        _u32(configuration.flags),
    ]
    for item in configuration.digital_in:
        parts.extend((_u8(item.enabled), _u8(item.voltage_level)))
    for item in configuration.digital_out:
        parts.extend((_u8(item.enabled), _u8(item.voltage_level), _u8(item.initial_high)))
    for item in configuration.analog_in:
        parts.append(_u8(item.enabled))
    for item in configuration.analog_out:
        parts.append(_u8(item.enabled))
    for item in configuration.pwm_in:
        parts.extend((_u8(item.enabled), _u8(item.voltage_level)))
    for item in configuration.pwm_out:
        parts.extend(
            (
                _u8(item.enabled),
                _u8(item.voltage_level),
                _u32(item.initial_period_nanoseconds),
                _u16(item.initial_duty_cycle_permyriad),
            )
        )
    for item in configuration.can:
        parts.extend(
            (
                _u8(item.enabled),
                _u32(item.bit_rate),
                _u16(item.filter_id),
                _u16(item.filter_mask),
            )
        )
    for item in configuration.spi:
        parts.extend(
            (
                _u8(item.enabled),
                _u32(item.bit_rate),
                _u8(item.role),
                _u8(item.data_width),
                _u8(item.bit_order),
                _u8(item.clock_polarity),
                _u8(item.clock_phase),
            )
        )
    for item in configuration.uart:
        parts.extend(
            (
                _u8(item.enabled),
                _u32(item.baud_rate),
                _u8(item.electrical_mode),
                _u8(item.word_length),
                _u8(item.parity),
                _u8(item.stop_bits),
                _u8(item.rx_enabled),
                _u8(item.tx_enabled),
            )
        )
    for item in configuration.i2c:
        parts.extend(
            (
                _u8(item.enabled),
                _u32(item.bit_rate),
                _u8(item.role),
                _u16(item.own_address_7bit),
                _u8(item.voltage_level),
                _u8(item.pull_up),
            )
        )
    parts.extend((_u8(len(configuration.extension_data)), configuration.extension_data))
    return _fnv1a(parts)


def instruction_semantic_digest(instruction: protocol.TestInstruction) -> int:
    """Digest fixed instruction semantics in stable field order, excluding Test ID."""
    parts = [_u32(instruction.tick_number)]
    parts.extend(_u8(item.high) for item in instruction.digital_outputs)
    parts.extend(_u32(item.microvolts) for item in instruction.analog_outputs)
    for item in instruction.pwm_outputs:
        parts.extend((_u32(item.period_nanoseconds), _u16(item.duty_cycle_permyriad)))
    return _fnv1a(parts)


def application_message_name(message: protocol.ApplicationMessage) -> str:
    if type(message) is protocol.SystemInfoRequest:
        return "SYSTEM_INFO_REQUEST"
    if type(message) is protocol.SystemInfoResponse:
        return "SYSTEM_INFO_RESPONSE"
    if type(message) is protocol.TestConfiguration:
        return "TEST_CONFIGURATION"
    if type(message) is protocol.TestInstruction:
        return "TEST_INSTRUCTION"
    if type(message) is protocol.UpdateInstruction:
        return "UPDATE_INSTRUCTION"
    if type(message) is protocol.FinalizeTestUpload:
        return "FINALIZE_TEST_UPLOAD"
    if type(message) is protocol.ExecutionControl:
        return "EXECUTION_CONTROL"
    if type(message) is protocol.GlobalControl:
        return "GLOBAL_CONTROL"
    if type(message) is protocol.TestResult:
        return "TEST_RESULT"
    if type(message) is protocol.VariableTestResult:
        return "VARIABLE_TEST_RESULT"
    if type(message) is protocol.ApplicationResponse:
        return "RESPONSE"
    if type(message) is protocol.ApplicationErrorMessage:
        return "ERROR"
    raise TypeError("unsupported Application message")
