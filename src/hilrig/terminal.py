"""Persistent command-line application for automatic HIL-RIG runs."""

from __future__ import annotations

import cmd
import sys
import threading
from pathlib import Path
from typing import TextIO

from hilrig.runner import ProtocolWorker, RunSnapshot


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
        """run <path> -- Load and automatically execute one Python test definition."""
        path = _path_argument(argument)
        if path is None:
            self._write_line('Usage: run "path to test.py"')
            return
        if not self.worker.submit(path):
            self._write_line("A test is already active. Use 'status' or 'abort'.")
            return
        self._write_line(f"Run queued: {path}")

    def do_help(self, argument: str) -> None:
        """help -- Show the commands available in this first terminal version."""
        if argument.strip():
            self._write_line("Usage: help")
            return
        self._write_line(
            "Commands:\n"
            "  run <path>  Load and automatically execute a test-definition file.\n"
            "  status      Show the current or most recently completed run.\n"
            "  abort       Request cancellation of the active run.\n"
            "  help        Show this command list.\n"
            "  quit        Abort any active run and close the terminal."
        )

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


def _format_status(snapshot: RunSnapshot) -> str:
    lines = [f"State: {snapshot.state.value}", f"Detail: {snapshot.detail}"]
    if snapshot.test_name is not None:
        lines.append(f"Test: {snapshot.test_name}")
    if snapshot.test_path is not None:
        lines.append(f"Definition: {snapshot.test_path}")
    if snapshot.protocol_state is not None:
        lines.append(f"Protocol: {snapshot.protocol_state}")
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


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HilRigShell", "main"]
