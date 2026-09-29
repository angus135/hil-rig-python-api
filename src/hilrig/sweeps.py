"""Reusable adaptive sweep strategies over independently persisted tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal

from hilrig.workloads import LoopbackWorkloadPoint


@dataclass(frozen=True, slots=True)
class UtilizationObservation:
    """Repeated pass/fail evidence collected for one requested percentage."""

    utilization_percent: float
    outcomes: tuple[bool, ...]

    @property
    def passed_count(self) -> int:
        return sum(self.outcomes)

    @property
    def failed_count(self) -> int:
        return len(self.outcomes) - self.passed_count

    @property
    def mixed(self) -> bool:
        return self.passed_count > 0 and self.failed_count > 0


@dataclass(frozen=True, slots=True)
class UtilizationSearchResult:
    """Final reliable boundary and all evidence from one binary search."""

    highest_passing_percent: float | None
    lowest_failing_percent: float | None
    observations: tuple[UtilizationObservation, ...]

    @property
    def unstable_percentages(self) -> tuple[float, ...]:
        return tuple(item.utilization_percent for item in self.observations if item.mixed)


class UtilizationBinarySearch:
    """Repeat-aware binary search for the highest passing wire utilisation."""

    def __init__(
        self,
        *,
        minimum_percent: float = 0.0,
        maximum_percent: float = 100.0,
        resolution_percent: float = 1.0,
        repeats_per_point: int = 3,
        required_passes: int | None = None,
    ) -> None:
        self._minimum = _percentage(minimum_percent, name="minimum_percent")
        self._maximum = _percentage(maximum_percent, name="maximum_percent")
        self._resolution = _positive_decimal(
            resolution_percent,
            name="resolution_percent",
        )
        if self._minimum >= self._maximum:
            raise ValueError("minimum_percent must be less than maximum_percent")
        if not isinstance(repeats_per_point, int) or isinstance(repeats_per_point, bool):
            raise TypeError("repeats_per_point must be an integer")
        if repeats_per_point <= 0:
            raise ValueError("repeats_per_point must be positive")
        if required_passes is None:
            required_passes = repeats_per_point // 2 + 1
        if not isinstance(required_passes, int) or isinstance(required_passes, bool):
            raise TypeError("required_passes must be an integer")
        if not 1 <= required_passes <= repeats_per_point:
            raise ValueError("required_passes must be within repeats_per_point")
        self._repeats = repeats_per_point
        self._required_passes = required_passes
        self._outcomes: dict[Decimal, list[bool]] = {}
        self._classifications: dict[Decimal, bool] = {}
        self._lower_pass: Decimal | None = None
        self._upper_fail: Decimal | None = None
        self._current: Decimal | None = self._minimum
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def repeats_per_point(self) -> int:
        return self._repeats

    def next_percent(self) -> float | None:
        """Return the next requested percentage, repeated until classified."""
        return None if self._current is None else float(self._current)

    def next_point(
        self,
        template: LoopbackWorkloadPoint,
    ) -> LoopbackWorkloadPoint | None:
        """Return the next distinct test point derived from a template."""
        percentage = self.next_percent()
        if percentage is None:
            return None
        return replace(template, target_utilization_percent=percentage)

    def record(self, passed: bool, *, utilization_percent: float | None = None) -> None:
        """Record one completed run and advance only after enough repeats."""
        if not isinstance(passed, bool):
            raise TypeError("passed must be a bool")
        if self._current is None:
            raise RuntimeError("The utilization search is already complete")
        requested = (
            self._current
            if utilization_percent is None
            else _percentage(utilization_percent, name="utilization_percent")
        )
        if requested != self._current:
            raise ValueError(
                f"Expected a result for {float(self._current):g}%, "
                f"received {float(requested):g}%"
            )
        outcomes = self._outcomes.setdefault(self._current, [])
        if len(outcomes) >= self._repeats:
            raise RuntimeError("This utilization point has already been classified")
        outcomes.append(passed)
        if len(outcomes) < self._repeats:
            return
        classified_pass = sum(outcomes) >= self._required_passes
        self._classifications[self._current] = classified_pass
        self._advance(classified_pass)

    def result(self) -> UtilizationSearchResult:
        """Return the current boundary and repeat evidence."""
        observations = tuple(
            UtilizationObservation(float(percentage), tuple(outcomes))
            for percentage, outcomes in sorted(self._outcomes.items())
        )
        return UtilizationSearchResult(
            highest_passing_percent=(
                None if self._lower_pass is None else float(self._lower_pass)
            ),
            lowest_failing_percent=(
                None if self._upper_fail is None else float(self._upper_fail)
            ),
            observations=observations,
        )

    def _advance(self, classified_pass: bool) -> None:
        assert self._current is not None
        if self._current == self._minimum and self._lower_pass is None:
            if not classified_pass:
                self._upper_fail = self._minimum
                self._finish()
                return
            self._lower_pass = self._minimum
            self._current = self._maximum
            return
        if self._current == self._maximum and self._upper_fail is None:
            if classified_pass:
                self._lower_pass = self._maximum
                self._finish()
                return
            self._upper_fail = self._maximum
        elif classified_pass:
            self._lower_pass = self._current
        else:
            self._upper_fail = self._current

        assert self._lower_pass is not None
        assert self._upper_fail is not None
        if self._upper_fail - self._lower_pass <= self._resolution:
            self._finish()
            return
        midpoint = _floor_to_resolution(
            (self._lower_pass + self._upper_fail) / Decimal(2),
            self._resolution,
        )
        if midpoint <= self._lower_pass:
            midpoint = self._lower_pass + self._resolution
        if midpoint >= self._upper_fail:
            self._finish()
            return
        self._current = midpoint

    def _finish(self) -> None:
        self._current = None
        self._complete = True


def burst_sweep_points(
    template: LoopbackWorkloadPoint,
    *,
    maximum_passing_percent: float,
    headroom_percent: float,
    burst_intervals_ticks: tuple[int, ...],
) -> tuple[LoopbackWorkloadPoint, ...]:
    """Create post-boundary burst points while retaining average wire utilisation."""
    maximum = _percentage(maximum_passing_percent, name="maximum_passing_percent")
    headroom = _percentage(headroom_percent, name="headroom_percent")
    target = maximum * (Decimal(100) - headroom) / Decimal(100)
    points = []
    for interval in burst_intervals_ticks:
        if not isinstance(interval, int) or isinstance(interval, bool):
            raise TypeError("burst intervals must be integers")
        if interval <= 0:
            raise ValueError("burst intervals must be positive")
        points.append(
            replace(
                template,
                target_utilization_percent=float(target),
                burst_interval_ticks=interval,
            )
        )
    return tuple(points)


def _floor_to_resolution(value: Decimal, resolution: Decimal) -> Decimal:
    units = (value / resolution).to_integral_value(rounding=ROUND_FLOOR)
    return units * resolution


def _percentage(value: object, *, name: str) -> Decimal:
    decimal_value = _decimal(value, name=name)
    if not Decimal(0) <= decimal_value <= Decimal(100):
        raise ValueError(f"{name} must be between 0 and 100")
    return decimal_value


def _positive_decimal(value: object, *, name: str) -> Decimal:
    decimal_value = _decimal(value, name=name)
    if decimal_value <= 0:
        raise ValueError(f"{name} must be positive")
    return decimal_value


def _decimal(value: object, *, name: str) -> Decimal:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError(f"{name} must be finite")
    return decimal_value
