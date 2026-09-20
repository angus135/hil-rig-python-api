"""Test firmware result rule, independent of the production oracle and digests.

Serialize semantic fields only; Application wire encoding still uses the public codec.
"""

import struct
from dataclasses import astuple

import hil_rig_protocol as protocol


def _digest(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def firmware_result(
    configuration: protocol.TestConfiguration, instruction: protocol.TestInstruction
) -> protocol.TestResult:
    configuration_data = struct.pack(
        "<III",
        configuration.tick_duration_us.microseconds,
        configuration.expected_tick_count,
        configuration.flags,
    )
    # Explicit semantic family order and integer widths from the documented rule.
    for family, widths in (
        ("digital_in", "BB"),
        ("digital_out", "BBB"),
        ("analog_in", "B"),
        ("analog_out", "B"),
        ("pwm_in", "BB"),
        ("pwm_out", "BBIH"),
        ("can", "BIHH"),
        ("spi", "BIBBBBB"),
        ("uart", "BIBBBBBB"),
        ("i2c", "BIBHBB"),
    ):
        for channel in getattr(configuration, family):
            configuration_data += struct.pack("<" + widths, *astuple(channel))
    configuration_data += bytes([len(configuration.extension_data)]) + configuration.extension_data
    instruction_data = struct.pack("<I", instruction.tick_number)
    for family, widths in (
        ("digital_outputs", "B"),
        ("analog_outputs", "I"),
        ("pwm_outputs", "IH"),
    ):
        for channel in getattr(instruction, family):
            instruction_data += struct.pack("<" + widths, *astuple(channel))
    analog = (_digest(configuration_data) % 20_000_001, _digest(instruction_data) % 20_000_001)
    return protocol.TestResult(
        test_id=instruction.test_id,
        tick_number=instruction.tick_number,
        digital_inputs=tuple(
            protocol.DigitalInputValue(enabled.enabled and output.high)
            for enabled, output in zip(
                configuration.digital_in, instruction.digital_outputs, strict=True
            )
        ),
        analog_inputs=tuple(
            protocol.AnalogInputValue(value if channel.enabled else 0)
            for channel, value in zip(configuration.analog_in, analog, strict=True)
        ),
        pwm_inputs=tuple(
            protocol.PWMInputValue(
                output.period_nanoseconds if channel.enabled else 0,
                output.duty_cycle_permyriad if channel.enabled else 0,
            )
            for channel, output in zip(configuration.pwm_in, instruction.pwm_outputs, strict=True)
        ),
        condition=protocol.ResultCondition.OK,
        problem_detail=0,
    )
