from pathlib import Path

test_terminal_path = Path("tests/test_terminal.py")
content = test_terminal_path.read_text(encoding="utf-8")

# Update imports
content = content.replace(
    "from hilrig.runner import ManualSessionSnapshot, ManualSessionState, RunSnapshot, WorkerState",
    "from hilrig.protocol import ProtocolFamily\nfrom hilrig.runner import ManualSessionSnapshot, ManualSessionState, RunSnapshot, WorkerState",
)

# Update _StubWorker.submit
old_worker = """    def submit(self, path: Path, *, stepped: bool = False) -> bool:
        self.submitted.append((path, stepped))
        return True"""

new_worker = """    def submit(
        self,
        path: Path,
        *,
        stepped: bool = False,
        protocol_family: ProtocolFamily = ProtocolFamily.VARIABLE,
    ) -> bool:
        self.submitted.append((path, stepped, protocol_family))
        return True"""

content = content.replace(old_worker, new_worker)

# Update test_terminal_minimum_commands
old_min = """    assert worker.submitted == [
        (Path("C:\\\\Test Files\\\\motor.py"), False),
        (Path("C:\\\\Test Files\\\\manual.py"), True),
    ]"""

new_min = """    assert worker.submitted == [
        (Path("C:\\\\Test Files\\\\motor.py"), False, ProtocolFamily.VARIABLE),
        (Path("C:\\\\Test Files\\\\manual.py"), True, ProtocolFamily.VARIABLE),
    ]"""

content = content.replace(old_min, new_min)

# Update usage string in test_terminal_rejects_missing_arguments_and_unknown_commands
old_usage = "assert 'Usage: run [--step] \"path to test.py\"' in rendered"
new_usage = "assert 'Usage: run [--step] [--legacy | --family variable|legacy] \"path to test.py\"' in rendered"
content = content.replace(old_usage, new_usage)

# Add new test for legacy and family options
new_test = """

def test_terminal_run_supports_legacy_and_family_flags() -> None:
    output = StringIO()
    worker = _StubWorker()
    shell = HilRigShell(worker=worker, stdout=output)

    shell.onecmd('run --legacy "C:\\\\Test Files\\\\legacy_test.py"')
    shell.onecmd('run --family legacy "C:\\\\Test Files\\\\family_legacy.py"')
    shell.onecmd('run --family variable --step "C:\\\\Test Files\\\\family_var.py"')

    assert worker.submitted == [
        (Path("C:\\\\Test Files\\\\legacy_test.py"), False, ProtocolFamily.LEGACY),
        (Path("C:\\\\Test Files\\\\family_legacy.py"), False, ProtocolFamily.LEGACY),
        (Path("C:\\\\Test Files\\\\family_var.py"), True, ProtocolFamily.VARIABLE),
    ]
    rendered = output.getvalue()
    assert "Run (legacy message family) queued:" in rendered
    assert "Stepped run (variable message family) queued:" in rendered
"""

content = content + new_test
test_terminal_path.write_text(content, encoding="utf-8")
print("test_terminal.py updated successfully")
