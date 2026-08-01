# HIL-RIG Python API

Host-side Python library for defining, validating, and compiling hardware-in-the-loop
tests for the HIL-RIG.

The host software is an offline test planner and compiler. A user describes a complete
test in Python, the library validates and compiles it, and a future IDC adapter will
serialize and transfer the resulting execution plan to the rig.

## Project status

This repository contains the initial package structure and a small working API slice.
The IDC package format, USB transport, result parsing, assertions, and reporting will
be added as their designs are finalised.

## Requirements

- Python 3.10 or newer
- Git

## Set up a development environment

From PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The editable install (`-e`) means changes under `src/hilrig/` are used immediately
without reinstalling the package.

## Try the API

```python
from hilrig import FrequencyMode, Test

test = Test("Digital output example")
test.configure(mode=FrequencyMode.KHZ_1)

led = test.digital_out(0)
led.high(at=200)
led.low(at=100)

plan = test.compile()

for time_slot in plan.time_slots:
    print(time_slot.timestamp, time_slot.instructions)
```

Instructions may be added in any order. Compilation validates them, sorts them by
timestamp, and groups instructions that share a timestamp into a `TimeSlot`.

## Run the development checks

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

To automatically format the code:

```powershell
python -m ruff format .
```

## Repository structure

```text
.
|-- .github/workflows/ci.yml       Pull request and main-branch checks
|-- docs/architecture.md           Package boundaries and extension guide
|-- examples/basic_digital_test.py Small runnable example
|-- src/hilrig/                    Installable Python package
|   |-- api.py                     Public Test and channel-handle API
|   |-- compiler.py                Validation and execution-plan construction
|   |-- exceptions.py              Library-specific exception hierarchy
|   `-- models/                    Internal data model
|-- tests/                         Unit tests
`-- pyproject.toml                 Package, dependency, and tool configuration
```

See [docs/architecture.md](docs/architecture.md) for why these boundaries exist and
where future IDC, result, assertion, and reporting code should go.

## Continuous integration

GitHub Actions runs automatically:

- for every pull request;
- for pushes to `main`;
- across Python 3.10, 3.11, 3.12, and 3.13;
- with a separate quality job for linting, formatting, and package building.

After this workflow is present on GitHub, protect `main` in the repository settings
and require the `Unit tests` and `Code quality and package build` checks before merge.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the normal branch, test, and pull-request
workflow.
