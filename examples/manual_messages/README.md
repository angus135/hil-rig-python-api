# Manual Application-message examples

These files each construct and send exactly one Application message. They do not create
a `Test`, compile a test definition, or generate run artifacts.

All examples use this Application Test ID:

```text
00112233445566778899aabbccddeeff
```

Typical debugging sequence:

```text
HIL-RIG> manual connect COM=2 --skip-system-info
HIL-RIG> manual send "examples\manual_messages\configuration.json"
HIL-RIG> manual send "examples\manual_messages\instruction.json" --transport-only
HIL-RIG> manual send "examples\manual_messages\start.json"
HIL-RIG> manual inbox
HIL-RIG> manual disconnect
```

Supported `type` values are:

- `test_configuration`
- `test_instruction`
- `execution_control`
- `global_control`

Configuration channel lists contain only enabled/overridden channels; omitted channels
are disabled. Instruction lists contain nonzero/true output entries; omitted instruction
outputs are encoded with the protocol defaults (`false` or zero). Values use protocol
units: tick duration in microseconds, analogue output in microvolts, PWM period in
nanoseconds, and PWM duty cycle in permyriad (`5000` means 50%).
