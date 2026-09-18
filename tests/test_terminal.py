from __future__ import annotations

from io import StringIO
from pathlib import Path

from hilrig.runner import ManualSessionSnapshot, ManualSessionState, RunSnapshot, WorkerState
from hilrig.terminal import HilRigShell


class _StubWorker:
    def __init__(self) -> None:
        self.started = False
        self.submitted: list[tuple[Path, bool]] = []
        self.abort_result = True
        self.step_result = True
        self.continue_result = True
        self.shutdown_called = False
        self.manual_connects: list[tuple[str | None, bool]] = []
        self.manual_sends: list[tuple[Path, bool]] = []
        self.manual_disconnect_result = True
        self.manual_current = ManualSessionSnapshot(
            state=ManualSessionState.READY,
            detail="Manual protocol session ready.",
            device="COM2",
            skip_system_info=True,
            inbox_count=1,
        )
        self.current = RunSnapshot(
            state=WorkerState.RUNNING,
            detail="Receiving results.",
            test_name="Terminal test",
            expected_tick_count=100,
            received_tick_count=25,
        )

    def start(self) -> None:
        self.started = True

    def submit(self, path: Path, *, stepped: bool = False) -> bool:
        self.submitted.append((path, stepped))
        return True

    def step(self) -> bool:
        return self.step_result

    def continue_run(self) -> bool:
        return self.continue_result

    def snapshot(self) -> RunSnapshot:
        return self.current

    def abort(self) -> bool:
        return self.abort_result

    def manual_connect(self, *, device=None, skip_system_info=False) -> bool:
        self.manual_connects.append((device, skip_system_info))
        return True

    def manual_send(self, path: Path, *, transport_only=False) -> bool:
        self.manual_sends.append((path, transport_only))
        return True

    def manual_inbox(self) -> tuple[str, ...]:
        return ("ApplicationResponse(scope=TICK, outcome=ACCEPTED, tick=10)",)

    def manual_snapshot(self) -> ManualSessionSnapshot:
        return self.manual_current

    def manual_disconnect(self) -> bool:
        return self.manual_disconnect_result

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_terminal_minimum_commands() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd('run "C:\\Test Files\\motor.py"')
    shell.onecmd('run --step "C:\\Test Files\\manual.py"')
    shell.onecmd("status")
    shell.onecmd("help")
    shell.onecmd("step")
    shell.onecmd("continue")
    shell.onecmd("abort")
    should_quit = shell.onecmd("quit")

    assert worker.started
    assert worker.submitted == [
        (Path("C:\\Test Files\\motor.py"), False),
        (Path("C:\\Test Files\\manual.py"), True),
    ]
    assert worker.shutdown_called
    assert should_quit
    rendered = output.getvalue()
    assert "Run queued:" in rendered
    assert "State: running" in rendered
    assert "Results: 25/100 ticks" in rendered
    assert "run --step <path>" in rendered
    assert "Step requested." in rendered
    assert "Continue requested" in rendered
    assert "Abort requested." in rendered


def test_terminal_rejects_missing_arguments_and_unknown_commands() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd("run")
    shell.onecmd("status extra")
    shell.onecmd("unknown")

    rendered = output.getvalue()
    assert 'Usage: run [--step] "path to test.py"' in rendered
    assert "Usage: status" in rendered
    assert "Unknown command" in rendered


def test_terminal_manual_commands_parse_port_flags_and_quoted_message_path() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd("manual connect COM=2 --skip-system-info")
    shell.onecmd(
        'manual send "C:\\Message Files\\instruction.json" --transport-only'
    )
    shell.onecmd("manual inbox")
    shell.onecmd("manual status")
    shell.onecmd("manual disconnect")

    assert worker.manual_connects == [("COM2", True)]
    assert worker.manual_sends == [
        (Path("C:\\Message Files\\instruction.json"), True)
    ]
    rendered = output.getvalue()
    assert "Manual connection queued: COM2; System Information disabled." in rendered
    assert "Manual send queued:" in rendered
    assert "ApplicationResponse(scope=TICK" in rendered
    assert "Manual state: ready" in rendered
    assert "Manual disconnect requested." in rendered
