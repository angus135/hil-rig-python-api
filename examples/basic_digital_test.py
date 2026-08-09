from hilrig import (
    DigitalState,
    FrequencyMode,
    LogicVoltage,
    StartMode,
    Test,
)

test = Test(name="Digital input/output example")
test.configure(
    frequency_mode=FrequencyMode.HZ_1K,
    start_mode=StartMode.IMMEDIATE,
)

button = test.digital_input(channel=0)
button.configure(voltage=LogicVoltage.V3_3)

led = test.digital_output(channel=0)
led.configure(voltage=LogicVoltage.V3_3, initial_state=DigitalState.LOW)
led.high(at_ms=100)
led.low(at_s=0.2)

test.expect(button).high(at_tick=100)
test.expect(button).remain_high(from_ms=100, until_ms=150)

print(f"test ID: {test.test_id:032x}")
for instruction in test.instructions:
    print(instruction)
for assertion in test.assertions:
    print(assertion)

compiled = test.compile()

json_text = compiled.to_json()  # JSON string, no file created
compiled.write_json("build/my-test.json")  # machine-readable RIG input
compiled.write_excel("build/my-test.xlsx")  # human-readable review workbook
