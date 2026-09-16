# HIL-RIG Python API

Host-side Python library for constructing an internal model of a hardware-in-the-loop
test for the HIL-RIG.

The current implementation covers test and peripheral configuration, stimulus
instructions, exact user-time-to-tick conversion, digital, PWM, and analogue-input
assertion definitions, protocol-neutral JSON and Excel intermediate representations,
persistent captured-run storage, and host-side assertion evaluation with JSON and
Markdown reports. The optional protocol integration lowers fixed Digital, Analogue,
and PWM configuration/stimulus state through `hil-rig-protocol`, services its Transport
over a USB CDC COM port, and stores decoded fixed Test Results in the captured-run
database. Protocol v0.2.0 discovery, semantic Responses, Application Errors, START,
ABORT, and RESET_APPLICATION are integrated. Variable communication messages remain
deferred.

## Requirements

- Python 3.12 or newer
- Git

Fixed-I/O hardware communication additionally requires `hil-rig-protocol` 0.2.0 or newer
and `pyserial`. Until dependency packaging is finalized, install them into the active
environment from the adjacent protocol checkout:

```powershell
python -m pip install ..\hil-rig-protocol pyserial
```

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

## HIL-RIG terminal application

The installed `hil-rig` command starts a persistent terminal application. It runs one
test at a time on a dedicated protocol worker thread, so the terminal stays responsive
to status and abort commands while the worker owns and services the USB connection.

Start it from an activated development environment:

```powershell
hil-rig
```

It can also be started without activating the environment:

```powershell
.\.venv\Scripts\hil-rig.exe
```

A terminal-loadable Python test file must define a no-argument `build_test()` function
that creates and returns a fresh `hilrig.Test`. The file describes the test only; the
terminal owns compilation, connection, upload, capture, evaluation, and output files:

```python
from hilrig import FrequencyMode, LogicVoltage, StartMode, Test


def build_test() -> Test:
    test = Test(name="Observation test")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )
    test.digital_input(channel=0).configure(voltage=LogicVoltage.V3_3)
    return test
```

Test-definition files are trusted Python code. Loading one executes its top-level code
before `build_test()` is called. Do not run files from untrusted sources.

The initial terminal provides five commands:

```text
help
run <path>
status
abort
quit
```

Paths containing spaces may be quoted. For example:

```text
HIL-RIG> run "C:\HIL-RIG Tests\motor-startup.py"
Run queued: C:\HIL-RIG Tests\motor-startup.py

HIL-RIG> status
State: running
Detail: Receiving test results (412 received).
Results: 412/1751 ticks
```

`run` uses the strict protocol-v0.2 workflow: System Information discovery, exact
version confirmation, correlated Application Responses for configuration and sparse
ticks, Complete Test acceptance, START completion, and the complete ordered result
set. `HOST_COMMAND` tests are started automatically by this automatic runner after
upload acceptance. `EXTERNAL_TRIGGER` is not supported by this first terminal version.

Each run creates a unique directory beside the test file:

```text
runs/
`-- 20260916-184200-observation-test-<run-id>/
    |-- test-definition.json
    |-- test-review.xlsx
    |-- captured-run.sqlite3
    |-- run-manifest.json
    |-- fixed-results.csv
    |-- communication-results.csv
    |-- application-errors.csv
    |-- evaluation-report.json
    `-- evaluation-report.md
```

The terminal prints that directory when the run finishes. An aborted run retains and
reports any partial capture. A failed run writes `run-error.txt` when its output
directory had already been created. After a run completes, fails, or is aborted, the
same terminal can run another test. The initial implementation opens a fresh protocol
connection for every run.

See [`examples/terminal_test.py`](examples/terminal_test.py) for a dedicated terminal
definition. The existing [`examples/basic_digital_test.py`](examples/basic_digital_test.py)
also follows the contract and can either be loaded by the terminal or executed directly:

```text
HIL-RIG> run "examples\basic_digital_test.py"
```

`examples/captured_run.py` and `examples/assertion_evaluator.py` are offline
demonstrations that fabricate captured data, so they are not terminal-loadable hardware
test definitions. `examples/example_test.py` also retains its older standalone form.

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

Every `Test` receives a random 128-bit integer `test_id`. This is the immutable logical
identity of the test definition, not the Application Test ID sent on the wire. Each
upload gets a separate random Application Test ID, and a restarted upload gets a fresh
one while continuing to refer to the same logical test:

```python
upload_attempt = compiled.new_upload_attempt()
# Send upload_attempt.application_test_id in Application-layer messages.

retry_attempt = upload_attempt.restart()
assert retry_attempt.definition_test_id == compiled.test_id
assert retry_attempt.application_test_id != upload_attempt.application_test_id
```

Every stimulus instruction receives a sequential integer `instruction_id`, starting at
zero. These identifiers are created by the API rather than supplied by the user.
Host-side assertions independently receive sequential `assertion_id` values starting
at zero, which are retained for future evaluation reports but are not sent to the RIG.

Every peripheral channel must be explicitly configured before it can be referenced by a
stimulus or assertion command. Compilation checks this relationship again so malformed
or manually altered internal models cannot describe an enabled operation on a disabled
protocol channel.

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
- Analogue input usage declarations and analogue output initial voltage
- I2C role, speed, logic voltage, pull-up value, and slave address
- SPI role, supported baud rate, frame size, mode, and bit order
- UART electrical mode, baud rate, parity, word length, and stop bits

There is no per-channel recording or measurement-enable configuration. The model
assumes the rig records all channels.

Channel indices are checked against the physical protocol layout: digital inputs and
outputs use `0..9`, analogue outputs use `0..5`, and analogue inputs, PWM inputs and
outputs, I2C, SPI, and UART use `0..1`. Invalid indices are rejected when a handle is
requested and checked again during compilation.

Analogue channels are declared explicitly so they appear in the internal model and
compiled IR. Analogue outputs also accept an initial voltage in volts, which defaults
to `0.0`:

```python
analogue_input = test.analogue_input(channel=0).configure()
analogue_output = test.analogue_output(channel=0).configure(initial_voltage=1.25)

analogue_output.set_voltage(3.3, at_ms=100)
```

An analogue input's compiled configuration has an empty `parameters` object. An analogue
output's compiled parameters include `initial_voltage`, and it must be configured before
voltage stimuli can be scheduled. Initial and scheduled analogue output voltages must be
finite and within the inclusive hardware range of `0.0` to `20.0` V.

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
pwm = test.pwm_input(channel=0).configure(voltage=LogicVoltage.V3_3)
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
captured-run database. They can therefore be reevaluated later using only that database;
the original user script and in-memory `Test` object are not required.

## Captured-run intermediate representation

`CapturedRunBuilder` is the stable destination for decoded application results. It
receives typed, protocol-neutral records and writes them to a new SQLite
database without holding an entire run in memory:

```python
from hilrig import CapturedRunBuilder, PWMMeasurement, TickResult

upload_attempt = compiled.new_upload_attempt()

builder = CapturedRunBuilder.from_compiled_test(
    "results/run.sqlite3",
    compiled,
    upload_attempt=upload_attempt,
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
- `run_metadata` stores the logical test ID, actual Application Test ID, run ID,
  timing, provenance, counts, and capture status.

`CapturedRunIR` provides streaming channel and range queries, so assertion code does not
contain SQL. It can also derive a small JSON manifest and separate CSV files for fixed
results, communication captures, and application errors. SQLite remains the
authoritative copy.

The builder factory copies the logical test ID, test name, tick period, expected tick
count, compiled IR version, and host-only assertions from the same compiled snapshot
used for the run. It also accepts the exact `UploadAttempt` used for the wire transfer
and persists its Application Test ID. If omitted, the factory creates a new attempt.
Assertions remain absent from the RIG-facing JSON. The lower-level builder constructor
remains available for tests and protocol-independent use; it creates an empty original
assertion set when no compiled definitions are supplied.

`IncomingResultAdapter` implements the fixed-result end of this flow:

```text
USB bytes -> transport messages -> application messages -> typed builder records
```

The adapter maps every decoded protocol `TestResult` to one `TickResult`, rejects a
wire Test ID that differs from `builder.application_test_id`, converts PWM records to
nanoseconds/permyriad, and queues the result for SQLite. `EXECUTION_PROBLEM` values are
stored as SQL `NULL` rather than accepting the protocol's placeholder zeroes as real
measurements. The capture database retains both the wire ID and immutable logical
`test_id`.

## Fixed-I/O protocol and USB CDC connection

`FixedIOProtocolAdapter` turns a `CompiledTestIR` and `UploadAttempt` into the public
`hil-rig-protocol` values. Configuration arrays are always complete; unconfigured
channels use canonical disabled records. Communication peripheral configuration and
instructions stay in the host IR but are deliberately not emitted yet.

Stimulus messages are sparse. The adapter starts from configured Digital/PWM state and
the initial Analogue voltage, applies all fixed-output changes at the next instruction
tick, and emits one complete fixed state for that tick. It does not iterate through
unchanged ticks. Because the protocol configuration has no initial Analogue value, any
configured Analogue output causes a tick-zero state to be emitted. A tick-zero user
stimulus is applied before that single state is sent. Disabled PWM outputs are encoded
with period and duty both zero while their requested frequency/duty remain in the host
state for a later enable.

```python
from hilrig import CapturedRunBuilder, FixedIOProtocolConnection

with FixedIOProtocolConnection.connect() as connection:
    while not connection.session_confirmed:
        connection.service()

    info = connection.session_info
    attempt = compiled.new_upload_attempt()
    builder = CapturedRunBuilder.from_compiled_test(
        "results/run.sqlite3",
        compiled,
        upload_attempt=attempt,
        application_protocol_version=info.protocol_version,
        firmware_version=info.firmware_version,
    )
    connection.bind_result_builder(builder)
    connection.queue_upload(compiled, upload_attempt=attempt)
    while not connection.upload_accepted:
        connection.service()

    # IMMEDIATE queues START after Complete Test is accepted. HOST_COMMAND waits
    # here for connection.start() to be called. EXTERNAL_TRIGGER deliberately has
    # no protocol action.
    while not connection.results_complete:
        connection.service()

    captured_run = builder.finalize()
```

The connection scans `serial.tools.list_ports.comports()` and uses the first port whose
description is exactly `USB Serial Device`. No match is an error. It opens that port as
115200 baud, 8 data bits, no parity, one stop bit, no flow control, non-blocking reads,
and with DTR/RTS disabled. The baud/line coding is explicit even if the direct USB CDC
firmware ignores it.

`service()` owns the byte-stream details: it retains any Transport receive suffix,
drains bounded events and Application data, preserves partial serial-write offsets, and
commits a Transport output only after pySerial accepts every byte. Each new Transport
session first exchanges System Information and requires an exact protocol-version
match. Configuration and sparse tick operations then wait for both Transport delivery
and their correlated Application Response before the next operation is submitted.
After the final sparse tick is accepted, the connection waits for the firmware's
Complete Test Response.

`IMMEDIATE` automatically queues `START`; `HOST_COMMAND` exposes `connection.start()`.
`connection.abort()` and `connection.reset_application()` send the corresponding
response-gated controls. `EXTERNAL_TRIGGER` remains representable in the compiled IR
but intentionally performs no protocol action. Responses are correlated by scope,
Application Test ID, tick and command. A rejection, mismatch, timeout, delivery
failure, or Transport session reset fails the workflow rather than guessing that an
operation succeeded. The default connection enables Transport retransmission with a
250 ms timeout and three retries; callers can supply a different `TransportConfig`.

Application Error messages associated with the active run are converted to
`ApplicationErrorRecord` and queued into the same SQLite writer as results. Their
category, recoverability, optional tick, numeric detail (stored losslessly as decimal
text by the current schema), and diagnostic bytes are preserved. Errors do not replace
the required fixed Test Results. The connection considers result transfer complete
only after receiving the expected ordered result ticks `0..N-1`.

If configuration, a tick, or whole-test validation is rejected, that upload ID is
retired and the caller can restart immediately with `attempt.restart()`. A builder for
the abandoned attempt must first be finalized, then a builder created for the fresh
attempt can be attached with `connection.bind_result_builder(new_builder,
replace=True)`. Reusing the retired wire ID is rejected locally. A rejected START leaves
the accepted upload ready for an explicit retry; uncertain Transport failures and
`FAILED` control outcomes still require ABORT, RESET_APPLICATION, or reconnection.

## Evaluate captured assertions

Evaluation happens only after the builder has finalized the capture. The evaluator reads
the immutable assertion snapshot from SQLite, dispatches each definition to its
peripheral-specific handler, and returns an in-memory `EvaluationReport`:

```python
from hilrig import evaluate_assertions

report = evaluate_assertions(captured_run)

print(report.verdict)  # EvaluationVerdict.PASS, FAIL, or INCONCLUSIVE
print(report.failed_count)
report.write_json("results/report.json")
report.write_markdown("results/report.md")
```

A complete hardware-free demonstration is available in
`examples/assertion_evaluator.py`. Run it from the repository root:

```powershell
python examples/assertion_evaluator.py
```

It creates a uniquely named directory under `examples/build/` containing the SQLite
capture, `evaluation-report.json`, and `evaluation-report.md`, then prints each assertion
outcome and the exact output paths.

The JSON report is useful for later tools and automation. The Markdown report contains a
run summary, a compact assertion table, and detailed expected/observed evidence for each
assertion. Generating a report does not modify the capture database, so evaluation can be
rerun at any time.

Point assertions are inconclusive when their tick is missing or invalid. Range
assertions fail when any valid sample proves a violation; otherwise a gap or invalid
sample makes them inconclusive instead of silently passing. Digital transitions require
two adjacent valid ticks. A zero PWM period is treated as a valid report of no measurable
waveform and fails a PWM assertion. Even when individual assertions pass, an incomplete
capture, a non-recoverable application error, or an empty assertion set prevents an
overall pass.

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
chronological stimulus instructions. The logical test-definition IDs are written as 32
hexadecimal digits, enum members use their stable symbolic names, and byte strings use
`0x`-prefixed hex. The per-upload Application Test ID is deliberately absent because it
is created only when an upload attempt starts.
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
assertions even though their definitions are not transmitted. Compilation rejects an
expected tick count of 1,000,000 or greater so the complete test remains within the
protocol-compatible limit.

Protocol-facing fixed output values are rejected before compilation if conversion
would require rounding: Analogue output voltages must align to one microvolt, PWM duty
cycles must align to one permyriad, and PWM frequencies must produce a whole-nanosecond
period in the unsigned 32-bit range.

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
|   |-- runner.py                  Automatic run controller and protocol worker
|   |-- terminal.py                Persistent `hil-rig` command shell
|   |-- timing.py                  Exact conversion into ticks
|   |-- compiler.py                Validation and immutable IR snapshot construction
|   |-- exporters/                 JSON machine IR and human-readable Excel export
|   |-- evaluation/                Assertion dispatch, handlers, and report export
|   |-- exceptions.py              Library-specific exception hierarchy
|   |-- protocol/                  Application lowering and USB CDC Transport service
|   |-- results/                   Result mapping, SQLite storage, and query facade
|   `-- models/                    Internal configuration/instruction/assertion data
|-- tests/                         Unit tests
`-- pyproject.toml                 Package, dependency, and tool configuration
```

See [docs/architecture.md](docs/architecture.md) for the current model boundaries.

## Continuous integration

GitHub Actions runs automatically for every pull request and pushes to `main`. It runs
unit tests on supported Python versions starting with Python 3.12 and separately checks
linting, formatting, and package building.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the normal branch, test, and pull-request
workflow.
