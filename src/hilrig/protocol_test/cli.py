"""Command-line entry point for the temporary DEV-138 Transport hardware harness."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from .connection import ProtocolTestConnection
from .models import SerialSelector
from .runner import ProtocolTestRunner, ScenarioFailure
from .serial_port import (
    FaultInjectingProvider,
    FaultPlan,
    PySerialProvider,
    SerialDependencyError,
    SerialIOError,
    SerialSelectionError,
)
from .trace import TraceWriter


def _auto_int(value: str) -> int:
    return int(value, 0)


def _operation_set(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()
    return frozenset(int(item) for item in value.split(",") if item.strip())


def _corruption(value: str | None) -> tuple[tuple[int, int], ...]:
    if not value:
        return ()
    result: list[tuple[int, int]] = []
    for item in value.split(","):
        operation, offset = item.split(":", 1)
        result.append((int(operation), int(offset)))
    return tuple(result)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--vid", type=_auto_int)
    parser.add_argument("--pid", type=_auto_int)
    parser.add_argument("--serial-number")
    parser.add_argument("--poll-ms", type=float, default=1.0)
    parser.add_argument("--request-timeout-ms", type=int, default=3000)
    parser.add_argument("--reconnect-timeout-ms", type=int, default=15000)
    parser.add_argument("--output-dir", type=Path, default=Path("protocol-test-evidence"))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument("--fault-max-read", type=int)
    parser.add_argument("--fault-max-write", type=int)
    parser.add_argument(
        "--fault-zero-write", help="comma-separated 1-based write operation numbers"
    )
    parser.add_argument(
        "--fault-drop-write", help="comma-separated 1-based write operation numbers"
    )
    parser.add_argument(
        "--fault-duplicate-write", help="comma-separated 1-based write operation numbers"
    )
    parser.add_argument(
        "--fault-corrupt-write", help="comma-separated operation:byte-offset entries"
    )
    parser.add_argument("--fault-delay-read", help="comma-separated 1-based read operation numbers")
    parser.add_argument(
        "--fault-delay-write", help="comma-separated 1-based write operation numbers"
    )
    parser.add_argument("--fault-delay-ms", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="scenario", required=True)
    for name in ("smoke", "status", "boundaries"):
        sub = subparsers.add_parser(name)
        _add_common(sub)
    repeat = subparsers.add_parser("repeat")
    _add_common(repeat)
    repeat.add_argument("--count", type=int, required=True)
    reset = subparsers.add_parser("reset-reconnect")
    _add_common(reset)
    reset.add_argument("--cycles", type=int, default=1)
    soak = subparsers.add_parser("soak")
    _add_common(soak)
    soak.add_argument("--duration-seconds", type=float)
    soak.add_argument("--count", type=int)
    soak.add_argument("--status-every", type=int, default=100)
    return parser


def _fault_plan(args: argparse.Namespace) -> FaultPlan:
    return FaultPlan(
        max_read_chunk=args.fault_max_read,
        max_write_accept=args.fault_max_write,
        zero_write_operations=_operation_set(args.fault_zero_write),
        delay_read_operations=_operation_set(args.fault_delay_read),
        delay_write_operations=_operation_set(args.fault_delay_write),
        drop_write_operations=_operation_set(args.fault_drop_write),
        duplicate_write_operations=_operation_set(args.fault_duplicate_write),
        corrupt_write_operations=_corruption(args.fault_corrupt_write),
        delay_ms=args.fault_delay_ms,
        seed=args.seed,
    )


def _prompt(message: str) -> None:
    input(f"{message}. Press Enter after reset has been initiated: ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))
    try:
        selector = SerialSelector(args.port, args.vid, args.pid, args.serial_number)
    except ValueError as exc:
        parser.error(str(exc))
    trace = TraceWriter(args.output_dir, args.scenario, seed=args.seed)
    plan = _fault_plan(args)
    base_provider = PySerialProvider()
    provider = (
        FaultInjectingProvider(
            base_provider,
            plan,
            lambda action: trace.record("fault_injection", **action),
        )
        if plan.enabled
        else base_provider
    )
    connection: ProtocolTestConnection | None = None
    runner: ProtocolTestRunner | None = None
    passed = False
    failure_reason: str | None = None
    result: dict[str, object] = {}
    try:
        connection = ProtocolTestConnection(provider, selector, baud=args.baud)
        runner = ProtocolTestRunner(
            connection,
            trace,
            poll_ms=args.poll_ms,
            request_timeout_ms=args.request_timeout_ms,
            reconnect_timeout_ms=args.reconnect_timeout_ms,
            seed=args.seed,
        )
        runner.open()
        if args.scenario == "smoke":
            result = runner.run_smoke()
        elif args.scenario == "status":
            result = {"status": runner.run_status()}
        elif args.scenario == "boundaries":
            result = runner.run_boundaries()
        elif args.scenario == "repeat":
            result = runner.run_repeat(args.count)
        elif args.scenario == "reset-reconnect":
            result = runner.run_reset_reconnect(args.cycles, prompt=_prompt)
        elif args.scenario == "soak":
            result = runner.run_soak(
                duration_seconds=args.duration_seconds,
                count=args.count,
                status_every=args.status_every,
            )
        else:  # pragma: no cover - argparse guarantees the set above.
            raise AssertionError(args.scenario)
        passed = True
    except (
        ScenarioFailure,
        SerialDependencyError,
        SerialSelectionError,
        SerialIOError,
        RuntimeError,
        ValueError,
    ) as exc:
        failure_reason = str(exc)
        trace.record("scenario_failure", reason=failure_reason)
    finally:
        diagnostics = None
        serial_device = None
        effective_config = None
        if connection is not None:
            try:
                diagnostics = connection.get_diagnostics() if not connection.closed else None
                serial_device = connection.serial_identity
                effective_config = connection.transport_config
            except RuntimeError:
                pass
        if runner is not None:
            try:
                runner.close()
            except RuntimeError as exc:
                if passed:
                    passed = False
                    failure_reason = f"cleanup failed: {exc}"
        summary = trace.finish(
            passed=passed,
            failure_reason=failure_reason,
            diagnostics=diagnostics,
            extra={
                "result": result,
                "selected_serial_device": serial_device,
                "effective_transport_config": effective_config,
                "fault_plan": plan,
            },
        )
    print(
        f"{args.scenario}: {'PASS' if passed else 'FAIL'} | "
        f"summary={trace.summary_path} | trace={trace.trace_path}"
    )
    if not passed and failure_reason:
        print(f"reason: {failure_reason}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
