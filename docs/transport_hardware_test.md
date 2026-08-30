# DEV-138 Transport hardware-test harness

This document describes the temporary host-side hardware harness on branch
`test/DEV-138--protocol-test`. The branch exists to exercise the shared Transport
implementation against the MCU firmware. It is not intended to be merged directly into
`main`. Reusable serial/connection pieces may later be extracted into focused production
work, while the HRTP codec, scenarios, and CLI are disposable test infrastructure.

## Compatibility

The shared protocol repository is a Git submodule at `external/hil-rig-protocol` and must
be pinned to:

`a24fccc403007cbf6268ff7d0d21f50566a6b2de`

The matching firmware test branch is `test/DEV-138--protocol-test` in
`angus135/hil-rig-mcu-firmware`. See
`docs/transport_hardware_test_compatibility.json` for the machine-readable compatibility
manifest. The paired firmware revision for this compatibility snapshot is
`c6c0af2af586108949c30fb4a12df54eb9dd2fda`.

A normal checkout/setup is:

```sh
git submodule update --init --recursive
python -m pip install -e external/hil-rig-protocol
python -m pip install -e ".[dev,hardware-test]"
```

To verify the submodule pin in a checkout, use:

```sh
git submodule update --init --recursive
git -C external/hil-rig-protocol fetch origin
git -C external/hil-rig-protocol checkout --detach a24fccc403007cbf6268ff7d0d21f50566a6b2de
git add .gitmodules external/hil-rig-protocol
git diff --cached --submodule
```

The staged gitlink must resolve to exactly
`a24fccc403007cbf6268ff7d0d21f50566a6b2de` before the branch commit is created.

This branch requires CPython 3.12 or later because the pinned protocol package requires
Python 3.12 or later. CI covers 3.12 and 3.13.

The protocol package contains a CFFI extension backed by the shared C Transport core.
Building it from source requires a C11 compiler, CPython development headers, CMake 3.17
or later, and the PEP 517 dependencies declared by the protocol repository. The Python
API repository never imports `hil_rig_protocol._native` or defines its own CFFI layer.

## Architecture

`hilrig.protocol_test.connection.ProtocolTestConnection` is the reusable boundary. It
owns exactly one HOST-role public `hil_rig_protocol.Transport`, one serial handle, caller
retained input, one staged output item and partial-write offset, bounded Application and
event queues, link generation state, and service diagnostics. Every Transport call is
made synchronously from the thread that created the connection. There is no internal
worker thread.

The connection layer deals only with complete opaque Application byte strings. It has no
dependency on HRTP, ECHO, STATUS, request IDs, test compilation, captured-run storage,
exporters, or `results/adapter.py`.

The temporary layers are:

- `serial_port.py`: USB serial discovery, pyserial normalization, and deterministic fault
  wrapping.
- `connection.py`: caller-driven serial/Transport servicing.
- `harness_codec.py`: the temporary HRTP ECHO/STATUS envelope.
- `runner.py`: one-request-at-a-time scenario policy and request correlation.
- `trace.py`: JSON Lines evidence and final JSON summaries.
- `cli.py`: `argparse` command line entry point.

## Transport configuration and servicing

The harness uses the public `TransportConfig` API through the single
`hardware_test_transport_config()` factory. Its default HOST test configuration matches
the firmware hardware-test settings:

- maximum Application message size: 512 bytes;
- maximum encoded frame size: 640 bytes;
- HOST session seed: generated normally by the public Transport facade;
- initial reliable sequence: 0;
- connection timeout: 0 ms;
- retransmission timeout: 100 ms;
- maximum retries: 5.

Retransmission remains entirely inside the shared Transport implementation. The Python
harness only supplies the configuration, which allows accepted-but-dropped or corrupted
fault-injection writes to exercise Transport-level recovery. The effective configuration
actually owned by the created Transport, including its generated HOST session seed, is
recorded in each run.

Transport time is always derived from monotonic time and wrapped to uint32 milliseconds.
The service loop normally runs about every 1 ms. Each iteration is bounded and performs
output retry, serial receive, prefix-aware `receive_bytes`, `process(NORMAL)`, event and
Application draining, zero-byte receive when released capacity may unblock retained
work, a bounded second process pass, and another output pass. `process()` is called even
when no serial bytes arrived.

Caller receive bytes live in a bounded `bytearray`. Only the prefix reported by
`ReceiveResult.bytes_consumed` is deleted. The exact suffix is offered again later.
Zero-byte `receive_bytes(b"")` calls are used after queue draining, output commits, and
other state changes that may release native capacity.

Output follows the public `peek_output`/`commit_output` contract. A peeked immutable item
is retained across partial or zero-byte serial writes. `commit_output(now_ms)` is called
exactly once only after pyserial has accepted every byte. The staged Python state is
cleared in a `finally` path even if commit raises or reports a non-OK status, so bytes
already accepted by the external writer cannot be recommitted or immediately resent by
Python.

## Serial selection and permissions

Use an explicit `--port` or one or more USB identity fields: `--vid`, `--pid`, and
`--serial-number`. An explicit port has priority. Identity selection must match exactly
one discovered device; ambiguity is a hard error that lists the candidates. The harness
never chooses the first serial device automatically.

USB CDC is opened nonblocking with `timeout=0` and `write_timeout=0`. The default API baud
argument is 115200, but USB CDC baud is only a host serial API setting and does not define
Transport timing.

On Linux, the current user needs permission to open the device, commonly through the
system's serial-device group or an appropriate udev rule. On Windows, use the enumerated
COM port or a stable USB serial-number selector where available.

## Temporary HRTP envelope

This is test-only and is not the future production Application protocol.

Header size is 16 bytes, little-endian:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 4 | ASCII `HRTP` |
| 4 | 1 | envelope version, currently `1` |
| 5 | 1 | opcode |
| 6 | 2 | flags, currently zero |
| 8 | 4 | request ID |
| 12 | 4 | payload length |
| 16 | N | payload |

Opcodes are `0x01` ECHO request, `0x81` ECHO response, `0x02` STATUS request, and `0x82`
STATUS response. ECHO responses preserve the request ID and payload exactly. STATUS
requests have no payload.

STATUS schema version 1 is exactly 48 bytes and contains twelve consecutive
little-endian `uint32` values, matching firmware PR #63:

| Index | Field |
| ---: | --- |
| 0 | `schema_version` (`1`) |
| 1 | `link_state` |
| 2 | `link_generation` |
| 3 | `transport_event_count` |
| 4 | `usb_rx_bytes` |
| 5 | `usb_tx_bytes` |
| 6 | `application_requests_received` |
| 7 | `responses_submitted` |
| 8 | `usb_tx_busy_retries` |
| 9 | `invalid_harness_messages` |
| 10 | `maximum_service_gap_ms` |
| 11 | `transport_session_state` |

There are no Python-only reserved bytes and no operation-budget counter in the firmware
STATUS v1 wire payload. The exact raw STATUS payload is retained as hex, together with
its hash and size, even if typed decoding fails.

The maximum ECHO test payload is derived from the effective Transport maximum
Application message size minus the 16-byte HRTP header. It is never hardcoded separately.

## CLI

The installed entry point is `hilrig-protocol-test`:

```sh
hilrig-protocol-test smoke --port /dev/ttyACM0
hilrig-protocol-test status --vid 0x1234 --pid 0x5678 --serial-number ABC123
hilrig-protocol-test boundaries --port COM7
hilrig-protocol-test repeat --port /dev/ttyACM0 --count 1000
hilrig-protocol-test reset-reconnect --vid 0x1234 --pid 0x5678 --serial-number ABC123 --cycles 3
hilrig-protocol-test soak --port /dev/ttyACM0 --duration-seconds 3600 --count 10000
```

Common timing/evidence options include `--poll-ms`, `--request-timeout-ms`,
`--reconnect-timeout-ms`, `--output-dir`, `--seed`, and `--log-level`.

### Scenarios

`smoke` waits for a session, queries STATUS, then ECHOs empty, ASCII, zero-containing,
framing-relevant binary, deterministic pseudorandom, and maximum-size payloads.

`boundaries` covers payload sizes 0, 1, 15, 16, a size near the COBS encoded-frame block
boundary, maximum minus one, maximum, and verifies that maximum plus one is rejected
locally without submitting it to Transport.

`repeat` performs a requested number of deterministic one-at-a-time ECHOs and requires
exactly one matching response for each.

`reset-reconnect` completes an ECHO, asks the operator to reset the board, and requires an
observed physical serial disconnect by default. If no disconnect is observed before the
observation deadline, the scenario fails and does not claim that the MCU reset occurred.
For systems where USB disconnect is known to be unobservable, the explicit
`--allow-unobserved-reset` option permits a host-link close/reopen fallback. That mode
still allocates a new link generation, re-establishes a Transport session, and completes
the post-reconnect ECHO, but its result is classified as a host-link recycle with
`mcu_reset_verified=false`. A USB path may change on re-enumeration, so serial-number or
VID/PID selection is preferred.

`soak` supports both elapsed-time and transfer-count limits, deterministic payloads,
periodic STATUS requests, progress evidence, and immediate trace flushing. Long soak
runs are never part of CI.

## Deterministic serial faults

Fault shaping is disabled by default and wraps the serial abstraction rather than
changing pyserial itself. Available CLI controls include maximum read chunks, maximum
accepted write chunks, selected zero writes, selected delayed reads/writes, selected
accepted-but-dropped writes, selected duplicate writes, and selected single-byte write
corruption. Operation numbers are deterministic and 1-based. Accepted-but-dropped writes
return the accepted count so retry/recovery is exercised by Transport, not by immediate
Python retransmission.

Examples:

```sh
hilrig-protocol-test repeat --port /dev/ttyACM0 --count 20 --fault-max-write 3
hilrig-protocol-test repeat --port /dev/ttyACM0 --count 20 --fault-zero-write 2,5
hilrig-protocol-test repeat --port /dev/ttyACM0 --count 20 --fault-drop-write 4
hilrig-protocol-test repeat --port /dev/ttyACM0 --count 20 --fault-corrupt-write 3:1
```

## Evidence

Every run creates one `<run-id>.jsonl` trace and one `<run-id>.summary.json` in the output
directory. Evidence includes expected compatibility revisions and separately observed
Git revisions when available, Python working-tree dirty state, Python and OS versions,
pyserial version, selected device identity, effective Transport configuration, link
generations, request IDs, payload sizes and SHA-256 hashes, timestamps/latencies, public
Transport events, serial and service counters, disconnect/reconnect actions, fault
injection, maximum service gap, and the final pass/failure reason. Large payload contents
are not logged by default. Routine idle 1 ms service iterations are not written to JSONL.
Budget exhaustion and late service gaps are written immediately, and final diagnostics
retain total service-loop, late-loop, maximum-gap, and budget-exhaustion counts. The
JSONL writer flushes every record that is retained.

## Pytest

Physical tests are under `tests/hardware/` and carry the `hardware` marker. The project
pytest configuration excludes this marker by default. Explicit hardware collection/run
uses:

```sh
python -m pytest -m hardware --collect-only
python -m pytest -m hardware tests/hardware
```

Hardware tests require `HILRIG_TEST_PORT` or an unambiguous combination of
`HILRIG_TEST_VID`, `HILRIG_TEST_PID`, and `HILRIG_TEST_SERIAL_NUMBER`. Reset tests also
require explicit manual-reset opt-in, and soak tests require an explicit soak opt-in.
The manual reset test is strict by default. Set
`HILRIG_TEST_ALLOW_UNOBSERVED_RESET=1` only when deliberately testing the host-link
fallback classification rather than verifying an observed MCU reset.

## Known USB CDC limitations

- A reset may remove and recreate the device and may change its path.
- Host serial APIs can report that bytes were accepted before the physical link later
  loses them. The connection therefore commits based on external acceptance and relies on
  Transport for retry/recovery.
- OS scheduling can introduce service gaps above the nominal 1 ms poll period. The harness
  records current/max gaps and late-loop counts rather than assuming real-time scheduling.
- `write_timeout=0` can surface immediate write timeouts on some pyserial backends; these
  are treated as link failures and trigger a controlled disconnect.

## Troubleshooting

**Protocol/CFFI build fails:** confirm Python 3.12+, CPython development headers, a C11
compiler, CMake 3.17+, CFFI, and the protocol repository's build requirements are
installed. Install the protocol package before this repository.

**`hil_rig_protocol` cannot be imported:** run
`python -m pip install -e external/hil-rig-protocol` in the same environment used by the
CLI.

**pyserial is missing:** run `python -m pip install -e ".[hardware-test]"` or the full
`.[dev,hardware-test]` setup command.

**Permission denied:** grant the current user serial-device access or apply the platform's
normal serial permission/driver setup. Do not run a broad privileged process as a normal
workaround.

**Device disappears during reset:** select by USB serial number when available, or by an
unambiguous VID/PID pair, and allow `--reconnect-timeout-ms` to cover re-enumeration.

**Ambiguous selector:** the CLI reports all matching candidates. Add `--serial-number`, a
more specific VID/PID pair, or an explicit `--port`; the harness will not guess.
