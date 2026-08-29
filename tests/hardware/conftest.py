from __future__ import annotations

import os
from pathlib import Path

import pytest

from hilrig.protocol_test.connection import ProtocolTestConnection
from hilrig.protocol_test.models import SerialSelector
from hilrig.protocol_test.runner import ProtocolTestRunner
from hilrig.protocol_test.serial_port import PySerialProvider
from hilrig.protocol_test.trace import TraceWriter


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _env_int(name: str) -> int | None:
    value = os.getenv(name)
    return None if not value else int(value, 0)


@pytest.fixture
def hardware_runner(tmp_path: Path, request: pytest.FixtureRequest):
    port = os.getenv("HILRIG_TEST_PORT")
    vid = _env_int("HILRIG_TEST_VID")
    pid = _env_int("HILRIG_TEST_PID")
    serial_number = os.getenv("HILRIG_TEST_SERIAL_NUMBER")
    if port is None and vid is None and pid is None and serial_number is None:
        pytest.skip("set HILRIG_TEST_PORT or explicit USB identity fields")
    selector = SerialSelector(port, vid, pid, serial_number)
    trace = TraceWriter(tmp_path, request.node.name, seed=1)
    connection = ProtocolTestConnection(PySerialProvider(), selector)
    runner = ProtocolTestRunner(connection, trace)
    try:
        runner.open()
    except Exception as exc:
        diagnostics = None if connection.closed else connection.get_diagnostics()
        runner.close()
        trace.finish(passed=False, failure_reason=str(exc), diagnostics=diagnostics)
        raise
    try:
        yield runner, trace
    finally:
        report = getattr(request.node, "rep_call", None)
        failed = bool(report is not None and report.failed)
        failure_reason = None if not failed else str(report.longrepr)
        diagnostics = None if connection.closed else connection.get_diagnostics()
        runner.close()
        trace.finish(
            passed=not failed,
            failure_reason=failure_reason,
            diagnostics=diagnostics,
        )
