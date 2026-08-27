# Host software internal model

## Current responsibility

The implemented library constructs an in-memory description of a test and compiles it
into a protocol-neutral intermediate representation. The returned-data side now has a
protocol-neutral typed ingestion boundary and SQLite-backed captured-run IR. It still
does not define how outgoing data becomes an IDC message, how bytes are transported,
how final application-message objects are mapped, or how assertions are evaluated.

```text
User script
    |
    v
Public Test and channel-handle API
    |
    v
Internal model
    |-- test ID and test-level configuration
    |-- shared channel identities and peripheral configurations
    |-- sequentially identified stimulus instructions
    `-- host-side digital-input assertions

    |
    v
Immutable CompiledTestIR
    |-- versioned machine JSON (summary, configurations, instructions)
    `-- human Excel workbook (also includes assertions)

Future outgoing, intentionally undecided:
    IDC lowering/serialization -> transport

Future incoming protocol adapter:
    USB bytes -> transport -> application message
                                  |
                                  v
Implemented stable boundary:
    typed result records -> batched CapturedRunBuilder -> SQLite -> CapturedRunIR
```

## Test ownership

One `Test` owns:

- a random 128-bit test identifier;
- a human-readable name;
- one test-level and peripheral `Configuration` model;
- one insertion-ordered `InstructionList`;
- one insertion-ordered `AssertionList`;
- stable handles for every referenced channel.

The same immutable `Channel` identity is referenced by its peripheral configuration,
stimulus instructions, and assertions. These objects do not create independent copies
of a channel.

SPI transfers are currently defined only for master channels because the operation
requires the rig to generate clock pulses. Slave channels can be configured, but their
stimulus or preload behavior remains unspecified. UART text writes are converted to
bytes when the instruction is created, so text encoding is not part of the future rig
protocol.

## Public handles and internal data

User-facing handles translate readable operations into data objects. For example:

```python
led = test.digital_output(channel=0)
led.high(at_ms=100)
```

creates a `DigitalOutputInstruction` containing a sequential ID, converted tick,
shared channel identity, and `HIGH` action. It does not communicate with hardware.

All channel accessors require the explicit keyword `channel=`. Handles are cached, so
asking for the same peripheral kind and channel index returns the same object.

## Configuration model

The root configuration stores `FrequencyMode` and `StartMode`. Channel configurations
are stored in a read-only mapping keyed by shared `Channel` identities. A channel may
only be configured once.

No recording-enable setting exists. Recording is treated as rig-wide behaviour rather
than user-selected channel configuration.

Analogue inputs and outputs use zero-field configuration marker objects. Calling
`configure()` has no electrical effect; it explicitly declares that the channel belongs
to the test, produces an empty `parameters` object in the compiled IR, and gives later
validation and assertion features a stable channel identity. Analogue output stimuli
require this declaration first.

## Time model

`timing.py` converts ticks, milliseconds, or seconds into integer ticks using the
configured `FrequencyMode`. Exactly one unit must be supplied and the requested time
must align with a whole tick. Range assertions use the same conversion rules for both
bounds.

Changing the test frequency after timed instructions or assertions exist is rejected,
because doing so would invalidate their already-converted timestamps.

## Instruction model

Every stimulus inherits from `Instruction`, which stores:

- `instruction_id`;
- integer `timestamp` in ticks;
- shared `Channel` identity.

IDs are assigned in creation order from zero and remain attached to their instructions
when a chronological view is produced. `InstructionList` retains insertion order and
also provides stable sorting and grouping helpers.

## Assertion model

Assertions remain separate from stimulus instructions and are intended to be evaluated
on the host against returned time-series data. The current internal model only defines
digital-input point, remain-high, and transition assertions. It does not yet define
results or evaluation algorithms.

## Compiler and intermediate-representation boundary

`compile()` runs the current validation, makes instruction ordering deterministic, and
returns an immutable `CompiledTestIR` snapshot. The snapshot contains copied scalar IR
data rather than live configuration collections. A successful compile freezes the
source `Test`; repeated calls return the same compiled object.

The machine-readable JSON IR is explicitly versioned. It contains the test summary,
peripheral configurations, and chronological stimulus instructions. It omits assertions
because those are host-side operations and must not be sent to the RIG. Bytes are hex,
test IDs are fixed-width hex, and enums are stored by symbolic member name.

Compilation also derives an inclusive expected result count:

```text
latest_relevant_tick = max(latest stimulus, latest assertion end, 0)
expected_tick_count = latest_relevant_tick + frequency_hz + 1
```

`frequency_hz` supplies exactly one second of settling ticks. The final `+1` represents
tick zero: a count of 1,001 describes ticks `0..1000`, not `0..1001`. Point assertions
use their timestamp; range assertions use `until_tick`. Assertions remain absent from
the machine instruction list, but their latest required tick can extend this transmitted
duration so the RIG captures enough evidence for host evaluation. The additive field
changes the outgoing IR schema version from 1.0 to 1.1.

The human-readable `.xlsx` view contains `Test Summary`, `Configurations`,
`Instructions`, and `Assertions` sheets. It is generated from the same compiled
snapshot, so it cannot disagree with the JSON about rig-facing data.

Neither representation defines an IDC package, wire format, instruction opcode, USB
transport, or returned-result format. The future IDC layer will lower the machine IR
into the eventual transport representation without changing the user-facing test API.

An observation-only test may contain assertions without stimulus instructions, because
the rig is expected to record all channels.

## Captured-run boundary

The incoming protocol is isolated from result storage. The final adapter will turn one
complete application message into one `TickResult`, zero or more raw
`CommunicationResult` values, or an `ApplicationErrorRecord`. These typed records have
no dependency on a C binding, USB framing, application union layout, or IDC opcodes.

`TickResult` currently mirrors the stable semantic content identified in the
application design:

- one non-negative tick number;
- ten digital input values;
- two signed analogue values in integer microvolts;
- two PWM period/duty measurements in nanoseconds and permyriad;
- `OK`, `PARTIAL`, or `EXECUTION_PROBLEM` validity and optional problem detail.

`PARTIAL` means the fixed measurements remain valid. `EXECUTION_PROBLEM` normalizes all
fixed measurements to absent values, which SQLite stores as `NULL`. Communication data
is retained as unmodified bytes with peripheral, channel, tick, and per-channel/tick
ordinal. Decoding or cleaning those bytes belongs in a later derived parser, never in
the evidence capture step.

### Batched writer

`CapturedRunBuilder` creates a new database and will not overwrite an existing file.
Producers submit records to a bounded queue; if storage falls behind, producers receive
backpressure rather than silent data loss. One dedicated thread owns the SQLite
connection. It collects all record kinds and atomically commits them when either:

- the batch reaches 2,000 records by default; or
- the oldest pending record reaches 25 ms by default.

SQLite uses WAL journal mode and `synchronous=NORMAL`. `flush()` sends a barrier through
the same queue and waits for every older record to commit. A failed batch is rolled back
as a unit and the failure is surfaced to the caller. Previously committed batches stay
intact.

`CapturedRunBuilder.from_compiled_test()` copies the test ID, name, tick period, and
expected tick count directly from `CompiledTestIR`. This prevents the outgoing RIG
configuration and incoming completion check from calculating different run lengths.

Finalization checks that unique fixed results cover every tick from zero through
`expected_tick_count - 1`. It automatically records `COMPLETE` or `INCOMPLETE`; the
future execution orchestrator can instead record `SESSION_LOST`, `PROTOCOL_ERROR`, or
`ABORTED`. Capture status is deliberately separate from future assertion verdicts.

### SQLite representation

The SQLite file is authoritative and schema-versioned:

- `run_metadata` contains test/run IDs, timing, provenance, live tick counts, and state;
- `tick_results` contains one wide row per tick rather than one row per channel;
- `communication_results` contains sparse variable-size payload BLOBs;
- `application_errors` contains recoverable/non-recoverable diagnostics.

Keeping fixed results wide limits a 100 kHz capture to 100,000 fixed rows per second,
instead of multiplying that by the number of input channels. Payload BLOBs are separate
because they are sparse and variable length.

### Read-only logical IR

`CapturedRunIR` validates and opens the database, then provides tick, digital, analogue,
PWM, communication, and diagnostic queries. Range methods return streaming iterators.
A future assertion evaluator therefore asks for channel samples instead of importing
`sqlite3` or embedding SQL. This abstraction leaves room for another storage backend if
measurements later show one is needed.

The IR derives optional review artifacts while keeping bulk values out of JSON:

- a small JSON manifest;
- a wide fixed-results CSV;
- a raw communication-results CSV;
- an application-errors CSV.

## Remaining planned boundaries

Add these only after their designs are agreed:

```text
src/hilrig/
|-- idc/          Application-message serialization and parsing
|-- transport/    USB CDC connection and byte transfer
`-- assertions/   Evaluation of stored assertions against result series
```

`results/adapter.py` reserves the incoming orchestration and mapping methods. They are
documented stubs because inventing a Python representation for
`HIL_Application_Message_T` before the binding is final would create the wrong
dependency. The implemented builder can already be tested with fabricated typed
records.

## Testing approach

- API tests verify handles, configuration, automatic IDs, and removed behaviour.
- Timing tests verify exact conversion and alignment errors.
- Instruction tests verify each specified stimulus payload and I2C role rules.
- Assertion tests verify only the currently specified digital-input definitions.
- Captured-run tests verify batching barriers, transactional rollback, finalization,
  validity normalization, raw payload preservation, queries, and derived exports.
- Future IDC and result-adapter tests should use agreed known message vectors.
- Future hardware tests should be a separate, explicitly selected test category.

The default CI workflow runs deterministic tests that require no connected rig.
