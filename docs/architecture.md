# Host software architecture

## Responsibility

The library defines and compiles complete tests before execution. It does not perform
live hardware I/O. The rig remains responsible for deterministic execution and data
collection.

```text
User script
    |
    v
Public API
    |
    v
Internal test model
    |
    v
Validation and compilation
    |
    v
Execution plan
    |
    v
IDC serialization and transport (future)
    |
    v
HIL-RIG firmware
    |
    v
Result parsing, assertions, and reports (future)
```

## Current package boundaries

### `api.py`

Owns the interface test authors use. Channel handles such as `DigitalOutput` translate
readable calls like `led.high(at=100)` into internal instruction objects.

### `models/`

Contains data, not workflow:

- immutable configuration values;
- channel identities;
- individual instructions;
- the mutable instruction collection used while defining a test;
- immutable `ExecutionPlan` and `TimeSlot` compiler output.

An `InstructionList` wraps a normal Python list. This keeps insertion straightforward
while giving ordering and grouping operations a clear home.

### `compiler.py`

Validates the internal model and transforms it into a chronological execution plan.
Instructions with the same timestamp remain in insertion order because Python sorting
is stable.

### `exceptions.py`

Defines errors callers can catch without depending on low-level implementation
exceptions.

## Planned boundaries

Add these only when their requirements are stable:

```text
src/hilrig/
|-- assertions/   Host-side assertion evaluation
|-- idc/          Application-message serialization and parsing
|-- transport/    USB CDC connection and byte transfer
|-- results/      Measurements and firmware result parsing
`-- reporting/    Human- and machine-readable reports
```

IDC encoding belongs below the compiler. It should consume an `ExecutionPlan` (or a
later protocol-neutral intermediate representation), not inspect the public API's
channel handles. This allows protocol changes without redesigning how users write
tests.

## Compilation lifecycle

1. The user creates a `Test`.
2. Configuration and instruction calls populate the internal model.
3. `compile()` validates the complete model.
4. Instructions are stably sorted and grouped into `TimeSlot` objects.
5. Successful compilation freezes the test definition.
6. A future IDC serializer converts the immutable plan into application messages.

A failed compilation leaves the test editable so the author can correct it. Repeated
successful calls to `compile()` return the same immutable plan.

## Testing approach

Unit tests should cover each layer independently:

- API tests verify readable calls create the expected model;
- compiler tests verify validation, ordering, grouping, and immutability;
- future IDC tests should use known byte vectors;
- future transport tests should use a fake serial connection;
- integration tests can exercise multiple layers without physical hardware;
- hardware tests should be a separate, explicitly selected test category.

The default CI workflow runs only deterministic tests that require no connected rig.
