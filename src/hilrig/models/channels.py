"""Physical channel identities shared by configuration and instructions."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class ChannelKind(str, Enum):
    """Kinds of channel currently represented by the internal model."""

    DIGITAL_INPUT = "digital_input"
    DIGITAL_OUTPUT = "digital_output"
    PWM_INPUT = "pwm_input"
    PWM_OUTPUT = "pwm_output"
    ANALOGUE_INPUT = "analogue_input"
    ANALOGUE_OUTPUT = "analogue_output"
    I2C = "i2c"
    SPI = "spi"
    UART = "uart"


CHANNEL_COUNTS: Mapping[ChannelKind, int] = MappingProxyType(
    {
        ChannelKind.DIGITAL_INPUT: 10,
        ChannelKind.DIGITAL_OUTPUT: 10,
        ChannelKind.PWM_INPUT: 2,
        ChannelKind.PWM_OUTPUT: 2,
        ChannelKind.ANALOGUE_INPUT: 2,
        ChannelKind.ANALOGUE_OUTPUT: 6,
        ChannelKind.I2C: 2,
        ChannelKind.SPI: 2,
        ChannelKind.UART: 2,
    }
)


def validate_channel_index(kind: ChannelKind, index: object) -> int:
    """Validate one zero-based physical channel index and return it."""
    if not isinstance(kind, ChannelKind):
        raise TypeError("kind must be a ChannelKind")
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("channel must be an integer")
    count = CHANNEL_COUNTS[kind]
    if not 0 <= index < count:
        valid_range = "0 or 1" if count == 2 else f"between 0 and {count - 1}"
        raise ValueError(f"{kind.value} channel must be {valid_range}")
    return index


@dataclass(frozen=True, slots=True)
class Channel:
    """A stable identity for one physical rig channel."""

    kind: ChannelKind
    index: int

    def __post_init__(self) -> None:
        validate_channel_index(self.kind, self.index)
