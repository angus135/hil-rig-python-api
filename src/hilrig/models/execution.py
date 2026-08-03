"""Immutable compiler output organised by timestamp."""

from dataclasses import dataclass

from hilrig.models.configuration import Configuration
from hilrig.models.instructions import Instruction


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """All instructions scheduled for one timestamp."""

    timestamp: int
    instructions: tuple[Instruction, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Preliminary ordered view of a test; no IDC representation is implied."""

    test_id: int
    name: str
    configuration: Configuration
    time_slots: tuple[TimeSlot, ...]
