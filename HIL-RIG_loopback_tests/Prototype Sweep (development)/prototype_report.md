# HIL-RIG Loopback Characterization — Automated Sweep Campaign Report

## Sweep 1 — Utilization Ceiling Boundaries

| Configuration | 100 Hz | 1 kHz | 10 kHz |
|---|---|---|---|
| **0 — Prototype (UART2 + SPI2)** | 58% (Pass Soak) | 99% (Pass Soak) | 97% (Pass Soak) |

## Sweep 2 — Burstiness Limits

| Configuration | 100 Hz | 1 kHz | 10 kHz |
|---|---|---|---|
| **0 — Prototype (UART2 + SPI2)** | 1t @ 43.5% (Pass Soak) | 7t @ 74.2% (Pass Soak) | 57t @ 72.8% (Fail Soak) |

## Cell-by-Cell Boundary Justification & Failure Attribution

| Configuration | Frequency | Sweep | Verified Stable | Lowest Failing Target | Failing Stage | Limiting Failure Domain | Root Cause / Observed Reason |
|---|---:|---|---:|---:|---|---|---|
| **0 — Prototype (UART2 + SPI2)** | 100 Hz | Utilization | 58% | 59% | bracket_refinement | `4_peripheral_queue_buffer` | SPI ch1 peak tick payload (4149 B) exceeds TX buffer (4096 B) |
| **0 — Prototype (UART2 + SPI2)** | 1000 Hz | Utilization | 99% | 100% | coarse_discovery | `4_peripheral_queue_buffer` | Analog Out rejected: Queue Full at boundary 505 |
| **0 — Prototype (UART2 + SPI2)** | 10000 Hz | Utilization | 97% | 98% | bracket_refinement | `6_receive_timing_integrity` | Run verification verdict: fail |
| **0 — Prototype (UART2 + SPI2)** | 100 Hz | Burstiness | 1 ticks | 2 ticks | coarse_discovery | `4_peripheral_queue_buffer` | SPI ch1 peak tick payload (6118 B) exceeds TX buffer (4096 B) |
| **0 — Prototype (UART2 + SPI2)** | 1000 Hz | Burstiness | 7 ticks | 8 ticks | bracket_refinement | `4_peripheral_queue_buffer` | SPI ch1 peak tick payload (4174 B) exceeds TX buffer (4096 B) |
| **0 — Prototype (UART2 + SPI2)** | 10000 Hz | Burstiness | 57 ticks | 59 ticks | soak_validation | `2_instruction_upload` | Received a TestResult before START completed |

## Verified Operating Point Resource Headroom & Diagnostics

| Configuration | Frequency | Verified Ceiling | Peak ISR Margin | Instruction Buffer Headroom | Result RAM / Flash Activity | Peripheral Buffer Headroom | Diagnostics & Health |
|---|---:|---:|---|---|---|---|---|
| **0 — Prototype (UART2 + SPI2)** | 100 Hz | 58% | 72462 / 1800000 cycles (96.0% idle) | 2852 B unread (34.8% headroom) | Result RAM peak: 7321 B (0.0% headroom)<br>Flash: 0 refills, 0 drains | SPI ch0: peak 0 B (100.0% headroom)<br>SPI ch1: peak 3824 B (6.6% headroom)<br>UART ch0: peak 0 B (100.0% headroom)<br>UART ch1: peak 1160 B (43.4% headroom)<br>CAN ch0: peak 0 frames (100.0% headroom)<br>CAN ch1: peak 0 frames (100.0% headroom) | 0 loss, 0 RX drops (CAN drops: 0) |
| **0 — Prototype (UART2 + SPI2)** | 1000 Hz | 99% | 18692 / 180000 cycles (89.6% idle) | 928 B unread (11.3% headroom) | Result RAM peak: 2972 B (0.0% headroom)<br>Flash: 0 refills, 0 drains | SPI ch0: peak 0 B (100.0% headroom)<br>SPI ch1: peak 697 B (83.0% headroom)<br>UART ch0: peak 0 B (100.0% headroom)<br>UART ch1: peak 404 B (80.3% headroom)<br>CAN ch0: peak 0 frames (100.0% headroom)<br>CAN ch1: peak 0 frames (100.0% headroom) | 0 loss, 0 RX drops (CAN drops: 0) |
| **0 — Prototype (UART2 + SPI2)** | 10000 Hz | 97% | 7153 / 18000 cycles (60.3% idle) | 112 B unread (1.4% headroom) | Result RAM peak: 3085 B (0.0% headroom)<br>Flash: 0 refills, 0 drains | SPI ch0: peak 0 B (100.0% headroom)<br>SPI ch1: peak 69 B (98.3% headroom)<br>UART ch0: peak 0 B (100.0% headroom)<br>UART ch1: peak 44 B (97.9% headroom)<br>CAN ch0: peak 0 frames (100.0% headroom)<br>CAN ch1: peak 0 frames (100.0% headroom) | 0 loss, 0 RX drops (CAN drops: 0) |

## Upload Bandwidth, Execution Duration & USB Host Efficiency

| Configuration | Frequency | Verified Ceiling | Total Requested Runtime | Total Python Wall Time | Avg Upload Bandwidth | Execution Duty Efficiency | Host Overhead Ratio |
|---|---:|---:|---:|---:|---:|---:|---:|
| **0 — Prototype (UART2 + SPI2)** | 100 Hz | 58% | 200.0 s | 730.3 s | N/A | 24.2% | 3.153 |
| **0 — Prototype (UART2 + SPI2)** | 1000 Hz | 99% | 90.0 s | 598.3 s | N/A | 14.3% | 6.365 |
| **0 — Prototype (UART2 + SPI2)** | 10000 Hz | 97% | 85.0 s | 1041.8 s | N/A | 7.6% | 12.608 |