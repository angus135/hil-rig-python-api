# HIL-RIG Python API

Host-side Python library for constructing an internal model of a hardware-in-the-loop
test for the HIL-RIG.

The current implementation covers test and peripheral configuration, stimulus
instructions, exact user-time-to-tick conversion, digital, PWM, and analogue-input
assertion definitions, protocol-neutral JSON and Excel intermediate representations,
and persistent captured-run storage. It does not define an IDC representation,
communicate over USB, translate the unfinished application-message interface, or execute
assertions.

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

compiled = test.compile()
compiled.write_json("motor-controller-startup.json")
compiled.write_excel("motor-controller-startup.xlsx")
```

Every `Test` receives a random 128-bit integer `test_id`. Every stimulus instruction
receives a sequential integer `instruction_id`, starting at zero. These identifiers are
created by the API rather than supplied by the user. Host-side assertions independently
receive sequential `assertion_id` values starting at zero, which are retained for future
evaluation reports but are not sent to the RIG.

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
- Analogue input and output usage declarations with no hardware parameters
- I2C role, speed, logic voltage, pull-up value, and slave address
- SPI role, supported baud rate, frame size, mode, and bit order
- UART electrical mode, baud rate, parity, word length, and stop bits

There is no per-channel recording or measurement-enable configuration. The model
assumes the rig records all channels.

Analogue channels have nothing electrical to configure, but they are declared explicitly
so they appear in the internal model and compiled IR:

```python
analogue_input = test.analogue_input(channel=0).configure()
analogue_output = test.analogue_output(channel=0).configure()

analogue_output.set_voltage(3.3, at_ms=100)
```

Their compiled configuration has an empty `parameters` object. An analogue output must
be configured before voltage stimuli can be scheduled.

## Implemented stimuli

- Digital output: high, low, and toggle
- PWM output: enable, disable, atomic frequency/duty update, frequency update, and
  duty-cycle update
- Analogue output: set voltage
- I2C master: write and read
- I2C slave: preload response
- SPI master: transfer with independent transmitted data and receive length
- UART: write raw bytes or host-encoded text

The proposed analogue ramp and peripherals without detailed designs remain
unimplemented. SPI slave stimulus behavior is also deferred; `transfer()` is currently
master-only because it represents an operation in which the rig generates the clock.

### SPI example

```python
from hilrig import SPIBaud, SPIFirst, SPIMode, SPIRole, SPISize

spi = test.spi(channel=0)
spi.configure(
    role=SPIRole.MASTER,
    baud=SPIBaud.BAUD_45MBIT,
    data_size=SPISize.SIZE_8BIT,
    mode=SPIMode.MODE_0,
    first_bit=SPIFirst.MSB,
)
spi.transfer(tx_data=b"\xaa\xbb", rx_length=2, at_tick=250)
```

`rx_length` is a byte count. In 16-bit mode, both the transmitted byte count and
`rx_length` must be even so every transfer contains complete frames.

### UART example

```python
from hilrig import UARTLengthBits, UARTMode, UARTParity, UARTStopBits

uart = test.uart(channel=0)
uart.configure(
    mode=UARTMode.TTL_3V3,
    baud_hz=115_200,
    parity=UARTParity.ODD,
    length=UARTLengthBits.EIGHT,
    stop=UARTStopBits.TWO,
)
uart.write(data=b"START\r\n", at_ms=100)
uart.write_text(data="READY\r\n", encoding="ascii", at_tick=200)
```

`write_text()` performs the encoding immediately; the resulting instruction stores
only the bytes that will eventually be sent to the rig.

## Implemented assertions

The following assertion definitions are implemented:

- Digital input: high or low at one point, remain high or low over a range, and a
  transition within a range.
- PWM input: period, frequency, duty cycle, or combined waveform near a target at one
  point; frequency or duty cycle remaining within a range.
- Analogue input: voltage near a target or within a band at one point; voltage remaining
  within a band, above a threshold, or below a threshold over a range.

For example:

```python
pwm = test.pwm_input(channel=0)
test.expect(pwm).frequency_near(
    frequency_hz=50_000,
    tolerance_hz=500,
    at_tick=100,
)

analogue = test.analogue_input(channel=0).configure()
test.expect(analogue).remain_within(
    minimum_v=4.9,
    maximum_v=5.1,
    from_tick=100,
    until_tick=500,
)
```

Analogue assertion arguments use volts for readability. They are immediately converted
to exact integer microvolts in the internal model, matching captured analogue samples.
Values finer than one microvolt are rejected rather than rounded. Analogue input channel
indices are limited to `0` and `1`, matching the two physical inputs.

Assertion definitions are retained in the compiled host model and snapshotted into each
captured-run database. Result data and assertion evaluation remain separate: captured
result storage is implemented, while the evaluator is deliberately not implemented yet.

## Captured-run intermediate representation

`CapturedRunBuilder` is the stable destination for the future application-message
adapter. It receives typed, protocol-neutral records and writes them to a new SQLite
database without holding an entire run in memory:

```python
from hilrig import CapturedRunBuilder, PWMMeasurement, TickResult

builder = CapturedRunBuilder.from_compiled_test(
    "results/run.sqlite3",
    compiled,
)

builder.add_tick_result(
    TickResult(
        tick=0,
        digital_inputs=(False,) * 10,
        analogue_inputs_uv=(1_250_000, 0),
        pwm_inputs=(
            PWMMeasurement(period_ns=20_000, duty_permyriad=5_000),
            PWMMeasurement(period_ns=0, duty_permyriad=0),
        ),
    )
)

captured_run = builder.finalize()
sample = captured_run.digital_input(channel=0).sample_at(0)
original_assertions = captured_run.original_assertion_set
```

The fixed channel counts currently match the application-layer result shape: ten
digital inputs, two analogue inputs, and two PWM inputs. `PARTIAL` tick results retain
valid fixed measurements. `EXECUTION_PROBLEM` results store SQL `NULL` values so
firmware placeholders cannot be mistaken for real zeroes.

The builder owns a bounded producer queue. One background thread owns the SQLite
connection and commits records together when either the configured batch size or flush
interval is reached. `flush()` is an explicit durability barrier. `finalize()` flushes
the remaining records, verifies the expected fixed tick range, writes a terminal
capture status, stops the writer, and returns a read-only `CapturedRunIR`.

Bulk evidence is separated by shape:

- `tick_results` stores one wide fixed-size row per tick;
- `communication_results` stores raw variable-length I2C, SPI, or UART payloads;
- `application_errors` stores diagnostics;
- `assertion_sets` identifies versioned host-side assertion snapshots;
- `assertion_definitions` stores each compiled assertion and its scalar arguments;
- `run_metadata` stores identifiers, timing, provenance, counts, and capture status.

`CapturedRunIR` provides streaming channel and range queries, so future assertion code
does not contain SQL. It can also derive a small JSON manifest and separate CSV files
for fixed results, communication captures, and application errors. SQLite remains the
authoritative copy.

The builder factory copies the test ID, test name, tick period, expected tick count,
compiled IR version, and host-only assertions from the same compiled snapshot used for
the run. Assertions remain absent from the RIG-facing JSON. The lower-level builder
constructor remains available for tests and protocol-independent use; it creates an
empty original assertion set when no compiled definitions are supplied.

`IncomingResultAdapter` contains documented skeleton methods for the future flow:

```text
USB bytes -> transport messages -> application messages -> typed builder records
```

Those methods intentionally raise `NotImplementedError` until the transport/application
Python interfaces and application-message field mapping are final.

## Compile and export

`test.compile()` runs the current validation checks, chronologically orders stimulus
instructions by tick and instruction ID, freezes the `Test`, and returns an immutable
`CompiledTestIR` snapshot. Compilation itself does not create files, so the same
validated snapshot can be inspected, tested, or exported more than once:

```python
compiled = test.compile()

json_text = compiled.to_json()  # JSON string, no file created
compiled.write_json("build/my-test.json")  # machine-readable RIG input
compiled.write_excel("build/my-test.xlsx")  # human-readable review workbook
```

The versioned JSON document contains the test summary, peripheral configurations, and
chronological stimulus instructions. Test IDs are written as 32 hexadecimal digits,
enum members use their stable symbolic names, and byte strings use `0x`-prefixed hex.
Assertions are deliberately excluded because they are evaluated on the host rather
than sent to the RIG. The JSON test summary does include `expected_tick_count`, which is
calculated as:

```text
max(latest stimulus tick, latest assertion tick/range end, 0)
    + one second of ticks
    + 1 for inclusive tick zero
```

For example, a final event at tick 750 in 1 kHz mode produces 1,751 expected application
results, covering ticks `0..1750`. An observation-only test in that mode produces 1,001
results covering ticks `0..1000`. This keeps the RIG capturing long enough for host-side
assertions even though their definitions are not transmitted.

The Excel workbook contains four sheets:

- `Test Summary`
- `Configurations`
- `Instructions`
- `Assertions`

The workbook is a review/reporting view, not an IDC packet definition. Neither exporter
performs USB communication.

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
|   |-- compiler.py                Validation and immutable IR snapshot construction
|   |-- exporters/                 JSON machine IR and human-readable Excel export
|   |-- exceptions.py              Library-specific exception hierarchy
|   |-- results/                   Batched SQLite capture storage and query facade
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
