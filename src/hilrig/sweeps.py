"""Reusable adaptive sweep strategies and campaign orchestrator for HIL-RIG characterization."""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any

from hilrig.workloads import (
    ExposureClass,
    LoopbackConfiguration,
    LoopbackWorkloadPoint,
    check_workload_admissibility,
    workload_point_for_exposure,
)


class PointClassification(str, Enum):
    """Authoritative classification for a tested point."""

    STABLE_PASS = "stable_pass"
    STABLE_FAIL = "stable_fail"
    UNSTABLE = "unstable"
    INCONCLUSIVE = "inconclusive"
    COMPILE_TIME_INFEASIBLE = "compile_time_infeasible"


class FailureDomain(str, Enum):
    """First limiting domain identified for a failed run."""

    COMPILATION = "1_workload_compilation"
    UPLOAD = "2_instruction_upload"
    SUPPLY = "3_instruction_supply"
    BUFFER = "4_peripheral_queue_buffer"
    ISR_DEADLINE = "5_execution_isr_deadline"
    INTEGRITY = "6_receive_timing_integrity"
    STORAGE_NAND = "7_result_buffering_nand"
    TRANSPORT_HOST = "8_result_transport_host"


class SweepKind(str, Enum):
    """The high-level category of loopback sweep."""

    UTILIZATION_CEILING = "sweep_1_utilization_ceiling"
    BURSTINESS = "sweep_2_burstiness"


class SweepStage(str, Enum):
    """Execution stage within a sweep campaign cell."""

    COARSE_DISCOVERY = "coarse_discovery"
    BRACKET_REFINEMENT = "bracket_refinement"
    BOUNDARY_CONFIRMATION = "boundary_confirmation"
    SOAK_VALIDATION = "soak_validation"


@dataclass(frozen=True, slots=True)
class SweepRunOutcome:
    """Detailed evidence from one concrete test run."""

    config_id: int
    frequency_hz: int
    point: LoopbackWorkloadPoint
    stage: SweepStage
    passed: bool
    failure_domain: FailureDomain | None = None
    failure_reason: str | None = None
    first_fault_tick: int | None = None
    maximum_isr_cycles: int | None = None
    deadline_cycles: int | None = None
    isr_margin_cycles: int | None = None
    actual_utilization_percent: float | None = None
    payload_bytes: int | None = None
    duration_s: float | None = None
    run_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config_id,
            "frequency_hz": self.frequency_hz,
            "point": self.point.to_dict(),
            "stage": self.stage.value,
            "passed": self.passed,
            "failure_domain": None if self.failure_domain is None else self.failure_domain.value,
            "failure_reason": self.failure_reason,
            "first_fault_tick": self.first_fault_tick,
            "maximum_isr_cycles": self.maximum_isr_cycles,
            "deadline_cycles": self.deadline_cycles,
            "isr_margin_cycles": self.isr_margin_cycles,
            "actual_utilization_percent": self.actual_utilization_percent,
            "payload_bytes": self.payload_bytes,
            "duration_s": self.duration_s,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class UtilizationObservation:
    """Repeated pass/fail evidence collected for one requested percentage."""

    utilization_percent: float
    outcomes: tuple[bool, ...]
    runs: tuple[SweepRunOutcome, ...] = ()

    @property
    def passed_count(self) -> int:
        return sum(self.outcomes)

    @property
    def failed_count(self) -> int:
        return len(self.outcomes) - self.passed_count

    @property
    def mixed(self) -> bool:
        return self.passed_count > 0 and self.failed_count > 0

    @property
    def classification(self) -> PointClassification:
        if not self.outcomes:
            return PointClassification.INCONCLUSIVE
        if self.passed_count == len(self.outcomes):
            return PointClassification.STABLE_PASS
        if self.failed_count == len(self.outcomes):
            return PointClassification.STABLE_FAIL
        return PointClassification.UNSTABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "utilization_percent": self.utilization_percent,
            "outcomes": list(self.outcomes),
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "classification": self.classification.value,
            "run_count": len(self.runs),
        }


@dataclass(frozen=True, slots=True)
class BurstObservation:
    """Repeated pass/fail evidence collected for one candidate burst interval."""

    burst_interval_ticks: int
    outcomes: tuple[bool, ...]
    runs: tuple[SweepRunOutcome, ...] = ()

    @property
    def passed_count(self) -> int:
        return sum(self.outcomes)

    @property
    def failed_count(self) -> int:
        return len(self.outcomes) - self.passed_count

    @property
    def mixed(self) -> bool:
        return self.passed_count > 0 and self.failed_count > 0

    @property
    def classification(self) -> PointClassification:
        if not self.outcomes:
            return PointClassification.INCONCLUSIVE
        if self.passed_count == len(self.outcomes):
            return PointClassification.STABLE_PASS
        if self.failed_count == len(self.outcomes):
            return PointClassification.STABLE_FAIL
        return PointClassification.UNSTABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "burst_interval_ticks": self.burst_interval_ticks,
            "outcomes": list(self.outcomes),
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "classification": self.classification.value,
            "run_count": len(self.runs),
        }


@dataclass(frozen=True, slots=True)
class UtilizationSearchResult:
    """Final reliable boundary and all evidence from one binary search."""

    highest_passing_percent: float | None
    lowest_failing_percent: float | None
    observations: tuple[UtilizationObservation, ...]

    @property
    def unstable_percentages(self) -> tuple[float, ...]:
        return tuple(item.utilization_percent for item in self.observations if item.mixed)

    def to_dict(self) -> dict[str, object]:
        return {
            "highest_passing_percent": self.highest_passing_percent,
            "lowest_failing_percent": self.lowest_failing_percent,
            "unstable_percentages": list(self.unstable_percentages),
            "observations": [obs.to_dict() for obs in self.observations],
        }


@dataclass(frozen=True, slots=True)
class CellSearchResult:
    """Full campaign results for one matrix cell (Configuration, Frequency) in Sweep 1."""

    config: LoopbackConfiguration
    frequency_hz: int
    highest_passing_percent: float | None
    lowest_failing_percent: float | None
    confirmed_stable_ceiling_percent: float | None
    unstable_percentages: tuple[float, ...]
    soak_passed: bool
    soak_survived_ticks: int
    limiting_failure_domain: FailureDomain | None
    limiting_fault_reason: str | None
    observations: tuple[UtilizationObservation, ...]
    soak_runs: tuple[SweepRunOutcome, ...] = ()

    @property
    def total_runs(self) -> int:
        return sum(len(obs.outcomes) for obs in self.observations) + len(self.soak_runs)

    @property
    def total_exposure_ticks(self) -> int:
        ticks = sum(
            run.point.total_ticks for obs in self.observations for run in obs.runs if run.point
        )
        ticks += sum(run.point.total_ticks for run in self.soak_runs if run.point)
        return ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config.config_id,
            "config_name": self.config.name,
            "frequency_hz": self.frequency_hz,
            "highest_passing_percent": self.highest_passing_percent,
            "lowest_failing_percent": self.lowest_failing_percent,
            "confirmed_stable_ceiling_percent": self.confirmed_stable_ceiling_percent,
            "unstable_percentages": list(self.unstable_percentages),
            "soak_passed": self.soak_passed,
            "soak_survived_ticks": self.soak_survived_ticks,
            "limiting_failure_domain": (
                None if self.limiting_failure_domain is None else self.limiting_failure_domain.value
            ),
            "limiting_fault_reason": self.limiting_fault_reason,
            "total_runs": self.total_runs,
            "total_exposure_ticks": self.total_exposure_ticks,
            "observations": [obs.to_dict() for obs in self.observations],
            "soak_runs": [run.to_dict() for run in self.soak_runs],
        }


@dataclass(frozen=True, slots=True)
class BurstCellSearchResult:
    """Full campaign results for one matrix cell in Sweep 2 (Burstiness)."""

    config: LoopbackConfiguration
    frequency_hz: int
    target_utilization_percent: float
    highest_passing_burst_interval: int | None
    lowest_failing_burst_interval: int | None
    confirmed_stable_burst_interval: int | None
    unstable_intervals: tuple[int, ...]
    soak_passed: bool
    soak_survived_ticks: int
    observations: tuple[BurstObservation, ...]
    soak_runs: tuple[SweepRunOutcome, ...] = ()

    @property
    def total_runs(self) -> int:
        return sum(len(obs.outcomes) for obs in self.observations) + len(self.soak_runs)

    @property
    def total_exposure_ticks(self) -> int:
        ticks = sum(
            run.point.total_ticks for obs in self.observations for run in obs.runs if run.point
        )
        ticks += sum(run.point.total_ticks for run in self.soak_runs if run.point)
        return ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "config_id": self.config.config_id,
            "config_name": self.config.name,
            "frequency_hz": self.frequency_hz,
            "target_utilization_percent": self.target_utilization_percent,
            "highest_passing_burst_interval": self.highest_passing_burst_interval,
            "lowest_failing_burst_interval": self.lowest_failing_burst_interval,
            "confirmed_stable_burst_interval": self.confirmed_stable_burst_interval,
            "unstable_intervals": list(self.unstable_intervals),
            "soak_passed": self.soak_passed,
            "soak_survived_ticks": self.soak_survived_ticks,
            "total_runs": self.total_runs,
            "total_exposure_ticks": self.total_exposure_ticks,
            "observations": [obs.to_dict() for obs in self.observations],
            "soak_runs": [run.to_dict() for run in self.soak_runs],
        }


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
        return None if self._current is None else float(self._current)

    def next_point(
        self,
        template: LoopbackWorkloadPoint,
    ) -> LoopbackWorkloadPoint | None:
        percentage = self.next_percent()
        if percentage is None:
            return None
        return replace(template, target_utilization_percent=percentage)

    def record(self, passed: bool, *, utilization_percent: float | None = None) -> None:
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
                f"Expected a result for {float(self._current):g}%, received {float(requested):g}%"
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
        observations = tuple(
            UtilizationObservation(float(percentage), tuple(outcomes))
            for percentage, outcomes in sorted(self._outcomes.items())
        )
        return UtilizationSearchResult(
            highest_passing_percent=(None if self._lower_pass is None else float(self._lower_pass)),
            lowest_failing_percent=(None if self._upper_fail is None else float(self._upper_fail)),
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


class UtilizationCeilingSweep:
    """Self-discovering, boundary-confirming, and soak-validating engine for Sweep 1."""

    def __init__(
        self,
        config: LoopbackConfiguration,
        frequency_hz: int,
        *,
        coarse_points: tuple[float, ...] = (
            25.0,
            50.0,
            75.0,
            90.0,
            100.0,
        ),
        coarse_seeds: tuple[int, ...] = (1,),
        refinement_repeats: int = 1,
        confirmation_seeds: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
        confirmation_repeats: int = 1,
        resolution_percent: float = 1.0,
        soak_headroom_fraction: float = 1.0,
        soak_exposures: tuple[ExposureClass, ...] = (ExposureClass.SOAK_60S,),
    ) -> None:
        self.config = config
        self.frequency_hz = frequency_hz
        self.coarse_points = tuple(sorted(coarse_points))
        self.coarse_seeds = coarse_seeds
        self.refinement_repeats = refinement_repeats
        self.confirmation_seeds = confirmation_seeds
        self.confirmation_repeats = confirmation_repeats
        self.resolution_percent = resolution_percent
        self.soak_headroom_fraction = soak_headroom_fraction
        self.soak_exposures = soak_exposures

        self._stage = SweepStage.COARSE_DISCOVERY
        self._outcomes_by_pct: dict[float, list[bool]] = {}
        self._runs_by_pct: dict[float, list[SweepRunOutcome]] = {}
        self._soak_runs: list[SweepRunOutcome] = []

        # Coarse discovery queue
        self._coarse_queue: list[tuple[float, int]] = [
            (pct, seed) for pct in self.coarse_points for seed in self.coarse_seeds
        ]
        self._coarse_idx = 0

        # Refinement binary search state
        self._refine_search: UtilizationBinarySearch | None = None
        self._refine_seed_idx = 0

        # Confirmation state
        self._confirmation_points_to_test: list[float] = []
        self._confirmation_runs_queue: list[tuple[float, int]] = []
        self._confirmed_ceiling: float | None = None
        self._limiting_domain: FailureDomain | None = None
        self._limiting_reason: str | None = None

        # Soak state
        self._soak_queue: list[ExposureClass] = []
        self._soak_idx = 0
        self._soak_passed = False
        self._soak_survived_ticks = 0

    @property
    def stage(self) -> SweepStage:
        return self._stage

    def next_point(self) -> LoopbackWorkloadPoint | None:
        """Return the next workload point to execute, or None when campaign cell is complete."""
        match self._stage:
            case SweepStage.COARSE_DISCOVERY:
                if self._coarse_idx < len(self._coarse_queue):
                    pct, seed = self._coarse_queue[self._coarse_idx]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=pct,
                        exposure=ExposureClass.DISCOVERY,
                        burst_interval_ticks=1,
                        seed=seed,
                    )
                self._transition_from_coarse()
                return self.next_point()

            case SweepStage.BRACKET_REFINEMENT:
                if self._refine_search is not None and not self._refine_search.complete:
                    pct = self._refine_search.next_percent()
                    if pct is not None:
                        seed = self.coarse_seeds[self._refine_seed_idx % len(self.coarse_seeds)]
                        return workload_point_for_exposure(
                            frequency_hz=self.frequency_hz,
                            target_utilization_percent=pct,
                            exposure=ExposureClass.DISCOVERY,
                            burst_interval_ticks=1,
                            seed=seed,
                        )
                self._transition_from_refinement()
                return self.next_point()

            case SweepStage.BOUNDARY_CONFIRMATION:
                if self._confirmation_runs_queue:
                    pct, seed = self._confirmation_runs_queue[0]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=pct,
                        exposure=ExposureClass.DISCOVERY,
                        burst_interval_ticks=1,
                        seed=seed,
                    )
                self._transition_from_confirmation()
                return self.next_point()

            case SweepStage.SOAK_VALIDATION:
                if self._soak_idx < len(self._soak_queue) and self._confirmed_ceiling is not None:
                    exposure = self._soak_queue[self._soak_idx]
                    guarded_pct = self._calculate_guarded_point(self._confirmed_ceiling)
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=guarded_pct,
                        exposure=exposure,
                        burst_interval_ticks=1,
                        seed=100 + self._soak_idx,
                    )
                return None

    def record(
        self,
        passed_or_run: bool | SweepRunOutcome,
        *,
        utilization_percent: float | None = None,
    ) -> None:
        """Record the outcome of one completed run."""
        if isinstance(passed_or_run, SweepRunOutcome):
            run = passed_or_run
            passed = run.passed
            pct = run.point.target_utilization_percent
        else:
            passed = passed_or_run
            pct = (
                utilization_percent
                if utilization_percent is not None
                else self._current_requested_pct()
            )
            run = SweepRunOutcome(
                config_id=self.config.config_id,
                frequency_hz=self.frequency_hz,
                point=workload_point_for_exposure(
                    frequency_hz=self.frequency_hz,
                    target_utilization_percent=pct,
                    exposure=(
                        ExposureClass.DISCOVERY
                        if self._stage
                        in {
                            SweepStage.COARSE_DISCOVERY,
                            SweepStage.BRACKET_REFINEMENT,
                            SweepStage.BOUNDARY_CONFIRMATION,
                        }
                        else self._soak_queue[self._soak_idx]
                    ),
                    burst_interval_ticks=1,
                ),
                stage=self._stage,
                passed=passed,
            )

        if not passed and run.failure_domain is not None:
            self._limiting_domain = run.failure_domain
            self._limiting_reason = run.failure_reason

        match self._stage:
            case SweepStage.COARSE_DISCOVERY:
                self._outcomes_by_pct.setdefault(pct, []).append(passed)
                self._runs_by_pct.setdefault(pct, []).append(run)
                self._coarse_idx += 1
                if not passed:
                    # Early termination of coarse climbing: as soon as a point fails,
                    # skip remaining higher coarse points and begin bracket refinement
                    self._coarse_idx = len(self._coarse_queue)

            case SweepStage.BRACKET_REFINEMENT:
                self._outcomes_by_pct.setdefault(pct, []).append(passed)
                self._runs_by_pct.setdefault(pct, []).append(run)
                self._refine_seed_idx += 1
                if self._refine_search is not None:
                    self._refine_search.record(passed, utilization_percent=pct)

            case SweepStage.BOUNDARY_CONFIRMATION:
                self._outcomes_by_pct.setdefault(pct, []).append(passed)
                self._runs_by_pct.setdefault(pct, []).append(run)
                if self._confirmation_runs_queue:
                    self._confirmation_runs_queue.pop(0)

            case SweepStage.SOAK_VALIDATION:
                self._soak_runs.append(run)
                if passed:
                    self._soak_survived_ticks += run.point.total_ticks
                    self._soak_idx += 1
                    if self._soak_idx >= len(self._soak_queue):
                        self._soak_passed = True
                else:
                    # Soak failed: back off to next lower passing candidate (max 2 backoff attempts)
                    backoff_count = getattr(self, "_backoff_soak_count", 0)
                    if backoff_count < 2 and self._confirmed_ceiling is not None:
                        self._backoff_soak_count = backoff_count + 1
                        lower_candidates = [
                            p
                            for p in sorted(self._outcomes_by_pct.keys(), reverse=True)
                            if p < self._confirmed_ceiling and all(self._outcomes_by_pct[p])
                        ]
                        if lower_candidates:
                            self._confirmed_ceiling = lower_candidates[0]
                            self._soak_idx = 0
                            return
                    self._soak_idx = len(self._soak_queue)  # Terminate soak on exhausted backoff

    def _current_requested_pct(self) -> float:
        pt = self.next_point()
        if pt is None:
            raise RuntimeError("Sweep has no current point")
        return pt.target_utilization_percent

    def _transition_from_coarse(self) -> None:
        passing_coarse = [
            pct
            for pct in self.coarse_points
            if all(self._outcomes_by_pct.get(pct, [False]))
            and len(self._outcomes_by_pct.get(pct, [])) == len(self.coarse_seeds)
        ]
        failing_coarse = [
            pct for pct in self.coarse_points if not all(self._outcomes_by_pct.get(pct, [True]))
        ]

        lower_pass = max(passing_coarse, default=0.0)
        upper_fail = min(failing_coarse, default=100.0)

        if lower_pass >= 100.0 or upper_fail <= lower_pass + self.resolution_percent:
            # No binary search needed, jump directly to confirmation
            self._stage = SweepStage.BOUNDARY_CONFIRMATION
            self._setup_confirmation(lower_pass, upper_fail if upper_fail > lower_pass else None)
        else:
            self._stage = SweepStage.BRACKET_REFINEMENT
            self._refine_search = UtilizationBinarySearch(
                minimum_percent=lower_pass,
                maximum_percent=upper_fail,
                resolution_percent=self.resolution_percent,
                repeats_per_point=self.refinement_repeats,
                required_passes=self.refinement_repeats,
            )
            # Record already collected points
            for _ in range(self.refinement_repeats):
                self._refine_search.record(True, utilization_percent=lower_pass)
            for _ in range(self.refinement_repeats):
                self._refine_search.record(False, utilization_percent=upper_fail)

    def _transition_from_refinement(self) -> None:
        self._stage = SweepStage.BOUNDARY_CONFIRMATION
        if self._refine_search is not None:
            res = self._refine_search.result()
            lower = res.highest_passing_percent or 0.0
            upper = res.lowest_failing_percent
            self._setup_confirmation(lower, upper)
        else:
            self._setup_confirmation(0.0, None)

    def _setup_confirmation(self, candidate_pass: float, candidate_fail: float | None) -> None:
        pts = [candidate_pass]
        self._confirmation_points_to_test = pts
        runs: list[tuple[float, int]] = []
        for _ in range(self.confirmation_repeats):
            for seed in self.confirmation_seeds:
                for pct in pts:
                    runs.append((pct, seed))
        self._confirmation_runs_queue = runs

    def _transition_from_confirmation(self) -> None:
        # Evaluate highest stable pass
        stable_pass: float | None = None
        for pct in sorted(self._outcomes_by_pct.keys(), reverse=True):
            outcomes = self._outcomes_by_pct[pct]
            if len(outcomes) >= (len(self.confirmation_seeds) * self.confirmation_repeats) and all(
                outcomes
            ):
                stable_pass = pct
                break

        if stable_pass is None:
            # Fall back to best coarse pass; if not yet confirmed, confirm it (max 2 backoffs)
            passing_pts = [pct for pct, outcomes in self._outcomes_by_pct.items() if all(outcomes)]
            fallback_pass = max(passing_pts, default=0.0)
            backoff_count = getattr(self, "_backoff_confirm_count", 0)
            if (
                fallback_pass > 0.0
                and fallback_pass not in self._confirmation_points_to_test
                and backoff_count < 2
            ):
                self._backoff_confirm_count = backoff_count + 1
                self._setup_confirmation(fallback_pass, None)
                return
            stable_pass = fallback_pass

        self._confirmed_ceiling = stable_pass
        self._stage = SweepStage.SOAK_VALIDATION
        self._soak_queue = list(self.soak_exposures)
        self._soak_idx = 0

    def _calculate_guarded_point(self, ceiling_pct: float) -> float:
        guarded = ceiling_pct * self.soak_headroom_fraction
        # Ensure it's strictly below any failing or unstable point
        lowest_issue = min(
            (
                pct
                for pct, outcomes in self._outcomes_by_pct.items()
                if not all(outcomes) and pct > 0
            ),
            default=101.0,
        )
        if guarded >= lowest_issue:
            guarded = max(0.0, lowest_issue - 1.0)
        return round(guarded, 1)

    def run(
        self,
        executor: Callable[
            [LoopbackConfiguration, LoopbackWorkloadPoint, SweepStage],
            bool | SweepRunOutcome,
        ],
    ) -> CellSearchResult:
        """Run the full autonomous search, confirmation, and soak validation."""
        while True:
            pt = self.next_point()
            if pt is None:
                break
            result = executor(self.config, pt, self._stage)
            self.record(result)
        return self.result()

    def result(self) -> CellSearchResult:
        """Return the finalized comprehensive matrix cell results."""
        observations = tuple(
            UtilizationObservation(
                utilization_percent=pct,
                outcomes=tuple(outcomes),
                runs=tuple(self._runs_by_pct.get(pct, ())),
            )
            for pct, outcomes in sorted(self._outcomes_by_pct.items())
        )
        passing_pts = [obs.utilization_percent for obs in observations if all(obs.outcomes)]
        failing_pts = [obs.utilization_percent for obs in observations if not all(obs.outcomes)]
        unstable_pts = tuple(obs.utilization_percent for obs in observations if obs.mixed)

        return CellSearchResult(
            config=self.config,
            frequency_hz=self.frequency_hz,
            highest_passing_percent=max(passing_pts, default=None),
            lowest_failing_percent=min(failing_pts, default=None),
            confirmed_stable_ceiling_percent=self._confirmed_ceiling,
            unstable_percentages=unstable_pts,
            soak_passed=self._soak_passed,
            soak_survived_ticks=self._soak_survived_ticks,
            limiting_failure_domain=self._limiting_domain,
            limiting_fault_reason=self._limiting_reason,
            observations=observations,
            soak_runs=tuple(self._soak_runs),
        )


class BurstinessSweep:
    """Self-discovering and soak-validating engine for Sweep 2 (Burstiness)."""

    def __init__(
        self,
        config: LoopbackConfiguration,
        frequency_hz: int,
        target_utilization_percent: float,
        *,
        burst_intervals: tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100),
        discovery_seeds: tuple[int, ...] = (1,),
        confirmation_seeds: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
        confirmation_repeats: int = 1,
        soak_exposures: tuple[ExposureClass, ...] = (ExposureClass.SOAK_60S,),
    ) -> None:
        self.config = config
        self.frequency_hz = frequency_hz
        self.target_utilization_percent = target_utilization_percent
        self.burst_intervals = tuple(sorted(burst_intervals))
        self.discovery_seeds = discovery_seeds
        self.confirmation_seeds = confirmation_seeds
        self.confirmation_repeats = confirmation_repeats
        self.soak_exposures = soak_exposures

        self._stage = SweepStage.COARSE_DISCOVERY
        self._outcomes_by_interval: dict[int, list[bool]] = {}
        self._runs_by_interval: dict[int, list[SweepRunOutcome]] = {}
        self._soak_runs: list[SweepRunOutcome] = []

        self._discovery_queue: list[tuple[int, int]] = [
            (interval, seed) for interval in self.burst_intervals for seed in self.discovery_seeds
        ]
        self._discovery_idx = 0
        self._refine_lower: int | None = None
        self._refine_upper: int | None = None
        self._refine_current: int | None = None
        self._refine_seed_idx = 0

        self._confirmation_queue: list[tuple[int, int]] = []
        self._confirmed_burst: int | None = None

        self._soak_queue: list[ExposureClass] = []
        self._soak_idx = 0
        self._soak_passed = False
        self._soak_survived_ticks = 0

    @property
    def stage(self) -> SweepStage:
        return self._stage

    def next_point(self) -> LoopbackWorkloadPoint | None:
        match self._stage:
            case SweepStage.COARSE_DISCOVERY:
                if self._discovery_idx < len(self._discovery_queue):
                    interval, seed = self._discovery_queue[self._discovery_idx]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=self.target_utilization_percent,
                        exposure=ExposureClass.DISCOVERY,
                        burst_interval_ticks=interval,
                        seed=seed,
                    )
                self._transition_from_coarse()
                return self.next_point()

            case SweepStage.BRACKET_REFINEMENT:
                if self._refine_current is not None:
                    seed = self.discovery_seeds[self._refine_seed_idx % len(self.discovery_seeds)]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=self.target_utilization_percent,
                        exposure=ExposureClass.DISCOVERY,
                        burst_interval_ticks=self._refine_current,
                        seed=seed,
                    )
                self._transition_to_confirmation()
                return self.next_point()

            case SweepStage.BOUNDARY_CONFIRMATION:
                if self._confirmation_queue:
                    interval, seed = self._confirmation_queue[0]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=self.target_utilization_percent,
                        exposure=ExposureClass.DISCOVERY,
                        burst_interval_ticks=interval,
                        seed=seed,
                    )
                self._transition_to_soak()
                return self.next_point()

            case SweepStage.SOAK_VALIDATION:
                if self._soak_idx < len(self._soak_queue) and self._confirmed_burst is not None:
                    exposure = self._soak_queue[self._soak_idx]
                    return workload_point_for_exposure(
                        frequency_hz=self.frequency_hz,
                        target_utilization_percent=self.target_utilization_percent,
                        exposure=exposure,
                        burst_interval_ticks=self._confirmed_burst,
                        seed=200 + self._soak_idx,
                    )
                return None

    def record(
        self,
        passed_or_run: bool | SweepRunOutcome,
        *,
        burst_interval_ticks: int | None = None,
    ) -> None:
        if isinstance(passed_or_run, SweepRunOutcome):
            run = passed_or_run
            passed = run.passed
            interval = run.point.burst_interval_ticks
        else:
            passed = passed_or_run
            interval = (
                burst_interval_ticks
                if burst_interval_ticks is not None
                else self._current_requested_interval()
            )
            run = SweepRunOutcome(
                config_id=self.config.config_id,
                frequency_hz=self.frequency_hz,
                point=workload_point_for_exposure(
                    frequency_hz=self.frequency_hz,
                    target_utilization_percent=self.target_utilization_percent,
                    exposure=ExposureClass.DISCOVERY,
                    burst_interval_ticks=interval,
                ),
                stage=self._stage,
                passed=passed,
            )

        match self._stage:
            case SweepStage.COARSE_DISCOVERY:
                self._outcomes_by_interval.setdefault(interval, []).append(passed)
                self._runs_by_interval.setdefault(interval, []).append(run)
                self._discovery_idx += 1
                if not passed:
                    self._discovery_idx = len(self._discovery_queue)

            case SweepStage.BRACKET_REFINEMENT:
                self._outcomes_by_interval.setdefault(interval, []).append(passed)
                self._runs_by_interval.setdefault(interval, []).append(run)
                self._refine_seed_idx += 1
                assert self._refine_lower is not None
                assert self._refine_upper is not None
                if passed:
                    self._refine_lower = interval
                else:
                    self._refine_upper = interval
                if self._refine_upper - self._refine_lower <= 1:
                    self._refine_current = None
                    self._transition_to_confirmation()
                else:
                    mid = (self._refine_lower + self._refine_upper) // 2
                    if mid == self._refine_lower:
                        mid += 1
                    self._refine_current = mid

            case SweepStage.BOUNDARY_CONFIRMATION:
                self._outcomes_by_interval.setdefault(interval, []).append(passed)
                self._runs_by_interval.setdefault(interval, []).append(run)
                if self._confirmation_queue:
                    self._confirmation_queue.pop(0)

            case SweepStage.SOAK_VALIDATION:
                self._soak_runs.append(run)
                if passed:
                    self._soak_survived_ticks += run.point.total_ticks
                    self._soak_idx += 1
                    if self._soak_idx >= len(self._soak_queue):
                        self._soak_passed = True
                else:
                    # Soak failed: back off to next lower passing burst interval
                    # (max 2 backoff attempts)
                    backoff_count = getattr(self, "_backoff_soak_count", 0)
                    if backoff_count < 2 and self._confirmed_burst is not None:
                        self._backoff_soak_count = backoff_count + 1
                        lower_candidates = [
                            iv
                            for iv in sorted(self._outcomes_by_interval.keys(), reverse=True)
                            if iv < self._confirmed_burst and all(self._outcomes_by_interval[iv])
                        ]
                        if lower_candidates:
                            self._confirmed_burst = lower_candidates[0]
                            self._soak_idx = 0
                            return
                    self._soak_idx = len(self._soak_queue)  # Terminate soak on exhausted backoff

    def _current_requested_interval(self) -> int:
        pt = self.next_point()
        if pt is None:
            raise RuntimeError("Sweep has no current point")
        return pt.burst_interval_ticks

    def _transition_from_coarse(self) -> None:
        passing = [
            iv
            for iv in self.burst_intervals
            if all(self._outcomes_by_interval.get(iv, [False]))
            and len(self._outcomes_by_interval.get(iv, [])) == len(self.discovery_seeds)
        ]
        failing = [
            iv for iv in self.burst_intervals if not all(self._outcomes_by_interval.get(iv, [True]))
        ]

        lower_pass = max(passing, default=1)
        upper_fail = min(failing, default=None)

        if upper_fail is None or upper_fail - lower_pass <= 1:
            self._transition_to_confirmation()
        else:
            self._stage = SweepStage.BRACKET_REFINEMENT
            self._refine_lower = lower_pass
            self._refine_upper = upper_fail
            mid = (lower_pass + upper_fail) // 2
            if mid == lower_pass:
                mid += 1
            self._refine_current = mid

    def _transition_to_confirmation(self, candidate_override: int | None = None) -> None:
        self._stage = SweepStage.BOUNDARY_CONFIRMATION
        if candidate_override is not None:
            candidate_pass = candidate_override
        else:
            passing = [
                iv
                for iv in sorted(self._outcomes_by_interval.keys())
                if all(self._outcomes_by_interval.get(iv, [False]))
            ]
            candidate_pass = max(passing, default=1)

        self._confirmation_points_to_test = [candidate_pass]
        pts = [candidate_pass]
        runs: list[tuple[int, int]] = []
        for _ in range(self.confirmation_repeats):
            for seed in self.confirmation_seeds:
                for iv in pts:
                    runs.append((iv, seed))
        self._confirmation_queue = runs

    def _transition_to_soak(self) -> None:
        stable_burst: int | None = None
        for iv in sorted(self._outcomes_by_interval.keys(), reverse=True):
            outcomes = self._outcomes_by_interval[iv]
            if len(outcomes) >= (len(self.confirmation_seeds) * self.confirmation_repeats) and all(
                outcomes
            ):
                stable_burst = iv
                break

        if stable_burst is None:
            # Fall back to best coarse pass; if not yet confirmed, confirm it (max 2 backoffs)
            passing = [
                iv
                for iv in sorted(self._outcomes_by_interval.keys(), reverse=True)
                if all(self._outcomes_by_interval[iv])
            ]
            fallback = max(passing, default=1)
            backoff_count = getattr(self, "_backoff_confirm_count", 0)
            tested = getattr(self, "_confirmation_points_to_test", [])
            if fallback > 1 and fallback not in tested and backoff_count < 2:
                self._backoff_confirm_count = backoff_count + 1
                self._transition_to_confirmation(candidate_override=fallback)
                return
            stable_burst = fallback

        self._confirmed_burst = stable_burst
        self._stage = SweepStage.SOAK_VALIDATION
        self._soak_queue = list(self.soak_exposures)
        self._soak_idx = 0

    def run(
        self,
        executor: Callable[
            [LoopbackConfiguration, LoopbackWorkloadPoint, SweepStage],
            bool | SweepRunOutcome,
        ],
    ) -> BurstCellSearchResult:
        while True:
            pt = self.next_point()
            if pt is None:
                break
            result = executor(self.config, pt, self._stage)
            self.record(result)
        return self.result()

    def result(self) -> BurstCellSearchResult:
        observations = tuple(
            BurstObservation(
                burst_interval_ticks=iv,
                outcomes=tuple(outcomes),
                runs=tuple(self._runs_by_interval.get(iv, ())),
            )
            for iv, outcomes in sorted(self._outcomes_by_interval.items())
        )
        passing = [obs.burst_interval_ticks for obs in observations if all(obs.outcomes)]
        failing = [obs.burst_interval_ticks for obs in observations if not all(obs.outcomes)]
        unstable = tuple(obs.burst_interval_ticks for obs in observations if obs.mixed)

        return BurstCellSearchResult(
            config=self.config,
            frequency_hz=self.frequency_hz,
            target_utilization_percent=self.target_utilization_percent,
            highest_passing_burst_interval=max(passing, default=None),
            lowest_failing_burst_interval=min(failing, default=None),
            confirmed_stable_burst_interval=self._confirmed_burst,
            unstable_intervals=unstable,
            soak_passed=self._soak_passed,
            soak_survived_ticks=self._soak_survived_ticks,
            observations=observations,
            soak_runs=tuple(self._soak_runs),
        )


@dataclass(frozen=True, slots=True)
class CampaignReport:
    """Consolidated characterization report for all sweeps across all matrix cells."""

    sweep_1_results: tuple[CellSearchResult, ...]
    sweep_2_results: tuple[BurstCellSearchResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "sweep_1_utilization_ceiling": [r.to_dict() for r in self.sweep_1_results],
            "sweep_2_burstiness": [r.to_dict() for r in self.sweep_2_results],
        }

    def to_markdown(self) -> str:
        """Generate full markdown tables formatted identically to loopback_test_matrix.md."""
        lines: list[str] = [
            "# HIL-RIG Loopback Characterization — Automated Sweep Campaign Report",
            "",
            "## Sweep 1 — Utilization Ceiling Boundaries",
            "",
            "| Configuration | 100 Hz | 1 kHz | 10 kHz |",
            "|---|---|---|---|",
        ]

        freqs = (100, 1_000, 10_000)
        config_map_s1: dict[int, dict[int, CellSearchResult]] = {}
        for r in self.sweep_1_results:
            config_map_s1.setdefault(r.config.config_id, {})[r.frequency_hz] = r

        for cid in sorted(config_map_s1.keys()):
            cfg = LoopbackConfiguration.from_id(cid)
            row_cells: list[str] = [f"**{cfg.name}**"]
            for f in freqs:
                cell = config_map_s1.get(cid, {}).get(f)
                if cell is None:
                    row_cells.append("N/A")
                else:
                    ceiling = (
                        f"{cell.confirmed_stable_ceiling_percent:g}%"
                        if cell.confirmed_stable_ceiling_percent is not None
                        else "0%"
                    )
                    soak = "Pass Soak" if cell.soak_passed else "Fail Soak"
                    row_cells.append(f"{ceiling} ({soak})")
            lines.append(f"| {' | '.join(row_cells)} |")

        lines.extend(
            [
                "",
                "## Sweep 2 — Burstiness Limits",
                "",
                "| Configuration | 100 Hz | 1 kHz | 10 kHz |",
                "|---|---|---|---|",
            ]
        )

        config_map_s2: dict[int, dict[int, BurstCellSearchResult]] = {}
        for r in self.sweep_2_results:
            config_map_s2.setdefault(r.config.config_id, {})[r.frequency_hz] = r

        for cid in sorted(config_map_s2.keys()):
            cfg = LoopbackConfiguration.from_id(cid)
            row_cells = [f"**{cfg.name}**"]
            for f in freqs:
                cell_s2 = config_map_s2.get(cid, {}).get(f)
                if cell_s2 is None:
                    row_cells.append("N/A")
                else:
                    burst = (
                        f"{cell_s2.confirmed_stable_burst_interval}t"
                        if cell_s2.confirmed_stable_burst_interval is not None
                        else "None"
                    )
                    soak = "Pass Soak" if cell_s2.soak_passed else "Fail Soak"
                    row_cells.append(f"{burst} @ {cell_s2.target_utilization_percent:g}% ({soak})")
            lines.append(f"| {' | '.join(row_cells)} |")

        return "\n".join(lines)

    def to_csv(self) -> str:
        """Export tabular summary in CSV format."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            [
                "sweep",
                "config_id",
                "config_name",
                "frequency_hz",
                "ceiling_or_burst",
                "soak_passed",
                "total_runs",
                "total_ticks",
                "limiting_domain",
                "limiting_reason",
            ]
        )
        for r in self.sweep_1_results:
            writer.writerow(
                [
                    "Sweep 1 (Utilization)",
                    r.config.config_id,
                    r.config.name,
                    r.frequency_hz,
                    r.confirmed_stable_ceiling_percent,
                    r.soak_passed,
                    r.total_runs,
                    r.total_exposure_ticks,
                    r.limiting_failure_domain.value if r.limiting_failure_domain else "",
                    r.limiting_fault_reason or "",
                ]
            )
        for r in self.sweep_2_results:
            writer.writerow(
                [
                    "Sweep 2 (Burstiness)",
                    r.config.config_id,
                    r.config.name,
                    r.frequency_hz,
                    r.confirmed_stable_burst_interval,
                    r.soak_passed,
                    r.total_runs,
                    r.total_exposure_ticks,
                    "",
                    "",
                ]
            )
        return out.getvalue()


class LoopbackSweepCampaign:
    """Master orchestrator executing both Sweep 1 and Sweep 2 across the test matrix."""

    def __init__(
        self,
        configurations: tuple[LoopbackConfiguration, ...] | None = None,
        frequencies_hz: tuple[int, ...] = (100, 1_000, 10_000),
        *,
        include_sweep_1: bool = True,
        include_sweep_2: bool = True,
        sweep_2_config_ids: tuple[int, ...] = (1, 4),
        sweep_2_base_utilization_fraction: float = 0.75,
        coarse_points: tuple[float, ...] = (
            25.0,
            50.0,
            75.0,
            90.0,
            100.0,
        ),
        coarse_seeds: tuple[int, ...] = (1,),
        refinement_repeats: int = 1,
        confirmation_seeds: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
        confirmation_repeats: int = 1,
        resolution_percent: float = 1.0,
        soak_headroom_fraction: float = 1.0,
        soak_exposures: tuple[ExposureClass, ...] = (ExposureClass.SOAK_60S,),
        burst_intervals: tuple[int, ...] = (1, 2, 5, 10, 20, 50, 100),
        burst_discovery_seeds: tuple[int, ...] = (1,),
    ) -> None:
        self.configurations = configurations or LoopbackConfiguration.all_configurations()
        self.frequencies_hz = frequencies_hz
        self.include_sweep_1 = include_sweep_1
        self.include_sweep_2 = include_sweep_2
        self.sweep_2_config_ids = sweep_2_config_ids
        self.sweep_2_base_utilization_fraction = sweep_2_base_utilization_fraction
        self.coarse_points = coarse_points
        self.coarse_seeds = coarse_seeds
        self.refinement_repeats = refinement_repeats
        self.confirmation_seeds = confirmation_seeds
        self.confirmation_repeats = confirmation_repeats
        self.resolution_percent = resolution_percent
        self.soak_headroom_fraction = soak_headroom_fraction
        self.soak_exposures = soak_exposures
        self.burst_intervals = burst_intervals
        self.burst_discovery_seeds = burst_discovery_seeds

    def run(
        self,
        executor: Callable[
            [LoopbackConfiguration, LoopbackWorkloadPoint, SweepStage],
            bool | SweepRunOutcome,
        ],
    ) -> CampaignReport:
        """Run the configured campaign matrix against the provided executor."""
        s1_results: list[CellSearchResult] = []
        confirmed_ceilings: dict[tuple[int, int], float] = {}

        if self.include_sweep_1:
            for config in self.configurations:
                for freq in self.frequencies_hz:
                    engine = UtilizationCeilingSweep(
                        config=config,
                        frequency_hz=freq,
                        coarse_points=self.coarse_points,
                        coarse_seeds=self.coarse_seeds,
                        refinement_repeats=self.refinement_repeats,
                        confirmation_seeds=self.confirmation_seeds,
                        confirmation_repeats=self.confirmation_repeats,
                        resolution_percent=self.resolution_percent,
                        soak_headroom_fraction=self.soak_headroom_fraction,
                        soak_exposures=self.soak_exposures,
                    )
                    cell_res = engine.run(executor)
                    s1_results.append(cell_res)
                    confirmed_ceilings[(config.config_id, freq)] = (
                        cell_res.confirmed_stable_ceiling_percent or 50.0
                    )

        s2_results: list[BurstCellSearchResult] = []
        if self.include_sweep_2:
            target_configs = [
                cfg for cfg in self.configurations if cfg.config_id in self.sweep_2_config_ids
            ]
            for config in target_configs:
                for freq in self.frequencies_hz:
                    ceiling = confirmed_ceilings.get((config.config_id, freq), 50.0)
                    base_pct = round(ceiling * self.sweep_2_base_utilization_fraction, 1)
                    burst_engine = BurstinessSweep(
                        config=config,
                        frequency_hz=freq,
                        target_utilization_percent=base_pct,
                        burst_intervals=self.burst_intervals,
                        discovery_seeds=self.burst_discovery_seeds,
                        confirmation_seeds=self.confirmation_seeds,
                        confirmation_repeats=self.confirmation_repeats,
                        soak_exposures=self.soak_exposures,
                    )
                    burst_res = burst_engine.run(executor)
                    s2_results.append(burst_res)

        return CampaignReport(
            sweep_1_results=tuple(s1_results),
            sweep_2_results=tuple(s2_results),
        )

    @classmethod
    def create_simulated_executor(
        cls,
        simulated_ceilings: dict[tuple[int, int], float] | None = None,
        simulated_burst_limits: dict[tuple[int, int], int] | None = None,
    ) -> Callable[
        [LoopbackConfiguration, LoopbackWorkloadPoint, SweepStage],
        SweepRunOutcome,
    ]:
        """Create a deterministic simulation executor for offline dry-runs and automated tests."""
        default_ceilings = {
            (1, 100): 94.0,
            (1, 1_000): 98.0,
            (1, 10_000): 99.0,
            (2, 100): 50.0,
            (2, 1_000): 95.0,
            (2, 10_000): 98.0,
            (3, 100): 90.0,
            (3, 1_000): 96.0,
            (3, 10_000): 99.0,
            (4, 100): 20.0,
            (4, 1_000): 85.0,
            (4, 10_000): 75.0,
        }
        ceilings = simulated_ceilings or default_ceilings
        burst_limits = simulated_burst_limits or {
            (1, 100): 1,
            (1, 1_000): 10,
            (1, 10_000): 50,
            (4, 100): 1,
            (4, 1_000): 2,
            (4, 10_000): 10,
        }

        def _executor(
            config: LoopbackConfiguration,
            point: LoopbackWorkloadPoint,
            stage: SweepStage,
        ) -> SweepRunOutcome:
            ceiling = ceilings.get((config.config_id, point.frequency_hz), 80.0)
            burst_limit = burst_limits.get((config.config_id, point.frequency_hz), 10)

            admissible, reason = check_workload_admissibility(config, point)
            if not admissible:
                return SweepRunOutcome(
                    config_id=config.config_id,
                    frequency_hz=point.frequency_hz,
                    point=point,
                    stage=stage,
                    passed=False,
                    failure_domain=FailureDomain.BUFFER,
                    failure_reason=reason,
                )

            passed = (point.target_utilization_percent <= ceiling) and (
                point.burst_interval_ticks <= burst_limit
            )
            return SweepRunOutcome(
                config_id=config.config_id,
                frequency_hz=point.frequency_hz,
                point=point,
                stage=stage,
                passed=passed,
                failure_domain=None if passed else FailureDomain.BUFFER,
                failure_reason=None if passed else "Simulated capacity ceiling reached",
            )

        return _executor

    @classmethod
    def create_live_executor(
        cls,
        runs_directory: Any | None = None,
        *,
        port: str | None = None,
        connection_factory: Any | None = None,
    ) -> Callable[
        [LoopbackConfiguration, LoopbackWorkloadPoint, SweepStage],
        SweepRunOutcome,
    ]:
        import secrets
        from concurrent.futures import ThreadPoolExecutor
        from contextlib import suppress
        from datetime import UTC, datetime
        from pathlib import Path

        from hilrig.exceptions import ProtocolSessionError
        from hilrig.protocol import (
            FixedIOProtocolConnection,
            ProtocolFamily,
            ProtocolWorkflowState,
            UploadAdvanceMode,
        )
        from hilrig.protocol.serial import SerialConnectionSettings
        from hilrig.results import CapturedRunBuilder, CaptureStatus
        from hilrig.runner import write_run_artifacts
        from hilrig.workloads import build_loopback_test

        base_dir = Path(runs_directory) if runs_directory else Path("runs")
        base_dir.mkdir(parents=True, exist_ok=True)
        artifact_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sweep_artifact")

        if connection_factory is not None:
            factory = connection_factory
        elif port is not None:
            settings = SerialConnectionSettings(device=port)

            def factory(**kwargs: Any) -> FixedIOProtocolConnection:
                return FixedIOProtocolConnection.connect(serial_settings=settings, **kwargs)
        else:
            factory = FixedIOProtocolConnection.connect

        active_connection = [None]

        def _purge_serial(conn):
            if conn is None:
                return
            port_obj = getattr(conn, "serial_port", None)
            if port_obj is not None:
                with suppress(Exception):
                    if hasattr(port_obj, "reset_input_buffer"):
                        port_obj.reset_input_buffer()
                    if hasattr(port_obj, "reset_output_buffer"):
                        port_obj.reset_output_buffer()

        def _reset_connection(conn):
            if conn is None:
                return
            _purge_serial(conn)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and getattr(
                conn, "_transport_delivery_pending", False
            ):
                conn.service()
                time.sleep(0.002)

            conn._pending_operation = None
            conn.result_adapter = None
            try:
                conn.reset_application()
                reset_wait = time.monotonic() + 2.0
                while (
                    time.monotonic() < reset_wait
                    and conn.workflow_state is ProtocolWorkflowState.RESETTING
                ):
                    rep = conn.service()
                    if not (
                        rep.serial_bytes_read
                        or rep.serial_bytes_written
                        or rep.application_message_submitted
                    ):
                        time.sleep(0.002)
            except Exception:
                pass

            readiness_wait = time.monotonic() + 2.0
            while time.monotonic() < readiness_wait and not conn.ready_for_upload:
                rep = conn.service()
                if not (
                    rep.serial_bytes_read
                    or rep.serial_bytes_written
                    or rep.application_message_submitted
                ):
                    time.sleep(0.002)

        def _get_connection():
            conn = active_connection[0]
            if conn is not None:
                try:
                    if (
                        conn.session_confirmed
                        and conn.ready_for_upload
                        and getattr(conn, "_pending_operation", None) is None
                    ):
                        return conn
                    _reset_connection(conn)
                    if conn.session_confirmed and conn.ready_for_upload:
                        return conn
                except Exception:
                    pass
                with suppress(Exception):
                    conn.close()
            # Retry connecting up to 3 times in case MCU is draining/resetting
            for attempt in range(3):
                time.sleep(0.3 if attempt == 0 else 1.0)
                try:
                    try:
                        conn = factory(protocol_family=ProtocolFamily.VARIABLE)
                    except TypeError:
                        conn = factory()
                    _purge_serial(conn)
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and not conn.session_confirmed:
                        rep = conn.service()
                        if not (
                            rep.serial_bytes_read
                            or rep.serial_bytes_written
                            or rep.application_message_submitted
                        ):
                            time.sleep(0.002)
                    if conn.session_confirmed:
                        if (
                            not conn.ready_for_upload
                            or getattr(conn, "_pending_operation", None) is not None
                        ):
                            _reset_connection(conn)
                        if conn.ready_for_upload:
                            active_connection[0] = conn
                            return conn
                    with suppress(Exception):
                        conn.close()
                except Exception:
                    if conn is not None:
                        with suppress(Exception):
                            conn.close()

            raise ProtocolSessionError("Failed to establish session with RIG after retries")

            active_connection[0] = conn
            return conn

        def _live_executor(
            config: LoopbackConfiguration,
            point: LoopbackWorkloadPoint,
            stage: SweepStage,
        ) -> SweepRunOutcome:
            print(
                f"[{stage.name}] Config {config.config_id} @ {point.frequency_hz} Hz | "
                f"Util {point.target_utilization_percent:g}% (burst={point.burst_interval_ticks}, "
                f"seed={point.seed}, {point.duration_s}s)... ",
                end="",
                flush=True,
            )

            # Check compile-time admissibility first (e.g. peripheral buffer overflow)
            admissible, reason = check_workload_admissibility(config, point)
            if not admissible:
                print(f"FAIL (buffer/admissibility: {reason})", flush=True)
                return SweepRunOutcome(
                    config_id=config.config_id,
                    frequency_hz=point.frequency_hz,
                    point=point,
                    stage=stage,
                    passed=False,
                    failure_domain=FailureDomain.BUFFER,
                    failure_reason=reason,
                )

            test = build_loopback_test(config, point)
            compiled = test.compile()
            run_id = secrets.randbits(128)
            run_id_hex = f"{run_id:032x}"
            timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            cfg_slug = (
                f"config{config.config_id}-{point.frequency_hz}hz-"
                f"{point.target_utilization_percent:g}pct"
            )
            out_dir = base_dir / f"{timestamp}-{cfg_slug}-{run_id_hex}"
            out_dir.mkdir(parents=True, exist_ok=True)

            # Offload heavy Excel generation to background worker so live test starts immediately
            artifact_pool.submit(compiled.write_excel, out_dir / "test-review.xlsx")

            connection = None
            builder = None
            try:
                connection = _get_connection()

                info = connection.session_info
                attempt = compiled.new_upload_attempt()
                builder = CapturedRunBuilder.from_compiled_test(
                    out_dir / "captured-run.sqlite3",
                    compiled,
                    upload_attempt=attempt,
                    run_id=run_id,
                    application_protocol_version=info.protocol_version if info else None,
                    firmware_version=info.firmware_version if info else None,
                    attempt_number=1,
                    started_at=datetime.now(UTC).isoformat(),
                )
                connection.bind_result_builder(builder, replace=True)
                connection.queue_upload(
                    compiled,
                    upload_attempt=attempt,
                    advance_mode=UploadAdvanceMode.AUTOMATIC,
                )

                host_start_requested = False
                expected_duration_s = float(point.duration_s)
                tick_allowance_s = float(point.total_ticks) * 0.002
                watchdog_deadline = time.monotonic() + max(
                    30.0, tick_allowance_s + expected_duration_s * 3.0 + 15.0
                )
                while not connection.report_received:
                    if time.monotonic() > watchdog_deadline:
                        raise TimeoutError(
                            f"Execution timed out after {expected_duration_s}s "
                            "(no run report received from RIG)"
                        )
                    rep = connection.service()
                    if connection.workflow_state is ProtocolWorkflowState.FAILED:
                        err_msg = (
                            getattr(connection, "last_error", None) or "Protocol workflow failed"
                        )
                        raise ProtocolSessionError(err_msg)
                    if (
                        getattr(connection, "workflow_state", None) is not None
                        and str(connection.workflow_state).endswith("READY_TO_START")
                        and not host_start_requested
                    ):
                        connection.start()
                        host_start_requested = True
                    if not (
                        rep.serial_bytes_read
                        or rep.serial_bytes_written
                        or rep.stored_tick_results
                        or rep.application_message_submitted
                    ):
                        time.sleep(0.001)

                captured_run = builder.finalize(
                    report=connection.run_report,
                    responses=connection.lifecycle_responses,
                    status_events=connection.status_events,
                )
                builder = None
                verdict = write_run_artifacts(captured_run, out_dir)
                passed = verdict == "pass" and (
                    captured_run.report.run_outcome == "SUCCESS" if captured_run.report else True
                )

                max_isr = None
                margin_isr = None
                if captured_run.report and captured_run.report.isr_timing:
                    isr_t = captured_run.report.isr_timing
                    max_isr = (
                        isr_t.get("maximum_cycles")
                        if isinstance(isr_t, dict)
                        else getattr(isr_t, "maximum_cycles", None)
                    )
                    if max_isr is not None:
                        deadline = 180_000_000 // point.frequency_hz
                        margin_isr = deadline - max_isr

                print(
                    f"{'PASS' if passed else 'FAIL'} "
                    f"(verdict={verdict}, isr_max={max_isr if max_isr is not None else '-'})",
                    flush=True,
                )

                # Reset application state cleanly on RIG for next test point
                try:
                    _reset_connection(connection)
                except Exception:
                    with suppress(Exception):
                        connection.close()
                    active_connection[0] = None

                return SweepRunOutcome(
                    config_id=config.config_id,
                    frequency_hz=point.frequency_hz,
                    point=point,
                    stage=stage,
                    passed=passed,
                    failure_domain=None if passed else FailureDomain.INTEGRITY,
                    failure_reason=None if passed else f"Run verdict: {verdict}",
                    maximum_isr_cycles=max_isr,
                    isr_margin_cycles=margin_isr,
                    payload_bytes=compiled.expected_tick_count,
                    duration_s=float(point.duration_s),
                    run_id=run_id_hex,
                )
            except Exception as err:
                print(f"ERROR ({err})", flush=True)
                if active_connection[0] is not None:
                    with suppress(Exception):
                        if active_connection[0].session_confirmed:
                            active_connection[0].abort()
                            active_connection[0].service()
                    with suppress(Exception):
                        active_connection[0].close()
                    active_connection[0] = None
                if builder:
                    with suppress(Exception):
                        captured_run = builder.finalize(status=CaptureStatus.FAILED)
                        write_run_artifacts(captured_run, out_dir)
                time.sleep(0.5)
                return SweepRunOutcome(
                    config_id=config.config_id,
                    frequency_hz=point.frequency_hz,
                    point=point,
                    stage=stage,
                    passed=False,
                    failure_domain=FailureDomain.UPLOAD,
                    failure_reason=str(err),
                    run_id=run_id_hex,
                )

        return _live_executor


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
