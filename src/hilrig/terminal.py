"""Persistent command-line application for automatic or stepped HIL-RIG runs."""

from __future__ import annotations

import os
import shlex
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO

from prompt_toolkit import HTML, PromptSession, print_formatted_text
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout

from hilrig.runner import (
    ManualSessionSnapshot,
    ManualSessionState,
    ProtocolWorker,
    RunSnapshot,
    WorkerState,
)

_DEFAULT_HISTORY_FILE = Path.home() / ".hilrig_history"

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_GREEN = "\033[92m"
_ANSI_RED = "\033[91m"
_ANSI_YELLOW = "\033[93m"
_ANSI_CYAN = "\033[96m"
_ANSI_DIM = "\033[2m"


class HilRigCompleter(Completer):
    """Context-aware auto-completer for HIL-RIG commands, subcommands, and paths."""

    def __init__(self) -> None:
        self.py_path_completer = PathCompleter(
            file_filter=lambda p: p.endswith(".py") or Path(p).is_dir(),
            expanduser=True,
        )
        self.json_path_completer = PathCompleter(
            file_filter=lambda p: p.endswith(".json") or Path(p).is_dir(),
            expanduser=True,
        )
        self.commands = [
            "run",
            "step",
            "continue",
            "status",
            "abort",
            "manual",
            "ports",
            "reset",
            "help",
            "quit",
            "clear",
            "cls",
        ]
        self.manual_subcommands = [
            "connect",
            "send",
            "finalize",
            "inbox",
            "reset",
            "status",
            "disconnect",
        ]

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text_before_cursor = document.text_before_cursor
        words = text_before_cursor.lstrip().split()
        is_trailing_space = text_before_cursor.endswith(" ")

        # Complete top-level command
        if not words or (len(words) == 1 and not is_trailing_space):
            prefix = words[0] if words else ""
            for cmd in self.commands:
                if cmd.startswith(prefix.lower()):
                    yield Completion(cmd, start_position=-len(prefix))
            return

        cmd = words[0].lower()
        if cmd in {"r", "run"}:
            current_token = document.get_word_before_cursor(WORD=True)
            if (
                not is_trailing_space
                and "--step".startswith(current_token.lower())
                and current_token.startswith("-")
            ):
                yield Completion("--step", start_position=-len(current_token))
            yield from self.py_path_completer.get_completions(document, complete_event)
            return

        if cmd in {"m", "manual"}:
            if len(words) == 1 and is_trailing_space:
                for sub in self.manual_subcommands:
                    yield Completion(sub, start_position=0)
                return
            if len(words) == 2 and not is_trailing_space:
                sub_prefix = words[1].lower()
                for sub in self.manual_subcommands:
                    if sub.startswith(sub_prefix):
                        yield Completion(sub, start_position=-len(sub_prefix))
                return

            subcmd = words[1].lower()
            if subcmd == "connect":
                current_token = document.get_word_before_cursor(WORD=True)
                for opt in ["--skip-system-info", "COM="]:
                    if opt.lower().startswith(current_token.lower()):
                        yield Completion(opt, start_position=-len(current_token))
            elif subcmd == "send":
                current_token = document.get_word_before_cursor(WORD=True)
                if (
                    not is_trailing_space
                    and "--transport-only".startswith(current_token.lower())
                    and current_token.startswith("-")
                ):
                    yield Completion("--transport-only", start_position=-len(current_token))
                yield from self.json_path_completer.get_completions(document, complete_event)
            elif subcmd == "inbox":
                current_token = document.get_word_before_cursor(WORD=True)
                if "clear".startswith(current_token.lower()):
                    yield Completion("clear", start_position=-len(current_token))
            return

        if cmd == "help":
            if len(words) == 1 and is_trailing_space:
                for c in self.commands:
                    yield Completion(c, start_position=0)
            elif len(words) == 2 and not is_trailing_space:
                prefix = words[1].lower()
                for c in self.commands:
                    if c.startswith(prefix):
                        yield Completion(c, start_position=-len(prefix))
            return


class HilRigShell:
    """Persistent interactive terminal front end for the protocol worker."""

    intro = "HIL-RIG terminal. Enter 'help' for commands."
    prompt = HTML("<ansicyan><b>HIL-RIG&gt;</b></ansicyan> ")
    aliases: dict[str, str] = {
        "r": "run",
        "s": "status",
        "st": "step",
        "c": "continue",
        "p": "ports",
        "q": "quit",
        "exit": "quit",
        "cls": "clear",
    }

    def __init__(
        self,
        *,
        worker: ProtocolWorker | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        history: History | None = None,
    ) -> None:
        self._custom_stdin = stdin
        self._custom_stdout = stdout
        self._output_lock = threading.Lock()
        self.completer = HilRigCompleter()
        if history is not None:
            self.history: History = history
        elif stdin is None and stdout is None:
            self.history = FileHistory(str(_DEFAULT_HISTORY_FILE))
        else:
            self.history = InMemoryHistory()

        self.worker = worker or ProtocolWorker(notification_callback=self._write_notification)
        self.worker.start()

    @property
    def stdin(self) -> TextIO:
        return self._custom_stdin if self._custom_stdin is not None else sys.stdin

    @property
    def stdout(self) -> TextIO:
        return self._custom_stdout if self._custom_stdout is not None else sys.stdout

    @property
    def is_interactive(self) -> bool:
        return self._custom_stdout is None

    def onecmd(self, line: str) -> bool:
        """Execute one command line synchronously and return whether the shell should stop."""
        trimmed = line.strip()
        if not trimmed:
            self.emptyline()
            return False

        command, _, argument = trimmed.partition(" ")
        command_lower = command.lower()
        resolved_cmd = self.aliases.get(command_lower, command_lower)

        handler = getattr(self, f"do_{resolved_cmd}", None)
        if callable(handler):
            result = handler(argument.strip())
            return bool(result)

        self.default(trimmed)
        return False

    def do_run(self, argument: str) -> None:
        """run [--step] <path> -- Execute a Python test definition."""
        parsed = _run_argument(argument)
        if parsed is None:
            self._write_line('Usage: run [--step] "path to test.py"')
            return
        path, stepped = parsed
        if not self.worker.submit(path, stepped=stepped):
            if self.worker.manual_snapshot().active:
                self._write_line(
                    "A manual session is currently active. Use 'manual disconnect' before "
                    "running a test."
                )
            else:
                self._write_line("A test is already active. Use 'status' or 'abort'.")
            return
        mode = "Stepped run" if stepped else "Run"
        self._write_line(f"{mode} queued: {path}")

    def do_ports(self, argument: str) -> None:
        """ports -- List available serial COM ports and detect the HIL-RIG."""
        if argument.strip():
            self._write_line("Usage: ports")
            return
        try:
            import serial.tools.list_ports

            comports = list(serial.tools.list_ports.comports())
        except Exception as error:
            self._write_line(f"Could not list COM ports: {error}")
            return

        if not comports:
            self._write_line("No COM ports detected.")
            return

        self._write_line("Available COM ports:")
        for p in comports:
            desc = p.description or "Unknown"
            is_match = desc.startswith("USB Serial Device") or (
                getattr(p, "vid", None) == 0x0483 and getattr(p, "pid", None) == 0x5740
            )
            match_marker = f" {_ANSI_GREEN}[HIL-RIG match]{_ANSI_RESET}" if is_match else ""
            hwid = f" ({p.hwid})" if p.hwid and p.hwid != "n/a" else ""
            device_label = f"{_ANSI_BOLD}{p.device:<6}{_ANSI_RESET}"
            self._write_line(f"  {device_label} - {desc}{match_marker}{hwid}")

    def do_reset(self, argument: str) -> None:
        """reset -- Send RESET_APPLICATION GlobalControl in active manual session."""
        self.do_manual(f"reset {argument}".strip())

    def do_help(self, argument: str) -> None:
        """help -- Show the commands available in the terminal."""
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
            "  ports              List available serial COM ports and detect the HIL-RIG.\n"
            "  reset              Send RESET_APPLICATION in the active manual session.\n"
            "  manual connect [COM=<n>] [--skip-system-info]\n"
            "                     Open a persistent manual protocol session.\n"
            "  manual send <message-file> [--transport-only]\n"
            "                     Send one standalone JSON Application message.\n"
            "  manual finalize <test-id>\n"
            "                     Send FINALIZE_TEST_UPLOAD and await Complete Test acceptance.\n"
            "  manual reset      Send RESET_APPLICATION GlobalControl.\n"
            "  manual inbox [clear]\n"
            "                     Show or clear received manual Application messages.\n"
            "  manual status     Show manual-session status.\n"
            "  manual disconnect Close the manual protocol session.\n"
            "  clear / cls        Clear the console screen.\n"
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
        self._write_line(_format_status(self.worker.snapshot(), color=self.is_interactive))

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
        elif command == "finalize":
            self._manual_finalize(tokens)
        elif command == "reset":
            if tokens:
                self._write_line("Usage: manual reset")
            elif self.worker.manual_reset():
                self._write_line("Manual reset queued: GlobalControl RESET_APPLICATION.")
            else:
                self._write_line("The manual session is not ready for a reset.")
        elif command == "inbox":
            if tokens in (["clear"], ["--clear"]):
                self.worker.manual_clear_inbox()
                self._write_line("Manual inbox cleared.")
            elif tokens:
                self._write_line("Usage: manual inbox [clear]")
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
                self._write_line(
                    _format_manual_status(self.worker.manual_snapshot(), color=self.is_interactive)
                )
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
            if self.worker.snapshot().busy:
                self._write_line("A test run is currently active. Use 'status' or 'abort'.")
            else:
                self._write_line("A manual session is already active. Use 'manual disconnect'.")
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

    def _manual_finalize(self, tokens: list[str]) -> None:
        if len(tokens) != 1:
            self._write_line(_manual_finalize_usage())
            return
        raw_test_id = tokens[0].replace("-", "")
        if len(raw_test_id) != 32:
            self._write_line("Test ID must contain exactly 32 hexadecimal digits.")
            return
        try:
            application_test_id = int(raw_test_id, 16)
        except ValueError:
            self._write_line("Test ID must contain only hexadecimal digits.")
            return
        if not self.worker.manual_finalize(application_test_id):
            self._write_line("The manual session is not ready for upload finalization.")
            return
        self._write_line(f"Manual finalize queued for Test ID {application_test_id:032x}.")

    def do_clear(self, argument: str) -> None:
        """clear / cls -- Clear the terminal screen."""
        if argument.strip():
            self._write_line("Usage: clear")
            return
        with self._output_lock:
            if sys.platform == "win32":
                os.system("cls")
            else:
                self.stdout.write("\033[H\033[2J")
                self.stdout.flush()

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
        self._write_line(message)

    def _write_line(self, message: str) -> None:
        with self._output_lock:
            if self._custom_stdout is not None:
                self._custom_stdout.write(message + "\n")
                self._custom_stdout.flush()
            else:
                try:
                    print_formatted_text(ANSI(message), file=sys.stdout)
                except Exception:
                    sys.stdout.write(message + "\n")
                    sys.stdout.flush()

    def _bottom_toolbar(self) -> str:
        snapshot = self.worker.snapshot()
        if snapshot.busy:
            step_str = " (Stepped)" if snapshot.stepped else ""
            ticks_str = (
                f" | Ticks: {snapshot.received_tick_count}/{snapshot.expected_tick_count}"
                if snapshot.expected_tick_count
                else ""
            )
            return f" [Run: {snapshot.state.value.upper()}{step_str}{ticks_str}] "
        manual = self.worker.manual_snapshot()
        if manual.active:
            port = f" on {manual.device}" if manual.device else ""
            return f" [Manual: {manual.state.value.upper()}{port} | Inbox: {manual.inbox_count}] "
        return " [HIL-RIG: IDLE] "

    def cmdloop(self, intro: str | None = None) -> None:
        """Run the prompt_toolkit interactive shell loop."""
        if intro is not None:
            self._write_line(intro)
        elif self.intro:
            self._write_line(self.intro)

        session: PromptSession[str] = PromptSession(
            history=self.history,
            completer=self.completer,
            auto_suggest=AutoSuggestFromHistory(),
        )

        with patch_stdout():
            while True:
                try:
                    line = session.prompt(
                        self.prompt,
                        bottom_toolbar=self._bottom_toolbar,
                    )
                except KeyboardInterrupt:
                    self._write_line("Interrupt received.")
                    if self.worker.abort():
                        self._write_line("Abort requested; the terminal remains active.")
                    else:
                        self._write_line("There is no active run. Enter 'quit' to exit.")
                    continue
                except EOFError:
                    self._write_line("")
                    self.do_quit("")
                    break

                should_stop = self.onecmd(line)
                if should_stop:
                    break


def main() -> int:
    """Run the installed ``hil-rig`` terminal entry point."""
    shell = HilRigShell()
    try:
        shell.cmdloop()
        return 0
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


def _format_status(snapshot: RunSnapshot, *, color: bool = False) -> str:
    state_styled = snapshot.state.value
    if color:
        if snapshot.state in {WorkerState.COMPLETED}:
            state_styled = f"{_ANSI_GREEN}{state_styled}{_ANSI_RESET}"
        elif snapshot.state in {WorkerState.FAILED, WorkerState.ABORTED}:
            state_styled = f"{_ANSI_RED}{state_styled}{_ANSI_RESET}"
        elif snapshot.state in {WorkerState.RUNNING, WorkerState.CONNECTING, WorkerState.UPLOADING}:
            state_styled = f"{_ANSI_CYAN}{state_styled}{_ANSI_RESET}"
        elif snapshot.state in {WorkerState.WAITING_FOR_OPERATOR}:
            state_styled = f"{_ANSI_YELLOW}{state_styled}{_ANSI_RESET}"

    lines = [f"State: {state_styled}", f"Detail: {snapshot.detail}"]
    if snapshot.test_name is not None:
        lines.append(f"Test: {snapshot.test_name}")
    if snapshot.test_path is not None:
        lines.append(f"Definition: {snapshot.test_path}")
    if snapshot.protocol_state is not None:
        lines.append(f"Protocol: {snapshot.protocol_state}")
    if snapshot.stepped:
        mode_styled = f"{_ANSI_YELLOW}stepped{_ANSI_RESET}" if color else "stepped"
        lines.append(f"Mode: {mode_styled}")
    if snapshot.next_operation is not None:
        op_styled = (
            f"{_ANSI_YELLOW}{snapshot.next_operation}{_ANSI_RESET}"
            if color
            else snapshot.next_operation
        )
        lines.append(f"Next operation: {op_styled}")
    if snapshot.expected_tick_count:
        lines.append(
            f"Results: {snapshot.received_tick_count}/{snapshot.expected_tick_count} ticks"
        )
    if snapshot.verdict is not None:
        v_upper = snapshot.verdict.upper()
        if color:
            v_color = _ANSI_GREEN if v_upper == "PASS" else _ANSI_RED
            lines.append(f"Verdict: {v_color}{v_upper}{_ANSI_RESET}")
        else:
            lines.append(f"Verdict: {v_upper}")
    if snapshot.output_directory is not None:
        lines.append(f"Output: {snapshot.output_directory}")
    if snapshot.error is not None:
        err_styled = f"{_ANSI_RED}{snapshot.error}{_ANSI_RESET}" if color else snapshot.error
        lines.append(f"Error: {err_styled}")
    return "\n".join(lines)


def _format_manual_status(snapshot: ManualSessionSnapshot, *, color: bool = False) -> str:
    state_styled = snapshot.state.value
    if color:
        if snapshot.state in {ManualSessionState.READY}:
            state_styled = f"{_ANSI_GREEN}{state_styled}{_ANSI_RESET}"
        elif snapshot.state in {ManualSessionState.FAILED}:
            state_styled = f"{_ANSI_RED}{state_styled}{_ANSI_RESET}"
        elif snapshot.state in {ManualSessionState.CONNECTING, ManualSessionState.SENDING}:
            state_styled = f"{_ANSI_CYAN}{state_styled}{_ANSI_RESET}"

    lines = [
        f"Manual state: {state_styled}",
        f"Detail: {snapshot.detail}",
    ]
    if snapshot.device is not None:
        port_styled = f"{_ANSI_CYAN}{snapshot.device}{_ANSI_RESET}" if color else snapshot.device
        lines.append(f"Port: {port_styled}")
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
        if color:
            success_label = (
                f"{_ANSI_GREEN}successful{_ANSI_RESET}"
                if snapshot.last_result.success
                else f"{_ANSI_RED}unsuccessful{_ANSI_RESET}"
            )
        else:
            success_label = "successful" if snapshot.last_result.success else "unsuccessful"
        lines.append(f"Last send: {success_label} ({snapshot.last_result.label})")
    lines.append(f"Inbox messages: {snapshot.inbox_count}")
    if snapshot.error is not None:
        err_styled = f"{_ANSI_RED}{snapshot.error}{_ANSI_RESET}" if color else snapshot.error
        lines.append(f"Error: {err_styled}")
    return "\n".join(lines)


def _manual_usage() -> str:
    return (
        "Manual commands:\n"
        "  manual connect [COM=<n>] [--skip-system-info]\n"
        "  manual send <message-file> [--transport-only]\n"
        "  manual finalize <test-id>\n"
        "  manual reset\n"
        "  manual inbox [clear]\n"
        "  manual status\n"
        "  manual disconnect"
    )


def _manual_connect_usage() -> str:
    return "Usage: manual connect [COM=<n>] [--skip-system-info]"


def _manual_send_usage() -> str:
    return 'Usage: manual send "path to message.json" [--transport-only]'


def _manual_finalize_usage() -> str:
    return "Usage: manual finalize <32-hex-test-id>"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HilRigCompleter", "HilRigShell", "main"]
