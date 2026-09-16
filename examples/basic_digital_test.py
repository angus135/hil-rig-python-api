"""Basic definition usable by both the terminal and direct Python execution."""

from hilrig import DigitalState, FrequencyMode, LogicVoltage, StartMode, Test


def build_test() -> Test:
    """Return a fresh digital input/output test for one terminal run."""
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
    return test


def main() -> None:
    """Retain the original standalone compile-and-export demonstration."""
    test = build_test()
    print(f"test ID: {test.test_id:032x}")
    for instruction in test.instructions:
        print(instruction)
    for assertion in test.assertions:
        print(assertion)

    compiled = test.compile()
    compiled.write_json("build/my-test.json")
    compiled.write_excel("build/my-test.xlsx")


if __name__ == "__main__":
    main()
