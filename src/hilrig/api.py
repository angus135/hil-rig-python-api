"""User-facing objects for defining a HIL-RIG test."""

from __future__ import annotations

from dataclasses import replace

from hilrig.compiler import compile_test
from hilrig.exceptions import FrozenTestError
from hilrig.models.channels import Channel, ChannelKind
from hilrig.models.configuration import Configuration, FrequencyMode
from hilrig.models.execution import ExecutionPlan
from hilrig.models.instructions import (
    DigitalLevel,
    DigitalOutputInstruction,
    Instruction,
    InstructionList,
)


class DigitalOutput:
    """A reusable handle for one digital output channel."""

    def __init__(self, test: Test, channel: Channel) -> None:
        self._test = test
        self._channel = channel

    @property
    def channel(self) -> int:
        """Return the physical channel index."""
        return self._channel.index

    def high(self, *, at: int) -> DigitalOutput:
        """Schedule this output high at a test timestamp."""
        return self._set(DigitalLevel.HIGH, at=at)

    def low(self, *, at: int) -> DigitalOutput:
        """Schedule this output low at a test timestamp."""
        return self._set(DigitalLevel.LOW, at=at)

    def _set(self, level: DigitalLevel, *, at: int) -> DigitalOutput:
        self._test._add_instruction(
            DigitalOutputInstruction(timestamp=at, channel=self._channel, level=level)
        )
        return self


class Test:
    """Root object containing one complete HIL-RIG test definition."""

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A test name must be a non-empty string")

        self._name = name
        self._configuration = Configuration()
        self._instructions = InstructionList()
        self._digital_outputs: dict[int, DigitalOutput] = {}
        self._compiled_plan: ExecutionPlan | None = None

    @property
    def name(self) -> str:
        """Return the human-readable test name."""
        return self._name

    @property
    def configuration(self) -> Configuration:
        """Return the current immutable configuration value."""
        return self._configuration

    @property
    def instructions(self) -> InstructionList:
        """Return a read-only view-like wrapper around scheduled instructions."""
        return self._instructions

    @property
    def is_compiled(self) -> bool:
        """Return whether compilation has completed successfully."""
        return self._compiled_plan is not None

    def configure(self, *, mode: FrequencyMode) -> Test:
        """Set static rig configuration used by this initial API slice."""
        self._ensure_mutable()
        if not isinstance(mode, FrequencyMode):
            raise TypeError("mode must be a FrequencyMode")
        self._configuration = replace(self._configuration, frequency_mode=mode)
        return self

    def digital_out(self, channel: int) -> DigitalOutput:
        """Return a stable handle for a digital output channel."""
        if not isinstance(channel, int) or isinstance(channel, bool):
            raise TypeError("channel must be an integer")
        if channel < 0:
            raise ValueError("channel must be non-negative")

        if channel not in self._digital_outputs:
            self._ensure_mutable()
            identity = Channel(kind=ChannelKind.DIGITAL_OUTPUT, index=channel)
            self._digital_outputs[channel] = DigitalOutput(self, identity)
        return self._digital_outputs[channel]

    def compile(self) -> ExecutionPlan:
        """Validate, order, group, and freeze this test definition."""
        if self._compiled_plan is None:
            self._compiled_plan = compile_test(
                name=self._name,
                configuration=self._configuration,
                instructions=self._instructions,
            )
        return self._compiled_plan

    def _add_instruction(self, instruction: Instruction) -> None:
        self._ensure_mutable()
        self._instructions._append(instruction)

    def _ensure_mutable(self) -> None:
        if self.is_compiled:
            raise FrozenTestError("A successfully compiled test cannot be changed")
