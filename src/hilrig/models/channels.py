"""Physical channel identities shared by configuration and instructions."""

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True, slots=True)
class Channel:
    """A stable identity for one physical rig channel."""

    kind: ChannelKind
    index: int
