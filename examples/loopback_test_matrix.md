# HIL-RIG Loopback Characterization — Test Matrix

> **Platform:** STM32F446ZE @ 180 MHz | 128 KB SRAM | QSPI NAND (5.4 MB/s combined R+W)
> **SPI max:** 2.813 Mbps | All figures are estimates except where noted. ISR figures in clock cycles.

---

## Configurations

| | **1 — Automotive Gateway** | **2 — High-Speed Sensor** | **3 — Balanced Single-Ch** | **4 — Full Saturation** |
|---|---|---|---|---|
| SPI | 1× @ 2.813 Mbps | 1× @ 2.813 Mbps | 1× @ 2.813 Mbps | 2× @ 2.813 Mbps |
| UART | 1× @ 115.2 kbps | 1× @ 1.0 Mbps | 1× @ 2.0 Mbps | 2× @ 2.0 Mbps |
| CAN | 2× @ 500 kbps | — | 1× @ 1.0 Mbps | 2× @ 1.0 Mbps |
| **Aggregate** | **413 KB/s** | **441 KB/s** | **611 KB/s** | **1,193 KB/s** |
| Adapters | 6 | 4 | 5 | 8 |
| ISR (cycles) | ~11,100 | ~7,400 | ~9,250 † | ~14,800 |
| **Purpose** | Real-world ECU tester | Best 10 kHz timing | Per-peripheral isolation | Hard limit characterisation |

> † Empirical: 9,252 cycles measured at 10 kHz 96% utilization (Config 3 equivalent). Used to derive 1,850 cycles/adapter.

---

## Per-Tick Data Volume (@ 100% utilization)

| Frequency | Tick (cycles) | **1** | **2** | **3** | **4** |
|---|---|---|---|---|---|
| 100 Hz | 1,800,000 | 4,224 B | 4,516 B | 6,109 B | 12,218 B |
| 1 kHz | 180,000 | 422 B | 452 B | 611 B | 1,222 B |
| 10 kHz | 18,000 | 42 B | 45 B | 61 B | 122 B |

**ISR as % of tick period (adapter overhead only, before data processing):**

| Frequency | Tick (cycles) | **1** (11,100) | **2** (7,400) | **3** (9,250) | **4** (14,800) |
|---|---|---|---|---|---|
| 100 Hz | 1,800,000 | 0.6% | 0.4% | 0.5% | 0.8% |
| 1 kHz | 180,000 | 6.2% | 4.1% | 5.1% | 8.2% |
| 10 kHz | 18,000 | **61.7%** | **41.1%** | **51.4%** | **82.2%** ⚠️ |

---

## Constraint Summary

| Constraint | Value | Active at |
|---|---|---|
| Instruction max size | 8,192 B | 100 Hz (large per-tick payloads) |
| Result record max | 2,040 B/record | 100 Hz SPI above ~58% util (chunked transparently) |
| SPI TX buffer | 4,096 B/channel | Limits burst_interval at all frequencies |
| UART TX buffer | 2,048 B/channel | Limits burst_interval at 1 kHz |
| Flash bus (combined) | 5.4 MB/s | **Not a constraint** — max load (Config 4 at 100%) = 44% of bus |
| ISR vs tick period | < tick period | 10 kHz dominant; Config 4 leaves only ~3,200 cycles headroom |

---

## Sweep 1 — Utilization Ceiling (`burst_interval_ticks = 1`)

Estimated ceiling % at which first failure occurs. Limiting factor shown below each cell.

| | **100 Hz** | **1 kHz** | **10 kHz** |
|---|---|---|---|
| **1** Automotive | **~92%** | **~95%** | **~90%** |
| | *Instruction buffer* | *Flash drain / NAND latency* | *ISR cycles (61.7% base + data)* |
| **2** Sensor | **~88%** | **~95%** | **~96%** |
| | *Instruction buffer* | *Flash drain / NAND latency* | *ISR cycles (41.1% base — best margin)* |
| **3** Balanced | **~64%** ‡ | **~95%** | **~95%** |
| | *Instruction buffer* | *Flash drain / NAND latency* | *ISR cycles (51.4% base)* |
| **4** Saturation | **~32%** | **~88%** | **~60%** ⚠️ |
| | *Instruction buffer* | *Flash drain / NAND latency* | *ISR cycles (82.2% base, ~3,200 cycles headroom)* |

> ‡ Empirically observed: passes at 64%, fails at 65% with instruction upload error.

**Utilization sweep steps:** 10%, 25%, 50%, 75%, 85%, 90%, 95%, 98%, 100%

---

## Sweep 2 — Burstiness (`burst_interval_ticks` sweep at 75% of ceiling)

A burst interval of N packs N ticks of data into one instruction and TX buffers in one tick. Estimated maximum `burst_interval_ticks` before first failure.

**Base utilization (75% × ceiling estimate):**

| | 100 Hz base | 1 kHz base | 10 kHz base |
|---|---|---|---|
| **1** | 69% | 71% | 68% |
| **2** | 66% | 71% | 72% |
| **3** | 48% | 71% | 71% |
| **4** | 24% | 66% | 45% |

**Estimated max burst_interval before failure:**

| | **100 Hz** | **1 kHz** | **10 kHz** |
|---|---|---|---|
| **1** Automotive | **~1** | **~16** | **~60** |
| | *SPI TX buffer (4 KB ≈ 1.2× one tick of SPI data)* | *SPI TX buffer (4 KB / 250 B/tick)* | *ISR spike on burst tick* |
| **2** Sensor | **~1** | **~16** | **~80** |
| | *SPI TX buffer* | *SPI TX buffer* | *ISR spike (most headroom — fewest adapters)* |
| **3** Balanced | **~1** | **~14** | **~50** |
| | *SPI TX buffer / Instruction buffer* | *UART TX buffer (2 KB / 142 B/tick)* | *ISR spike on burst tick* |
| **4** Saturation | **~2** | **~8** | **~15** ⚠️ |
| | *Instruction buffer (8 KB / ~3 KB/tick)* | *Instruction buffer (8 KB / ~806 B/tick)* | *ISR spike (only ~3,200 cycles headroom)* |

> [!NOTE]
> At 100 Hz, the SPI TX buffer (4,096 B) holds only ~1.2 ticks of SPI data at full rate. This makes `burst_interval_ticks = 2` infeasible for Configs 1, 2, and 3 at their base utilization. Config 4 achieves burst_interval = 2 only because its lower base utilization (24%) keeps per-tick SPI data to ~879 B/channel.

> [!NOTE]
> At 10 kHz, the ISR spike from processing N× bytes in one tick is the limiting factor. The ~1,850-cycle-per-adapter baseline consumes 41–82% of the tick, leaving limited headroom before the burst tick's commit loop pushes the ISR over the tick period.

**Burstiness sweep steps:** `burst_interval_ticks ∈ [1, 2, 5, 10, 20, 50, 100]`

---

## Metrics to Capture Per Test Point

| Metric | Source | Unit |
|---|---|---|
| Pass/fail boundary | Python API result | % utilization or burst_interval |
| ISR avg / peak | `run_state status` | **cycles** |
| ISR peak sample index | `run_state status` | tick # |
| SPI rx_unread_peak | `run_state status` | bytes |
| UART rx_unread_peak | `run_state status` | bytes |
| SPI/UART tx_rejects | `run_state status` | count |
| CAN RX dropped | `run_state status` | count |
| Result TX rate | `host status` | msgs/s |
| USB TX rejects | `host status` | count |
| Result custody chain | `host status` | match / mismatch |

---

## Test Execution Order

1. **Config 1** — Automotive Gateway *(expect high ceilings, establishes system credibility)*
2. **Config 2** — High-Speed Sensor *(best 10 kHz ISR margin, cleanest timing data)*
3. **Config 3** — Balanced Single-Ch *(per-peripheral isolation, 100 Hz chunking visible)*
4. **Config 4** — Full Saturation *(all three constraints hit, motivates architecture discussion)*
5. **Burstiness sweeps** on Configs 1 and 4 *(comfortable vs stressed burst tolerance)*
