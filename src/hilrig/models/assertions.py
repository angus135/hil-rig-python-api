"""Host-side assertions over digital input time-series data."""

from collections.abc import Iterator
from dataclasses import dataclass

from hilrig.models.channels import Channel
from hilrig.models.configuration import DigitalState


@dataclass(frozen=True, slots=True)
class Assertion:
    """Information shared by every host-side assertion."""

    channel: Channel


@dataclass(frozen=True, slots=True)
class DigitalInputPointAssertion(Assertion):
    """Expect a digital input state at one tick."""

    timestamp: int
    expected_state: DigitalState


@dataclass(frozen=True, slots=True)
class DigitalInputRemainHighAssertion(Assertion):
    """Expect a digital input to remain high over an inclusive tick range."""

    from_tick: int
    until_tick: int


@dataclass(frozen=True, slots=True)
class DigitalInputTransitionAssertion(Assertion):
    """Expect a digital input transition within an inclusive tick range."""

    from_state: DigitalState
    to_state: DigitalState
    from_tick: int
    until_tick: int


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
