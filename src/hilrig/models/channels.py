"""Physical channel identities shared by configuration and instructions."""

from dataclasses import dataclass
from enum import Enum


class ChannelKind(str, Enum):
    """Kinds of channel currently represented by the internal model."""

    DIGITAL_OUTPUT = "digital_output"


@dataclass(frozen=True, slots=True)
class Channel:
    """A stable identity for one physical rig channel."""

    kind: ChannelKind
    index: int
