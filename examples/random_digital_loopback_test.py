"""Deterministic three-channel digital loopback stress test."""

from random import Random

from hilrig import DigitalState, FrequencyMode, LogicVoltage, StartMode, Test

CHANNELS = (0, 1, 2)
TOTAL_TICKS = 1_000
RANDOM_SEED = 0x48494C


def build_test() -> Test:
    """Drive and verify a reproducible random three-bit pattern at 10 kHz."""
    random = Random(RANDOM_SEED)
    patterns = tuple(random.getrandbits(len(CHANNELS)) for _ in range(TOTAL_TICKS))

    test = Test(name="Three-channel random digital loopback")
    test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.IMMEDIATE,
    )

    digital_inputs = tuple(
        test.digital_input(channel=channel).configure(voltage=LogicVoltage.V3_3)
        for channel in CHANNELS
    )
    digital_outputs = tuple(
        test.digital_output(channel=channel).configure(
            voltage=LogicVoltage.V3_3,
            initial_state=_state(patterns[0], channel),
        )
        for channel in CHANNELS
    )

    # Configuration establishes the output pattern for interval tick 0. Each later
    # tick schedules all three output bits, including unchanged values, to exercise
    # one complete random pattern at every 100 us interval.
    for tick, pattern in enumerate(patterns):
        if tick > 0:
            for channel, digital_output in zip(CHANNELS, digital_outputs, strict=True):
                if _bit(pattern, channel):
                    digital_output.high(at_tick=tick)
                else:
                    digital_output.low(at_tick=tick)

        for channel, digital_input in zip(CHANNELS, digital_inputs, strict=True):
            expectation = test.expect(digital_input)
            if _bit(pattern, channel):
                expectation.high(at_tick=tick)
            else:
                expectation.low(at_tick=tick)

    return test


def _bit(pattern: int, channel: int) -> bool:
    return bool(pattern & (1 << channel))


def _state(pattern: int, channel: int) -> DigitalState:
    return DigitalState.HIGH if _bit(pattern, channel) else DigitalState.LOW
