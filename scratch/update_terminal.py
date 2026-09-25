from pathlib import Path

terminal_path = Path("src/hilrig/terminal.py")
content = terminal_path.read_text(encoding="utf-8")

# 1. Imports
content = content.replace(
    "from hilrig.runner import (",
    "from hilrig.protocol import ProtocolFamily\nfrom hilrig.runner import (",
)

# 2. Completer
old_completer_run = """        if cmd in {"r", "run"}:
            current_token = document.get_word_before_cursor(WORD=True)
            if (
                not is_trailing_space
                and "--step".startswith(current_token.lower())
                and current_token.startswith("-")
            ):
                yield Completion("--step", start_position=-len(current_token))
            yield from self.py_path_completer.get_completions(document, complete_event)
            return"""

new_completer_run = """        if cmd in {"r", "run"}:
            current_token = document.get_word_before_cursor(WORD=True)
            if not is_trailing_space and current_token.startswith("-"):
                for opt in ["--step", "--legacy", "--family"]:
                    if opt.startswith(current_token.lower()):
                        yield Completion(opt, start_position=-len(current_token))
            elif not is_trailing_space and words[-1].lower() in {"--family", "-f"}:
                for fam in ["variable", "legacy"]:
                    if fam.startswith(current_token.lower()):
                        yield Completion(fam, start_position=-len(current_token))
            yield from self.py_path_completer.get_completions(document, complete_event)
            return"""

content = content.replace(old_completer_run, new_completer_run)

# 3. do_run
old_do_run = """    def do_run(self, argument: str) -> None:
        \"\"\"run [--step] <path> -- Execute a Python test definition.\"\"\"
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
        self._write_line(f"{mode} queued: {path}")"""

new_do_run = """    def do_run(self, argument: str) -> None:
        \"\"\"run [--step] [--legacy | --family variable|legacy] <path> -- Execute a Python test definition.\"\"\"
        parsed = _run_argument(argument)
        if parsed is None:
            self._write_line('Usage: run [--step] [--legacy | --family variable|legacy] "path to test.py"')
            return
        path, stepped, family = parsed
        if not self.worker.submit(path, stepped=stepped, protocol_family=family):
            if self.worker.manual_snapshot().active:
                self._write_line(
                    "A manual session is currently active. Use 'manual disconnect' before "
                    "running a test."
                )
            else:
                self._write_line("A test is already active. Use 'status' or 'abort'.")
            return
        mode = "Stepped run" if stepped else "Run"
        family_label = f" ({family.value} message family)"
        self._write_line(f"{mode}{family_label} queued: {path}")"""

content = content.replace(old_do_run, new_do_run)

# 4. _run_argument
old_run_arg = """def _run_argument(argument: str) -> tuple[Path, bool] | None:
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
    return None if path is None else (path, stepped)"""

new_run_arg = """def _run_argument(argument: str) -> tuple[Path, bool, ProtocolFamily] | None:
    try:
        tokens = shlex.split(argument, posix=False)
    except ValueError:
        return None
    if not tokens:
        return None

    stepped = False
    family = ProtocolFamily.VARIABLE
    path_str: str | None = None

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        token_lower = token.lower()
        if token_lower in {"--step", "-s"}:
            stepped = True
            idx += 1
        elif token_lower in {"--legacy", "-l"}:
            family = ProtocolFamily.LEGACY
            idx += 1
        elif token_lower in {"--family", "-f", "--protocol-family"}:
            if idx + 1 >= len(tokens):
                return None
            val = tokens[idx + 1].lower()
            if val not in {"variable", "legacy"}:
                return None
            family = ProtocolFamily(val)
            idx += 2
        elif token_lower.startswith("--family="):
            val = token.partition("=")[2].lower()
            if val not in {"variable", "legacy"}:
                return None
            family = ProtocolFamily(val)
            idx += 1
        elif token_lower.startswith("--protocol-family="):
            val = token.partition("=")[2].lower()
            if val not in {"variable", "legacy"}:
                return None
            family = ProtocolFamily(val)
            idx += 1
        elif token.startswith("-"):
            return None
        else:
            if path_str is not None:
                return None
            path_str = token
            idx += 1

    if path_str is None:
        return None
    path = _path_argument(path_str)
    return None if path is None else (path, stepped, family)"""

content = content.replace(old_run_arg, new_run_arg)

# 5. _format_status
old_format_status = """    if snapshot.protocol_state is not None:
        lines.append(f"Protocol: {snapshot.protocol_state}")"""

new_format_status = """    if snapshot.protocol_state is not None:
        lines.append(f"Protocol: {snapshot.protocol_state}")
    if snapshot.protocol_family is not None:
        lines.append(f"Family: {snapshot.protocol_family}")"""

content = content.replace(old_format_status, new_format_status)

# 6. do_help
old_help = """            "  run <path>         Load and automatically execute a test-definition file.\\n"
            "  run --step <path>  Pause before configuration, each tick, and START.\\n\""""

new_help = """            "  run [--step] [--legacy] <path>\\n"
            "                     Load and automatically execute a test-definition file.\\n"
            "                     (--legacy uses fixed TestInstruction/TestResult, default is variable)\\n"
            "  run --step <path>  Pause before configuration, each tick, and START.\\n\""""

content = content.replace(old_help, new_help)

terminal_path.write_text(content, encoding="utf-8")
print("terminal.py updated successfully")
