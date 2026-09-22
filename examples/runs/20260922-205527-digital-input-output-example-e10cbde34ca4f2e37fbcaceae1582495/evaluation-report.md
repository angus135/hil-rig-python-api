# HIL-RIG Test Report: Digital input/output example

**Overall verdict:** `INCONCLUSIVE`  
**Capture status:** `PROTOCOL_ERROR`  
**Test ID:** `af871f314255561efc325fd8429cc358`  
**Application Test ID:** `5c67be1818258c0c65ad377835455229`  
**Run ID:** `e10cbde34ca4f2e37fbcaceae1582495`  
**Capture database:** `captured-run.sqlite3`  
**Evaluated at:** `2026-09-22T10:56:26.688667+00:00`

## Summary

- Expected fixed ticks: 701
- Received fixed ticks: 0
- Assertion set: `original`
- Compiled IR version: `1.1`
- Passed: 0
- Failed: 0
- Inconclusive: 1

## Assertion results

| ID | Verdict | Assertion | Channel | Tick/window | Summary |
|---:|---|---|---:|---|---|
| 0 | INCONCLUSIVE | digital_input.remain_high | 0 | 310–600 | No violation was observed, but the result is inconclusive because 291 expected samples were missing and 0 samples were invalid. |

### Assertion 0: INCONCLUSIVE

- Definition: `digital_input[0].remain_high`
- Tick/window: `310–600`
- Expected: `from_tick=310`; `until_tick=600`
- Observed: `high_sample_count=0`; `low_sample_count=0`
- Valid samples: 0
- Missing samples: 291
- Invalid samples: 0
- Violations: 0
- First failure tick: —

No violation was observed, but the result is inconclusive because 291 expected samples were missing and 0 samples were invalid.

## Warnings

- Capture status is protocol_error; received 0 of 701 expected fixed-tick results.
