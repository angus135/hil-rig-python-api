"""Reusable fixtures and semantic oracles for Application hardware testing."""

from __future__ import annotations

import secrets
import struct
from dataclasses import replace

import hil_rig_protocol as protocol

APPLICATION_CODEC_CONFIG = protocol.ApplicationConfig(
    max_encoded_message_size=512,
    max_variable_data_size=255,
    max_variable_transfers_per_tick=8,
    max_expected_tick_count=1_000_000,
)

PROTOCOL_VERSION = (0, 1, 0)
COMPATIBILITY_PROFILE_ID = 0x41505031

REPRESENTATIVE_CONFIGURATION_SIZE = 242
ALL_DISABLED_CONFIGURATION_SIZE = 226
MAX_EXTENSION_CONFIGURATION_SIZE = 481
FIXED_INSTRUCTION_SIZE = 73
FIXED_RESULT_SIZE = 62

REPRESENTATIVE_CONFIGURATION_DIGEST = 0xDF35534C
ALL_DISABLED_CONFIGURATION_DIGEST = 0x98E57BA3
MAX_EXTENSION_CONFIGURATION_DIGEST = 0x60672F03
INSTRUCTION_DIGESTS = (0x80089EF8, 0x8DE22BBE, 0x6AC9DD7A)

REPRESENTATIVE_EXTENSION = bytes.fromhex("00 01 7E 7F 80 FE FF 48 52 54 50 00 A5 5A C3 3C")
MAX_EXTENSION = bytes((index * 37) & 0xFF for index in range(255))

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
            protocol.CANConfig(True, 500_000, 64, 0x123, 0x7FF),
            protocol.CANConfig(True, 250_000, 64, 0x400, 0x700),
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
                64,
            ),
            protocol.SPIConfig(
                True,
                703_125,
                protocol.BusRole.SLAVE,
                protocol.SPIDataWidth.BITS_16,
                protocol.SPIBitOrder.LSB_FIRST,
                protocol.SPIClockPolarity.IDLE_HIGH,
                protocol.SPIClockPhase.SECOND_EDGE,
                64,
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
                64,
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
                64,
            ),
        ),
        i2c=(
            protocol.I2CConfig(
                True,
                100_000,
                protocol.BusRole.MASTER,
                0,
                protocol.I2CVoltage.V_3V3,
                protocol.I2CPullUp.OHM_4K7,
                64,
            ),
            protocol.I2CConfig(
                True,
                400_000,
                protocol.BusRole.SLAVE,
                0x42,
                protocol.I2CVoltage.V_5V,
                protocol.I2CPullUp.OHM_2K2,
                64,
            ),
        ),
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


def maximum_extension_configuration(
    test_id: bytes | protocol.TestId,
) -> protocol.TestConfiguration:
    """Build representative peripherals with the maximum 255-byte extension."""
    return replace(
        representative_configuration(test_id), expected_tick_count=1, extension_data=MAX_EXTENSION
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
                _u32(item.capture_limit_bytes),
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
                _u32(item.capture_limit_bytes),
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
                _u32(item.capture_limit_bytes),
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
                _u32(item.capture_limit_bytes),
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
    if type(message) is protocol.TestConfiguration:
        return "TEST_CONFIGURATION"
    if type(message) is protocol.TestInstruction:
        return "TEST_INSTRUCTION"
    if type(message) is protocol.TestResult:
        return "TEST_RESULT"
    raise TypeError("unsupported Application message")
