# Contributing

## Normal workflow

1. Create a short-lived branch from `main`.
2. Make one focused change.
3. Add or update unit tests in `tests/`.
4. Run the checks locally.
5. Push the branch and open a pull request.
6. Merge only after review and all GitHub Actions checks pass.

## Local checks

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

Tests should describe externally visible behaviour. Avoid testing private
implementation details unless that detail is itself an important invariant.

## Adding a subsystem

Keep user-facing convenience methods in `api.py`, domain data in `models/`, and
validation/translation logic in compiler modules. When the IDC design is stable,
add serialization and transport as separate packages so protocol details do not leak
into the public test-definition API.

## Pull requests

In the pull-request description, state:

- what changed;
- why it changed;
- how it was tested;
- any design or protocol questions that remain open.
