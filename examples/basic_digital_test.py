from hilrig import FrequencyMode, Test

test = Test("Digital output example")
test.configure(mode=FrequencyMode.KHZ_1)

led = test.digital_out(0)
led.high(at=200)
led.low(at=100)

plan = test.compile()

for time_slot in plan.time_slots:
    print(f"tick {time_slot.timestamp}: {time_slot.instructions}")
