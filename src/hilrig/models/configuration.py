"""Static configuration applied before a HIL-RIG test executes."""

from dataclasses import dataclass
from enum import Enum


class FrequencyMode(Enum):
    """Externally visible execution frequency and tick granularity."""

    HZ_100 = 100
    KHZ_1 = 1_000
    KHZ_10 = 10_000


@dataclass(frozen=True, slots=True)
class Configuration:
    """Protocol-neutral, immutable test configuration."""

    frequency_mode: FrequencyMode = FrequencyMode.KHZ_1
