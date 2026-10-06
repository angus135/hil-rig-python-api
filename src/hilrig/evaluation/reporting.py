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
            "application_test_id": report.application_test_id_hex,
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
        "assertion_groups": [
            {
                "group_id": group.group_id,
                "name": group.name,
                "verdict": group.verdict.value,
                "passed_count": group.passed_count,
                "failed_count": group.failed_count,
                "inconclusive_count": group.inconclusive_count,
                "stimulus_ids": [stimulus.instruction_id for stimulus in group.stimuli],
                "assertion_ids": [result.assertion_id for result in group.assertion_results],
            }
            for group in report.assertion_groups
        ],
        "stimuli": [
            {
                "instruction_id": stimulus.instruction_id,
                "group_id": stimulus.group_id,
                "group_name": stimulus.group_name,
                "subject_name": stimulus.subject_name,
                "tick": stimulus.tick,
                "time_ns": stimulus.time_ns,
                "peripheral": stimulus.peripheral,
                "channel": stimulus.channel,
                "operation": stimulus.operation,
                "arguments": dict(stimulus.arguments),
                "message": stimulus.message,
            }
            for stimulus in report.stimuli
        ],
        "assertions": [
            {
                "assertion_id": result.assertion_id,
                "group_id": result.group_id,
                "group_name": result.group_name,
                "subject_name": result.subject_name,
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
    """Render an operator-focused report while retaining full evidence in JSON/SQLite."""
    lines = [
        f"# HIL-RIG Test Report: {report.test_name}",
        "",
        f"**Overall verdict:** `{report.verdict.value.upper()}`  ",
        f"**Capture status:** `{report.capture_status.value.upper()}`  ",
        f"**Assertions:** {report.passed_count} passed, {report.failed_count} failed, "
        f"{report.inconclusive_count} inconclusive",
        "",
        "## Failures",
        "",
    ]
    failures = tuple(
        result for result in report.assertion_results if result.verdict.value == "fail"
    )
    if failures:
        lines.extend(
            [
                "| Group | Subject | Tick/window | Reason |",
                "|---|---|---|---|",
            ]
        )
        lines.extend(
            f"| {_cell(result.group_name)} | {_cell(result.subject_name)} | "
            f"{_tick_window(result.evaluated_from_tick, result.evaluated_until_tick)} | "
            f"{_cell(_compact_text(result.message))} |"
            for result in failures
        )
    else:
        lines.append("No failed assertions.")

    lines.extend(["", "## Group overview", ""])
    if report.assertion_groups:
        lines.extend(
            [
                "| Group | Verdict | Commands | Payload | Assertions |",
                "|---|---|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| {_cell(group.name)} | {group.verdict.value.upper()} | "
            f"{len(group.stimuli)} | {_format_byte_count(_group_payload_bytes(group.stimuli))} | "
            f"{len(group.assertion_results)} |"
            for group in report.assertion_groups
        )
        lines.extend(["", "## Group details", ""])
        for group in report.assertion_groups:
            lines.extend(
                [
                    f"### {_heading(group.name)}: {group.verdict.value.upper()}",
                    "",
                ]
            )
            if group.stimuli:
                lines.extend(
                    [
                        "**Commanded stimuli**",
                        "",
                        "| Subject | Operation | Commands | Active ticks | Tick range | "
                        "Payload | Avg/active tick | Max/active tick |",
                        "|---|---|---:|---:|---|---:|---:|---:|",
                    ]
                )
                lines.extend(
                    _stimulus_summary_row(summary) for summary in _stimulus_summaries(group.stimuli)
                )
                examples = _representative_stimuli(group.stimuli)
                lines.extend(["", "Examples:"])
                lines.extend(f"- {_compact_text(item.message)}" for item in examples)
                if len(group.stimuli) > len(examples):
                    lines.append(
                        f"- {len(group.stimuli) - len(examples)} additional commands are "
                        "retained in `evaluation-report.json` and `captured-run.sqlite3`."
                    )
                lines.append("")
            if group.assertion_results:
                lines.extend(
                    [
                        "**Assertions**",
                        "",
                        "| ID | Verdict | Subject | Assertion | Tick/window | Expected | Summary |",
                        "|---:|---|---|---|---|---|---|",
                    ]
                )
                lines.extend(
                    "| "
                    f"{result.assertion_id} | {result.verdict.value.upper()} | "
                    f"{_cell(result.subject_name)} | {_cell(result.assertion)} | "
                    f"{_tick_window(result.evaluated_from_tick, result.evaluated_until_tick)} | "
                    f"{_cell(_compact_text(_format_fields(result.expected)))} | "
                    f"{_cell(_compact_text(result.message))} |"
                    for result in group.assertion_results
                )
            else:
                lines.append("No assertions belong to this group.")
            lines.append("")
    else:
        lines.append("No assertion groups were evaluated.")

    issues = tuple(result for result in report.assertion_results if result.verdict.value != "pass")
    if issues:
        lines.extend(["", "## Assertion evidence"])
        for result in issues:
            lines.extend(
                [
                    "",
                    f"### Assertion {result.assertion_id}: {result.verdict.value.upper()}",
                    "",
                    f"- Group: `{result.group_name}`",
                    f"- Subject: `{result.subject_name}`",
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
                    f"{'-' if result.first_failure_tick is None else result.first_failure_tick}",
                    "",
                    result.message,
                ]
            )

    lines.extend(
        [
            "",
            "## Run details",
            "",
            f"- **Test ID:** `{report.test_id_hex}`",
            f"- **Application Test ID:** `{report.application_test_id_hex}`",
            f"- **Run ID:** `{report.run_id_hex}`",
            f"- Capture database: `{report.capture_database}`",
            f"- Evaluated at: `{report.evaluated_at}`",
            f"- Fixed ticks: {report.received_tick_count} received / "
            f"{report.expected_tick_count} expected",
            f"- Assertion set: `{report.assertion_set_id}`",
            f"- Compiled IR version: `{report.compiled_ir_version}`",
            "",
            "## Warnings",
            "",
        ]
    )
    if report.warnings:
        lines.extend(f"- {warning}" for warning in report.warnings)
    else:
        lines.append("No evaluation warnings.")
    return "\n".join(lines) + "\n"


def _stimulus_summaries(stimuli) -> tuple[dict[str, object], ...]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    for stimulus in stimuli:
        key = (stimulus.subject_name, stimulus.operation)
        summary = grouped.setdefault(
            key,
            {
                "subject": stimulus.subject_name,
                "operation": stimulus.operation,
                "commands": 0,
                "first_tick": stimulus.tick,
                "last_tick": stimulus.tick,
                "bytes_by_tick": {},
            },
        )
        summary["commands"] = int(summary["commands"]) + 1
        summary["last_tick"] = stimulus.tick
        payload_bytes = _stimulus_payload_bytes(stimulus.arguments)
        bytes_by_tick = summary["bytes_by_tick"]
        assert isinstance(bytes_by_tick, dict)
        bytes_by_tick[stimulus.tick] = bytes_by_tick.get(stimulus.tick, 0) + payload_bytes
    return tuple(grouped.values())


def _stimulus_summary_row(summary: dict[str, object]) -> str:
    bytes_by_tick = summary["bytes_by_tick"]
    assert isinstance(bytes_by_tick, dict)
    payload_bytes = sum(bytes_by_tick.values())
    active_ticks = len(bytes_by_tick)
    average = payload_bytes / active_ticks if active_ticks else 0
    maximum = max(bytes_by_tick.values(), default=0)
    first_tick = int(summary["first_tick"])
    last_tick = int(summary["last_tick"])
    tick_range = str(first_tick) if first_tick == last_tick else f"{first_tick}-{last_tick}"
    return (
        f"| {_cell(str(summary['subject']))} | {_cell(str(summary['operation']))} | "
        f"{summary['commands']} | {active_ticks} | {tick_range} | "
        f"{_format_byte_count(payload_bytes)} | {average:.2f} B | {maximum} B |"
    )


def _representative_stimuli(stimuli):
    if len(stimuli) <= 2:
        return tuple(stimuli)
    return (stimuli[0], stimuli[-1])


def _group_payload_bytes(stimuli) -> int:
    return sum(_stimulus_payload_bytes(item.arguments) for item in stimuli)


def _stimulus_payload_bytes(arguments: Mapping[str, EvaluationScalar]) -> int:
    for name in ("data", "tx_data"):
        value = arguments.get(name)
        if isinstance(value, str) and value.startswith("0x"):
            return max(0, (len(value) - 2) // 2)
    return 0


def _format_byte_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f} MB"
    if value >= 1_000:
        return f"{value / 1_000:.2f} kB"
    return f"{value} B"


def _compact_text(value: str, *, limit: int = 120) -> str:
    compact = value.replace("\r", " ").replace("\n", " ").strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def write_evaluation_report_markdown(
    report: EvaluationReport,
    path: str | Path,
) -> Path:
    """Write a Markdown evaluation report and return its absolute path."""
    output = _output_path(path, suffix=".md")
    output.write_text(
        render_evaluation_report_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    return output


def _tick_window(from_tick: int, until_tick: int) -> str:
    return str(from_tick) if from_tick == until_tick else f"[{from_tick}, {until_tick})"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _heading(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _format_fields(values: Mapping[str, EvaluationScalar]) -> str:
    if not values:
        return "-"
    return "; ".join(f"`{name}={_format_value(name, value)}`" for name, value in values.items())


def _format_value(name: str, value: EvaluationScalar) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and name.endswith("_uv"):
        return f"{value / 1_000_000:g} V ({value} uV)"
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
