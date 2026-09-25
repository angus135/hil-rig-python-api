# HIL-RIG Python API repository reference

This document is a working map of the repository. It is intended for someone who needs
to make a change without first learning every implementation detail.

Use the other top-level documents alongside it:

- `README.md` explains how to install and use the package.
- `docs/architecture.md` records more detailed design decisions and boundaries.
- `CONTRIBUTING.md` describes the normal branch, test, and review workflow.

## What this repository does

This is the host-side Python package for HIL-RIG. It has four main jobs:

1. Let a user describe a hardware test with a readable Python API.
2. Validate and compile that description into a stable, read-only form.
3. Send supported parts of the test to a RIG over USB and collect its results.
4. Store, query, evaluate, and export those results.

The repository does not contain the firmware or the shared wire-protocol
implementation. Protocol support is supplied by the adjacent `hil-rig-protocol`
package and is loaded only when hardware communication is used.

## The whole flow

```text
Python test file
    |
    v
Test and channel handles              src/hilrig/api.py
    |
    v
Configuration + instructions +       src/hilrig/models/
host-side assertions
    |
    v
CompiledTestIR                        src/hilrig/compiler.py
    |                    \
    |                     +----------> JSON and Excel exports
    v
FixedIOProtocolAdapter                src/hilrig/protocol/application.py
    |
    v
UploadPlan and Application messages
    |
    v
FixedIOProtocolConnection             src/hilrig/protocol/connection.py
    |
    +---- Transport over USB CDC ----> HIL-RIG
    |
    <---- decoded results and errors --+
    |
    v
IncomingResultAdapter                 src/hilrig/results/adapter.py
    |
    v
CapturedRunBuilder -> SQLite          src/hilrig/results/
    |
    v
CapturedRunIR -> assertion evaluator  src/hilrig/evaluation/
    |
    v
JSON, Markdown, and CSV review files
```

There are two important separation points in this flow:

- `CompiledTestIR`, the compiled intermediate representation (IR), separates the
  user-facing test model from any particular wire
  protocol. Code above this point should not need to know about USB or protocol message
  classes.
- `TickResult`, `CommunicationResult`, and `ApplicationErrorRecord` separate decoded
  protocol messages from result storage. SQLite code should not need to know about USB
  or protocol wrapper objects.

These boundaries keep the test API usable without connected hardware and make most of
the repository straightforward to unit test.

## Quick development setup

The package requires Python 3.12 or newer. From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The normal test and quality checks are:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

Hardware and protocol work also needs the protocol checkout and pySerial:

```powershell
python -m pip install ..\hil-rig-protocol pyserial
```

Those packages are deliberately not needed for model, compiler, exporter, capture, or
evaluator work. Protocol modules import them lazily and raise a clear
`ProtocolDependencyError` when they are missing.

## Repository map

| Path | Responsibility | Start here when... |
| --- | --- | --- |
| `src/hilrig/__init__.py` | The supported top-level imports from `hilrig`. | A new public type or function must be exported. |
| `src/hilrig/api.py` | `Test`, channel handles, readable commands, and early input validation. | Changing what test authors can write. |
| `src/hilrig/timing.py` | Exact conversion from ticks, milliseconds, or seconds into integer ticks. | Adding or changing time arguments. |
| `src/hilrig/compiler.py` | Final validation and creation of `CompiledTestIR`. | Changing compiled operations or test-wide rules. |
| `src/hilrig/exceptions.py` | The package-specific exception hierarchy. | A failure needs a stable public error type. |
| `src/hilrig/models/channels.py` | Physical channel kinds, channel counts, and channel identity. | Adding a peripheral or changing the hardware layout. |
| `src/hilrig/models/configuration.py` | Test-level and per-channel configuration values. | Adding a setting or configuration enum. |
| `src/hilrig/models/instructions.py` | Protocol-neutral stimulus records. | Adding an output or communication operation. |
| `src/hilrig/models/assertions.py` | Protocol-neutral host assertion records. | Adding an expected input behaviour. |
| `src/hilrig/models/execution.py` | Immutable compiled records and IR schema version. | Changing the compiled snapshot or its export methods. |
| `src/hilrig/models/identifiers.py` | Logical test IDs and per-upload Application Test IDs. | Working on run identity or retry behaviour. |
| `src/hilrig/exporters/json_ir.py` | RIG-facing JSON representation. | Changing machine-readable test output. |
| `src/hilrig/exporters/excel.py` | Four-sheet human review workbook. | Changing the review view. |
| `src/hilrig/protocol/application.py` | Converts compiled fixed I/O into public protocol values and upload operations. | Mapping model data onto the Application protocol. |
| `src/hilrig/protocol/connection.py` | Services Transport and enforces the response-gated upload/run state machine. | Changing live USB workflow, correlation, retries, or control commands. |
| `src/hilrig/protocol/serial.py` | Base-description COM discovery and pySerial setup. | Changing USB CDC discovery or serial settings. |
| `src/hilrig/protocol/manual.py` | Validates standalone JSON and creates one protocol message. | Extending terminal manual-message support. |
| `src/hilrig/results/models.py` | Typed, protocol-neutral incoming records and capture metadata. | Adding data received from a run. |
| `src/hilrig/results/adapter.py` | Maps decoded protocol results and errors into result records. | Supporting a new incoming Application message. |
| `src/hilrig/results/builder.py` | Bounded queue and background SQLite writer lifecycle. | Changing batching, backpressure, flush, or finalization. |
| `src/hilrig/results/sqlite_store.py` | SQLite schema, transactions, validation, and low-level queries. | Changing persistent capture data. |
| `src/hilrig/results/ir.py` | Read-only, channel-based query API and CSV/manifest exports. | Changing how callers read a capture. |
| `src/hilrig/evaluation/` | Assertion dispatch, evidence access, handlers, verdicts, and reports. | Changing how stored evidence is judged. |
| `src/hilrig/runner.py` | Loads test files and coordinates complete runs on a worker thread. | Changing run orchestration or generated artifacts. |
| `src/hilrig/terminal.py` | The installed `hil-rig` command shell and command parsing. | Adding or changing a terminal command. |
| `examples/` | Small test definitions, offline capture/evaluation demos, and manual messages. | Looking for a runnable usage example. |
| `tests/` | Hardware-free unit and integration tests. | Finding the expected behaviour of a subsystem. |
| `tests/protocol_fakes.py` | Fake public protocol values, codec, Transport, and serial helpers. | Testing protocol work without a RIG. |
| `pyproject.toml` | Packaging, Python support, command entry point, pytest, coverage, and Ruff settings. | Changing dependencies or development tools. |
| `.github/workflows/ci.yml` | Python 3.12/3.13 tests, lint, format check, and package build. | Changing continuous integration. |

`__init__.py` files inside subpackages collect their local public surface. Most users
should import from `hilrig`; internal code can import directly from the owning module.

## Test-definition model

`Test` is the root of one definition. It owns:

- a random 128-bit logical test ID and a name;
- one test-level `Configuration`;
- configured physical channels;
- an insertion-ordered `InstructionList`;
- an insertion-ordered `AssertionList`; and
- cached channel handles.

A handle such as `DigitalOutput` is a convenient way to create model records. It does
not talk to hardware. For example, `output.high(at_ms=100)` creates a
`DigitalOutputInstruction` and appends it to the owning test.

The same immutable `Channel` identity is used by configuration, instructions, and
assertions. Asking a test for the same kind and channel number returns the same handle.
This makes it hard for the three parts of a definition to disagree about which physical
channel they mean.

### Important model rules

- Configure a channel before using it in a stimulus or assertion.
- Configure each channel at most once.
- The defaults are 1 kHz and immediate start. If overriding them, configure the test
  before adding instructions or assertions.
- Supply exactly one time unit for each point or range.
- Times must map to a whole tick. The API rejects values that would need rounding.
- Ranges include both their first and last tick.
- Instruction IDs and assertion IDs are separate zero-based sequences.
- A successful `compile()` freezes the `Test`; later mutation raises
  `FrozenTestError`.
- Repeated calls to `compile()` return the same compiled object.

Validation happens both while the user builds the test and again during compilation.
The second check is intentional: it protects the compiler if internal model objects
have been changed directly or incorrectly.

## Supported peripheral surface

| Peripheral | Channels | Test-definition support | Normal hardware run support |
| --- | ---: | --- | --- |
| Digital input | 0-9 | Voltage configuration and digital assertions | Configuration and fixed results |
| Digital output | 0-9 | Voltage/initial state; high, low, toggle | Configuration and sparse state upload |
| PWM input | 0-1 | Voltage configuration and waveform assertions | Configuration and fixed results |
| PWM output | 0-1 | Voltage/frequency/duty/enable configuration and updates | Configuration and sparse state upload |
| Analogue input | 0-1 | Usage declaration and voltage assertions | Configuration and fixed results |
| Analogue output | 0-5 | Initial voltage and voltage updates | Configuration and sparse state upload |
| I2C | 0-1 | Master read/write and slave response preload | Retained in compiled IR; not sent by the normal runner yet |
| SPI | 0-1 | Master transfer; slave can be configured only | Retained in compiled IR; not sent by the normal runner yet |
| UART | 0-1 | Byte or text write | Retained in compiled IR; not sent by the normal runner yet |

The capture model already has a `CommunicationResult` for raw I2C, SPI, and UART data,
but the normal protocol adapter does not yet emit or receive variable communication
messages. Manual terminal mode is a separate firmware-debug path and can send the
standalone Application message types supported by `protocol/manual.py`.

## Compilation and exported definitions

`compiler.py` sorts instructions by `(timestamp, instruction_id)`. The second key keeps
creation order deterministic when several actions share a tick. It then copies the
model into frozen `CompiledConfiguration`, `CompiledInstruction`,
`CompiledAssertion`, and `CompiledTestIR` values.

The compiled form is deliberately plain: peripheral and operation names are strings,
enums use stable member names, bytes become hexadecimal text, and argument mappings are
read-only copies. It is a boundary object, not a view over the mutable source model.

The expected number of result ticks is:

```text
latest relevant tick + one second of settling ticks + 1
```

The latest relevant tick is the latest stimulus tick or assertion end, whichever is
later. The final `+1` includes tick zero. Assertions therefore affect run length even
though they are not sent to the RIG. A count of 1,001 describes ticks `0..1000`.
Compilation rejects counts of 1,000,000 or more for protocol compatibility.

The current compiled IR schema version is 1.2. The source of truth is
`models/execution.py`.

### Definition outputs

- JSON is the versioned, machine-readable RIG input. It contains summary,
  configurations, and instructions. It intentionally excludes host assertions.
- Excel is a review artifact with `Test Summary`, `Configurations`, `Instructions`,
  and `Assertions` sheets.

Both outputs come from the same compiled snapshot. Neither is a USB wire message.

## Identity: three IDs with different jobs

Do not treat these IDs as interchangeable:

| ID | Lifetime | Purpose |
| --- | --- | --- |
| `CompiledTestIR.test_id` | One logical definition | Identifies the compiled content on the host. |
| `UploadAttempt.application_test_id` | One upload attempt | The 128-bit ID sent in Application messages and checked on results. |
| `CapturedRunBuilder.run_id` | One captured execution | Identifies the stored run and its output directory. |

Restarting a rejected or abandoned upload keeps the definition ID but creates a new
Application Test ID. The wire ID is stored in the capture database so incoming results
can be traced to the exact attempt that produced them.

## Protocol and live connection

`FixedIOProtocolAdapter` handles translation, while
`FixedIOProtocolConnection` handles live state and I/O.

The adapter creates complete fixed-channel arrays. Unconfigured channels use disabled
protocol records. Instructions are sparse: it sends a complete fixed output state only
at ticks where supported state changes. It retains output state between those ticks.

Notable fixed-output rules are:

- Digital and PWM initial values come from configuration.
- The protocol configuration has no analogue initial value, so a configured analogue
  output causes a tick-zero instruction to be emitted.
- Real tick-zero changes are applied before that single tick-zero state is emitted.
- A disabled PWM is sent as period/duty `0/0`, while its requested values are retained
  so a later enable restores them.

The adapter groups messages into semantic `UploadOperation` values: configuration,
each sparse tick, and possibly START. Grouping by operation means a future tick can
contain several wire messages without changing terminal stepping.

The connection is a non-blocking service loop. A caller repeatedly calls `service()`;
each pass reads serial data, advances Transport, handles decoded messages, writes as
much queued output as possible, and returns a `ProtocolServiceReport`.

Every operation waits for both reliable Transport delivery and its correctly correlated
Application Response. Configuration, tick, START, ABORT, and RESET_APPLICATION are not
assumed to have succeeded merely because bytes were written. A timeout, rejection,
correlation mismatch, delivery failure, or session reset fails the workflow safely.

Each established session first requests System Information and checks the exact
protocol version. Manual mode may explicitly skip that Application-level check, but it
still establishes and services Transport.

## Thread ownership

There are three useful execution contexts:

1. The terminal main thread reads commands and renders immutable status.
2. `ProtocolWorker` owns a dedicated thread that creates, services, and closes the
   connection. All protocol and serial calls stay on this thread.
3. Each `CapturedRunBuilder` owns another thread that is the only writer to its SQLite
   connection.

Commands cross these boundaries through queues and events. Status crosses back through
frozen snapshot objects. Keep this ownership when adding features; calling a connection
from the terminal thread would break an explicit protocol-wrapper rule.

## Captured results and SQLite

Decoded results become protocol-neutral records before storage:

- `TickResult` holds all fixed inputs for one tick.
- `CommunicationResult` holds one raw, variable-length communication payload.
- `ApplicationErrorRecord` holds an Application diagnostic.

`TickResult` has a condition of `OK`, `PARTIAL`, or `EXECUTION_PROBLEM`. `PARTIAL`
keeps its fixed measurements. `EXECUTION_PROBLEM` replaces all fixed values with
`None`, which becomes SQL `NULL`; firmware placeholder zeroes must not look like real
measurements.

`CapturedRunBuilder` writes to a new database and never overwrites an existing file.
Its bounded queue provides backpressure instead of dropping records. By default, the
writer commits when it has 2,000 records or the oldest pending record is 25 ms old.
`flush()` is a barrier: after it returns, everything queued before it is committed.

`finalize()` checks for one unique fixed result for every expected tick. With no
explicit status, a complete range becomes `COMPLETE`; otherwise it becomes
`INCOMPLETE`. Capture status is separate from assertion verdicts.

The SQLite database is the authoritative result. Its main tables are:

- `run_metadata`
- `assertion_sets` and `assertion_definitions`
- `tick_results`
- `communication_results`
- `application_errors`

Fixed measurements use one wide row per tick. This avoids multiplying high-frequency
runs by the number of input channels. Variable payloads live separately because they
are sparse and differently sized.

`CapturedRunIR` is the read-only access layer. Callers should use its tick, channel,
communication, error, and assertion-set methods instead of writing SQL. Range queries
stream rows, so large captures do not need to be loaded fully into memory.

The current captured-result schema version is 1.2. The source of truth is
`results/models.py`. Older unsupported schemas are rejected; there is no automatic
migration layer at present. When changing the schema, update the version and tests
together.

## Assertion evaluation

Assertions are host-side rules stored with the compiled definition and copied into the
capture database. This snapshot lets a teammate reevaluate a run without the original
Python script.

`AssertionEvaluator` reads a finalized `CapturedRunIR`, loads an assertion set, and
looks up a handler in `evaluation/registry.py` by `(peripheral, operation)`. The handler
uses `EvaluationContext` to obtain point or range evidence without knowing SQL.

An assertion can be:

- `PASS`: valid evidence proves the expected behaviour.
- `FAIL`: valid evidence proves a violation.
- `INCONCLUSIVE`: evidence is missing or invalid, so no safe decision can be made.

For ranges, a known violation wins over missing evidence and produces `FAIL`. If no
violation is found, any missing or invalid tick produces `INCONCLUSIVE`. Digital
transitions require adjacent valid ticks. A zero PWM period is a known absence of a
measurable waveform, not missing data.

The overall report fails if any assertion fails. Otherwise it is inconclusive if any
assertion is inconclusive, the capture is not complete, a non-recoverable Application
error exists, or the selected assertion set is empty.

Evaluation is read-only. JSON and Markdown reports are derived files and never change
the capture database.

## Terminal runner

The installed `hil-rig` command is defined by the `project.scripts` entry in
`pyproject.toml`. `terminal.py` parses commands; `runner.py` performs the work.

A loadable test file must contain:

```python
def build_test() -> Test:
    ...
```

The file is trusted Python code. `runner.load_test_definition()` executes its top-level
code, calls `build_test()`, checks that the result is a `Test`, and compiles it.

For each run, the worker creates a unique `runs/<timestamp>-<name>-<run-id>/` directory
beside the test file. It writes the compiled JSON and Excel before connecting, then
writes the SQLite capture, CSVs, manifest, and evaluation reports. A failure after the
directory exists also writes `run-error.txt`. Aborted runs retain partial evidence.

Automatic and stepped modes use the same protocol checks. Stepping only controls when a
semantic upload operation is released; it does not bypass delivery, response, or
correlation checks. `EXTERNAL_TRIGGER` remains representable in compiled tests but is
not supported by the terminal runner.

## How to make common changes

### Add or change a public configuration option

1. Add the enum or frozen configuration field in `models/configuration.py`.
2. Expose and validate it in the relevant handle in `api.py`.
3. Check compiled scalar conversion in `compiler.py`.
4. If hardware uses it, map it explicitly in `protocol/application.py`.
5. Export a new public type from `hilrig/__init__.py` if users need it.
6. Add API, compiler/export, and protocol tests as applicable.

### Add a stimulus operation

1. Add a frozen instruction record in `models/instructions.py`.
2. Add the readable handle method and early validation in `api.py`.
3. Add its stable operation name to `_INSTRUCTION_OPERATIONS` in `compiler.py`.
4. Map it in `protocol/application.py` if the normal hardware path supports it.
5. Test model creation, compiled output, same-tick ordering, and wire state changes.

### Add an assertion

1. Add the assertion record in `models/assertions.py`.
2. Add the expectation method and unit conversion in `api.py`.
3. Add its compiled name to `_ASSERTION_OPERATIONS` in `compiler.py`.
4. Write a small evaluator handler in the relevant `evaluation/` module.
5. Register `(peripheral, operation)` in `evaluation/registry.py`.
6. Test definition validation, compiled arguments, SQLite snapshot round-trip, all
   verdicts, and JSON/Markdown reporting.

Do not add a central `if/elif` chain to the evaluator; the registry is the intended
extension point.

### Add incoming result data

1. Define a protocol-neutral record in `results/models.py`.
2. Translate the decoded protocol value in `results/adapter.py`.
3. Add storage and queries in `results/sqlite_store.py`.
4. Expose a read-only view from `results/ir.py`.
5. Update evaluation only if the data supports assertions.
6. If the SQLite layout changes, increment the result schema version and add explicit
   compatibility or rejection tests.

### Change live protocol workflow

Keep message construction in `protocol/application.py` and connection state in
`protocol/connection.py`. Preserve these rules:

- one active reliable Application operation at a time;
- Transport delivery and semantic response are separate completion conditions;
- every response is correlated to its expected scope, ID, tick, or command;
- partial serial writes retain their offset;
- uncertain or rejected uploads are not silently replayed with the same wire ID; and
- all connection calls remain on the creating thread.

Use `tests/protocol_fakes.py` rather than connected hardware for deterministic tests.

### Add a terminal command

Keep parsing and display in `terminal.py`. Put long-running work and state changes in
`runner.py`, using its command queues and immutable snapshots. Add focused tests to both
`test_terminal.py` and `test_runner.py` when the change crosses that boundary.

## Test suite guide

| Test file | Main coverage |
| --- | --- |
| `test_api.py` | Handles, configuration, validation, and public naming |
| `test_timing.py` | Exact timestamps and half-open ranges |
| `test_instructions.py`, `test_spi_uart.py` | Stimulus records and role/frame rules |
| `test_assertions.py`, `test_extended_assertions.py` | Assertion builders and values |
| `test_compiler.py`, `test_ir_export.py` | Freezing, compiled IR, run length, JSON, Excel |
| `test_identifiers.py` | Definition and upload identity lifecycle |
| `test_assertion_snapshots.py` | SQLite persistence of compiled assertions |
| `test_captured_run.py` | Builder, transactions, finalization, queries, and exports |
| `test_evaluator.py` | Handler dispatch, evidence gaps, verdicts, and reports |
| `test_protocol_application.py` | Fixed-I/O lowering and semantic upload plans |
| `test_protocol_connection.py` | Transport service, correlation, control flow, and manual mode |
| `test_protocol_serial.py` | COM discovery and serial settings |
| `test_protocol_manual.py` | Standalone JSON Application messages |
| `test_runner.py`, `test_terminal.py` | Threaded orchestration, artifacts, and shell commands |

Tests should describe visible behaviour and important invariants. The normal suite does
not need a connected RIG. If a change touches a boundary, test both sides—for example,
an assertion feature needs model/compiler tests and evaluator tests.

## Error guide

All stable library errors derive from `HilRigError`:

- `ValidationError` covers invalid complete definitions. `ConfigurationError`,
  `PeripheralError`, and `TimingError` are more specific causes.
- `FrozenTestError` means code tried to change a successfully compiled test.
- `CaptureError` and its subclasses cover builder state, database writes, and schema
  problems.
- `ProtocolIntegrationError` and its subclasses cover missing optional dependencies,
  serial discovery, and unsafe protocol/session failures.
- `EvaluationError` covers malformed or unsupported stored assertions.

Ordinary Python `TypeError` and `ValueError` are also used for bad values at small API
boundaries. Terminal runs catch failures, show the concrete exception type and message,
and preserve a `run-error.txt` file when an output directory already exists.

When debugging an end-to-end failure, follow the same direction as the main flow:

1. Inspect the compiled JSON/Excel to confirm the definition.
2. Inspect the connection workflow state and last Application response.
3. Inspect `run-manifest.json` and capture metadata for counts and status.
4. Query the SQLite file through `CapturedRunIR` for missing or invalid ticks.
5. Read the evaluation report for the exact evidence used by each assertion.

## Current limits to remember

- The normal hardware path lowers fixed Digital, Analogue, and PWM I/O only.
- I2C, SPI, and UART definitions compile but their variable protocol messages are still
  deferred.
- SPI transfer is master-only; a slave channel can be configured but cannot schedule a
  transfer.
- There is no per-channel recording switch; the model assumes rig-wide recording.
- `EXTERNAL_TRIGGER` has no terminal-run behaviour yet.
- Captured database schemas are version-checked but not automatically migrated.
- `hil-rig-protocol` and pySerial currently require a separate install for hardware
  work.
- Hardware tests should remain separate from the default deterministic test suite.

When one of these limits changes, update this document, `README.md`, and
`docs/architecture.md` together so usage, navigation, and design notes stay aligned.
