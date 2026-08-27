import pytest

from hilrig import I2CRole, I2CSpeed, LogicVoltage, Pullup
from hilrig import Test as HilRigTest
from hilrig.exceptions import ConfigurationError, PeripheralError
from hilrig.models.instructions import (
    AnalogueOutputInstruction,
    DigitalOutputAction,
    DigitalOutputInstruction,
    I2CPreloadResponseInstruction,
    I2CReadInstruction,
    I2CWriteInstruction,
    PwmEnableInstruction,
    PwmSetDutyCycleInstruction,
    PwmSetFrequencyInstruction,
    PwmSetInstruction,
)


def test_instruction_ids_increment_across_peripheral_types() -> None:
    test = HilRigTest(name="Instruction IDs")

    test.digital_output(channel=0).high(at_tick=0)
    test.pwm_output(channel=0).enable(at_tick=1)
    analogue_output = test.analogue_output(channel=0)
    analogue_output.configure()
    analogue_output.set_voltage(3.2, at_tick=2)

    assert [instruction.instruction_id for instruction in test.instructions] == [0, 1, 2]


def test_digital_output_stimuli_create_expected_actions() -> None:
    test = HilRigTest(name="Digital actions")
    output = test.digital_output(channel=0)

    output.high(at_tick=1).low(at_ms=2).toggle(at_s=0.003)

    instructions = tuple(test.instructions)
    assert all(isinstance(item, DigitalOutputInstruction) for item in instructions)
    assert [item.action for item in instructions] == [
        DigitalOutputAction.HIGH,
        DigitalOutputAction.LOW,
        DigitalOutputAction.TOGGLE,
    ]
    assert [item.timestamp for item in instructions] == [1, 2, 3]


def test_pwm_stimuli_preserve_atomic_and_individual_updates() -> None:
    test = HilRigTest(name="PWM actions")
    pwm = test.pwm_output(channel=1)

    pwm.enable(at_tick=10)
    pwm.disable(at_tick=20)
    pwm.set(frequency_hz=2_000, duty_cycle=0.5, at_tick=30)
    pwm.set_frequency(frequency_hz=5_000, at_tick=40)
    pwm.set_duty_cycle(duty_cycle=0.75, at_tick=50)

    instructions = tuple(test.instructions)
    assert isinstance(instructions[0], PwmEnableInstruction)
    assert instructions[0].enabled is True
    assert isinstance(instructions[1], PwmEnableInstruction)
    assert instructions[1].enabled is False
    assert isinstance(instructions[2], PwmSetInstruction)
    assert instructions[2].frequency_hz == 2_000
    assert instructions[2].duty_cycle == 0.5
    assert isinstance(instructions[3], PwmSetFrequencyInstruction)
    assert isinstance(instructions[4], PwmSetDutyCycleInstruction)


def test_analogue_output_stimulus_stores_voltage() -> None:
    test = HilRigTest(name="Analogue action")
    analogue_output = test.analogue_output(channel=1)
    analogue_output.configure()

    analogue_output.set_voltage(18.4, at_tick=250)

    instruction = tuple(test.instructions)[0]
    assert isinstance(instruction, AnalogueOutputInstruction)
    assert instruction.voltage == 18.4


def test_analogue_output_stimulus_requires_configuration() -> None:
    test = HilRigTest(name="Unconfigured analogue output")

    with pytest.raises(ConfigurationError, match="must be configured"):
        test.analogue_output(channel=0).set_voltage(3.3, at_tick=0)


def test_i2c_master_write_and_read_are_stored() -> None:
    test = HilRigTest(name="I2C master actions")
    i2c = test.i2c(channel=0)
    i2c.configure(
        role=I2CRole.MASTER,
        speed=I2CSpeed.FAST_400KHZ,
        logic_voltage=LogicVoltage.V3_3,
        pullup=Pullup.OHM_2K2,
    )

    i2c.write(address=0x68, data=b"\x10\x01", at_tick=100)
    i2c.read(address=0x68, length=6, at_tick=110)

    write, read = tuple(test.instructions)
    assert isinstance(write, I2CWriteInstruction)
    assert write.address == 0x68
    assert write.data == b"\x10\x01"
    assert isinstance(read, I2CReadInstruction)
    assert read.length == 6


def test_i2c_slave_preload_response_is_stored() -> None:
    test = HilRigTest(name="I2C slave action")
    i2c = test.i2c(channel=1)
    i2c.configure(
        role=I2CRole.SLAVE,
        own_address=0x42,
        speed=I2CSpeed.STANDARD_100KHZ,
        logic_voltage=LogicVoltage.V5,
        pullup=Pullup.OHM_4K7,
    )

    i2c.preload_response(data=b"\x10\x20\x30", at_tick=90)

    instruction = tuple(test.instructions)[0]
    assert isinstance(instruction, I2CPreloadResponseInstruction)
    assert instruction.data == b"\x10\x20\x30"


def test_i2c_stimulus_requires_configuration() -> None:
    test = HilRigTest(name="Unconfigured I2C")

    with pytest.raises(ConfigurationError, match="must be configured"):
        test.i2c(channel=0).write(address=0x42, data=b"\x00", at_tick=0)


def test_i2c_operation_must_match_configured_role() -> None:
    test = HilRigTest(name="Wrong I2C role")
    i2c = test.i2c(channel=0)
    i2c.configure(
        role=I2CRole.MASTER,
        speed=I2CSpeed.STANDARD_100KHZ,
        logic_voltage=LogicVoltage.V3_3,
        pullup=Pullup.DISABLED,
    )

    with pytest.raises(PeripheralError, match="not slave"):
        i2c.preload_response(data=b"\x00", at_tick=0)
