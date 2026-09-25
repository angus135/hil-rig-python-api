"""Host-side assertions over captured peripheral time-series data."""

from collections.abc import Iterator
from dataclasses import dataclass

from hilrig.models.channels import Channel
from hilrig.models.configuration import DigitalState

DEFAULT_ASSERTION_GROUP_ID = 0
DEFAULT_ASSERTION_GROUP_NAME = "Assertions"


@dataclass(frozen=True, slots=True)
class AssertionGroupDefinition:
    """One named collection of related host-side assertions."""

    group_id: int
    name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Assertion:
    """Information shared by every host-side assertion."""

    assertion_id: int
    channel: Channel
    group_id: int = DEFAULT_ASSERTION_GROUP_ID


@dataclass(frozen=True, slots=True)
class PointAssertion(Assertion):
    """Information shared by assertions evaluated at one tick."""

    timestamp: int


@dataclass(frozen=True, slots=True)
class RangeAssertion(Assertion):
    """Information shared by assertions evaluated over a half-open tick range."""

    from_tick: int
    until_tick: int


@dataclass(frozen=True, slots=True)
class DigitalInputPointAssertion(PointAssertion):
    """Expect a digital input state at one tick."""

    expected_state: DigitalState


@dataclass(frozen=True, slots=True)
class DigitalInputRemainHighAssertion(RangeAssertion):
    """Expect a digital input to remain high over a half-open tick range."""


@dataclass(frozen=True, slots=True)
class DigitalInputRemainLowAssertion(RangeAssertion):
    """Expect a digital input to remain low over a half-open tick range."""


@dataclass(frozen=True, slots=True)
class DigitalInputTransitionAssertion(RangeAssertion):
    """Expect a digital input transition within a half-open tick range."""

    from_state: DigitalState
    to_state: DigitalState


@dataclass(frozen=True, slots=True)
class PwmInputPeriodNearAssertion(PointAssertion):
    """Expect a PWM period to be near a target at one tick."""

    period_ns: int
    tolerance_ns: int


@dataclass(frozen=True, slots=True)
class PwmInputFrequencyNearAssertion(PointAssertion):
    """Expect a PWM frequency to be near a target at one tick."""

    frequency_hz: float
    tolerance_hz: float


@dataclass(frozen=True, slots=True)
class PwmInputDutyCycleNearAssertion(PointAssertion):
    """Expect a PWM duty cycle to be near a target at one tick."""

    duty_cycle: float
    duty_cycle_tolerance: float


@dataclass(frozen=True, slots=True)
class PwmInputWaveformNearAssertion(PointAssertion):
    """Expect PWM frequency and duty cycle to be near targets at one tick."""

    frequency_hz: float
    frequency_tolerance_hz: float
    duty_cycle: float
    duty_cycle_tolerance: float


@dataclass(frozen=True, slots=True)
class PwmInputFrequencyRemainWithinAssertion(RangeAssertion):
    """Expect PWM frequency to remain within a band over a half-open tick range."""

    minimum_hz: float
    maximum_hz: float


@dataclass(frozen=True, slots=True)
class PwmInputDutyCycleRemainWithinAssertion(RangeAssertion):
    """Expect PWM duty cycle to remain within a band over a half-open tick range."""

    minimum_duty_cycle: float
    maximum_duty_cycle: float


@dataclass(frozen=True, slots=True)
class AnalogueInputNearAssertion(PointAssertion):
    """Expect an analogue voltage to be near a target at one tick."""

    target_uv: int
    tolerance_uv: int


@dataclass(frozen=True, slots=True)
class AnalogueInputWithinAssertion(PointAssertion):
    """Expect an analogue voltage to be within an inclusive band at one tick."""

    minimum_uv: int
    maximum_uv: int


@dataclass(frozen=True, slots=True)
class AnalogueInputRemainWithinAssertion(RangeAssertion):
    """Expect an analogue voltage to remain within a band over a half-open tick range."""

    minimum_uv: int
    maximum_uv: int


@dataclass(frozen=True, slots=True)
class AnalogueInputRemainAboveAssertion(RangeAssertion):
    """Expect an analogue voltage to remain above a threshold."""

    threshold_uv: int


@dataclass(frozen=True, slots=True)
class AnalogueInputRemainBelowAssertion(RangeAssertion):
    """Expect an analogue voltage to remain below a threshold."""

    threshold_uv: int


@dataclass(frozen=True, slots=True)
class CommunicationReceiveAssertion(RangeAssertion):
    """Expect specific payload bytes received on a communication peripheral within a tick range."""

    expected_payload: bytes


@dataclass(frozen=True, slots=True)
class UARTReceiveAssertion(CommunicationReceiveAssertion):
    """Expect specific UART payload bytes received within a tick range."""


@dataclass(frozen=True, slots=True)
class SPIReceiveAssertion(CommunicationReceiveAssertion):
    """Expect specific SPI payload bytes received within a tick range."""


@dataclass(frozen=True, slots=True)
class I2CReceiveAssertion(CommunicationReceiveAssertion):
    """Expect specific I2C payload bytes received within a tick range."""


@dataclass(frozen=True, slots=True)
class CANReceiveAssertion(CommunicationReceiveAssertion):
    """Expect specific CAN frame payload bytes received within a tick range."""


class AssertionList:
    """Insertion-ordered collection of host-side assertions."""

    def __init__(self) -> None:
        self._items: list[Assertion] = []

    def __iter__(self) -> Iterator[Assertion]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def _append(self, assertion: Assertion) -> None:
        self._items.append(assertion)
