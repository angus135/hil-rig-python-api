"""Immutable results produced by host-side assertion evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

from hilrig.results.models import CaptureStatus

EVALUATION_REPORT_SCHEMA_VERSION = "1.0"
EvaluationScalar: TypeAlias = str | int | float | bool | None


class EvaluationVerdict(str, Enum):
    """Outcome of an assertion or complete assertion set."""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


def immutable_evaluation_fields(
    values: dict[str, EvaluationScalar],
) -> MappingProxyType[str, EvaluationScalar]:
    """Copy report fields into an immutable mapping."""
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """Outcome and compact evidence summary for one compiled assertion."""

    assertion_id: int
    verdict: EvaluationVerdict
    peripheral: str
    channel: int
    assertion: str
    evaluated_from_tick: int
    evaluated_until_tick: int
    expected: MappingProxyType[str, EvaluationScalar]
    observed: MappingProxyType[str, EvaluationScalar]
    valid_sample_count: int
    missing_sample_count: int
    invalid_sample_count: int
    violation_count: int
    first_failure_tick: int | None
    message: str


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Complete, reusable evaluation result for one captured run and assertion set."""

    test_id: int
    run_id: int
    test_name: str
    capture_database: str
    capture_status: CaptureStatus
    expected_tick_count: int
    received_tick_count: int
    assertion_set_id: str
    compiled_ir_version: str
    evaluated_at: str
    verdict: EvaluationVerdict
    assertion_results: tuple[AssertionResult, ...]
    warnings: tuple[str, ...]
    schema_version: str = EVALUATION_REPORT_SCHEMA_VERSION

    @property
    def test_id_hex(self) -> str:
        return f"{self.test_id:032x}"

    @property
    def run_id_hex(self) -> str:
        return f"{self.run_id:032x}"

    @property
    def passed_count(self) -> int:
        return self._verdict_count(EvaluationVerdict.PASS)

    @property
    def failed_count(self) -> int:
        return self._verdict_count(EvaluationVerdict.FAIL)

    @property
    def inconclusive_count(self) -> int:
        return self._verdict_count(EvaluationVerdict.INCONCLUSIVE)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible evaluation report."""
        from hilrig.evaluation.reporting import as_evaluation_report

        return as_evaluation_report(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the evaluation report as JSON text."""
        from hilrig.evaluation.reporting import dumps_evaluation_report

        return dumps_evaluation_report(self, indent=indent)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> Path:
        """Write the machine-readable evaluation report."""
        from hilrig.evaluation.reporting import write_evaluation_report_json

        return write_evaluation_report_json(self, path, indent=indent)

    def to_markdown(self) -> str:
        """Return the human-readable evaluation report as Markdown."""
        from hilrig.evaluation.reporting import render_evaluation_report_markdown

        return render_evaluation_report_markdown(self)

    def write_markdown(self, path: str | Path) -> Path:
        """Write the human-readable evaluation report."""
        from hilrig.evaluation.reporting import write_evaluation_report_markdown

        return write_evaluation_report_markdown(self, path)

    def _verdict_count(self, verdict: EvaluationVerdict) -> int:
        return sum(result.verdict is verdict for result in self.assertion_results)
