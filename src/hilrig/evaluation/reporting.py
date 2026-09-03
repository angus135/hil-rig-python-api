"""JSON and Markdown exporters for completed assertion evaluation reports."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from hilrig.evaluation.models import EvaluationReport, EvaluationScalar


def as_evaluation_report(report: EvaluationReport) -> dict[str, object]:
    """Return a stable JSON-compatible evaluation report document."""
    return {
        "evaluation_report_version": report.schema_version,
        "test": {
            "test_id": report.test_id_hex,
            "name": report.test_name,
        },
        "run": {
            "run_id": report.run_id_hex,
            "capture_database": report.capture_database,
            "capture_status": report.capture_status.value,
            "expected_tick_count": report.expected_tick_count,
            "received_tick_count": report.received_tick_count,
        },
        "assertion_set": {
            "assertion_set_id": report.assertion_set_id,
            "compiled_ir_version": report.compiled_ir_version,
        },
        "evaluation": {
            "evaluated_at": report.evaluated_at,
            "verdict": report.verdict.value,
            "passed_count": report.passed_count,
            "failed_count": report.failed_count,
            "inconclusive_count": report.inconclusive_count,
            "warnings": list(report.warnings),
        },
        "assertions": [
            {
                "assertion_id": result.assertion_id,
                "verdict": result.verdict.value,
                "peripheral": result.peripheral,
                "channel": result.channel,
                "assertion": result.assertion,
                "evaluated_from_tick": result.evaluated_from_tick,
                "evaluated_until_tick": result.evaluated_until_tick,
                "expected": dict(result.expected),
                "observed": dict(result.observed),
                "valid_sample_count": result.valid_sample_count,
                "missing_sample_count": result.missing_sample_count,
                "invalid_sample_count": result.invalid_sample_count,
                "violation_count": result.violation_count,
                "first_failure_tick": result.first_failure_tick,
                "message": result.message,
            }
            for result in report.assertion_results
        ],
    }


def dumps_evaluation_report(report: EvaluationReport, *, indent: int | None = 2) -> str:
    """Serialize an evaluation report to deterministic JSON text."""
    return json.dumps(as_evaluation_report(report), indent=indent, ensure_ascii=False) + "\n"


def write_evaluation_report_json(
    report: EvaluationReport,
    path: str | Path,
    *,
    indent: int | None = 2,
) -> Path:
    """Write a JSON evaluation report and return its absolute path."""
    output = _output_path(path, suffix=".json")
    output.write_text(dumps_evaluation_report(report, indent=indent), encoding="utf-8")
    return output


def render_evaluation_report_markdown(report: EvaluationReport) -> str:
    """Render a concise human-readable report with per-assertion evidence."""
    lines = [
        f"# HIL-RIG Test Report: {report.test_name}",
        "",
        f"**Overall verdict:** `{report.verdict.value.upper()}`  ",
        f"**Capture status:** `{report.capture_status.value.upper()}`  ",
        f"**Test ID:** `{report.test_id_hex}`  ",
        f"**Run ID:** `{report.run_id_hex}`  ",
        f"**Capture database:** `{report.capture_database}`  ",
        f"**Evaluated at:** `{report.evaluated_at}`",
        "",
        "## Summary",
        "",
        f"- Expected fixed ticks: {report.expected_tick_count}",
        f"- Received fixed ticks: {report.received_tick_count}",
        f"- Assertion set: `{report.assertion_set_id}`",
        f"- Compiled IR version: `{report.compiled_ir_version}`",
        f"- Passed: {report.passed_count}",
        f"- Failed: {report.failed_count}",
        f"- Inconclusive: {report.inconclusive_count}",
        "",
        "## Assertion results",
        "",
        "| ID | Verdict | Assertion | Channel | Tick/window | Summary |",
        "|---:|---|---|---:|---|---|",
    ]
    if report.assertion_results:
        lines.extend(
            "| "
            f"{result.assertion_id} | {result.verdict.value.upper()} | "
            f"{_cell(result.peripheral)}.{_cell(result.assertion)} | {result.channel} | "
            f"{_tick_window(result.evaluated_from_tick, result.evaluated_until_tick)} | "
            f"{_cell(result.message)} |"
            for result in report.assertion_results
        )
    else:
        lines.append("| — | INCONCLUSIVE | No assertions | — | — | Nothing to evaluate. |")

    for result in report.assertion_results:
        lines.extend(
            [
                "",
                f"### Assertion {result.assertion_id}: {result.verdict.value.upper()}",
                "",
                f"- Definition: `{result.peripheral}[{result.channel}].{result.assertion}`",
                "- Tick/window: "
                f"`{_tick_window(result.evaluated_from_tick, result.evaluated_until_tick)}`",
                f"- Expected: {_format_fields(result.expected)}",
                f"- Observed: {_format_fields(result.observed)}",
                f"- Valid samples: {result.valid_sample_count}",
                f"- Missing samples: {result.missing_sample_count}",
                f"- Invalid samples: {result.invalid_sample_count}",
                f"- Violations: {result.violation_count}",
                f"- First failure tick: "
                f"{'—' if result.first_failure_tick is None else result.first_failure_tick}",
                "",
                result.message,
            ]
        )

    lines.extend(["", "## Warnings", ""])
    if report.warnings:
        lines.extend(f"- {warning}" for warning in report.warnings)
    else:
        lines.append("No evaluation warnings.")
    return "\n".join(lines) + "\n"


def write_evaluation_report_markdown(
    report: EvaluationReport,
    path: str | Path,
) -> Path:
    """Write a Markdown evaluation report and return its absolute path."""
    output = _output_path(path, suffix=".md")
    output.write_text(render_evaluation_report_markdown(report), encoding="utf-8")
    return output


def _tick_window(from_tick: int, until_tick: int) -> str:
    return str(from_tick) if from_tick == until_tick else f"{from_tick}–{until_tick}"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _format_fields(values: Mapping[str, EvaluationScalar]) -> str:
    if not values:
        return "—"
    return "; ".join(f"`{name}={_format_value(name, value)}`" for name, value in values.items())


def _format_value(name: str, value: EvaluationScalar) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and name.endswith("_uv"):
        return f"{value / 1_000_000:g} V ({value} µV)"
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (name == "duty_cycle" or "duty_cycle" in name)
    ):
        return f"{value:g} ({value * 100:g}%)"
    return json.dumps(value, ensure_ascii=False)


def _output_path(path: str | Path, *, suffix: str) -> Path:
    output = Path(path).expanduser().resolve()
    if output.suffix.lower() != suffix:
        raise ValueError(f"Output path must end in {suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output
