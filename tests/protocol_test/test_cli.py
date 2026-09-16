from __future__ import annotations

from pathlib import Path

import pytest

import hilrig.protocol_test.cli as cli
from hilrig.protocol_test.cli import build_parser


def test_reset_reconnect_cli_exposes_explicit_unobserved_reset_fallback() -> None:
    args = build_parser().parse_args(
        ["reset-reconnect", "--port", "fake", "--allow-unobserved-reset"]
    )
    assert args.allow_unobserved_reset is True


@pytest.mark.parametrize(
    ("arguments", "scenario", "count"),
    [
        (["application-smoke", "--port", "fake"], "application-smoke", None),
        (["application-boundaries", "--port", "fake"], "application-boundaries", None),
        (["application-negative", "--port", "fake"], "application-negative", None),
        (["application-v02", "--port", "fake"], "application-v02", None),
        (
            ["application-repeat", "--port", "fake", "--count", "7"],
            "application-repeat",
            7,
        ),
        (
            ["application-reset-reconnect", "--port", "fake", "--allow-unobserved-reset"],
            "application-reset-reconnect",
            None,
        ),
    ],
)
def test_application_cli_parsing(arguments: list[str], scenario: str, count: int | None) -> None:
    args = build_parser().parse_args(arguments)
    assert args.scenario == scenario
    if count is not None:
        assert args.count == count


class _Trace:
    def __init__(self, output_dir: Path, scenario: str, *, seed: int) -> None:
        self.summary_path = output_dir / f"{scenario}.summary.json"
        self.trace_path = output_dir / f"{scenario}.jsonl"

    def record(self, kind: str, **fields: object) -> None:
        pass

    def finish(self, **fields: object) -> None:
        pass


class _Connection:
    def __init__(self, provider: object, selector: object, *, baud: int) -> None:
        self.closed = False
        self.serial_identity = "fake"
        self.transport_config = "config"

    def get_diagnostics(self) -> dict[str, object]:
        return {}


class _Runner:
    calls: list[tuple[str, object]] = []

    def __init__(self, connection: object, trace: object, **kwargs: object) -> None:
        self.connection = connection

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.connection.closed = True

    def run_application_smoke(self) -> dict[str, object]:
        self.calls.append(("application-smoke", None))
        return {}

    def run_application_boundaries(self) -> dict[str, object]:
        self.calls.append(("application-boundaries", None))
        return {}

    def run_application_negative(self) -> dict[str, object]:
        self.calls.append(("application-negative", None))
        return {}

    def run_application_v02(self) -> dict[str, object]:
        self.calls.append(("application-v02", None))
        return {}

    def run_application_repeat(self, count: int) -> dict[str, object]:
        self.calls.append(("application-repeat", count))
        return {}

    def run_application_reset_reconnect(
        self, *, prompt: object, allow_unobserved_reset: bool
    ) -> dict[str, object]:
        self.calls.append(("application-reset-reconnect", allow_unobserved_reset))
        return {}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["application-smoke", "--port", "fake"], ("application-smoke", None)),
        (["application-boundaries", "--port", "fake"], ("application-boundaries", None)),
        (["application-negative", "--port", "fake"], ("application-negative", None)),
        (["application-v02", "--port", "fake"], ("application-v02", None)),
        (
            ["application-repeat", "--port", "fake", "--count", "4"],
            ("application-repeat", 4),
        ),
        (
            ["application-reset-reconnect", "--port", "fake", "--allow-unobserved-reset"],
            ("application-reset-reconnect", True),
        ),
    ],
)
def test_application_cli_dispatch(
    monkeypatch, tmp_path: Path, arguments: list[str], expected
) -> None:
    _Runner.calls = []
    monkeypatch.setattr(cli, "TraceWriter", _Trace)
    monkeypatch.setattr(cli, "ProtocolTestConnection", _Connection)
    monkeypatch.setattr(cli, "ProtocolTestRunner", _Runner)
    monkeypatch.setattr(cli, "PySerialProvider", lambda: object())
    assert cli.main([*arguments, "--output-dir", str(tmp_path)]) == 0
    assert _Runner.calls == [expected]
