"""Build and query a small captured-run IR without needing connected hardware."""

from pathlib import Path

from hilrig import (
    CapturedRunBuilder,
    CommunicationPeripheral,
    CommunicationResult,
    PWMMeasurement,
    TickResult,
)

output = Path("build/example-run.sqlite3")

builder = CapturedRunBuilder(
    output,
    test_id=0x123456789ABCDEF00112233445566778,
    test_name="Synthetic captured run",
    tick_period_ns=1_000_000,
    expected_tick_count=2,
)

for tick in range(2):
    builder.add_tick_result(
        TickResult(
            tick=tick,
            digital_inputs=(tick == 1,) + (False,) * 9,
            analogue_inputs_uv=(1_000_000 + tick, 0),
            pwm_inputs=(
                PWMMeasurement(period_ns=20_000, duty_permyriad=5_000),
                PWMMeasurement(period_ns=0, duty_permyriad=0),
            ),
        )
    )

builder.add_communication_result(
    CommunicationResult(
        tick=1,
        peripheral=CommunicationPeripheral.UART,
        channel=0,
        payload=b"READY\r\n",
    )
)

run = builder.finalize()
run.write_manifest_json("build/example-run.json")
run.write_fixed_results_csv("build/example-fixed-results.csv")
run.write_communication_results_csv("build/example-communication-results.csv")

print(run.metadata)
print(run.digital_input(channel=0).sample_at(1))
