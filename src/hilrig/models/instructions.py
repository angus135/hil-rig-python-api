"""Timed actions in a user-defined HIL-RIG test."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum

from hilrig.models.channels import Channel


@dataclass(frozen=True, slots=True)
class Instruction:
    """Information shared by every timed instruction."""

    timestamp: int
    channel: Channel


class DigitalLevel(IntEnum):
    """Logical level driven by a digital output."""

    LOW = 0
    HIGH = 1


@dataclass(frozen=True, slots=True)
class DigitalOutputInstruction(Instruction):
    """Set a digital output to a logical level."""

    level: DigitalLevel


class InstructionList:
    """Insertion-ordered collection of instructions under construction."""

    def __init__(self) -> None:
        self._items: list[Instruction] = []

    def __iter__(self) -> Iterator[Instruction]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def _append(self, instruction: Instruction) -> None:
        self._items.append(instruction)

    def ordered(self) -> tuple[Instruction, ...]:
        """Return instructions sorted stably by timestamp."""
        return tuple(sorted(self._items, key=lambda instruction: instruction.timestamp))

    def group_by_timestamp(self) -> dict[int, tuple[Instruction, ...]]:
        """Return chronological instruction groups keyed by timestamp."""
        grouped: dict[int, list[Instruction]] = {}
        for instruction in self.ordered():
            grouped.setdefault(instruction.timestamp, []).append(instruction)
        return {timestamp: tuple(group) for timestamp, group in grouped.items()}
