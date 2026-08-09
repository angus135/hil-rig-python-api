"""User-facing objects for defining a HIL-RIG test."""

from __future__ import annotations

import math
import secrets
from collections.abc import Callable
from typing import TypeVar

from hilrig.compiler import compile_test
from hilrig.exceptions import ConfigurationError, FrozenTestError, PeripheralError, TimingError
from hilrig.models.assertions import (
    Assertion,
    AssertionList,
    DigitalInputPointAssertion,
    DigitalInputRemainHighAssertion,
    DigitalInputTransitionAssertion,
)
from hilrig.models.channels import Channel, ChannelKind
from hilrig.models.configuration import (
    Configuration,
    DigitalInputConfiguration,
    DigitalOutputConfiguration,
    DigitalState,
    FrequencyMode,
    I2CConfiguration,
    I2CRole,
    I2CSpeed,
    LogicVoltage,
    Pullup,
    PwmInputConfiguration,
    PwmOutputConfiguration,
    SPIBaud,
    SPIConfiguration,
    SPIFirst,
    SPIMode,
    SPIRole,
    SPISize,
    StartMode,
    UARTConfiguration,
    UARTLengthBits,
    UARTMode,
    UARTParity,
    UARTStopBits,
)
from hilrig.models.execution import ExecutionPlan
from hilrig.models.instructions import (
    AnalogueOutputInstruction,
    DigitalOutputAction,
    DigitalOutputInstruction,
    I2CPreloadResponseInstruction,
    I2CReadInstruction,
    I2CWriteInstruction,
    Instruction,
    InstructionList,
    PwmEnableInstruction,
    PwmSetDutyCycleInstruction,
    PwmSetFrequencyInstruction,
    PwmSetInstruction,
    SPITransferInstruction,
    UARTWriteInstruction,
)
from hilrig.timing import TimeRange, TimeValue, resolve_time_range, resolve_timestamp

InstructionType = TypeVar("InstructionType", bound=Instruction)


class _ChannelHandle:
    """Common behaviour for reusable user-facing channel handles."""

    def __init__(self, test: Test, channel: Channel) -> None:
        self._test = test
        self._identity = channel

    @property
    def channel(self) -> int:
        """Return the physical channel index."""
        return self._identity.index

    @property
    def identity(self) -> Channel:
        """Return the shared internal channel identity."""
        return self._identity


class DigitalInput(_ChannelHandle):
    """A reusable handle for one digital input channel."""

    def configure(self, *, voltage: LogicVoltage) -> DigitalInput:
        """Configure the input's logic voltage domain."""
        _require_enum(voltage, LogicVoltage, name="voltage")
        self._test._configure_channel(
            self._identity,
            DigitalInputConfiguration(voltage=voltage),
        )
        return self


class DigitalOutput(_ChannelHandle):
    """A reusable handle for one digital output channel."""

    def configure(
        self,
        *,
        voltage: LogicVoltage,
        initial_state: DigitalState,
    ) -> DigitalOutput:
        """Configure the output's voltage domain and initial state."""
        _require_enum(voltage, LogicVoltage, name="voltage")
        _require_enum(initial_state, DigitalState, name="initial_state")
        self._test._configure_channel(
            self._identity,
            DigitalOutputConfiguration(voltage=voltage, initial_state=initial_state),
        )
        return self

    def high(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> DigitalOutput:
        """Schedule this output high."""
        return self._set(DigitalOutputAction.HIGH, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def low(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> DigitalOutput:
        """Schedule this output low."""
        return self._set(DigitalOutputAction.LOW, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def toggle(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> DigitalOutput:
        """Schedule this output to toggle."""
        return self._set(DigitalOutputAction.TOGGLE, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def _set(
        self,
        action: DigitalOutputAction,
        *,
        at_tick: int | None,
        at_ms: TimeValue | None,
        at_s: TimeValue | None,
    ) -> DigitalOutput:
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: DigitalOutputInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                action=action,
            )
        )
        return self


class PwmInput(_ChannelHandle):
    """A reusable handle for one PWM capture channel."""

    def configure(self, *, voltage: LogicVoltage) -> PwmInput:
        """Configure the capture input's logic voltage domain."""
        _require_enum(voltage, LogicVoltage, name="voltage")
        self._test._configure_channel(
            self._identity,
            PwmInputConfiguration(voltage=voltage),
        )
        return self


class PwmOutput(_ChannelHandle):
    """A reusable handle for one PWM output channel."""

    def configure(
        self,
        *,
        voltage: LogicVoltage,
        initial_frequency_hz: int | float,
        initial_duty_cycle: int | float,
        initially_enabled: bool,
    ) -> PwmOutput:
        """Configure the PWM output's static and initial state."""
        _require_enum(voltage, LogicVoltage, name="voltage")
        if not isinstance(initially_enabled, bool):
            raise TypeError("initially_enabled must be a bool")
        _validate_pwm_voltage(self.channel, voltage)
        frequency = _positive_number(initial_frequency_hz, name="initial_frequency_hz")
        duty_cycle = _duty_cycle(initial_duty_cycle, name="initial_duty_cycle")
        self._test._configure_channel(
            self._identity,
            PwmOutputConfiguration(
                voltage=voltage,
                initial_frequency_hz=frequency,
                initial_duty_cycle=duty_cycle,
                initially_enabled=initially_enabled,
            ),
        )
        return self

    def enable(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> PwmOutput:
        """Schedule this PWM output to be enabled."""
        return self._set_enabled(True, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def disable(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> PwmOutput:
        """Schedule this PWM output to be disabled."""
        return self._set_enabled(False, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def set(
        self,
        *,
        frequency_hz: int | float,
        duty_cycle: int | float,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> PwmOutput:
        """Atomically schedule both PWM frequency and duty cycle."""
        frequency = _positive_number(frequency_hz, name="frequency_hz")
        duty = _duty_cycle(duty_cycle, name="duty_cycle")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: PwmSetInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                frequency_hz=frequency,
                duty_cycle=duty,
            )
        )
        return self

    def set_frequency(
        self,
        *,
        frequency_hz: int | float,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> PwmOutput:
        """Schedule a PWM frequency change without changing duty cycle."""
        frequency = _positive_number(frequency_hz, name="frequency_hz")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: PwmSetFrequencyInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                frequency_hz=frequency,
            )
        )
        return self

    def set_duty_cycle(
        self,
        *,
        duty_cycle: int | float,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> PwmOutput:
        """Schedule a PWM duty-cycle change without changing frequency."""
        duty = _duty_cycle(duty_cycle, name="duty_cycle")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: PwmSetDutyCycleInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                duty_cycle=duty,
            )
        )
        return self

    def _set_enabled(
        self,
        enabled: bool,
        *,
        at_tick: int | None,
        at_ms: TimeValue | None,
        at_s: TimeValue | None,
    ) -> PwmOutput:
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: PwmEnableInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                enabled=enabled,
            )
        )
        return self


class AnalogueOutput(_ChannelHandle):
    """A reusable handle for one analogue output channel."""

    def set_voltage(
        self,
        voltage: int | float,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> AnalogueOutput:
        """Schedule an analogue output voltage."""
        requested_voltage = _non_negative_number(voltage, name="voltage")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: AnalogueOutputInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                voltage=requested_voltage,
            )
        )
        return self


class I2C(_ChannelHandle):
    """A reusable handle for one I2C channel."""

    def configure(
        self,
        *,
        role: I2CRole,
        speed: I2CSpeed,
        logic_voltage: LogicVoltage,
        pullup: Pullup,
        own_address: int | None = None,
    ) -> I2C:
        """Configure this I2C channel as a master or slave."""
        _require_enum(role, I2CRole, name="role")
        _require_enum(speed, I2CSpeed, name="speed")
        _require_enum(logic_voltage, LogicVoltage, name="logic_voltage")
        _require_enum(pullup, Pullup, name="pullup")
        if logic_voltage not in (LogicVoltage.V3_3, LogicVoltage.V5):
            raise ConfigurationError("I2C supports only 3.3 V and 5 V logic")
        if role is I2CRole.MASTER and own_address is not None:
            raise ConfigurationError("A master I2C channel must not define own_address")
        if role is I2CRole.SLAVE:
            _i2c_address(own_address, name="own_address")
        self._test._configure_channel(
            self._identity,
            I2CConfiguration(
                role=role,
                speed=speed,
                logic_voltage=logic_voltage,
                pullup=pullup,
                own_address=own_address,
            ),
        )
        return self

    def write(
        self,
        *,
        address: int,
        data: bytes,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> I2C:
        """Schedule an I2C master write."""
        self._require_role(I2CRole.MASTER)
        target_address = _i2c_address(address, name="address")
        payload = _bytes(data)
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: I2CWriteInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                address=target_address,
                data=payload,
            )
        )
        return self

    def read(
        self,
        *,
        address: int,
        length: int,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> I2C:
        """Schedule an I2C master read."""
        self._require_role(I2CRole.MASTER)
        target_address = _i2c_address(address, name="address")
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise ValueError("length must be a positive integer")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: I2CReadInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                address=target_address,
                length=length,
            )
        )
        return self

    def preload_response(
        self,
        *,
        data: bytes,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> I2C:
        """Schedule response data for an I2C slave read request."""
        self._require_role(I2CRole.SLAVE)
        payload = _bytes(data)
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: I2CPreloadResponseInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                data=payload,
            )
        )
        return self

    def _require_role(self, required_role: I2CRole) -> None:
        configuration = self._test.configuration.for_channel(self._identity)
        if not isinstance(configuration, I2CConfiguration):
            raise ConfigurationError(
                f"I2C channel {self.channel} must be configured before adding stimuli"
            )
        if configuration.role is not required_role:
            raise PeripheralError(
                f"I2C channel {self.channel} is configured as {configuration.role.value}, "
                f"not {required_role.value}"
            )


class SPI(_ChannelHandle):
    """A reusable handle for one SPI channel."""

    def configure(
        self,
        *,
        role: SPIRole,
        baud: SPIBaud,
        data_size: SPISize,
        mode: SPIMode,
        first_bit: SPIFirst,
    ) -> SPI:
        """Configure this SPI channel."""
        _require_enum(role, SPIRole, name="role")
        _require_enum(baud, SPIBaud, name="baud")
        _require_enum(data_size, SPISize, name="data_size")
        _require_enum(mode, SPIMode, name="mode")
        _require_enum(first_bit, SPIFirst, name="first_bit")
        self._test._configure_channel(
            self._identity,
            SPIConfiguration(
                role=role,
                baud=baud,
                data_size=data_size,
                mode=mode,
                first_bit=first_bit,
            ),
        )
        return self

    def transfer(
        self,
        *,
        tx_data: bytes,
        rx_length: int,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> SPI:
        """Schedule one master-mode SPI transfer."""
        configuration = self._configuration_for_transfer()
        payload = _bytes(tx_data)
        received_bytes = _non_negative_integer(rx_length, name="rx_length")
        if not payload and received_bytes == 0:
            raise ValueError("An SPI transfer must transmit or receive at least one byte")
        if configuration.data_size is SPISize.SIZE_16BIT:
            if len(payload) % 2 != 0:
                raise ValueError("tx_data length must be even for 16-bit SPI frames")
            if received_bytes % 2 != 0:
                raise ValueError("rx_length must be even for 16-bit SPI frames")
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._schedule(
            lambda instruction_id: SPITransferInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                tx_data=payload,
                rx_length=received_bytes,
            )
        )
        return self

    def _configuration_for_transfer(self) -> SPIConfiguration:
        configuration = self._test.configuration.for_channel(self._identity)
        if not isinstance(configuration, SPIConfiguration):
            raise ConfigurationError(
                f"SPI channel {self.channel} must be configured before adding stimuli"
            )
        if configuration.role is not SPIRole.MASTER:
            raise PeripheralError(
                f"SPI channel {self.channel} is configured as slave; transfer() generates "
                "clocks and is only defined for a master channel"
            )
        return configuration


class UART(_ChannelHandle):
    """A reusable handle for one UART channel."""

    def configure(
        self,
        *,
        mode: UARTMode,
        baud_hz: int,
        parity: UARTParity,
        length: UARTLengthBits,
        stop: UARTStopBits,
    ) -> UART:
        """Configure this UART channel."""
        _require_enum(mode, UARTMode, name="mode")
        _require_enum(parity, UARTParity, name="parity")
        _require_enum(length, UARTLengthBits, name="length")
        _require_enum(stop, UARTStopBits, name="stop")
        baud = _positive_integer(baud_hz, name="baud_hz")
        if baud > 921_600:
            raise ValueError("baud_hz must not exceed 921600")
        self._test._configure_channel(
            self._identity,
            UARTConfiguration(
                mode=mode,
                baud_hz=baud,
                parity=parity,
                length=length,
                stop=stop,
            ),
        )
        return self

    def write(
        self,
        *,
        data: bytes,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> UART:
        """Schedule raw bytes for transmission on this UART channel."""
        payload = _bytes(data)
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        return self._write_bytes(payload, timestamp=timestamp)

    def write_text(
        self,
        *,
        data: str,
        encoding: str,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> UART:
        """Encode text immediately and store a raw UART byte instruction."""
        if not isinstance(data, str):
            raise TypeError("data must be a string")
        if not isinstance(encoding, str) or not encoding:
            raise TypeError("encoding must be a non-empty string")
        try:
            payload = data.encode(encoding)
        except LookupError as error:
            raise ValueError(f"Unknown text encoding: {encoding}") from error
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        return self._write_bytes(payload, timestamp=timestamp)

    def _write_bytes(self, data: bytes, *, timestamp: int) -> UART:
        self._test._schedule(
            lambda instruction_id: UARTWriteInstruction(
                instruction_id=instruction_id,
                timestamp=timestamp,
                channel=self._identity,
                data=data,
            )
        )
        return self


class DigitalInputExpectation:
    """Builder for assertions over one digital input's returned time series."""

    def __init__(self, test: Test, digital_input: DigitalInput) -> None:
        self._test = test
        self._input = digital_input

    def high(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> DigitalInputExpectation:
        """Expect the input to be high at one time."""
        return self._point(DigitalState.HIGH, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def low(
        self,
        *,
        at_tick: int | None = None,
        at_ms: TimeValue | None = None,
        at_s: TimeValue | None = None,
    ) -> DigitalInputExpectation:
        """Expect the input to be low at one time."""
        return self._point(DigitalState.LOW, at_tick=at_tick, at_ms=at_ms, at_s=at_s)

    def remain_high(
        self,
        *,
        from_tick: int | None = None,
        until_tick: int | None = None,
        from_ms: TimeValue | None = None,
        until_ms: TimeValue | None = None,
        from_s: TimeValue | None = None,
        until_s: TimeValue | None = None,
    ) -> DigitalInputExpectation:
        """Expect the input to stay high throughout one inclusive time range."""
        start, end = self._test._time_range(
            ticks=_optional_pair(from_tick, until_tick, names="from_tick and until_tick"),
            milliseconds=_optional_pair(from_ms, until_ms, names="from_ms and until_ms"),
            seconds=_optional_pair(from_s, until_s, names="from_s and until_s"),
        )
        self._test._add_assertion(
            DigitalInputRemainHighAssertion(
                channel=self._input.identity,
                from_tick=start,
                until_tick=end,
            )
        )
        return self

    def to_transition(
        self,
        *,
        from_state: bool,
        to_state: bool,
        between_ticks: TimeRange | None = None,
        between_ms: TimeRange | None = None,
        between_s: TimeRange | None = None,
    ) -> DigitalInputExpectation:
        """Expect a state transition within one inclusive time range."""
        if not isinstance(from_state, bool) or not isinstance(to_state, bool):
            raise TypeError("from_state and to_state must be bool values")
        if from_state is to_state:
            raise ValueError("A transition must change state")
        start, end = self._test._time_range(
            ticks=between_ticks,
            milliseconds=between_ms,
            seconds=between_s,
        )
        self._test._add_assertion(
            DigitalInputTransitionAssertion(
                channel=self._input.identity,
                from_state=DigitalState(from_state),
                to_state=DigitalState(to_state),
                from_tick=start,
                until_tick=end,
            )
        )
        return self

    def _point(
        self,
        state: DigitalState,
        *,
        at_tick: int | None,
        at_ms: TimeValue | None,
        at_s: TimeValue | None,
    ) -> DigitalInputExpectation:
        timestamp = self._test._timestamp(at_tick=at_tick, at_ms=at_ms, at_s=at_s)
        self._test._add_assertion(
            DigitalInputPointAssertion(
                channel=self._input.identity,
                timestamp=timestamp,
                expected_state=state,
            )
        )
        return self


class Test:
    """Root object containing one complete HIL-RIG test definition."""

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A test name must be a non-empty string")

        self._test_id = secrets.randbits(128)
        self._name = name
        self._configuration = Configuration()
        self._test_configuration_set = False
        self._instructions = InstructionList()
        self._assertions = AssertionList()
        self._next_instruction_id = 0
        self._handles: dict[tuple[ChannelKind, int], _ChannelHandle] = {}
        self._compiled_plan: ExecutionPlan | None = None

    @property
    def test_id(self) -> int:
        """Return this test's randomly generated 128-bit identifier."""
        return self._test_id

    @property
    def name(self) -> str:
        """Return the human-readable test name."""
        return self._name

    @property
    def configuration(self) -> Configuration:
        """Return this test's static configuration model."""
        return self._configuration

    @property
    def instructions(self) -> InstructionList:
        """Return the stimulus instruction collection."""
        return self._instructions

    @property
    def assertions(self) -> AssertionList:
        """Return the host-side assertion collection."""
        return self._assertions

    @property
    def is_compiled(self) -> bool:
        """Return whether the preliminary compiler has completed successfully."""
        return self._compiled_plan is not None

    def configure(
        self,
        *,
        frequency_mode: FrequencyMode,
        start_mode: StartMode,
    ) -> Test:
        """Configure test-level timing and start behaviour."""
        self._ensure_mutable()
        if self._test_configuration_set:
            raise ConfigurationError("The Test object is already configured")
        if self._instructions or self._assertions:
            raise ConfigurationError(
                "Test timing configuration must be set before instructions or assertions"
            )
        _require_enum(frequency_mode, FrequencyMode, name="frequency_mode")
        _require_enum(start_mode, StartMode, name="start_mode")
        self._configuration._configure_test(
            frequency_mode=frequency_mode,
            start_mode=start_mode,
        )
        self._test_configuration_set = True
        return self

    def digital_input(self, *, channel: int) -> DigitalInput:
        """Return a stable digital input handle."""
        return self._handle(ChannelKind.DIGITAL_INPUT, channel, DigitalInput)

    def digital_output(self, *, channel: int) -> DigitalOutput:
        """Return a stable digital output handle."""
        return self._handle(ChannelKind.DIGITAL_OUTPUT, channel, DigitalOutput)

    def pwm_input(self, *, channel: int) -> PwmInput:
        """Return a stable PWM capture handle."""
        return self._handle(ChannelKind.PWM_INPUT, channel, PwmInput)

    def pwm_output(self, *, channel: int) -> PwmOutput:
        """Return one of the two PWM output handles."""
        if channel not in (0, 1):
            raise ValueError("PWM output channel must be 0 (LV) or 1 (HV)")
        return self._handle(ChannelKind.PWM_OUTPUT, channel, PwmOutput)

    def analogue_out(self, *, channel: int) -> AnalogueOutput:
        """Return a stable analogue output handle."""
        return self._handle(ChannelKind.ANALOGUE_OUTPUT, channel, AnalogueOutput)

    def i2c(self, *, channel: int) -> I2C:
        """Return a stable I2C channel handle."""
        return self._handle(ChannelKind.I2C, channel, I2C)

    def spi(self, *, channel: int) -> SPI:
        """Return a stable SPI channel handle."""
        return self._handle(ChannelKind.SPI, channel, SPI)

    def uart(self, *, channel: int) -> UART:
        """Return a stable UART channel handle."""
        return self._handle(ChannelKind.UART, channel, UART)

    def expect(self, channel: DigitalInput) -> DigitalInputExpectation:
        """Begin a host-side assertion for a digital input channel."""
        self._ensure_mutable()
        if not isinstance(channel, DigitalInput) or channel._test is not self:
            raise TypeError("expect() currently supports a digital input from this Test")
        return DigitalInputExpectation(self, channel)

    def compile(self) -> ExecutionPlan:
        """Run the existing preliminary ordering compiler and freeze the model."""
        if self._compiled_plan is None:
            self._compiled_plan = compile_test(
                test_id=self._test_id,
                name=self._name,
                configuration=self._configuration,
                instructions=self._instructions,
            )
        return self._compiled_plan

    def _handle(
        self,
        kind: ChannelKind,
        channel: int,
        handle_type: type[_ChannelHandle],
    ):
        _channel_index(channel)
        key = (kind, channel)
        if key not in self._handles:
            self._ensure_mutable()
            self._handles[key] = handle_type(self, Channel(kind=kind, index=channel))
        return self._handles[key]

    def _configure_channel(self, channel: Channel, configuration) -> None:
        self._ensure_mutable()
        self._configuration._configure_channel(channel, configuration)

    def _timestamp(
        self,
        *,
        at_tick: int | None,
        at_ms: TimeValue | None,
        at_s: TimeValue | None,
    ) -> int:
        self._ensure_mutable()
        return resolve_timestamp(
            self._configuration.frequency_mode,
            at_tick=at_tick,
            at_ms=at_ms,
            at_s=at_s,
        )

    def _time_range(
        self,
        *,
        ticks: TimeRange | None,
        milliseconds: TimeRange | None,
        seconds: TimeRange | None,
    ) -> tuple[int, int]:
        self._ensure_mutable()
        return resolve_time_range(
            self._configuration.frequency_mode,
            ticks=ticks,
            milliseconds=milliseconds,
            seconds=seconds,
        )

    def _schedule(self, factory: Callable[[int], InstructionType]) -> InstructionType:
        self._ensure_mutable()
        instruction = factory(self._next_instruction_id)
        self._instructions._append(instruction)
        self._next_instruction_id += 1
        return instruction

    def _add_assertion(self, assertion: Assertion) -> None:
        self._ensure_mutable()
        self._assertions._append(assertion)

    def _ensure_mutable(self) -> None:
        if self.is_compiled:
            raise FrozenTestError("A successfully compiled test cannot be changed")


def _require_enum(value: object, enum_type: type[object], *, name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be a {enum_type.__name__}")


def _channel_index(channel: int) -> int:
    if not isinstance(channel, int) or isinstance(channel, bool):
        raise TypeError("channel must be an integer")
    if channel < 0:
        raise ValueError("channel must be non-negative")
    return channel


def _number(value: int | float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _non_negative_number(value: int | float, *, name: str) -> float:
    converted = _number(value, name=name)
    if converted < 0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _positive_number(value: int | float, *, name: str) -> float:
    converted = _number(value, name=name)
    if converted <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return converted


def _duty_cycle(value: int | float, *, name: str) -> float:
    converted = _number(value, name=name)
    if not 0.0 <= converted <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return converted


def _validate_pwm_voltage(channel: int, voltage: LogicVoltage) -> None:
    allowed = {
        0: (LogicVoltage.V3_3, LogicVoltage.V5),
        1: (LogicVoltage.V12, LogicVoltage.V24),
    }
    if voltage not in allowed[channel]:
        domain = "3.3 V or 5 V" if channel == 0 else "12 V or 24 V"
        raise ConfigurationError(f"PWM output channel {channel} supports only {domain}")


def _i2c_address(address: int | None, *, name: str) -> int:
    if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 0x7F:
        raise ValueError(f"{name} must be a 7-bit I2C address (0x00 to 0x7F)")
    return address


def _non_negative_integer(value: int, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_integer(value: int, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bytes(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    return data


def _optional_pair(
    start: TimeValue | None,
    end: TimeValue | None,
    *,
    names: str,
) -> TimeRange | None:
    if (start is None) is not (end is None):
        raise TimingError(f"Specify both {names}")
    if start is None:
        return None
    return (start, end)
