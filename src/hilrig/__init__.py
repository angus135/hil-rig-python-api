"""Public interface for the HIL-RIG host-side API."""

from hilrig.api import (
    I2C,
    AnalogueOutput,
    DigitalInput,
    DigitalInputExpectation,
    DigitalOutput,
    PwmInput,
    PwmOutput,
    Test,
)
from hilrig.exceptions import (
    ConfigurationError,
    FrozenTestError,
    HilRigError,
    PeripheralError,
    TimingError,
    ValidationError,
)
from hilrig.models.configuration import (
    DigitalState,
    FrequencyMode,
    I2CRole,
    I2CSpeed,
    LogicVoltage,
    Pullup,
    StartMode,
)

__all__ = [
    "AnalogueOutput",
    "ConfigurationError",
    "DigitalInput",
    "DigitalInputExpectation",
    "DigitalOutput",
    "DigitalState",
    "FrequencyMode",
    "FrozenTestError",
    "HilRigError",
    "I2C",
    "I2CRole",
    "I2CSpeed",
    "LogicVoltage",
    "PeripheralError",
    "Pullup",
    "PwmInput",
    "PwmOutput",
    "StartMode",
    "Test",
    "TimingError",
    "ValidationError",
]
