# HIL-RIG Python API

Host-side Python library for constructing an internal model of a hardware-in-the-loop
test for the HIL-RIG.

The current implementation covers test and peripheral configuration, stimulus
instructions, exact user-time-to-tick conversion, and digital-input assertion
definitions. It does not define an IDC representation, serialize a test, communicate
over USB, execute assertions, or parse result data.

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

## Current API example

```python
from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    Test,
)

test = Test(name="Motor controller startup")
test.configure(
    frequency_mode=FrequencyMode.HZ_1K,
    start_mode=StartMode.IMMEDIATE,
)

enable_feedback = test.digital_input(channel=0)
enable_feedback.configure(voltage=LogicVoltage.V12)

enable_command = test.digital_output(channel=0)
enable_command.configure(
    voltage=LogicVoltage.V12,
    initial_state=DigitalState.LOW,
)

enable_command.high(at_ms=100)
enable_command.low(at_s=0.5)

test.expect(enable_feedback).high(at_tick=100)
test.expect(enable_feedback).remain_high(from_ms=100, until_ms=400)
```

Every `Test` receives a random 128-bit integer `test_id`. Every stimulus instruction
receives a sequential integer `instruction_id`, starting at zero. These identifiers are
created by the API rather than supplied by the user.

## Time arguments

Every point stimulus and point assertion accepts exactly one of:

```python
at_tick = 100
at_ms = 100
at_s = 0.1
```

Millisecond and second values are converted using the test's configured frequency:

| Mode | Tick duration |
| --- | --- |
| `FrequencyMode.HZ_100` | 10 ms |
| `FrequencyMode.HZ_1K` | 1 ms |
| `FrequencyMode.HZ_10K` | 0.1 ms |

A time must align exactly with a tick. For example, `at_ms=5` is rejected in 100 Hz
mode instead of being silently rounded.

Range assertions use matching units:

```python
expectation.remain_high(from_tick=100, until_tick=500)
expectation.remain_high(from_ms=100, until_ms=500)
expectation.to_transition(
    from_state=False,
    to_state=True,
    between_s=(0.1, 0.5),
)
```

`from_state` is used because `from` is a reserved Python keyword.

## Implemented configuration

- Test frequency mode and stored start mode
- Digital input logic voltage
- Digital output logic voltage and initial state
- PWM input logic voltage
- PWM output voltage, initial frequency, initial duty cycle, and initial enable state
- I2C role, speed, logic voltage, pull-up value, and slave address

There is no per-channel recording or measurement-enable configuration. The model
assumes the rig records all channels.

## Implemented stimuli

- Digital output: high, low, and toggle
- PWM output: enable, disable, atomic frequency/duty update, frequency update, and
  duty-cycle update
- Analogue output: set voltage
- I2C master: write and read
- I2C slave: preload response

The proposed analogue ramp and peripherals without detailed designs remain
unimplemented.

## Implemented assertions

Only digital-input assertion definitions are implemented:

- high or low at one point;
- remain high over a range;
- transition between states within a range.

The assertion objects are only stored in the internal model. Result data and assertion
evaluation are deliberately not implemented yet.

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
|-- docs/architecture.md           Model boundaries and extension guide
|-- examples/basic_digital_test.py Small runnable example
|-- src/hilrig/                    Installable Python package
|   |-- api.py                     Public Test and channel-handle API
|   |-- timing.py                  Exact conversion into ticks
|   |-- compiler.py                Preliminary ordering only; not final packaging
|   |-- exceptions.py              Library-specific exception hierarchy
|   `-- models/                    Internal configuration/instruction/assertion data
|-- tests/                         Unit tests
`-- pyproject.toml                 Package, dependency, and tool configuration
```

See [docs/architecture.md](docs/architecture.md) for the current model boundaries.

## Continuous integration

GitHub Actions runs automatically for every pull request and pushes to `main`. It runs
unit tests on Python 3.10 through 3.13 and separately checks linting, formatting, and
package building.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the normal branch, test, and pull-request
workflow.
