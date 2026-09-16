from __future__ import annotations

from dataclasses import fields, replace

import hil_rig_protocol as protocol

from hilrig.protocol_test.application_hardware import (
    ALL_DISABLED_CONFIGURATION_DIGEST,
    ALL_DISABLED_CONFIGURATION_SIZE,
    APPLICATION_CODEC_CONFIG,
    BINARY_ERROR_DIAGNOSTIC,
    ERROR_FIXED_SIZE,
    FIXED_INSTRUCTION_SIZE,
    FIXED_RESPONSE_SIZE,
    FIXED_RESULT_SIZE,
    INSTRUCTION_DIGESTS,
    MAX_EXTENSION_CONFIGURATION_DIGEST,
    MAX_EXTENSION_CONFIGURATION_SIZE,
    REPRESENTATIVE_CONFIGURATION_DIGEST,
    REPRESENTATIVE_CONFIGURATION_SIZE,
    all_disabled_configuration,
    application_error_fixtures,
    configuration_semantic_digest,
    execution_controls,
    expected_result,
    instruction_semantic_digest,
    make_application_codec,
    maximum_extension_configuration,
    representative_configuration,
    representative_instructions,
    reset_application_control,
    response_fixtures,
    zero_instruction,
)

TEST_ID = protocol.TestId(bytes(range(16)))


def test_application_codec_configuration_matches_hardware_profile() -> None:
    assert (
        protocol.ApplicationConfig(
            max_encoded_message_size=512,
            max_variable_data_size=255,
            max_variable_transfers_per_tick=8,
            max_expected_tick_count=1_000_000,
        )
        == APPLICATION_CODEC_CONFIG
    )


def test_v02_control_response_and_error_fixtures_round_trip_with_exact_wire_sizes() -> None:
    codec = make_application_codec()
    start, abort = execution_controls(TEST_ID)
    reset = reset_application_control()
    assert (start.command, abort.command) == (
        protocol.ControlCommand.START,
        protocol.ControlCommand.ABORT,
    )
    assert start.test_id == abort.test_id == TEST_ID
    assert reset.command is protocol.GlobalControlCommand.RESET_APPLICATION

    responses = response_fixtures(TEST_ID)
    assert tuple(item.scope for item in responses) == (
        protocol.ResponseScope.TEST_CONFIGURATION,
        protocol.ResponseScope.TICK,
        protocol.ResponseScope.COMPLETE_TEST,
        protocol.ResponseScope.EXECUTION_CONTROL,
        protocol.ResponseScope.GLOBAL_CONTROL,
    )
    assert responses[-1].test_id is None
    for response in responses:
        encoded = codec.encode(response)
        assert len(encoded) == FIXED_RESPONSE_SIZE
        assert codec.decode(encoded) == response

    global_error, test_wide_error, tick_error = application_error_fixtures(TEST_ID)
    assert global_error.test_id is None and global_error.tick_number is None
    assert test_wide_error.test_id == TEST_ID and test_wide_error.tick_number is None
    assert tick_error.test_id == TEST_ID and tick_error.tick_number is not None
    assert global_error.diagnostic_data == b""
    assert test_wide_error.diagnostic_data == BINARY_ERROR_DIAGNOSTIC
    assert len(tick_error.diagnostic_data) == 255
    for error in (global_error, test_wide_error, tick_error):
        encoded = codec.encode(error)
        decoded = codec.decode(encoded)
        assert len(encoded) == ERROR_FIXED_SIZE + len(error.diagnostic_data)
        assert decoded == error
    maximum_decoded = codec.decode(codec.encode(tick_error))
    assert type(maximum_decoded) is protocol.ApplicationErrorMessage
    assert maximum_decoded.diagnostic_data == tick_error.diagnostic_data
    assert maximum_decoded.diagnostic_data is not tick_error.diagnostic_data


def test_representative_configuration_populates_every_family() -> None:
    configuration = representative_configuration(TEST_ID)
    assert all(item.enabled for item in configuration.digital_in)
    assert all(item.enabled for item in configuration.digital_out)
    assert all(item.enabled for item in configuration.analog_in)
    assert all(item.enabled for item in configuration.analog_out)
    assert all(item.enabled for item in configuration.pwm_in)
    assert all(item.enabled for item in configuration.pwm_out)
    assert all(item.enabled for item in configuration.can)
    assert all(item.enabled for item in configuration.spi)
    assert all(item.enabled for item in configuration.uart)
    assert all(item.enabled for item in configuration.i2c)


def test_can_configuration_has_filters_and_no_termination_property() -> None:
    field_names = {field.name for field in fields(protocol.CANConfig)}
    assert field_names == {
        "enabled",
        "bit_rate",
        "capture_limit_bytes",
        "filter_id",
        "filter_mask",
    }
    configuration = representative_configuration(TEST_ID)
    assert configuration.can[0].filter_id == 0x123
    assert configuration.can[0].filter_mask == 0x7FF
    assert configuration.can[1].filter_id == 0x400
    assert configuration.can[1].filter_mask == 0x700
    assert not hasattr(configuration.can[0], "termination")


def test_configuration_golden_sizes_and_semantic_digests() -> None:
    codec = make_application_codec()
    cases = (
        (
            all_disabled_configuration(TEST_ID),
            ALL_DISABLED_CONFIGURATION_SIZE,
            ALL_DISABLED_CONFIGURATION_DIGEST,
        ),
        (
            representative_configuration(TEST_ID),
            REPRESENTATIVE_CONFIGURATION_SIZE,
            REPRESENTATIVE_CONFIGURATION_DIGEST,
        ),
        (
            maximum_extension_configuration(TEST_ID),
            MAX_EXTENSION_CONFIGURATION_SIZE,
            MAX_EXTENSION_CONFIGURATION_DIGEST,
        ),
    )
    for message, size, digest in cases:
        encoded = codec.encode(message)
        assert len(encoded) == size
        assert configuration_semantic_digest(message) == digest
        assert codec.decode(encoded) == message


def test_instruction_golden_sizes_semantic_digests_and_round_trips() -> None:
    codec = make_application_codec()
    for instruction, digest in zip(
        representative_instructions(TEST_ID), INSTRUCTION_DIGESTS, strict=True
    ):
        encoded = codec.encode(instruction)
        assert len(encoded) == FIXED_INSTRUCTION_SIZE
        assert instruction_semantic_digest(instruction) == digest
        assert codec.decode(encoded) == instruction


def test_expected_results_have_exact_size_and_round_trip() -> None:
    codec = make_application_codec()
    configuration = representative_configuration(TEST_ID)
    for instruction in representative_instructions(TEST_ID):
        result = expected_result(configuration, instruction)
        assert result.test_id == instruction.test_id
        assert result.tick_number == instruction.tick_number
        assert result.condition is protocol.ResultCondition.OK
        assert result.problem_detail == 0
        assert len(codec.encode(result)) == FIXED_RESULT_SIZE
        assert codec.decode(codec.encode(result)) == result


def test_expected_result_oracle_matches_deterministic_values() -> None:
    configuration = representative_configuration(TEST_ID)
    results = [
        expected_result(configuration, item) for item in representative_instructions(TEST_ID)
    ]
    assert [item.analog_inputs[0].microvolts for item in results] == [4_813_713] * 3
    assert [item.analog_inputs[1].microvolts for item in results] == [
        8_048_525,
        409_671,
        11_614_241,
    ]


def test_disabled_inputs_produce_canonical_zero_result_values() -> None:
    configuration = all_disabled_configuration(TEST_ID)
    result = expected_result(configuration, zero_instruction(TEST_ID))
    assert all(not item.high for item in result.digital_inputs)
    assert all(item.microvolts == 0 for item in result.analog_inputs)
    assert all(item.period_nanoseconds == 0 for item in result.pwm_inputs)
    assert all(item.duty_cycle_permyriad == 0 for item in result.pwm_inputs)


def test_representative_configuration_matches_required_field_values() -> None:
    configuration = representative_configuration(TEST_ID)
    expected_voltages = (
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
    assert configuration.test_id == TEST_ID
    assert configuration.tick_duration_us.microseconds == 1_000
    assert configuration.expected_tick_count == 3
    assert configuration.flags == 0
    assert configuration.extension_data == bytes.fromhex(
        "00 01 7E 7F 80 FE FF 48 52 54 50 00 A5 5A C3 3C"
    )
    assert tuple(item.voltage_level for item in configuration.digital_in) == expected_voltages
    assert tuple(item.voltage_level for item in configuration.digital_out) == expected_voltages
    assert tuple(item.initial_high for item in configuration.digital_out) == (
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
    )
    assert configuration.pwm_in == (
        protocol.PWMInputConfig(True, protocol.PeripheralVoltage.V_3V3),
        protocol.PWMInputConfig(True, protocol.PeripheralVoltage.V_24V),
    )
    assert configuration.pwm_out == (
        protocol.PWMOutputConfig(True, protocol.PeripheralVoltage.V_3V3, 1_000_000, 2_500),
        protocol.PWMOutputConfig(True, protocol.PeripheralVoltage.V_24V, 2_000_000, 7_500),
    )
    assert configuration.can == (
        protocol.CANConfig(True, 500_000, 64, 0x123, 0x7FF),
        protocol.CANConfig(True, 250_000, 64, 0x400, 0x700),
    )
    assert configuration.spi == (
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
    )
    assert configuration.uart == (
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
    )
    assert configuration.i2c == (
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
    )


def test_all_disabled_configuration_uses_canonical_disabled_values() -> None:
    configuration = all_disabled_configuration(TEST_ID)
    assert configuration.tick_duration_us.microseconds == 10_000
    assert configuration.expected_tick_count == 1
    assert configuration.flags == 0
    assert configuration.extension_data == b""
    assert all(
        not item.enabled and item.voltage_level is protocol.PeripheralVoltage.INVALID
        for item in configuration.digital_in
    )
    assert all(
        not item.enabled
        and item.voltage_level is protocol.PeripheralVoltage.INVALID
        and not item.initial_high
        for item in configuration.digital_out
    )
    assert all(not item.enabled for item in configuration.analog_in)
    assert all(not item.enabled for item in configuration.analog_out)
    assert all(
        not item.enabled and item.voltage_level is protocol.PeripheralVoltage.INVALID
        for item in configuration.pwm_in
    )
    assert all(
        not item.enabled
        and item.voltage_level is protocol.PeripheralVoltage.INVALID
        and item.initial_period_nanoseconds == 0
        and item.initial_duty_cycle_permyriad == 0
        for item in configuration.pwm_out
    )
    assert all(
        not item.enabled
        and item.bit_rate == 0
        and item.capture_limit_bytes == 0
        and item.filter_id == 0
        and item.filter_mask == 0
        for item in configuration.can
    )
    assert all(
        not item.enabled
        and item.bit_rate == 0
        and item.role is protocol.BusRole.INVALID
        and item.data_width is protocol.SPIDataWidth.INVALID
        and item.bit_order is protocol.SPIBitOrder.INVALID
        and item.clock_polarity is protocol.SPIClockPolarity.INVALID
        and item.clock_phase is protocol.SPIClockPhase.INVALID
        and item.capture_limit_bytes == 0
        for item in configuration.spi
    )
    assert all(
        not item.enabled
        and item.baud_rate == 0
        and item.electrical_mode is protocol.UARTElectricalMode.INVALID
        and item.word_length is protocol.UARTWordLength.INVALID
        and item.parity is protocol.UARTParity.INVALID
        and item.stop_bits is protocol.UARTStopBits.INVALID
        and not item.rx_enabled
        and not item.tx_enabled
        and item.capture_limit_bytes == 0
        for item in configuration.uart
    )
    assert all(
        not item.enabled
        and item.bit_rate == 0
        and item.role is protocol.BusRole.INVALID
        and item.own_address_7bit == 0
        and item.voltage_level is protocol.I2CVoltage.INVALID
        and item.pull_up is protocol.I2CPullUp.INVALID
        and item.capture_limit_bytes == 0
        for item in configuration.i2c
    )


def test_maximum_extension_preserves_representative_peripherals_but_uses_one_tick() -> None:
    maximum = maximum_extension_configuration(TEST_ID)
    representative = representative_configuration(TEST_ID)
    assert maximum.expected_tick_count == 1
    assert maximum.extension_data == bytes((index * 37) & 0xFF for index in range(255))
    assert maximum.digital_in == representative.digital_in
    assert maximum.digital_out == representative.digital_out
    assert maximum.analog_in == representative.analog_in
    assert maximum.analog_out == representative.analog_out
    assert maximum.pwm_in == representative.pwm_in
    assert maximum.pwm_out == representative.pwm_out
    assert maximum.can == representative.can
    assert maximum.spi == representative.spi
    assert maximum.uart == representative.uart
    assert maximum.i2c == representative.i2c


def test_representative_instruction_values_match_required_ticks() -> None:
    instructions = representative_instructions(TEST_ID)
    assert tuple(item.tick_number for item in instructions) == (0, 1, 2)
    assert tuple(item.high for item in instructions[0].digital_outputs) == (
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
    )
    assert tuple(item.high for item in instructions[1].digital_outputs) == (
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
        True,
        False,
    )
    assert not any(item.high for item in instructions[2].digital_outputs)
    assert tuple(item.microvolts for item in instructions[0].analog_outputs) == (
        0,
        1_000,
        5_000,
        10_000,
        15_000,
        20_000,
    )
    assert tuple(item.microvolts for item in instructions[1].analog_outputs) == (
        20_000,
        15_000,
        10_000,
        5_000,
        1_000,
        0,
    )
    assert tuple(item.microvolts for item in instructions[2].analog_outputs) == (
        3_300,
        5_000,
        12_000,
        18_000,
        20_000,
        0,
    )
    assert tuple(
        (item.period_nanoseconds, item.duty_cycle_permyriad) for item in instructions[0].pwm_outputs
    ) == ((1_000_000, 2_500), (2_000_000, 7_500))
    assert tuple(
        (item.period_nanoseconds, item.duty_cycle_permyriad) for item in instructions[1].pwm_outputs
    ) == ((500_000, 5_000), (4_000_000, 1_000))
    assert tuple(
        (item.period_nanoseconds, item.duty_cycle_permyriad) for item in instructions[2].pwm_outputs
    ) == ((10_000_000, 0), (1_000_000, 10_000))


def test_maximum_extension_result_uses_configuration_digest() -> None:
    result = expected_result(
        maximum_extension_configuration(TEST_ID), representative_instructions(TEST_ID)[0]
    )
    assert result.analog_inputs[0].microvolts == 17_374_899


def test_non_golden_instruction_result_uses_semantics_at_same_and_later_tick() -> None:
    configuration = representative_configuration(TEST_ID)
    original = representative_instructions(TEST_ID)[0]
    modified = replace(original, analog_outputs=(protocol.AnalogOutputValue(1234),) * 6)
    assert modified.tick_number == original.tick_number
    assert (
        expected_result(configuration, modified).analog_inputs[1]
        != expected_result(configuration, original).analog_inputs[1]
    )
    for instruction in (modified, replace(modified, tick_number=17)):
        result = expected_result(configuration, instruction)
        assert (
            result.analog_inputs[1].microvolts
            == instruction_semantic_digest(instruction) % 20_000_001
        )
        assert result.analog_inputs[1].microvolts != 0


def test_disabled_analogue_inputs_ignore_nonzero_instruction_digest() -> None:
    configuration = replace(
        representative_configuration(TEST_ID),
        analog_in=(protocol.AnalogInputConfig(False),) * 2,
    )
    result = expected_result(configuration, representative_instructions(TEST_ID)[1])
    assert [item.microvolts for item in result.analog_inputs] == [0, 0]
