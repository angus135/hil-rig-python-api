"""Profile the production ISR while scheduling SPI1 transmissions.

Run through the hil-rig terminal. Probe Blue 2 / PE2 relative to board ground.
No loopback wiring or receive assertions are required. Connected DUTs must be
appropriate for SPI1 mode-0 traffic.
"""

from hilrig import (
    FrequencyMode,
    SPIBaud,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    StartMode,
    Test,
)

FREQUENCY_MODE = FrequencyMode.HZ_10K
EXECUTION_TICKS = 20_000
PAYLOAD = bytes(range(16))


def build_test() -> Test:
    """Send 16 bytes on SPI1 every tick, followed by one second of drain.

    Keep the requested duration through the compiler's normal one-second tail,
    rather than overriding its inferred execution length. The 16-byte SPI payload occupies approximately 45.5 us at 2.813 MHz, within a 100 us tick. Transfers are queued without waiting for wire completion.
    """
    frequency_hz = FREQUENCY_MODE.hertz
    if EXECUTION_TICKS <= frequency_hz:
        raise ValueError("Execution length must exceed the one-second drain tail")
    duration_s = EXECUTION_TICKS / frequency_hz
    test = Test(name=f"SPI1 ISR timing: {frequency_hz} Hz, {duration_s:g} seconds")
    test.configure(frequency_mode=FREQUENCY_MODE, start_mode=StartMode.IMMEDIATE)
    spi = test.spi(channel=0).named("SPI1").configure(
        role=SPIRole.MASTER,
        baud=SPIBaud.BAUD_2M813BIT,
        data_size=SPISize.SIZE_8BIT,
        mode=SPIMode.MODE_0,
        first_bit=SPIFirst.MSB,
    )
    for tick in range(EXECUTION_TICKS - frequency_hz):
        spi.transfer(tx_data=PAYLOAD, rx_length=0, at_tick=tick)
    return test


if __name__ == "__main__":
    compiled = build_test().compile()
    print(f"Frequency: {compiled.frequency_hz} Hz")
    print(f"Execution length: {compiled.expected_tick_count} ticks "
          f"({compiled.expected_tick_count / compiled.frequency_hz:g} seconds nominal)")
    print(f"Configured channels: {len(compiled.configurations)}")
    print(f"Output instructions: {len(compiled.instructions)}")
    print("Final second: no new transfers; measure active-workload pulses separately.")
    print("No receive assertions; judge timing from the scope and check run completion.")


