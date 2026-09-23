# Host software internal model

## Current responsibility

The implemented library constructs an in-memory description of a test and compiles it
into a protocol-neutral intermediate representation. The returned-data side now has a
protocol-neutral typed ingestion boundary and SQLite-backed captured-run IR. Fixed
Digital, Analogue, and PWM data is lowered through the public Application wrapper and
its Transport is serviced over USB CDC. Host-side evaluation of the current digital,
PWM, and analogue assertions is implemented against finalized captured runs. Variable
communication messages and Application Response/Execution Control remain deferred.

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
    `-- host-side digital, PWM, and analogue-input assertions

    |
    v
Immutable CompiledTestIR
    |-- versioned machine JSON (summary, configurations, instructions)
    |-- human Excel workbook (also includes assertions)
    `-- fixed-I/O state expander -> ApplicationCodec -> Transport -> USB CDC

USB CDC -> Transport -> ApplicationCodec -> fixed TestResult adapter
                                              |
                                              v
    typed result records -> batched CapturedRunBuilder -> SQLite -> CapturedRunIR
                                                              |
                                                              v
                                          assertion evaluator -> JSON/Markdown report
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
led.configure(voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW)
led.high(at_ms=100)
```

creates a `DigitalOutputInstruction` containing a sequential ID, converted tick,
shared channel identity, and `HIGH` action. It does not communicate with hardware.

All channel accessors require the explicit keyword `channel=`. Handles are cached, so
asking for the same peripheral kind and channel index returns the same object.
Every instruction and assertion is checked against the configuration mapping before it
is stored, and compilation repeats that check as a defensive validation boundary.

## Configuration model

The root configuration stores `FrequencyMode` and `StartMode`. Channel configurations
are stored in a read-only mapping keyed by shared `Channel` identities. A channel may
only be configured once.

No recording-enable setting exists. Recording is treated as rig-wide behaviour rather
than user-selected channel configuration.

Analogue inputs use zero-field configuration marker objects. Calling `configure()`
explicitly declares that the input belongs to the test and produces an empty
`parameters` object in the compiled IR. Analogue output configuration additionally
stores its initial voltage, defaulting to 0 V. Analogue input handles are limited to
physical channels 0 and 1. Analogue output values must be exactly representable in
whole microvolts. PWM output duty values must be exact permyriads, and their frequencies
must produce a whole-nanosecond period in the protocol's unsigned 32-bit range.

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
on the host against returned time-series data. Reusable `PointAssertion` and
`RangeAssertion` bases hold the converted point tick or half-open tick bounds. Concrete
definitions currently cover digital states and transitions, PWM period/frequency/duty
measurements, and analogue voltage targets, bands, and thresholds. Evaluator handlers are
registered by the compiled `(peripheral, assertion)` operation pair, so adding a new
assertion requires an explicit handler rather than a central conditional chain.

Analogue assertion builders accept volts (`target_v`, `minimum_v`, and similar names)
for user readability, then store signed integer microvolts (`target_uv`, `minimum_uv`,
and similar fields). This matches `CapturedRunIR` without requiring floating-point unit
conversion during future evaluation. The conversion must be exact to one microvolt;
sub-microvolt values are rejected rather than rounded.

Every assertion receives an API-assigned sequential `assertion_id`, starting at zero for
each test. This counter is independent of stimulus instruction IDs. The identifier is
preserved in `CompiledAssertion` and the human-readable Assertions sheet so future
evaluation results can refer back to a stable definition. Assertion definitions and IDs
remain excluded from the RIG-facing JSON.

## Compiler and intermediate-representation boundary

`compile()` runs the current validation, makes instruction ordering deterministic, and
returns an immutable `CompiledTestIR` snapshot. The snapshot contains copied scalar IR
data rather than live configuration collections. A successful compile freezes the
source `Test`; repeated calls return the same compiled object.

The machine-readable JSON IR is explicitly versioned. It contains the test summary,
peripheral configurations, and chronological stimulus instructions. It omits assertions
because those are host-side operations and must not be sent to the RIG. Bytes are hex,
test IDs are fixed-width hex, and enums are stored by symbolic member name.

Compilation also derives a half-open expected result count:

```text
latest_relevant_end = max(
    latest stimulus tick + 1,
    latest point assertion tick + 1,
    latest range assertion until_tick,
    0,
)
expected_tick_count = latest_relevant_end + frequency_hz
```

`frequency_hz` supplies exactly one second of settling intervals. A count of 1,000
describes ticks `0..999`. Point assertions require the interval containing their
timestamp; range assertions already carry an exclusive `until_tick`. Assertions remain absent from
the machine instruction list, but their latest required tick can extend this transmitted
duration so the RIG captures enough evidence for host evaluation. The additive field
originally changed the outgoing IR schema version from 1.0 to 1.1; adopting half-open
range semantics changes it to 1.2.

The human-readable `.xlsx` view contains `Test Summary`, `Configurations`,
`Instructions`, and `Assertions` sheets. It is generated from the same compiled
snapshot, so it cannot disagree with the JSON about rig-facing data.

Neither exported representation is itself a wire format. The separate protocol adapter
lowers the compiled snapshot into the teammate-owned public Application values without
changing the user-facing test API.

An observation-only test may contain assertions without stimulus instructions, because
the rig is expected to record all channels.

## Fixed-I/O protocol boundary

`FixedIOProtocolAdapter` depends only on `CompiledTestIR`, `UploadAttempt`, and the
public `hil_rig_protocol` module. It builds the complete fixed configuration arrays,
leaving every unconfigured and communication record canonical-disabled. The logical
definition ID never appears on the wire: the adapter encodes the upload attempt's
128-bit Application Test ID as 16 big-endian bytes in configuration, instruction, and
result correlation.

The outgoing iterator is a state expander rather than a tick counter. It initializes
Digital and PWM output state from configuration, separately retains the requested PWM
period/duty and enabled flag, then groups fixed-output IR instructions by their existing
sparse ticks. Every group is applied in instruction-ID order and produces one complete
fixed `TestInstruction`. A disabled PWM produces protocol period/duty `0/0`; changing
its frequency or duty updates retained state so a later enable restores the requested
waveform. Analogue initial voltage is absent from the protocol configuration, so any
configured Analogue output inserts tick zero into the sparse sequence. All real
tick-zero instructions are then applied in instruction-ID order to that initialized
state, and the merged state is emitted once for tick zero.

`FixedIOProtocolConnection` composes four replaceable pieces:

- exact-name COM discovery and a pySerial byte stream;
- one host-role protocol `Transport` owned and serviced on the creating thread;
- the stateless Application codec and fixed-I/O state adapter; and
- an optional `IncomingResultAdapter` bound to a `CapturedRunBuilder`.

The installed terminal application adds a deliberately thin layer above this
connection. The main thread owns command input and immutable status rendering. A
dedicated protocol worker thread creates and exclusively owns every
`FixedIOProtocolConnection`, receives run, step, continue, and abort requests through
thread-safe signals, and publishes immutable snapshots and user-facing notifications. Test files
expose `build_test() -> Test`; they do not own protocol or artifact lifetimes.

The worker supports the automatic strict workflow and an operator-stepped variant. Its
boundary is operation-oriented rather than encoded-message-oriented: it observes public
workflow states and releases public `UploadOperation` objects, never individual encoded
messages. One gate release therefore covers a complete configuration, tick, or START
operation. A future operation can contain several variable-peripheral messages followed
by one Application Response without changing the terminal/worker threading model.

The same worker also owns an exclusive persistent manual session. This path does not
construct a `Test` or `UploadPlan`: a standalone JSON document is validated into one
public protocol value, encoded by the Application codec, and submitted as one Transport
payload. A normal manual send remains pending through reliable Transport delivery and a
correlated Application Response. A Transport-only send completes on delivery
confirmation and treats any later Application message as inbox data. Manual sessions
can bypass System Information/version discovery, but never bypass Transport session
establishment or reliable-delivery handling. Normal runs continue using automatic
base-description COM discovery; manual sessions may explicitly select a COM device.

The caller repeatedly invokes non-blocking `service()`. The connection retains partial
Transport input and serial output, advances Transport with monotonic wrapped
milliseconds, drains events/application data, and submits at most one reliable
Application message at a time. A separate Application workflow retains each operation
after submission and advances only when both Transport delivery and the correlated
semantic response are complete. A pending tick owns a tuple of encoded messages: it
contains one fixed instruction today and can later contain declared communication data
without changing the stop-and-wait state machine.

Every established Transport session begins with BASIC System Information discovery and
an exact major/minor/patch compatibility check. `UploadPlan` then supplies ordered,
immutable operations containing their wire-message group and response-correlation
metadata. The response-gated sequence is Test Configuration, each non-consecutive sparse
tick, `FINALIZE_TEST_UPLOAD`, and optional START. Automatic mode preserves the
start-mode behavior: IMMEDIATE queues START automatically and HOST_COMMAND waits for an
explicit `start()` call. Operator-gated mode pauses configuration, every tick, and START;
upload finalization and Complete Test acceptance remain automatic. `continue_upload()` switches the remaining plan back to
automatic advancement without bypassing acknowledgements. EXTERNAL_TRIGGER remains in
the protocol-neutral IR but has no protocol behavior. ABORT and RESET_APPLICATION use the same single-outstanding-operation
mechanism. Session reset, delivery failure, response timeout, negative response, or
correlation mismatch abandons the workflow; an upload is never blindly replayed.

## Captured-run boundary

The incoming protocol is isolated from result storage. The implemented fixed adapter
turns one complete Application `TestResult` into one `TickResult`, while decoded
Application Errors become `ApplicationErrorRecord` values. Future public variable
result wrappers will add raw `CommunicationResult` values at the same boundary. These
typed storage records have no dependency on CFFI objects or USB framing.

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

`CompiledTestIR.test_id` is the immutable logical identity of a compiled definition.
`CompiledTestIR.new_upload_attempt()` creates an `UploadAttempt` with a separate random
Application Test ID for the protocol. Calling `UploadAttempt.restart()` retains the
definition ID but produces a fresh Application Test ID, as required when a rejected or
interrupted upload transaction is restarted.

`CapturedRunBuilder.from_compiled_test()` copies the definition ID, name, tick period,
expected tick count, compiled IR version, and assertion definitions directly from
`CompiledTestIR`. It accepts the `UploadAttempt` used for the wire transfer (or creates
one when omitted) and stores its Application Test ID. This prevents the outgoing RIG
configuration and incoming completion check from calculating different run lengths,
preserves the host evaluation plan, and records the mapping from the wire transaction
back to the logical definition.

Finalization checks that unique fixed results cover every tick from zero through
`expected_tick_count - 1`. It automatically records `COMPLETE` or `INCOMPLETE`; the
future execution orchestrator can instead record `SESSION_LOST`, `PROTOCOL_ERROR`, or
`ABORTED`. Capture status is deliberately separate from assertion verdicts.

### SQLite representation

The SQLite file is authoritative and schema-versioned:

- `run_metadata` contains the logical test ID, Application Test ID, run ID, timing,
  provenance, live tick counts, and state;
- `assertion_sets` identifies versioned immutable assertion snapshots;
- `assertion_definitions` stores compiled operations and JSON-encoded scalar arguments;
- `tick_results` contains one wide row per tick rather than one row per channel;
- `communication_results` contains sparse variable-size payload BLOBs;
- `application_errors` contains recoverable/non-recoverable diagnostics.

Adding assertion snapshots changed the captured-result IR schema from 1.0 to 1.1.
Persisting the per-upload Application Test ID changes it from 1.1 to 1.2. Unsupported
older databases are rejected explicitly rather than being interpreted using the wrong
table layout; a migration can be added if preserving pre-1.2 development captures
becomes necessary.

Keeping fixed results wide limits a 100 kHz capture to 100,000 fixed rows per second,
instead of multiplying that by the number of input channels. Payload BLOBs are separate
because they are sparse and variable length.

### Read-only logical IR

`CapturedRunIR` validates and opens the database, then provides assertion-set, tick,
digital, analogue, PWM, communication, and diagnostic queries. Range methods return
streaming iterators. `original_assertion_set` reconstructs immutable
`CompiledAssertion` objects, allowing an evaluator to rerun the exact definitions after
the original Python process has ended. The evaluator asks this facade for assertion
definitions and channel samples instead of importing `sqlite3` or embedding SQL. This
abstraction leaves room for another storage backend if measurements later show one is
needed.

The IR derives optional review artifacts while keeping bulk values out of JSON:

- a small JSON manifest;
- a wide fixed-results CSV;
- a raw communication-results CSV;
- an application-errors CSV.

## Assertion evaluation

`AssertionEvaluator` accepts a finalized `CapturedRunIR` and selects a stored assertion
set (the immutable `original` set by default). It steps through the definitions in
assertion-ID order. A registry maps each `(peripheral, assertion)` pair to a small handler
for that operation. Shared query helpers provide point evidence or stream half-open tick
ranges while making missing ticks explicit.

Each handler returns an immutable `AssertionResult` containing the verdict, expected
arguments, a compact observed-value summary, sample and violation counts, the first
known failure tick, and a human-readable explanation. The dispatcher combines those
results into an `EvaluationReport` with one of three verdicts:

- `PASS`: the available evidence proves the assertion;
- `FAIL`: at least one valid measurement proves a violation;
- `INCONCLUSIVE`: missing or invalid evidence prevents a reliable decision.

For a range assertion, a known violation takes precedence over missing evidence: the
assertion has definitely failed. If no violation is found, any missing or invalid tick
makes the range inconclusive. Digital transitions require adjacent valid ticks, because
a state on either side of a gap does not prove when the transition occurred. PWM
frequency is derived from `period_ns`; a reported zero period means no measurable
waveform and is a known failure rather than missing data.

The report-level verdict is `FAIL` if any assertion fails. Otherwise it is
`INCONCLUSIVE` if an assertion is inconclusive, the capture status is not `COMPLETE`, a
non-recoverable application error was stored, or the selected assertion set is empty.
This keeps measurement outcomes distinct from capture health while preventing a damaged
run from being reported as an overall pass.

Evaluation is read-only and returns the report object before creating files. The object
can be inspected directly or exported as deterministic JSON for tools and Markdown for
people. Reports are deliberately derived artifacts rather than database mutations, so
the same captured evidence and assertion snapshot can be evaluated again later.

## Remaining planned boundaries

When the protocol exposes variable communication instruction and result messages, add
their mappings beside the fixed mappings. Each tick operation already supports a tuple
of separately delivered messages followed by one Tick Response, so serial discovery,
Transport servicing, response correlation, upload identity, capture storage, and fixed
state expansion do not need to change for those additions.

## Testing approach

- API tests verify handles, configuration, automatic IDs, and removed behaviour.
- Timing tests verify exact conversion and alignment errors.
- Instruction tests verify each specified stimulus payload and I2C role rules.
- Assertion model tests verify the currently specified digital, PWM, and analogue-input
  definitions and their compiler representation.
- Evaluator tests cover every registered operation, verdict precedence, incomplete and
  invalid evidence, transition gaps, application errors, and JSON/Markdown reports.
- Captured-run tests verify batching barriers, transactional rollback, finalization,
  validity normalization, raw payload preservation, queries, and derived exports.
- Protocol adapter tests verify full-array configuration, sparse state retention,
  PWM disable/restore behavior, tick-zero Analogue initialization, result mapping,
  exact COM discovery, and partial serial writes.
- Future hardware tests should be a separate, explicitly selected test category.

The default CI workflow runs deterministic tests that require no connected rig.
