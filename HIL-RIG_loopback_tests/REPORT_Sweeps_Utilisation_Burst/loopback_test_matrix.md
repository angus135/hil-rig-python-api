# HIL-RIG Loopback Characterization — Test Matrix

> **Platform:** STM32F446ZE @ 180 MHz | 128 KB SRAM | QSPI NAND (5.4 MB/s combined R+W)
>
> Unless explicitly marked as measured, bandwidth and per-tick figures are theoretical values derived from configured wire rates. Sweep boundaries, timing distributions, and limiting mechanisms are populated only from recorded runs.

## Configurations

| | **1 — Automotive Gateway** | **2 — High-Speed Sensor** | **3 — Balanced Single-Ch** | **4 — Full Saturation** |
|---|---:|---:|---:|---:|
| SPI | 1 × 2.813 Mbps | 1 × 5.625 Mbps | 1 × 1.406 Mbps | 2 × 5.625 Mbps |
| UART | 1 × 115.2 kbps | 1 × 1.0 Mbps | 1 × 2.0 Mbps | 2 × 2.0 Mbps |
| CAN | 2 × 500 kbps | — | 1 × 1.0 Mbps | 2 × 1.0 Mbps |
| **Theoretical payload rate** | **413 KiB/s** | **784 KiB/s** | **425 KiB/s** | **1,879 KiB/s** |
| **Purpose** | Representative gateway | High-throughput single channel | Lower-rate isolation case | Maximum multi-channel stress |

The common requested utilization percentage is applied independently to every active communication path. The compiled manifest records the actual per-peripheral utilization produced after integer byte/frame rounding; this is metadata from the same run, not a separate utilization-vector sweep.

The SPI allocations above supersede the previous assumption that every configuration used 2.813 Mbps. They remain provisional until commissioning confirms the configured rates and physical loopbacks. All aggregate figures must be regenerated if a rate changes.

### Theoretical payload-rate model

- UART uses the configured framing cost, initially 10 wire bits per payload byte.
- SPI uses 8 clocked bits per payload byte. Chip-select and inter-transfer gaps are not included.
- CAN uses 8 payload bytes per frame and an initial model of 135 wire bits per frame.
- CAN bit stuffing, arbitration, physical gaps, DMA setup, and software overhead mean modeled utilization is not measured bus occupancy.

## Approximate Payload Per Tick at 100% Modeled Utilization

Values are aggregate payload bytes across the active communication paths. The exact compiled values can differ slightly because bytes and CAN frames are discrete.

| Frequency | Tick budget at 180 MHz | **1** | **2** | **3** | **4** |
|---|---:|---:|---:|---:|---:|
| 100 Hz | 1,800,000 cycles | 4,224 B | 8,031 B | 4,350 B | 19,240 B |
| 1 kHz | 180,000 cycles | 422 B | 803 B | 435 B | 1,924 B |
| 10 kHz | 18,000 cycles | 42 B | 80 B | 44 B | 192 B |

These values describe theoretical wire payload, not encoded instruction bytes, result bytes, or flash traffic. Instruction headers, operation headers, alignment, result headers, chunking, and NAND page behaviour must be reported separately from compiled and measured run metadata.

## Preliminary ISR Estimates

These are draft topology estimates retained as sanity-check baselines. They approximate fixed adapter overhead before workload-dependent copying, record production, queue management, interrupt preemption, and driver work. Final reporting replaces them with run metadata.

| Frequency | Tick budget | **1** (~11,100 cycles) | **2** (~7,400 cycles) | **3** (~9,250 cycles) | **4** (~14,800 cycles) |
|---|---:|---:|---:|---:|---:|
| 100 Hz | 1,800,000 cycles | 0.6% | 0.4% | 0.5% | 0.8% |
| 1 kHz | 180,000 cycles | 6.2% | 4.1% | 5.1% | 8.2% |
| 10 kHz | 18,000 cycles | 61.7% | 41.1% | 51.4% | 82.2% |

The 9,252-cycle reference originated from the existing `loopback_utilization_point.py` profile at 10 kHz and 96% utilization. That profile completed at 96% and failed at 97% through the SPI driver, not an observed ISR deadline exceedance. The other cycle values were inferred using approximately 1,850 cycles per active adapter. They are topology sanity checks, not predicted utilization ceilings; the empirical result indicates that Configurations 1–3 are more likely to encounter representation, driver, buffer, or throughput limits before the 10 kHz ISR deadline. Configuration 4 remains the only profile for which ISR margin is a plausible early constraint.

### Current ISR-overrun detection limitation

The firmware records ISR sample count, total, minimum, maximum, and the boundary producing the maximum. It does not currently compare the measured ISR duration with the configured execution period or latch an execution failure when the duration exceeds that period. `EXECUTION_MANAGER_FAILURE_INSTRUCTION_LATE` detects a logical instruction timestamp that is already behind the serviced execution boundary; it is not a wall-clock ISR-overrun detector.

Until explicit firmware detection exists, the Python run analysis must classify a run as failed when `maximum_cycles >= deadline_cycles` and must retain the raw cycle values. This post-run check detects that at least one ISR exceeded its period, but it cannot reconstruct how many hardware timer updates may have coalesced while the ISR was running. Strong bounded-fault claims therefore require a firmware-side deadline-exceed counter or fault latch.

## Run Lengths

Run length is authoritative in ticks. The workload contains an active stimulus interval followed by a quiescent drain interval long enough for the final asynchronous transfers to complete and be observed.

```text
total_run_ticks = stimulus_ticks + drain_ticks
```

Utilization and equal-exposure comparisons use `stimulus_ticks`; no new traffic is scheduled during `drain_ticks`.

| Run class | 100 Hz | 1 kHz | 10 kHz |
|---|---:|---:|---:|
| Discovery stimulus | 200 ticks | 2,000 ticks | 20,000 ticks |
| Boundary-confirmation stimulus | 1,000 ticks | 10,000 ticks | 100,000 ticks |
| 60-second soak stimulus | 6,000 ticks | 60,000 ticks | 600,000 ticks |
| Extended soak stimulus | 30,000 ticks (5m) | 300,000 ticks (5m) | 900,000 ticks (90s)* |

\* At 10 kHz, extended soak is capped at 900,000 ticks (90 seconds / 900k samples) to keep `expected_tick_count` safely below the protocol engine ceiling of `< 1,000,000` ticks (which includes settling time).

`drain_ticks` is calculated per compiled workload from the final commanded traffic, cumulative peripheral service time, and the configured receive-observation allowance. It is stored separately in the run manifest.

## Sweep 1 — Utilization Ceiling

Smooth traffic uses `burst_interval_ticks = 1`.

### Coarse discovery

```text
25%, 50%, 75%, 90%, 100%
```

- Adaptive discovery sweeps ascending coarse points with predetermined seed 1.
- Early termination on failure: as soon as an upper coarse point fails, higher points are skipped to immediately begin binary bracket refinement.
- Requested percentages that compile to an identical workload are deduplicated and retain aliases in the summary.
- The coarse grid is configurable to expanded resolution (e.g., `10%, 25%, 50%, 75%, 85%, 90%, 95%, 98%, 100%`) when deep unguided exploration is requested.

### Boundary search and confirmation

1. Bracket the transition from the contiguous all-pass region starting at zero utilization.
2. Refine the bracket via binary search until distinct compiled workloads are no more than one requested percentage point apart (`resolution = 1.0%`).
3. Scan every integer percentage in any mixed or non-monotonic region.
4. Confirm the candidate stable passing point and lowest failing point across six predetermined seeds (`seeds 1..6`, 6 runs per point).
5. Classify any mixed result as unstable; do not use majority voting.

The reported result is the highest utilization that passed the prescribed finite campaign, not an unlimited-duration maximum.

### Boundary results to populate

| | **100 Hz** | **1 kHz** | **10 kHz** |
|---|---|---|---|
| **1 — Automotive Gateway** | TBD from sweep | TBD from sweep | TBD from sweep |
| **2 — High-Speed Sensor** | TBD from sweep | TBD from sweep | TBD from sweep |
| **3 — Balanced Single-Ch** | TBD from sweep | TBD from sweep | TBD from sweep |
| **4 — Full Saturation** | TBD from sweep | TBD from sweep | TBD from sweep |

For each cell, report the requested and actual compiled utilization, highest stable pass, lowest fail, unstable points, run counts, exposure ticks, fault classes, and identified limiting mechanism.

### Preliminary planning estimates

The following estimates are retained for draft reporting and test sanity checks. They are first-order rescalings of the previous matrix: where a profile rate changed, the estimate assumes the limiting mechanism occurs at approximately the same absolute compiled traffic or service load. This is deliberately conservative and is not evidence of the final boundary.

| | **100 Hz** | **1 kHz** | **10 kHz** |
|---|---:|---:|---:|
| **1 — Automotive Gateway** | ~92–96% | ~95–100% | ~98–100% |
| Likely first constraint | Encoded tick/upload size | Flash latency or driver overhead | Driver/buffer overhead |
| **2 — High-Speed Sensor** | ~48–51% | ~90–100% | ~96–100% |
| Likely first constraint | Encoded tick/upload size | Flash latency or SPI driver | SPI driver/queue |
| **3 — Balanced Single-Ch** | ~88–94% | ~95–100% | ~98–100% |
| Likely first constraint | Encoded tick/upload size | Driver or flash overhead | Driver/buffer overhead |
| **4 — Full Saturation** | ~18–21% | ~80–90% | ~60–85% |
| Likely first constraint | Encoded tick/upload size | Combined flash/driver overhead | SPI/driver pressure; ISR margin also plausible |

Interpret a result that differs substantially from these estimates as an investigation trigger, not automatically as a defect. First compare actual encoded instruction size, actual per-peripheral utilization, peak ISR cycles, buffer high-water marks, and flash refill/drain metrics.

Historical checkpoints retained for context:

- An earlier Config 3-like workload passed at 64% and failed instruction upload at 65% at 100 Hz under the previous 2.813 Mbps SPI assumption. It is not the expected boundary for the revised 1.406 Mbps profile.
- The API example recorded a 92% smooth-workload hardware pass at 1 kHz on 2026-09-29.
- The current `loopback_utilization_point.py` profile passed at 96% and failed at 97% at 10 kHz through the SPI driver. Its 5.625 Mbps SPI, 2 Mbps UART, and 1 Mbps CAN profile is an empirical upper-load anchor, but it is not identical to any revised configuration.

## Sweep 2 — Burstiness

Run after a stable utilization ceiling is established.

- Primary configurations: 1 and 4.
- Initial load: approximately 75% of the measured stable ceiling.
- Optional configurations: 2 and 3 if project time permits.
- Candidate burst intervals:

```text
1, 2, 5, 10, 20, 50, 100 ticks
```

A burst interval of `N` places approximately `N` ticks of traffic into one commanded boundary while preserving the run's average modeled utilization. The compiled manifest must report maximum payload and encoded instruction size at an active boundary so failures can be attributed to instantaneous instruction, queue, buffer, or ISR pressure rather than average wire utilization.

### Preliminary burst estimates

These estimates use approximately 75% of the preliminary ceiling above. They retain the previous absolute-load assumptions and are sanity checks only.

| | 100 Hz base | 1 kHz base | 10 kHz base |
|---|---:|---:|---:|
| **1** | 69–72% | 71–75% | 74–75% |
| **2** | 36–38% | 68–75% | 72–75% |
| **3** | 66–71% | 71–75% | 74–75% |
| **4** | 14–16% | 60–68% | 45–64% |

| | **100 Hz** | **1 kHz** | **10 kHz** |
|---|---:|---:|---:|
| **1 — Automotive Gateway** | ~1 tick | ~16 ticks | ~60 ticks |
| **2 — High-Speed Sensor** | ~1 tick | ~8 ticks | ~80 ticks |
| **3 — Balanced Single-Ch** | ~1 tick | ~14 ticks | ~50 ticks |
| **4 — Full Saturation** | ~1 tick | ~3 ticks | ~10–15 ticks |

Before executing a proposed burst point, compilation must verify the maximum encoded instruction size and per-channel payload against the instruction, SPI, and UART buffer capacities. A compile-time infeasible point is recorded as an admission/representation boundary rather than a runtime result.

## MCU-Tick Timing Analysis

The sweep is evaluated entirely relative to the MCU execution tick. It does not require scope or logic-analyser capture during every run.

For every commanded transfer, the Python post-run analyser must reconstruct the expected ordered stream and determine:

- Commanded tick.
- First and final receive-observation tick attributable to that transfer.
- Modeled wire/service completion tick.
- Observation offset in ticks.
- Excess observation offset beyond modeled service time.
- Configured pipeline allowance.
- Pass/fail against the half-open assertion window.

UART and SPI result records are sparse stream chunks and need not align with commanded transfers. The analyser maps ordered byte ranges back to commanded transfers; the timestamp of the record containing a transfer's final byte is its conservative completion-observation tick. CAN retains frame boundaries and is matched by ordered frame identity and payload.

Expected timing must use a cumulative per-channel service model rather than treating every transfer independently:

```text
modeled_start[i]  = max(commanded_tick[i], modeled_finish[i - 1])
modeled_finish[i] = modeled_start[i] + modeled_wire_duration[i]
```

This accounts for rounding, queued back-to-back traffic, and burst workloads. Bus-specific framing and arbitration assumptions are stored in the manifest.

The firmware timestamp is the execution boundary at which accumulated receive data was consumed. It is an observation tick, not an exact physical arrival timestamp.

## Independent Real-Time Correlation Test

A separate scope/logic-analyser test establishes the link between MCU-tick-relative results and real-world time. It is not part of the utilization sweep.

The test must:

1. Measure an execution-boundary marker against an independent calibrated timebase.
2. Cover the 100 Hz, 1 kHz, and 10 kHz timer configurations, or document why one timer configuration is representative of the others.
3. Correlate representative UART, SPI, and CAN physical events with the boundary marker.
4. Record instrument accuracy, sample rate, trigger definition, measured period error, drift, jitter, and marker/instrumentation overhead.
5. Preserve the firmware build hash and instrument setup used for the correlation.

The report uses this experiment to translate tick-relative bounds into real-world time; it does not imply that firmware receive timestamps are hardware arrival timestamps.

## Full-Run Data Integrity

Every run verifies the complete ordered communication stream, not only representative transfers:

- Exact expected and observed byte/frame counts.
- Ordered payload digest or exact complete-stream comparison.
- Missing, duplicated, reordered, or corrupted byte/frame detection.
- Complete expected result capture and result-custody confirmation.
- No unexpected peripheral, application, protocol, buffer, or transport error.

The payload/schedule manifest hash covers the exact transfer ticks, lengths, ordered payload digests, generator definition, profile, compiled utilization, assertion bounds, and relevant software revision. The requested-point hash alone is not sufficient evidence of the exact emitted stream.

## Point Classification

| Classification | Definition |
|---|---|
| Stable pass | Every required run passes timing, integrity, custody, deadline, and diagnostic criteria. |
| Stable fail | Every required run fails with the same bounded and observable fault class. |
| Unstable | Mixed pass/fail results, inconsistent fault classes, or inconsistent repeated-seed behaviour. |
| Inconclusive | Invalid setup, insufficient capture evidence, host interruption, or missing required metadata. |

A stable pass requires:

- Terminal completion at the expected boundary.
- Every commanded transfer accounted for and within its MCU-tick observation bound.
- Exact full-stream data integrity.
- No execution deadline violation.
- No instruction underrun, result reservation/commit failure, TX rejection, CAN drop, or unclassified application/protocol error.
- Complete result custody with no audit overwrite.
- Valid required run-metadata sections.

## Failure Domains

Report the first limiting domain separately so a representation or upload limit is not described as an execution-timing limit:

1. Workload compilation or representation.
2. Instruction upload or admission.
3. Runtime instruction supply.
4. Peripheral queue/buffer acceptance.
5. Execution ISR deadline.
6. Receive timing or data integrity.
7. Result buffering and NAND service.
8. Post-run result transport and host custody.

Preserve the underlying fault code and first failing tick/boundary wherever available.

## Known Capacity and Deadline Constraints

| Constraint | Current value | Required evidence |
|---|---:|---|
| Instruction maximum | 8,192 B | Maximum compiled encoded instruction size |
| Result record maximum | 2,040 B/record | Chunk count and complete stream reconstruction |
| SPI TX buffer | 4,096 B/channel | Peak queued bytes and TX rejection count |
| UART TX buffer | 2,048 B/channel | Peak queued bytes and TX rejection count |
| NAND bus | 5.4 MB/s combined R+W | Measured instruction refill/result drain statistics |
| 100 Hz execution deadline | 1,800,000 cycles | Peak ISR cycles and margin |
| 1 kHz execution deadline | 180,000 cycles | Peak ISR cycles and margin |
| 10 kHz execution deadline | 18,000 cycles | Peak ISR cycles and margin |

Do not infer flash margin from aggregate wire payload alone. Flash load includes encoded instructions, result headers, alignment, chunking, page operations, and simultaneous instruction refill/result drain.

### Current diagnostic limitations

- UART and SPI circular RX DMA currently derive producer position from the modulo DMA index without tracking producer wraps. A complete unread lap can therefore appear empty instead of latching a driver overrun. Until wrap accounting is implemented, `rx_unread_peak` alone cannot prove that an RX overrun did not occur; exact full-stream byte custody is the authoritative sweep check.
- CAN internally counts RX drops with 32-bit counters, while the current aggregate diagnostic snapshot narrows them to 16 bits. Report metadata should preserve the full-width counters for long runs.
- ISR duration above the execution period is recorded only as a maximum-cycle observation, not as a firmware terminal fault. Python must apply the deadline classification described above until firmware-side detection is added.

For the stronger observable-and-classified-fault claim, add UART/SPI DMA wrap accounting and explicit ISR deadline detection before the final report campaign. Their absence does not prevent initial characterization because full-stream verification and post-run timing analysis still expose a failed run, but the fault may be host-classified rather than firmware-classified.

## Metrics Captured Per Run

### Identity and reproducibility

- Board identifier and hardware revision.
- Firmware, Python API, and test-suite revisions.
- Configuration and workload hashes.
- Frequency, stimulus ticks, drain ticks, and total run ticks.
- Requested and actual compiled utilization.
- Seed and exact ordered payload digest.
- Compiled instruction count, maximum instruction size, payload bytes, and result expectation count.

### Execution and timing

- Verdict, classification, first fault, and fault boundary.
- Expected and completed execution boundaries.
- Per-transfer commanded and receive-observation timing.
- Per-peripheral offset distribution: count, minimum, median, selected upper percentiles, and maximum.
- ISR sample count, average, peak cycles, peak boundary, and deadline margin.
- Instruction-buffer minimum unread bytes and boundary.

### Buffers, storage, and custody

- Result record/byte totals and peak pending bytes.
- Result reservation and commit failures.
- NAND instruction refill/result drain counts, maximum service times, service gaps, and contention count.
- UART/SPI unread-buffer peaks and TX rejects.
- CAN receive drops.
- USB/result-transport rejection counts and transfer rate.
- Result-custody status, invariant failure, and audit overwrite count.

Unavailable metrics are recorded as unavailable, not inferred.

## Endurance, Soak Validation, and Automated Backoff

After boundary confirmation, test a guarded operating point initially selected as 100% (or guarded fraction) of the confirmed stable ceiling, subject to the point remaining strictly below any observed failing/unstable percentage.

Escalate through discovery length (2s), boundary-confirmation length (10s), 60 seconds, and 5 minutes. 

### Automated resilience and soak backoff

If soak validation encounters a failure at the candidate boundary ceiling, the orchestrator triggers an automated **2-stage backoff engine**:
1. It steps down to the highest previously passing lower candidate point.
2. It restarts soak validation at the stepped-down utilization (up to two successive backoff attempts).
3. If backoff succeeds, the stepped-down ceiling is recorded as the confirmed soak-validated ceiling; if all backoff attempts fail, the cell is flagged with its limiting failure domain.

Report a failure at its first tick and elapsed time, or report survival for at least the configured exposure. A completed capped run is right-censored soak evidence, not proof of unlimited lifetime.

## Campaign & Test Suite Organization

The loopback test suite is partitioned into dedicated, independent test suites:

- **`REPORT_Sweeps_Utilisation_Burst/`**: Whole-rig characterization campaign orchestrator (`loopback_sweep_campaign.py`) and authoring matrix for Sweep 1 (Utilization Ceiling) and Sweep 2 (Burstiness) across Configurations 1..4.
- **`UART Sweep (development)/`**: Independent single-channel UART prototype sandbox (`uart_prototype_sweep.py`, `uart_prototype_burst_sweep.py`) for early hardware commissioning and fast iteration.
- **`Endurance/`**: Standalone qualification harness (`burn_in_test.py`) for deep multi-minute and multi-hour soak runs (e.g., 1-hour 360,000-tick burn-in @ 100 Hz) independent of the sweep grid.

## Execution Order

1. Commission Configuration 1 at low utilization and validate timing assumptions.
2. Run Configuration 1 utilization, confirmation, and initial soak campaigns.
3. Run Configurations 2 and 3 utilization and limited soak campaigns.
4. Commission multi-channel ownership and scheduling, then run Configuration 4.
5. Run burstiness campaigns on Configurations 1 and 4.
6. Run the independent real-time correlation test and connect its timebase evidence to the MCU-tick-relative sweep results.

Rotate seeds, frequencies, and utilization blocks according to a predetermined schedule. Standardize reset and preconditioning for repeatability runs; record separate no-reset and post-fault recovery campaigns rather than mixing their state histories into the boundary confirmation data.

## Sweep-Level Outputs

- Machine-readable JSON.
- CSV run summary.
- Human-readable Markdown summary.
- Highest confirmed stable passing utilization.
- Lowest confirmed failing utilization.
- Unstable/non-monotonic regions.
- Failure-domain and fault-class counts.
- Exposure totals in runs, ticks, transfers, frames, and bytes.
- Timing and deadline-margin distributions.
- Per-run artifact-directory references and hashes.

All report conclusions are limited to the tested boards, builds, configurations, environmental conditions, and finite exposure.
