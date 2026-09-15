"""Protocol-neutral stimulus instructions in a user-defined HIL-RIG test."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from hilrig.models.channels import Channel


@dataclass(frozen=True, slots=True)
class Instruction:
    """Information shared by every timed stimulus instruction."""

    instruction_id: int
    timestamp: int
    channel: Channel


class DigitalOutputAction(str, Enum):
    """Supported digital output stimulus operations."""

    HIGH = "high"
    LOW = "low"
    TOGGLE = "toggle"


@dataclass(frozen=True, slots=True)
class DigitalOutputInstruction(Instruction):
    """Drive or toggle a digital output."""

    action: DigitalOutputAction


@dataclass(frozen=True, slots=True)
class PwmEnableInstruction(Instruction):
    """Enable or disable a PWM output."""

    enabled: bool


@dataclass(frozen=True, slots=True)
class PwmSetInstruction(Instruction):
    """Atomically set both PWM frequency and duty cycle."""

    frequency_hz: float
    duty_cycle: float


@dataclass(frozen=True, slots=True)
class PwmSetFrequencyInstruction(Instruction):
    """Change only a PWM output's frequency."""

    frequency_hz: float


@dataclass(frozen=True, slots=True)
class PwmSetDutyCycleInstruction(Instruction):
    """Change only a PWM output's duty cycle."""

    duty_cycle: float


@dataclass(frozen=True, slots=True)
class AnalogueOutputInstruction(Instruction):
    """Set an analogue output voltage."""

    voltage: float


@dataclass(frozen=True, slots=True)
class I2CWriteInstruction(Instruction):
    """Perform an I2C master write."""

    address: int
    data: bytes


@dataclass(frozen=True, slots=True)
class I2CReadInstruction(Instruction):
    """Perform an I2C master read."""

    address: int
    length: int


@dataclass(frozen=True, slots=True)
class I2CPreloadResponseInstruction(Instruction):
    """Preload the response returned by an I2C slave channel."""

    data: bytes


@dataclass(frozen=True, slots=True)
class SPITransferInstruction(Instruction):
    """Perform one SPI transfer and optionally record received bytes."""

    tx_data: bytes
    rx_length: int


@dataclass(frozen=True, slots=True)
class UARTWriteInstruction(Instruction):
    """Write raw bytes on a UART channel."""

    data: bytes


class InstructionList:
    """Insertion-ordered collection of stimulus instructions under construction."""

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
