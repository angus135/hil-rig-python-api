"""Static test and peripheral configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import TypeAlias

from hilrig.exceptions import ConfigurationError
from hilrig.models.channels import Channel


class FrequencyMode(Enum):
    """Externally visible execution frequency and tick granularity."""

    HZ_100 = 100
    HZ_1K = 1_000
    HZ_10K = 10_000

    @property
    def hertz(self) -> int:
        """Return the number of execution ticks per second."""
        return int(self.value)


class StartMode(str, Enum):
    """Condition that will eventually start an uploaded test."""

    IMMEDIATE = "immediate"
    HOST_COMMAND = "host_command"
    EXTERNAL_TRIGGER = "external_trigger"


class LogicVoltage(float, Enum):
    """Supported digital logic voltage domains."""

    V3_3 = 3.3
    V5 = 5.0
    V12 = 12.0
    V24 = 24.0


class DigitalState(IntEnum):
    """Logical state used by digital configuration and assertions."""

    LOW = 0
    HIGH = 1


class I2CRole(str, Enum):
    """Role assigned to an I2C channel."""

    MASTER = "master"
    SLAVE = "slave"


class I2CSpeed(Enum):
    """Supported I2C bus speeds in hertz."""

    STANDARD_100KHZ = 100_000
    FAST_400KHZ = 400_000


class Pullup(Enum):
    """Selectable I2C pull-up resistor values in ohms."""

    DISABLED = None
    OHM_1K = 1_000
    OHM_2K2 = 2_200
    OHM_4K7 = 4_700
    OHM_10K = 10_000


class SPIRole(str, Enum):
    """Role assigned to an SPI channel."""

    MASTER = "master"
    SLAVE = "slave"


class SPIBaud(Enum):
    """Supported approximate SPI bit rates in bits per second."""

    BAUD_45MBIT = 45_000_000
    BAUD_22M5BIT = 22_500_000
    BAUD_11M25BIT = 11_250_000
    BAUD_5M625BIT = 5_625_000
    BAUD_2M813BIT = 2_813_000
    BAUD_1M406BIT = 1_406_000
    BAUD_703KBIT = 703_000
    BAUD_352KBIT = 352_000


class SPISize(IntEnum):
    """Supported SPI frame sizes in bits."""

    SIZE_8BIT = 8
    SIZE_16BIT = 16


class SPIMode(IntEnum):
    """SPI clock polarity and phase combinations."""

    MODE_0 = 0
    MODE_1 = 1
    MODE_2 = 2
    MODE_3 = 3


class SPIFirst(str, Enum):
    """Bit order within each SPI frame."""

    MSB = "msb"
    LSB = "lsb"


class UARTMode(str, Enum):
    """Electrical interface selected for a UART channel."""

    TTL_3V3 = "ttl_3v3"
    TTL_5V0 = "ttl_5v0"
    RS232 = "rs232"


class UARTParity(str, Enum):
    """Supported UART parity modes."""

    NONE = "none"
    ODD = "odd"
    EVEN = "even"


class UARTLengthBits(IntEnum):
    """Supported UART word lengths in bits."""

    EIGHT = 8
    NINE = 9


class UARTStopBits(IntEnum):
    """Supported UART stop-bit counts."""

    ONE = 1
    TWO = 2


@dataclass(frozen=True, slots=True)
class DigitalInputConfiguration:
    """Static configuration for a digital input channel."""

    voltage: LogicVoltage


@dataclass(frozen=True, slots=True)
class DigitalOutputConfiguration:
    """Static configuration for a digital output channel."""

    voltage: LogicVoltage
    initial_state: DigitalState


@dataclass(frozen=True, slots=True)
class PwmInputConfiguration:
    """Static configuration for a PWM capture channel."""

    voltage: LogicVoltage


@dataclass(frozen=True, slots=True)
class PwmOutputConfiguration:
    """Static configuration for a PWM output channel."""

    voltage: LogicVoltage
    initial_frequency_hz: float
    initial_duty_cycle: float
    initially_enabled: bool


@dataclass(frozen=True, slots=True)
class AnalogueInputConfiguration:
    """Marker declaring that an analogue input channel is used by the test."""


@dataclass(frozen=True, slots=True)
class AnalogueOutputConfiguration:
    """Marker declaring that an analogue output channel is used by the test."""


@dataclass(frozen=True, slots=True)
class I2CConfiguration:
    """Static configuration for an I2C channel."""

    role: I2CRole
    speed: I2CSpeed
    logic_voltage: LogicVoltage
    pullup: Pullup
    own_address: int | None = None


@dataclass(frozen=True, slots=True)
class SPIConfiguration:
    """Static configuration for an SPI channel."""

    role: SPIRole
    baud: SPIBaud
    data_size: SPISize
    mode: SPIMode
    first_bit: SPIFirst


@dataclass(frozen=True, slots=True)
class UARTConfiguration:
    """Static configuration for a UART channel."""

    mode: UARTMode
    baud_hz: int
    parity: UARTParity
    length: UARTLengthBits
    stop: UARTStopBits


PeripheralConfiguration: TypeAlias = (
    AnalogueInputConfiguration
    | AnalogueOutputConfiguration
    | DigitalInputConfiguration
    | DigitalOutputConfiguration
    | PwmInputConfiguration
    | PwmOutputConfiguration
    | I2CConfiguration
    | SPIConfiguration
    | UARTConfiguration
)


class Configuration:
    """Test-level settings and per-channel static configurations."""

    def __init__(
        self,
        *,
        frequency_mode: FrequencyMode = FrequencyMode.HZ_1K,
        start_mode: StartMode = StartMode.IMMEDIATE,
    ) -> None:
        self._frequency_mode = frequency_mode
        self._start_mode = start_mode
        self._channel_configurations: dict[Channel, PeripheralConfiguration] = {}

    @property
    def frequency_mode(self) -> FrequencyMode:
        """Return the configured execution frequency."""
        return self._frequency_mode

    @property
    def start_mode(self) -> StartMode:
        """Return the stored test start mode."""
        return self._start_mode

    @property
    def channel_configurations(self) -> Mapping[Channel, PeripheralConfiguration]:
        """Return a read-only mapping of channel identities to configurations."""
        return MappingProxyType(self._channel_configurations)

    def for_channel(self, channel: Channel) -> PeripheralConfiguration | None:
        """Return one channel's configuration, if configured."""
        return self._channel_configurations.get(channel)

    def _configure_test(
        self,
        *,
        frequency_mode: FrequencyMode,
        start_mode: StartMode,
    ) -> None:
        self._frequency_mode = frequency_mode
        self._start_mode = start_mode

    def _configure_channel(
        self,
        channel: Channel,
        configuration: PeripheralConfiguration,
    ) -> None:
        if channel in self._channel_configurations:
            raise ConfigurationError(
                f"{channel.kind.value} channel {channel.index} is already configured"
            )
        self._channel_configurations[channel] = configuration
