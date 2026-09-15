"""Shared captured-evidence access for assertion handlers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar

from hilrig.exceptions import EvaluationError
from hilrig.results import CapturedRunIR, PWMMeasurement, TickCondition

EvidenceValue = TypeVar("EvidenceValue")


@dataclass(frozen=True, slots=True)
class EvidenceSample(Generic[EvidenceValue]):
    """One expected tick, including explicit missing or invalid evidence."""

    tick: int
    value: EvidenceValue | None
    condition: TickCondition | None
    problem_detail: int | None
    missing: bool = False

    @property
    def valid(self) -> bool:
        return not self.missing and self.value is not None

    @property
    def invalid(self) -> bool:
        return not self.missing and self.value is None


class EvaluationContext:
    """Channel-oriented evidence queries shared by all evaluator handlers."""

    def __init__(self, captured_run: CapturedRunIR) -> None:
        self.captured_run = captured_run

    def digital_point(self, *, channel: int, tick: int) -> EvidenceSample[bool]:
        sample = self.captured_run.digital_input(channel=channel).sample_at(tick)
        if sample is None:
            return _missing(tick)
        return EvidenceSample(
            tick=sample.tick,
            value=sample.value,
            condition=sample.condition,
            problem_detail=sample.problem_detail,
        )

    def digital_range(
        self,
        *,
        channel: int,
        from_tick: int,
        until_tick: int,
    ) -> Iterator[EvidenceSample[bool]]:
        samples = (
            EvidenceSample(
                tick=sample.tick,
                value=sample.value,
                condition=sample.condition,
                problem_detail=sample.problem_detail,
            )
            for sample in self.captured_run.digital_input(channel=channel).iter_samples(
                from_tick=from_tick,
                until_tick=until_tick,
            )
        )
        return _include_missing_ticks(samples, from_tick=from_tick, until_tick=until_tick)

    def analogue_point(self, *, channel: int, tick: int) -> EvidenceSample[int]:
        sample = self.captured_run.analogue_input(channel=channel).sample_at(tick)
        if sample is None:
            return _missing(tick)
        return EvidenceSample(
            tick=sample.tick,
            value=sample.microvolts,
            condition=sample.condition,
            problem_detail=sample.problem_detail,
        )

    def analogue_range(
        self,
        *,
        channel: int,
        from_tick: int,
        until_tick: int,
    ) -> Iterator[EvidenceSample[int]]:
        samples = (
            EvidenceSample(
                tick=sample.tick,
                value=sample.microvolts,
                condition=sample.condition,
                problem_detail=sample.problem_detail,
            )
            for sample in self.captured_run.analogue_input(channel=channel).iter_samples(
                from_tick=from_tick,
                until_tick=until_tick,
            )
        )
        return _include_missing_ticks(samples, from_tick=from_tick, until_tick=until_tick)

    def pwm_point(self, *, channel: int, tick: int) -> EvidenceSample[PWMMeasurement]:
        sample = self.captured_run.pwm_input(channel=channel).sample_at(tick)
        if sample is None:
            return _missing(tick)
        return EvidenceSample(
            tick=sample.tick,
            value=sample.measurement,
            condition=sample.condition,
            problem_detail=sample.problem_detail,
        )

    def pwm_range(
        self,
        *,
        channel: int,
        from_tick: int,
        until_tick: int,
    ) -> Iterator[EvidenceSample[PWMMeasurement]]:
        samples = (
            EvidenceSample(
                tick=sample.tick,
                value=sample.measurement,
                condition=sample.condition,
                problem_detail=sample.problem_detail,
            )
            for sample in self.captured_run.pwm_input(channel=channel).iter_samples(
                from_tick=from_tick,
                until_tick=until_tick,
            )
        )
        return _include_missing_ticks(samples, from_tick=from_tick, until_tick=until_tick)


def _include_missing_ticks(
    samples: Iterable[EvidenceSample[EvidenceValue]],
    *,
    from_tick: int,
    until_tick: int,
) -> Iterator[EvidenceSample[EvidenceValue]]:
    """Yield exactly one evidence item for every requested tick."""
    expected_tick = from_tick
    for sample in samples:
        if sample.tick < expected_tick:
            raise EvaluationError("Captured range results are duplicated or out of order")
        while expected_tick < sample.tick:
            yield _missing(expected_tick)
            expected_tick += 1
        yield sample
        expected_tick += 1
    while expected_tick <= until_tick:
        yield _missing(expected_tick)
        expected_tick += 1


def _missing(tick: int) -> EvidenceSample:
    return EvidenceSample(
        tick=tick,
        value=None,
        condition=None,
        problem_detail=None,
        missing=True,
    )
