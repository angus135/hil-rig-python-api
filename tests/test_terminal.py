from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from hilrig.runner import ManualSessionSnapshot, ManualSessionState, RunSnapshot, WorkerState
from hilrig.terminal import HilRigCompleter, HilRigShell


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
        self.manual_finalizes: list[int] = []
        self.manual_resets = 0
        self.manual_inbox_clears = 0
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

    def manual_reset(self) -> bool:
        self.manual_resets += 1
        return True

    def manual_finalize(self, application_test_id: int) -> bool:
        self.manual_finalizes.append(application_test_id)
        return True

    def manual_clear_inbox(self) -> bool:
        self.manual_inbox_clears += 1
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
    shell.onecmd('manual send "C:\\Message Files\\instruction.json" --transport-only')
    shell.onecmd("manual finalize 00112233-4455-6677-8899-aabbccddeeff")
    shell.onecmd("manual inbox")
    shell.onecmd("manual status")
    shell.onecmd("manual disconnect")

    assert worker.manual_connects == [("COM2", True)]
    assert worker.manual_sends == [(Path("C:\\Message Files\\instruction.json"), True)]
    assert worker.manual_finalizes == [0x00112233445566778899AABBCCDDEEFF]
    rendered = output.getvalue()
    assert "Manual connection queued: COM2; System Information disabled." in rendered
    assert "Manual send queued:" in rendered
    assert "Manual finalize queued" in rendered
    assert "ApplicationResponse(scope=TICK" in rendered
    assert "Manual state: ready" in rendered
    assert "Manual disconnect requested." in rendered


def test_terminal_aliases_and_toolbar() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd('r "C:\\Test Files\\motor.py"')
    shell.onecmd("s")
    shell.onecmd("st")
    shell.onecmd("c")
    should_quit = shell.onecmd("q")

    assert worker.submitted[-1] == (Path("C:\\Test Files\\motor.py"), False)
    assert should_quit
    toolbar_text = shell._bottom_toolbar()
    assert "RUNNING" in toolbar_text
    assert "25/100" in toolbar_text


def test_terminal_completer() -> None:
    completer = HilRigCompleter()

    # Top-level commands
    top = [c.text for c in completer.get_completions(Document("ru"), None)]
    assert "run" in top

    # Manual subcommands
    manual_subs = [c.text for c in completer.get_completions(Document("manual "), None)]
    assert "connect" in manual_subs
    assert "send" in manual_subs
    assert "finalize" in manual_subs
    assert "inbox" in manual_subs
    assert "reset" in manual_subs

    # Manual connect options
    connect_opts = [c.text for c in completer.get_completions(Document("manual connect --"), None)]
    assert "--skip-system-info" in connect_opts

    # Manual inbox clear options
    inbox_opts = [c.text for c in completer.get_completions(Document("manual inbox cl"), None)]
    assert "clear" in inbox_opts


def test_terminal_ports_and_reset_commands() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd("ports")
    shell.onecmd("manual reset")
    shell.onecmd("reset")
    shell.onecmd("manual inbox clear")

    assert worker.manual_resets == 2
    assert worker.manual_inbox_clears == 1
    rendered = output.getvalue()
    assert "Manual reset queued" in rendered
    assert "Manual inbox cleared." in rendered


def test_terminal_ports_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    mock_ports = [
        SimpleNamespace(
            device="COM10",
            description="USB Serial Device (COM10)",
            hwid="USB VID:PID=0483:5740 SER=3868348C3534",
            vid=0x0483,
            pid=0x5740,
        ),
        SimpleNamespace(
            device="COM7",
            description="STMicroelectronics STLink Virtual COM Port (COM7)",
            hwid="USB VID:PID=0483:374B",
            vid=0x0483,
            pid=0x374B,
        ),
    ]

    import serial.tools.list_ports

    monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: mock_ports)

    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)
    shell.onecmd("ports")

    rendered = output.getvalue()
    assert "Available COM ports:" in rendered
    assert "COM10" in rendered
    assert "[HIL-RIG match]" in rendered
    assert "COM7" in rendered
