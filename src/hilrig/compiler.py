"""Validation and compilation of the internal test model."""

from collections.abc import Mapping, Sequence
from dataclasses import fields
from enum import Enum

from hilrig.exceptions import TimingError, ValidationError
from hilrig.models.assertions import (
    AnalogueInputNearAssertion,
    AnalogueInputRemainAboveAssertion,
    AnalogueInputRemainBelowAssertion,
    AnalogueInputRemainWithinAssertion,
    AnalogueInputWithinAssertion,
    Assertion,
    AssertionGroupDefinition,
    AssertionList,
    CANReceiveAssertion,
    DigitalInputPointAssertion,
    DigitalInputRemainHighAssertion,
    DigitalInputRemainLowAssertion,
    DigitalInputTransitionAssertion,
    I2CReceiveAssertion,
    PointAssertion,
    PwmInputDutyCycleNearAssertion,
    PwmInputDutyCycleRemainWithinAssertion,
    PwmInputFrequencyNearAssertion,
    PwmInputFrequencyRemainWithinAssertion,
    PwmInputPeriodNearAssertion,
    PwmInputWaveformNearAssertion,
    RangeAssertion,
    SPIReceiveAssertion,
    UARTReceiveAssertion,
)
from hilrig.models.channels import Channel, validate_channel_index
from hilrig.models.configuration import Configuration, configuration_type_for
from hilrig.models.execution import (
    CompiledAssertion,
    CompiledAssertionGroup,
    CompiledConfiguration,
    CompiledInstruction,
    CompiledTestIR,
    IRScalar,
    TimeSlot,
    immutable_fields,
)
from hilrig.models.instructions import (
    AnalogueOutputInstruction,
    CANTransmitInstruction,
    DigitalOutputInstruction,
    I2CPreloadResponseInstruction,
    I2CReadInstruction,
    I2CWriteInstruction,
    Instruction,
    InstructionList,
    PwmEnableInstruction,
    PwmSetDutyCycleInstruction,
    PwmSetFrequencyInstruction,
    PwmSetInstruction,
    SPITransferInstruction,
    UARTWriteInstruction,
)

_INSTRUCTION_OPERATIONS: dict[type[Instruction], str] = {
    DigitalOutputInstruction: "set_state",
    PwmEnableInstruction: "set_enabled",
    PwmSetInstruction: "set",
    PwmSetFrequencyInstruction: "set_frequency",
    PwmSetDutyCycleInstruction: "set_duty_cycle",
    AnalogueOutputInstruction: "set_voltage",
    I2CWriteInstruction: "write",
    I2CReadInstruction: "read",
    I2CPreloadResponseInstruction: "preload_response",
    SPITransferInstruction: "transfer",
    UARTWriteInstruction: "write",
    CANTransmitInstruction: "transmit",
}

_ASSERTION_OPERATIONS: dict[type[Assertion], str] = {
    DigitalInputPointAssertion: "state_at_tick",
    DigitalInputRemainHighAssertion: "remain_high",
    DigitalInputRemainLowAssertion: "remain_low",
    DigitalInputTransitionAssertion: "transition",
    PwmInputPeriodNearAssertion: "period_near",
    PwmInputFrequencyNearAssertion: "frequency_near",
    PwmInputDutyCycleNearAssertion: "duty_cycle_near",
    PwmInputWaveformNearAssertion: "waveform_near",
    PwmInputFrequencyRemainWithinAssertion: "frequency_remain_within",
    PwmInputDutyCycleRemainWithinAssertion: "duty_cycle_remain_within",
    AnalogueInputNearAssertion: "near",
    AnalogueInputWithinAssertion: "within",
    AnalogueInputRemainWithinAssertion: "remain_within",
    AnalogueInputRemainAboveAssertion: "remain_above",
    AnalogueInputRemainBelowAssertion: "remain_below",
    UARTReceiveAssertion: "receive",
    SPIReceiveAssertion: "receive",
    I2CReceiveAssertion: "receive",
    CANReceiveAssertion: "receive",
}

_POST_TEST_SETTLING_SECONDS = 1
_MAX_EXPECTED_TICK_COUNT_EXCLUSIVE = 1_000_000


def compile_test(
    *,
    test_id: int,
    name: str,
    configuration: Configuration,
    instructions: InstructionList,
    assertions: AssertionList,
    assertion_groups: Sequence[AssertionGroupDefinition],
    channel_names: Mapping[Channel, str],
    instruction_group_ids: Mapping[int, int] | None = None,
) -> CompiledTestIR:
    """Validate and copy a test definition into an immutable intermediate form."""
    _validate_configuration_channels(configuration)
    _validate_instructions(instructions, configuration=configuration)
    _validate_assertions(assertions, configuration=configuration)
    _validate_assertion_groups(assertions, assertion_groups)

    ordered_instructions = tuple(
        sorted(
            instructions,
            key=lambda instruction: (instruction.timestamp, instruction.instruction_id),
        )
    )
    time_slots = _build_time_slots(ordered_instructions)

    compiled_configurations = tuple(
        _compile_configuration(channel.kind.value, channel.index, channel_configuration)
        for channel, channel_configuration in sorted(
            configuration.channel_configurations.items(),
            key=lambda item: (item[0].kind.value, item[0].index),
        )
    )
    instruction_groups = {} if instruction_group_ids is None else dict(instruction_group_ids)
    _validate_instruction_groups(instructions, assertion_groups, instruction_groups)
    compiled_instructions = tuple(
        _compile_instruction(
            item,
            group_id=instruction_groups.get(item.instruction_id),
            subject_name=channel_names.get(
                item.channel,
                f"{item.channel.kind.value}[{item.channel.index}]",
            ),
        )
        for item in ordered_instructions
    )
    compiled_assertion_groups = tuple(
        CompiledAssertionGroup(group_id=item.group_id, name=item.name) for item in assertion_groups
    )
    compiled_assertions = tuple(
        _compile_assertion(
            item,
            subject_name=channel_names.get(
                item.channel,
                f"{item.channel.kind.value}[{item.channel.index}]",
            ),
        )
        for item in assertions
    )
    expected_tick_count = _expected_tick_count(
        instructions=ordered_instructions,
        assertions=assertions,
        frequency_hz=configuration.frequency_mode.hertz,
    )
    if expected_tick_count >= _MAX_EXPECTED_TICK_COUNT_EXCLUSIVE:
        raise ValidationError(
            "Expected tick count must be less than 1000000 for protocol compatibility"
        )

    return CompiledTestIR(
        test_id=test_id,
        name=name,
        frequency_mode=configuration.frequency_mode.name,
        frequency_hz=configuration.frequency_mode.hertz,
        expected_tick_count=expected_tick_count,
        start_mode=configuration.start_mode.name,
        configurations=compiled_configurations,
        instructions=compiled_instructions,
        assertion_groups=compiled_assertion_groups,
        assertions=compiled_assertions,
        time_slots=time_slots,
    )


def _expected_tick_count(
    *,
    instructions: tuple[Instruction, ...],
    assertions: AssertionList,
    frequency_hz: int,
) -> int:
    """Return the half-open result count through the last event plus one second."""
    latest_relevant_end = max(
        (instruction.timestamp + 1 for instruction in instructions),
        default=0,
    )
    for assertion in assertions:
        latest_relevant_end = max(latest_relevant_end, _assertion_end_tick(assertion))

    settling_ticks = frequency_hz * _POST_TEST_SETTLING_SECONDS
    return latest_relevant_end + settling_ticks


def _assertion_end_tick(assertion: Assertion) -> int:
    if isinstance(assertion, PointAssertion):
        return assertion.timestamp + 1
    if isinstance(assertion, RangeAssertion):
        return assertion.until_tick
    raise ValidationError(f"Unsupported assertion type: {type(assertion).__name__}")


def _build_time_slots(instructions: tuple[Instruction, ...]) -> tuple[TimeSlot, ...]:
    grouped: dict[int, list[Instruction]] = {}
    for instruction in instructions:
        grouped.setdefault(instruction.timestamp, []).append(instruction)
    return tuple(
        TimeSlot(timestamp=tick, instructions=tuple(items)) for tick, items in grouped.items()
    )


def _compile_configuration(
    peripheral: str,
    channel: int,
    configuration: object,
) -> CompiledConfiguration:
    return CompiledConfiguration(
        peripheral=peripheral,
        channel=channel,
        parameters=immutable_fields(
            {
                field.name: _ir_value(getattr(configuration, field.name))
                for field in fields(configuration)
            }
        ),
    )


def _compile_instruction(
    instruction: Instruction,
    *,
    group_id: int | None,
    subject_name: str,
) -> CompiledInstruction:
    operation = _INSTRUCTION_OPERATIONS.get(type(instruction))
    if operation is None:
        raise ValidationError(
            f"No intermediate representation is defined for {type(instruction).__name__}"
        )
    excluded = {"instruction_id", "timestamp", "channel"}
    arguments = {
        field.name: _ir_value(getattr(instruction, field.name))
        for field in fields(instruction)
        if field.name not in excluded
    }
    return CompiledInstruction(
        instruction_id=instruction.instruction_id,
        tick=instruction.timestamp,
        peripheral=instruction.channel.kind.value,
        channel=instruction.channel.index,
        operation=operation,
        arguments=immutable_fields(arguments),
        group_id=group_id,
        subject_name=subject_name,
    )


def _compile_assertion(assertion: Assertion, *, subject_name: str) -> CompiledAssertion:
    operation = _ASSERTION_OPERATIONS.get(type(assertion))
    if operation is None:
        raise ValidationError(
            f"No human-readable representation is defined for {type(assertion).__name__}"
        )
    excluded = {"assertion_id", "channel", "group_id"}
    arguments = {
        ("tick" if field.name == "timestamp" else field.name): _ir_value(
            getattr(assertion, field.name)
        )
        for field in fields(assertion)
        if field.name not in excluded
    }
    return CompiledAssertion(
        assertion_id=assertion.assertion_id,
        group_id=assertion.group_id,
        subject_name=subject_name,
        peripheral=assertion.channel.kind.value,
        channel=assertion.channel.index,
        assertion=operation,
        arguments=immutable_fields(arguments),
    )


def _validate_assertion_groups(
    assertions: AssertionList,
    assertion_groups: Sequence[AssertionGroupDefinition],
) -> None:
    if not assertion_groups:
        raise ValidationError("A test must define the default assertion group")
    group_ids = {group.group_id for group in assertion_groups}
    if len(group_ids) != len(assertion_groups):
        raise ValidationError("Assertion group IDs must be unique")
    for expected_id, group in enumerate(assertion_groups):
        if group.group_id != expected_id:
            raise ValidationError("Assertion group IDs must be sequential from zero")
        if not isinstance(group.name, str) or not group.name.strip():
            raise ValidationError("Assertion group names must be non-empty")
    for assertion in assertions:
        if assertion.group_id not in group_ids:
            raise ValidationError(
                f"Assertion {assertion.assertion_id} references unknown group {assertion.group_id}"
            )


def _validate_instruction_groups(
    instructions: InstructionList,
    assertion_groups: Sequence[AssertionGroupDefinition],
    instruction_group_ids: Mapping[int, int],
) -> None:
    instruction_ids = {instruction.instruction_id for instruction in instructions}
    group_ids = {group.group_id for group in assertion_groups}
    for instruction_id, group_id in instruction_group_ids.items():
        if instruction_id not in instruction_ids:
            raise ValidationError(f"Unknown grouped instruction ID {instruction_id}")
        if group_id not in group_ids:
            raise ValidationError(
                f"Instruction {instruction_id} references unknown group {group_id}"
            )


def _ir_value(value: object) -> IRScalar:
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError(f"Unsupported intermediate-representation value: {type(value).__name__}")


def _validate_instructions(
    instructions: InstructionList,
    *,
    configuration: Configuration,
) -> None:
    for expected_id, instruction in enumerate(instructions):
        if instruction.instruction_id != expected_id:
            raise ValidationError("Instruction IDs must be sequential from zero")
        _validate_model_channel(instruction.channel, label="Instruction")
        _validate_channel_is_configured(
            instruction.channel,
            configuration=configuration,
            label="Instruction",
        )
        _validate_tick(instruction.timestamp, label="Instruction timestamp")


def _validate_assertions(
    assertions: AssertionList,
    *,
    configuration: Configuration,
) -> None:
    for expected_id, assertion in enumerate(assertions):
        if assertion.assertion_id != expected_id:
            raise ValidationError("Assertion IDs must be sequential from zero")
        _validate_model_channel(assertion.channel, label="Assertion")
        _validate_channel_is_configured(
            assertion.channel,
            configuration=configuration,
            label="Assertion",
        )
        if type(assertion) not in _ASSERTION_OPERATIONS:
            raise ValidationError(f"Unsupported assertion type: {type(assertion).__name__}")
        if isinstance(assertion, PointAssertion):
            _validate_tick(assertion.timestamp, label="Assertion timestamp")
        elif isinstance(assertion, RangeAssertion):
            _validate_tick(assertion.from_tick, label="Assertion start tick")
            _validate_tick(assertion.until_tick, label="Assertion end tick")
            if assertion.from_tick >= assertion.until_tick:
                raise TimingError("Assertion start tick must be before its end tick")
        else:
            raise ValidationError(f"Unsupported assertion type: {type(assertion).__name__}")


def _validate_configuration_channels(configuration: Configuration) -> None:
    for channel in configuration.channel_configurations:
        _validate_model_channel(channel, label="Configuration")


def _validate_model_channel(channel: object, *, label: str) -> None:
    if not isinstance(channel, Channel):
        raise ValidationError(f"{label} channel must be a Channel")
    try:
        validate_channel_index(channel.kind, channel.index)
    except (TypeError, ValueError) as error:
        raise ValidationError(f"{label} uses an invalid channel: {error}") from error


def _validate_channel_is_configured(
    channel: Channel,
    *,
    configuration: Configuration,
    label: str,
) -> None:
    channel_configuration = configuration.for_channel(channel)
    expected_type = configuration_type_for(channel.kind)
    if not isinstance(channel_configuration, expected_type):
        raise ValidationError(
            f"{label} references unconfigured {channel.kind.value} channel {channel.index}"
        )


def _validate_tick(tick: object, *, label: str) -> None:
    if not isinstance(tick, int) or isinstance(tick, bool):
        raise TimingError(f"{label} must be an integer tick")
    if tick < 0:
        raise TimingError(f"{label} must be non-negative")
