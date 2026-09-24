from pathlib import Path

runner_path = Path("src/hilrig/runner.py")
content = runner_path.read_text(encoding="utf-8")

# 1. Imports
content = content.replace(
    """from hilrig.protocol import (
    FixedIOProtocolConnection,
    ManualApplicationMessage,
    ManualSendResult,
    ProtocolWorkflowState,""",
    """from hilrig.protocol import (
    FixedIOProtocolConnection,
    ManualApplicationMessage,
    ManualSendResult,
    ProtocolFamily,
    ProtocolWorkflowState,""",
)

# 2. RunSnapshot
content = content.replace(
    """    stepped: bool = False
    next_operation: str | None = None""",
    """    stepped: bool = False
    protocol_family: str | None = None
    next_operation: str | None = None""",
)

# 3. _RunCommand
content = content.replace(
    """@dataclass(frozen=True, slots=True)
class _RunCommand:
    path: Path
    stepped: bool""",
    """@dataclass(frozen=True, slots=True)
class _RunCommand:
    path: Path
    stepped: bool
    protocol_family: ProtocolFamily = ProtocolFamily.VARIABLE""",
)

# 4. ProtocolWorker.submit
old_submit = """    def submit(self, path: str | Path, *, stepped: bool = False) -> bool:
        \"\"\"Queue one run, optionally pausing before each semantic upload operation.\"\"\"
        if not isinstance(stepped, bool):
            raise TypeError("stepped must be a bool")
        self.start()
        candidate = Path(path).expanduser()
        with self._snapshot_lock:
            if self._stopping or self._snapshot.busy or self._manual_snapshot.active:
                return False
            self._abort_requested.clear()
            self._discard_advance_commands()
            self._advance_request_pending = False
            self._run_inbox.clear()
            self._idle.clear()
            self._snapshot = RunSnapshot(
                state=WorkerState.QUEUED,
                detail="Waiting for the protocol worker.",
                test_path=candidate,
                stepped=stepped,
            )
            self._commands.put(_RunCommand(candidate, stepped))
        return True"""

new_submit = """    def submit(
        self,
        path: str | Path,
        *,
        stepped: bool = False,
        protocol_family: ProtocolFamily | str = ProtocolFamily.VARIABLE,
    ) -> bool:
        \"\"\"Queue one run, optionally pausing before each semantic upload operation.\"\"\"
        if not isinstance(stepped, bool):
            raise TypeError("stepped must be a bool")
        if isinstance(protocol_family, str):
            try:
                protocol_family = ProtocolFamily(protocol_family.lower())
            except ValueError:
                raise ValueError(f"Unknown protocol family: {protocol_family!r}")
        elif not isinstance(protocol_family, ProtocolFamily):
            raise TypeError("protocol_family must be a ProtocolFamily or str")
        self.start()
        candidate = Path(path).expanduser()
        with self._snapshot_lock:
            if self._stopping or self._snapshot.busy or self._manual_snapshot.active:
                return False
            self._abort_requested.clear()
            self._discard_advance_commands()
            self._advance_request_pending = False
            self._run_inbox.clear()
            self._idle.clear()
            self._snapshot = RunSnapshot(
                state=WorkerState.QUEUED,
                detail="Waiting for the protocol worker.",
                test_path=candidate,
                stepped=stepped,
                protocol_family=protocol_family.value,
            )
            self._commands.put(_RunCommand(candidate, stepped, protocol_family))
        return True"""

content = content.replace(old_submit, new_submit)

# 5. _worker_loop
content = content.replace(
    "self._execute_run(command.path, stepped=command.stepped)",
    "self._execute_run(command.path, stepped=command.stepped, protocol_family=command.protocol_family)",
)

# 6. _execute_run
old_execute = """    def _execute_run(self, requested_path: Path, *, stepped: bool) -> None:
        connection: Any | None = None
        builder: CapturedRunBuilder | None = None
        output_directory: Path | None = None
        received_tick_count = 0
        try:
            self._set_snapshot(
                state=WorkerState.LOADING,
                detail="Loading and compiling the test definition.",
                test_path=requested_path,
                stepped=stepped,
                next_operation=None,
            )
            path, compiled = load_test_definition(requested_path)
            if compiled.start_mode == "EXTERNAL_TRIGGER":
                raise DefinitionFileError(
                    "Terminal runs do not yet support EXTERNAL_TRIGGER start mode"
                )
            run_id = secrets.randbits(128)
            output_directory = create_run_directory(path, compiled, run_id=run_id)
            compiled.write_json(output_directory / "test-definition.json")
            compiled.write_excel(output_directory / "test-review.xlsx")
            self._set_snapshot(
                state=WorkerState.CONNECTING,
                detail="Connecting to the RIG and confirming protocol compatibility.",
                test_path=path,
                test_name=compiled.name,
                output_directory=output_directory,
                expected_tick_count=compiled.expected_tick_count,
            )
            self._notify(f"Loaded {compiled.name!r}; connecting to the RIG...")
            self._raise_if_aborted()

            connection = self._connection_factory()"""

new_execute = """    def _execute_run(
        self,
        requested_path: Path,
        *,
        stepped: bool,
        protocol_family: ProtocolFamily = ProtocolFamily.VARIABLE,
    ) -> None:
        connection: Any | None = None
        builder: CapturedRunBuilder | None = None
        output_directory: Path | None = None
        received_tick_count = 0
        try:
            self._set_snapshot(
                state=WorkerState.LOADING,
                detail="Loading and compiling the test definition.",
                test_path=requested_path,
                stepped=stepped,
                protocol_family=protocol_family.value,
                next_operation=None,
            )
            path, compiled = load_test_definition(requested_path)
            if compiled.start_mode == "EXTERNAL_TRIGGER":
                raise DefinitionFileError(
                    "Terminal runs do not yet support EXTERNAL_TRIGGER start mode"
                )
            run_id = secrets.randbits(128)
            output_directory = create_run_directory(path, compiled, run_id=run_id)
            compiled.write_json(output_directory / "test-definition.json")
            compiled.write_excel(output_directory / "test-review.xlsx")
            self._set_snapshot(
                state=WorkerState.CONNECTING,
                detail="Connecting to the RIG and confirming protocol compatibility.",
                test_path=path,
                test_name=compiled.name,
                output_directory=output_directory,
                expected_tick_count=compiled.expected_tick_count,
                protocol_family=protocol_family.value,
            )
            self._notify(f"Loaded {compiled.name!r}; connecting to the RIG ({protocol_family.value} family)...")
            self._raise_if_aborted()

            try:
                connection = self._connection_factory(protocol_family=protocol_family)
            except TypeError:
                connection = self._connection_factory()"""

content = content.replace(old_execute, new_execute)

runner_path.write_text(content, encoding="utf-8")
print("runner.py updated successfully")
