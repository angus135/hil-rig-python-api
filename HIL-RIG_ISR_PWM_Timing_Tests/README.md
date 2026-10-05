# SPI1 execution ISR timing

Run the existing definition through the Python API terminal:

```text
run "HIL-RIG_ISR_PWM_Timing_Tests/isr_baseline_60s.py"
```

Despite the historical filename, the current settings are 10 kHz and 200,000
execution ticks: 20 seconds nominal. The test sends 16 bytes (`00` through `0F`)
on SPI1 every tick during the first 19 seconds, with the final second
reserved for draining transfers. SPI1 is master, mode 0, 8-bit, MSB first at
approximately 2.813 MHz (about 45.5 us per 16-byte transfer). Disconnect DUTs that should not receive
this traffic. No loopback or receive assertions are required.

`FREQUENCY_MODE` and `EXECUTION_TICKS` set the rate and duration. For a 30-second
10 kHz run, use `FrequencyMode.HZ_10K` and `300_000` ticks. Keep the tick count
below one million and greater than one second of ticks. Compilation uses the
API's normal one-second tail, without overriding the compiled test duration.

Probe Blue 2 / PE2 relative to board ground. Record rising-edge interval mean,
min/max, standard deviation and population, plus positive pulse-width mean/max.
Clear scope statistics before the run and exclude idle gaps. The final second
has no new SPI operations: measure the active-workload interval separately
when comparing loaded ISR pulse widths with the inert baseline.

The ISR queues transfers; peripheral/DMA work continues asynchronously. This
adds real work without inserting an artificial ISR delay. Configured interface
receive servicing may also contribute to load; there are no receive assertions.
Check normal completion and firmware diagnostics for rejected operations or
queue overruns. No assertions means a report alone does not prove transmitted
data was received correctly or timing was accurate.

Running the file directly prints a compilation summary only. The normal runner
handles the host session and test lifecycle; do not manually enter MCU
`run_state receive/configure/execute` commands for this test.

