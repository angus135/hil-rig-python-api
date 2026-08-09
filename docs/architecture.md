# Host software internal model

## Current responsibility

The implemented library constructs an in-memory description of a test and compiles it
into a protocol-neutral intermediate representation. It does not define how that IR
becomes an IDC application message, how bytes are transported to the rig, or how
returned time-series data is parsed and evaluated.

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

Future, intentionally undecided:
    IDC lowering/serialization -> transport -> result/assertion engine
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

The human-readable `.xlsx` view contains `Test Summary`, `Configurations`,
`Instructions`, and `Assertions` sheets. It is generated from the same compiled
snapshot, so it cannot disagree with the JSON about rig-facing data.

Neither representation defines an IDC package, wire format, instruction opcode, USB
transport, or returned-result format. The future IDC layer will lower the machine IR
into the eventual transport representation without changing the user-facing test API.

An observation-only test may contain assertions without stimulus instructions, because
the rig is expected to record all channels.

## Planned boundaries

Add these only after their designs are agreed:

```text
src/hilrig/
|-- idc/          Application-message serialization and parsing
|-- transport/    USB CDC connection and byte transfer
|-- results/      Typed channel time series and firmware result parsing
`-- assertions/   Evaluation of stored assertions against result series
```

## Testing approach

- API tests verify handles, configuration, automatic IDs, and removed behaviour.
- Timing tests verify exact conversion and alignment errors.
- Instruction tests verify each specified stimulus payload and I2C role rules.
- Assertion tests verify only the currently specified digital-input definitions.
- Future IDC tests should use agreed known byte vectors.
- Future hardware tests should be a separate, explicitly selected test category.

The default CI workflow runs deterministic tests that require no connected rig.
