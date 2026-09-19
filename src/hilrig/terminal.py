"""Persistent command-line application for automatic or stepped HIL-RIG runs."""

from __future__ import annotations

import cmd
import shlex
import sys
import threading
from pathlib import Path
from typing import TextIO

from hilrig.runner import ManualSessionSnapshot, ProtocolWorker, RunSnapshot


class HilRigShell(cmd.Cmd):
    """Minimal persistent terminal front end for the protocol worker."""

    intro = "HIL-RIG terminal. Enter 'help' for commands."
    prompt = "HIL-RIG> "

    def __init__(
        self,
        *,
        worker: ProtocolWorker | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        super().__init__(stdin=stdin, stdout=stdout)
        self._output_lock = threading.Lock()
        self.worker = worker or ProtocolWorker(notification_callback=self._write_notification)
        self.worker.start()

    def do_run(self, argument: str) -> None:
        """run [--step] <path> -- Execute a Python test definition."""
        parsed = _run_argument(argument)
        if parsed is None:
            self._write_line('Usage: run [--step] "path to test.py"')
            return
        path, stepped = parsed
        if not self.worker.submit(path, stepped=stepped):
            self._write_line("A test is already active. Use 'status' or 'abort'.")
            return
        mode = "Stepped run" if stepped else "Run"
        self._write_line(f"{mode} queued: {path}")

    def do_help(self, argument: str) -> None:
        """help -- Show the commands available in this first terminal version."""
        if argument.strip():
            self._write_line("Usage: help")
            return
        self._write_line(
            "Commands:\n"
            "  run <path>         Load and automatically execute a test-definition file.\n"
            "  run --step <path>  Pause before configuration, each tick, and START.\n"
            "  step               Release exactly one paused operation.\n"
            "  continue           Release the gate and finish automatically.\n"
            "  status             Show the current or most recently completed run.\n"
            "  abort              Request cancellation of the active run.\n"
            "  manual connect [COM=<n>] [--skip-system-info]\n"
            "                     Open a persistent manual protocol session.\n"
            "  manual send <message-file> [--transport-only]\n"
            "                     Send one standalone JSON Application message.\n"
            "  manual inbox      Show received manual Application messages.\n"
            "  manual status     Show manual-session status.\n"
            "  manual disconnect Close the manual protocol session.\n"
            "  help               Show this command list.\n"
            "  quit               Abort any active run and close the terminal."
        )

    def do_step(self, argument: str) -> None:
        """step -- Release one operation in a paused stepped run."""
        if argument.strip():
            self._write_line("Usage: step")
            return
        if self.worker.step():
            self._write_line("Step requested.")
        else:
            self._write_line("No stepped run is currently paused.")

    def do_continue(self, argument: str) -> None:
        """continue -- Make the remainder of a paused stepped run automatic."""
        if argument.strip():
            self._write_line("Usage: continue")
            return
        if self.worker.continue_run():
            self._write_line("Continue requested; the remainder will run automatically.")
        else:
            self._write_line("No stepped run is currently paused.")

    def do_status(self, argument: str) -> None:
        """status -- Show the current or most recently completed run."""
        if argument.strip():
            self._write_line("Usage: status")
            return
        self._write_line(_format_status(self.worker.snapshot()))

    def do_abort(self, argument: str) -> None:
        """abort -- Request cancellation of the active run."""
        if argument.strip():
            self._write_line("Usage: abort")
            return
        if self.worker.abort():
            self._write_line("Abort requested.")
        else:
            self._write_line("There is no abortable active run.")

    def do_manual(self, argument: str) -> None:
        """manual <command> -- Manage a standalone Application debugging session."""
        try:
            tokens = shlex.split(argument, posix=False)
        except ValueError as error:
            self._write_line(f"Invalid manual command: {error}")
            return
        if not tokens:
            self._write_line(_manual_usage())
            return
        command = tokens.pop(0).lower()
        if command == "connect":
            self._manual_connect(tokens)
        elif command == "send":
            self._manual_send(tokens)
        elif command == "inbox":
            if tokens:
                self._write_line("Usage: manual inbox")
            else:
                inbox = self.worker.manual_inbox()
                self._write_line(
                    "Manual inbox is empty."
                    if not inbox
                    else "Manual inbox:\n" + "\n".join(f"  {item}" for item in inbox)
                )
        elif command == "status":
            if tokens:
                self._write_line("Usage: manual status")
            else:
                self._write_line(_format_manual_status(self.worker.manual_snapshot()))
        elif command == "disconnect":
            if tokens:
                self._write_line("Usage: manual disconnect")
            elif self.worker.manual_disconnect():
                self._write_line("Manual disconnect requested.")
            else:
                self._write_line("There is no active manual session.")
        else:
            self._write_line(_manual_usage())

    def _manual_connect(self, tokens: list[str]) -> None:
        device = None
        skip_system_info = False
        for token in tokens:
            if token.lower() == "--skip-system-info":
                if skip_system_info:
                    self._write_line(_manual_connect_usage())
                    return
                skip_system_info = True
            elif token.lower().startswith("com="):
                if device is not None:
                    self._write_line(_manual_connect_usage())
                    return
                number = token.partition("=")[2]
                if not number.isdecimal() or int(number) < 1:
                    self._write_line("COM must be a positive port number, for example COM=2.")
                    return
                device = f"COM{int(number)}"
            else:
                self._write_line(_manual_connect_usage())
                return
        if not self.worker.manual_connect(
            device=device,
            skip_system_info=skip_system_info,
        ):
            self._write_line("A test run or manual session is already active.")
            return
        selected = device or "automatic COM-port discovery"
        suffix = "; System Information disabled" if skip_system_info else ""
        self._write_line(f"Manual connection queued: {selected}{suffix}.")

    def _manual_send(self, tokens: list[str]) -> None:
        transport_only = False
        path_token = None
        for token in tokens:
            if token.lower() == "--transport-only":
                if transport_only:
                    self._write_line(_manual_send_usage())
                    return
                transport_only = True
            elif path_token is None:
                path_token = token
            else:
                self._write_line(_manual_send_usage())
                return
        path = _path_argument(path_token or "")
        if path is None:
            self._write_line(_manual_send_usage())
            return
        if not self.worker.manual_send(path, transport_only=transport_only):
            self._write_line("The manual session is not ready for another message.")
            return
        mode = "transport only" if transport_only else "Application response required"
        self._write_line(f"Manual send queued: {path} ({mode}).")

    def do_quit(self, argument: str) -> bool:
        """quit -- Abort any active run and close the HIL-RIG terminal."""
        if argument.strip():
            self._write_line("Usage: quit")
            return False
        self._write_line("Stopping HIL-RIG terminal...")
        self.worker.shutdown()
        return True

    def do_EOF(self, argument: str) -> bool:
        """Close the terminal when standard input reaches end-of-file."""
        self._write_line("")
        return self.do_quit("")

    def emptyline(self) -> None:
        """Do not repeat the previous command when the user presses Enter."""

    def default(self, line: str) -> None:
        self._write_line(f"Unknown command: {line!r}. Enter 'help' for commands.")

    def _write_notification(self, message: str) -> None:
        self._write_line(f"\n{message}")

    def _write_line(self, message: str) -> None:
        with self._output_lock:
            self.stdout.write(message + "\n")
            self.stdout.flush()


def main() -> int:
    """Run the installed ``hil-rig`` terminal entry point."""
    shell = HilRigShell()
    while True:
        try:
            shell.cmdloop()
            return 0
        except KeyboardInterrupt:
            shell._write_line("\nInterrupt received.")
            if shell.worker.abort():
                shell._write_line("Abort requested; the terminal remains active.")
            else:
                shell._write_line("There is no active run. Enter 'quit' to exit.")
            shell.intro = None
        except BaseException as error:
            try:
                shell.worker.shutdown()
            finally:
                print(f"HIL-RIG terminal failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 1


def _path_argument(argument: str) -> Path | None:
    value = argument.strip()
    if not value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if not value:
        return None
    return Path(value)


def _run_argument(argument: str) -> tuple[Path, bool] | None:
    value = argument.strip()
    stepped = False
    if value == "--step":
        return None
    if value.startswith("--step") and len(value) > len("--step"):
        separator = value[len("--step")]
        if separator.isspace():
            stepped = True
            value = value[len("--step") :].strip()
    path = _path_argument(value)
    return None if path is None else (path, stepped)


def _format_status(snapshot: RunSnapshot) -> str:
    lines = [f"State: {snapshot.state.value}", f"Detail: {snapshot.detail}"]
    if snapshot.test_name is not None:
        lines.append(f"Test: {snapshot.test_name}")
    if snapshot.test_path is not None:
        lines.append(f"Definition: {snapshot.test_path}")
    if snapshot.protocol_state is not None:
        lines.append(f"Protocol: {snapshot.protocol_state}")
    if snapshot.stepped:
        lines.append("Mode: stepped")
    if snapshot.next_operation is not None:
        lines.append(f"Next operation: {snapshot.next_operation}")
    if snapshot.expected_tick_count:
        lines.append(
            f"Results: {snapshot.received_tick_count}/{snapshot.expected_tick_count} ticks"
        )
    if snapshot.verdict is not None:
        lines.append(f"Verdict: {snapshot.verdict.upper()}")
    if snapshot.output_directory is not None:
        lines.append(f"Output: {snapshot.output_directory}")
    if snapshot.error is not None:
        lines.append(f"Error: {snapshot.error}")
    return "\n".join(lines)


def _format_manual_status(snapshot: ManualSessionSnapshot) -> str:
    lines = [
        f"Manual state: {snapshot.state.value}",
        f"Detail: {snapshot.detail}",
    ]
    if snapshot.device is not None:
        lines.append(f"Port: {snapshot.device}")
    lines.append(
        "System Information: skipped"
        if snapshot.skip_system_info
        else "System Information: enabled"
    )
    if snapshot.protocol_version is not None:
        lines.append(f"Protocol version: {snapshot.protocol_version}")
    if snapshot.firmware_version is not None:
        lines.append(f"Firmware version: {snapshot.firmware_version}")
    if snapshot.pending_message is not None:
        lines.append(f"Pending message: {snapshot.pending_message}")
    if snapshot.last_result is not None:
        lines.append(
            f"Last send: {'successful' if snapshot.last_result.success else 'unsuccessful'} "
            f"({snapshot.last_result.label})"
        )
    lines.append(f"Inbox messages: {snapshot.inbox_count}")
    if snapshot.error is not None:
        lines.append(f"Error: {snapshot.error}")
    return "\n".join(lines)


def _manual_usage() -> str:
    return (
        "Manual commands:\n"
        "  manual connect [COM=<n>] [--skip-system-info]\n"
        "  manual send <message-file> [--transport-only]\n"
        "  manual inbox\n"
        "  manual status\n"
        "  manual disconnect"
    )


def _manual_connect_usage() -> str:
    return "Usage: manual connect [COM=<n>] [--skip-system-info]"


def _manual_send_usage() -> str:
    return 'Usage: manual send "path to message.json" [--transport-only]'


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HilRigShell", "main"]
