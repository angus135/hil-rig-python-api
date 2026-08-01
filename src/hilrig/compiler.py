"""Validation and compilation of the internal test model."""

from hilrig.exceptions import TimingError, ValidationError
from hilrig.models.configuration import Configuration
from hilrig.models.execution import ExecutionPlan, TimeSlot
from hilrig.models.instructions import Instruction, InstructionList


def compile_test(
    *,
    name: str,
    configuration: Configuration,
    instructions: InstructionList,
) -> ExecutionPlan:
    """Compile a test definition into an immutable, timestamp-oriented plan."""
    _validate_instructions(instructions)

    time_slots = tuple(
        TimeSlot(timestamp=timestamp, instructions=group)
        for timestamp, group in instructions.group_by_timestamp().items()
    )
    return ExecutionPlan(name=name, configuration=configuration, time_slots=time_slots)


def _validate_instructions(instructions: InstructionList) -> None:
    if not instructions:
        raise ValidationError("A test must contain at least one instruction")

    for instruction in instructions:
        _validate_timestamp(instruction)


def _validate_timestamp(instruction: Instruction) -> None:
    timestamp = instruction.timestamp
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise TimingError("Instruction timestamps must be integer ticks")
    if timestamp < 0:
        raise TimingError("Instruction timestamps must be non-negative")
