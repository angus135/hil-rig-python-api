"""Validation and compilation of the internal test model."""

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
    AssertionList,
    DigitalInputPointAssertion,
    DigitalInputRemainHighAssertion,
    DigitalInputRemainLowAssertion,
    DigitalInputTransitionAssertion,
    PointAssertion,
    PwmInputDutyCycleNearAssertion,
    PwmInputDutyCycleRemainWithinAssertion,
    PwmInputFrequencyNearAssertion,
    PwmInputFrequencyRemainWithinAssertion,
    PwmInputPeriodNearAssertion,
    PwmInputWaveformNearAssertion,
    RangeAssertion,
)
from hilrig.models.configuration import Configuration
from hilrig.models.execution import (
    CompiledAssertion,
    CompiledConfiguration,
    CompiledInstruction,
    CompiledTestIR,
    IRScalar,
    TimeSlot,
    immutable_fields,
)
from hilrig.models.instructions import (
    AnalogueOutputInstruction,
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
}

_POST_TEST_SETTLING_SECONDS = 1


def compile_test(
    *,
    test_id: int,
    name: str,
    configuration: Configuration,
    instructions: InstructionList,
    assertions: AssertionList,
) -> CompiledTestIR:
    """Validate and copy a test definition into an immutable intermediate form."""
    _validate_instructions(instructions)
    _validate_assertions(assertions)

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
    compiled_instructions = tuple(_compile_instruction(item) for item in ordered_instructions)
    compiled_assertions = tuple(_compile_assertion(item) for item in assertions)
    expected_tick_count = _expected_tick_count(
        instructions=ordered_instructions,
        assertions=assertions,
        frequency_hz=configuration.frequency_mode.hertz,
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
        assertions=compiled_assertions,
        time_slots=time_slots,
    )


def _expected_tick_count(
    *,
    instructions: tuple[Instruction, ...],
    assertions: AssertionList,
    frequency_hz: int,
) -> int:
    """Return ticks 0 through the last event plus one second, inclusively."""
    latest_relevant_tick = max(
        (instruction.timestamp for instruction in instructions),
        default=0,
    )
    for assertion in assertions:
        latest_relevant_tick = max(latest_relevant_tick, _assertion_end_tick(assertion))

    settling_ticks = frequency_hz * _POST_TEST_SETTLING_SECONDS
    return latest_relevant_tick + settling_ticks + 1


def _assertion_end_tick(assertion: Assertion) -> int:
    if isinstance(assertion, PointAssertion):
        return assertion.timestamp
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


def _compile_instruction(instruction: Instruction) -> CompiledInstruction:
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
    )


def _compile_assertion(assertion: Assertion) -> CompiledAssertion:
    operation = _ASSERTION_OPERATIONS.get(type(assertion))
    if operation is None:
        raise ValidationError(
            f"No human-readable representation is defined for {type(assertion).__name__}"
        )
    excluded = {"assertion_id", "channel"}
    arguments = {
        ("tick" if field.name == "timestamp" else field.name): _ir_value(
            getattr(assertion, field.name)
        )
        for field in fields(assertion)
        if field.name not in excluded
    }
    return CompiledAssertion(
        assertion_id=assertion.assertion_id,
        peripheral=assertion.channel.kind.value,
        channel=assertion.channel.index,
        assertion=operation,
        arguments=immutable_fields(arguments),
    )


def _ir_value(value: object) -> IRScalar:
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValidationError(f"Unsupported intermediate-representation value: {type(value).__name__}")


def _validate_instructions(instructions: InstructionList) -> None:
    for expected_id, instruction in enumerate(instructions):
        if instruction.instruction_id != expected_id:
            raise ValidationError("Instruction IDs must be sequential from zero")
        _validate_tick(instruction.timestamp, label="Instruction timestamp")


def _validate_assertions(assertions: AssertionList) -> None:
    for expected_id, assertion in enumerate(assertions):
        if assertion.assertion_id != expected_id:
            raise ValidationError("Assertion IDs must be sequential from zero")
        if type(assertion) not in _ASSERTION_OPERATIONS:
            raise ValidationError(f"Unsupported assertion type: {type(assertion).__name__}")
        if isinstance(assertion, PointAssertion):
            _validate_tick(assertion.timestamp, label="Assertion timestamp")
        elif isinstance(assertion, RangeAssertion):
            _validate_tick(assertion.from_tick, label="Assertion start tick")
            _validate_tick(assertion.until_tick, label="Assertion end tick")
            if assertion.from_tick > assertion.until_tick:
                raise TimingError("Assertion start tick must not be after its end tick")
        else:
            raise ValidationError(f"Unsupported assertion type: {type(assertion).__name__}")


def _validate_tick(tick: object, *, label: str) -> None:
    if not isinstance(tick, int) or isinstance(tick, bool):
        raise TimingError(f"{label} must be an integer tick")
    if tick < 0:
        raise TimingError(f"{label} must be non-negative")
