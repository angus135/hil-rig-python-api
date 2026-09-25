from pathlib import Path

path = Path("tests/test_terminal.py")
txt = path.read_text(encoding="utf-8")
txt = txt.replace(
    'assert "Run queued:" in rendered',
    'assert "Run (variable message family) queued:" in rendered',
)
txt = txt.replace(
    'assert worker.submitted[-1] == (Path("C:\\\\Test Files\\\\motor.py"), False)',
    'assert worker.submitted[-1] == (Path("C:\\\\Test Files\\\\motor.py"), False, ProtocolFamily.VARIABLE)',
)
path.write_text(txt, encoding="utf-8")
