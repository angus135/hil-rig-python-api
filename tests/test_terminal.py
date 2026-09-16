from __future__ import annotations

from io import StringIO
from pathlib import Path

from hilrig.runner import RunSnapshot, WorkerState
from hilrig.terminal import HilRigShell


class _StubWorker:
    def __init__(self) -> None:
        self.started = False
        self.submitted: list[Path] = []
        self.abort_result = True
        self.shutdown_called = False
        self.current = RunSnapshot(
            state=WorkerState.RUNNING,
            detail="Receiving results.",
            test_name="Terminal test",
            expected_tick_count=100,
            received_tick_count=25,
        )

    def start(self) -> None:
        self.started = True

    def submit(self, path: Path) -> bool:
        self.submitted.append(path)
        return True

    def snapshot(self) -> RunSnapshot:
        return self.current

    def abort(self) -> bool:
        return self.abort_result

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_terminal_minimum_commands() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd('run "C:\\Test Files\\motor.py"')
    shell.onecmd("status")
    shell.onecmd("help")
    shell.onecmd("abort")
    should_quit = shell.onecmd("quit")

    assert worker.started
    assert worker.submitted == [Path("C:\\Test Files\\motor.py")]
    assert worker.shutdown_called
    assert should_quit
    rendered = output.getvalue()
    assert "Run queued:" in rendered
    assert "State: running" in rendered
    assert "Results: 25/100 ticks" in rendered
    assert "run <path>" in rendered
    assert "Abort requested." in rendered


def test_terminal_rejects_missing_arguments_and_unknown_commands() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd("run")
    shell.onecmd("status extra")
    shell.onecmd("unknown")

    rendered = output.getvalue()
    assert 'Usage: run "path to test.py"' in rendered
    assert "Usage: status" in rendered
    assert "Unknown command" in rendered
