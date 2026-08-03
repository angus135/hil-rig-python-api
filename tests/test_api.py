from unittest.mock import patch

import pytest

from hilrig import (
    ConfigurationError,
    DigitalState,
    FrequencyMode,
    I2CRole,
    I2CSpeed,
    LogicVoltage,
    Pullup,
    StartMode,
)
from hilrig import (
    Test as HilRigTest,
)
from hilrig.models.configuration import (
    DigitalInputConfiguration,
    DigitalOutputConfiguration,
    I2CConfiguration,
    PwmInputConfiguration,
    PwmOutputConfiguration,
)


def test_test_receives_a_random_128_bit_integer_id() -> None:
    expected_id = 0x123456789ABCDEF00112233445566778

    with patch("hilrig.api.secrets.randbits", return_value=expected_id) as randbits:
        test = HilRigTest(name="Identified test")

    randbits.assert_called_once_with(128)
    assert test.test_id == expected_id


def test_test_configuration_stores_frequency_and_start_modes() -> None:
    test = HilRigTest(name="Configured test")

    returned_test = test.configure(
        frequency_mode=FrequencyMode.HZ_10K,
        start_mode=StartMode.HOST_COMMAND,
    )

    assert returned_test is test
    assert test.configuration.frequency_mode is FrequencyMode.HZ_10K
    assert test.configuration.start_mode is StartMode.HOST_COMMAND


def test_test_can_only_be_configured_once() -> None:
    test = HilRigTest(name="Configured once")
    test.configure(
        frequency_mode=FrequencyMode.HZ_1K,
        start_mode=StartMode.IMMEDIATE,
    )

    with pytest.raises(ConfigurationError, match="already configured"):
        test.configure(
            frequency_mode=FrequencyMode.HZ_100,
            start_mode=StartMode.IMMEDIATE,
        )


def test_channel_argument_is_explicitly_keyword_only() -> None:
    test = HilRigTest(name="Keyword channels")

    with pytest.raises(TypeError):
        test.digital_output(0)  # type: ignore[misc]

    assert test.digital_output(channel=0).channel == 0


def test_handles_are_reused_and_share_channel_identity() -> None:
    test = HilRigTest(name="Stable handles")

    first = test.digital_input(channel=2)
    second = test.digital_input(channel=2)

    assert first is second
    assert first.identity is second.identity


def test_digital_input_configuration_contains_only_voltage() -> None:
    test = HilRigTest(name="Digital input")
    digital_input = test.digital_input(channel=0)

    digital_input.configure(voltage=LogicVoltage.V12)

    configuration = test.configuration.for_channel(digital_input.identity)
    assert configuration == DigitalInputConfiguration(voltage=LogicVoltage.V12)
    assert not hasattr(configuration, "recording_enabled")


def test_digital_output_configuration_contains_voltage_and_initial_state() -> None:
    test = HilRigTest(name="Digital output")
    output = test.digital_output(channel=1)

    output.configure(voltage=LogicVoltage.V24, initial_state=DigitalState.LOW)

    assert test.configuration.for_channel(output.identity) == DigitalOutputConfiguration(
        voltage=LogicVoltage.V24,
        initial_state=DigitalState.LOW,
    )


def test_pwm_input_configuration_contains_voltage() -> None:
    test = HilRigTest(name="PWM input")
    pwm_input = test.pwm_input(channel=0)

    pwm_input.configure(voltage=LogicVoltage.V5)

    assert test.configuration.for_channel(pwm_input.identity) == PwmInputConfiguration(
        voltage=LogicVoltage.V5
    )


@pytest.mark.parametrize(
    ("channel", "voltage"),
    [
        (0, LogicVoltage.V3_3),
        (0, LogicVoltage.V5),
        (1, LogicVoltage.V12),
        (1, LogicVoltage.V24),
    ],
)
def test_pwm_output_configuration_accepts_each_channel_voltage_domain(
    channel: int,
    voltage: LogicVoltage,
) -> None:
    test = HilRigTest(name="PWM output")
    output = test.pwm_output(channel=channel)

    output.configure(
        voltage=voltage,
        initial_frequency_hz=1_000,
        initial_duty_cycle=0.25,
        initially_enabled=False,
    )

    assert test.configuration.for_channel(output.identity) == PwmOutputConfiguration(
        voltage=voltage,
        initial_frequency_hz=1_000.0,
        initial_duty_cycle=0.25,
        initially_enabled=False,
    )


@pytest.mark.parametrize(
    ("channel", "voltage"),
    [(0, LogicVoltage.V12), (1, LogicVoltage.V5)],
)
def test_pwm_output_rejects_wrong_channel_voltage_domain(
    channel: int,
    voltage: LogicVoltage,
) -> None:
    test = HilRigTest(name="Invalid PWM")

    with pytest.raises(ConfigurationError, match="supports only"):
        test.pwm_output(channel=channel).configure(
            voltage=voltage,
            initial_frequency_hz=1_000,
            initial_duty_cycle=0.5,
            initially_enabled=False,
        )


def test_i2c_master_configuration_is_stored() -> None:
    test = HilRigTest(name="I2C master")
    i2c = test.i2c(channel=0)

    i2c.configure(
        role=I2CRole.MASTER,
        speed=I2CSpeed.FAST_400KHZ,
        logic_voltage=LogicVoltage.V3_3,
        pullup=Pullup.OHM_4K7,
    )

    assert test.configuration.for_channel(i2c.identity) == I2CConfiguration(
        role=I2CRole.MASTER,
        speed=I2CSpeed.FAST_400KHZ,
        logic_voltage=LogicVoltage.V3_3,
        pullup=Pullup.OHM_4K7,
    )


def test_i2c_slave_requires_own_address() -> None:
    test = HilRigTest(name="I2C slave")

    with pytest.raises(ValueError, match="7-bit"):
        test.i2c(channel=1).configure(
            role=I2CRole.SLAVE,
            speed=I2CSpeed.STANDARD_100KHZ,
            logic_voltage=LogicVoltage.V5,
            pullup=Pullup.DISABLED,
        )


def test_old_digital_out_name_and_recording_commands_are_not_exposed() -> None:
    test = HilRigTest(name="Removed API")
    digital_input = test.digital_input(channel=0)

    assert not hasattr(test, "digital_out")
    assert not hasattr(digital_input, "record")
    assert not hasattr(digital_input, "enable_recording")


@pytest.mark.parametrize("name", ["", "   ", None])
def test_test_requires_a_name(name: str | None) -> None:
    with pytest.raises(ValueError):
        HilRigTest(name)  # type: ignore[arg-type]
