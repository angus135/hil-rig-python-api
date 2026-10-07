"""Typed, application-neutral records used by the incoming result boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hilrig.models.execution import CompiledAssertion, CompiledAssertionGroup, CompiledInstruction

RESULT_IR_SCHEMA_VERSION = "1.5"
ORIGINAL_ASSERTION_SET_ID = "original"

DIGITAL_INPUT_CHANNEL_COUNT = 10
ANALOGUE_INPUT_CHANNEL_COUNT = 2
PWM_INPUT_CHANNEL_COUNT = 2


class TickCondition(str, Enum):
    """Validity reported for one fixed-size application result."""

    OK = "ok"
    PARTIAL = "partial"
    EXECUTION_PROBLEM = "execution_problem"


class CaptureStatus(str, Enum):
    """State of receiving a run, separate from assertion pass/fail results."""

    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    SESSION_LOST = "session_lost"
    PROTOCOL_ERROR = "protocol_error"
    INDETERMINATE = "indeterminate"
    ABORTED = "aborted"


class CommunicationPeripheral(str, Enum):
    """Communication peripherals that can produce variable-length capture data."""

    I2C = "i2c"
    SPI = "spi"
    UART = "uart"
    CAN = "can"


@dataclass(frozen=True, slots=True)
class PWMMeasurement:
    """PWM measurements reported as true at one application tick."""

    period_ns: int
    duty_permyriad: int

    def __post_init__(self) -> None:
        _non_negative_int(self.period_ns, name="period_ns")
        _bounded_int(self.duty_permyriad, minimum=0, maximum=10_000, name="duty_permyriad")

    @property
    def duty_cycle(self) -> float:
        """Return the duty cycle as a fraction between zero and one."""
        return self.duty_permyriad / 10_000


@dataclass(frozen=True, slots=True)
class TickResult:
    """All fixed-size input measurements reported for one tick.

    ``PARTIAL`` still contains valid fixed measurements; it means some associated
    variable-length communication capture was incomplete. For
    ``EXECUTION_PROBLEM``, placeholder wire values are deliberately normalized to
    ``None`` so that a firmware zero can never be mistaken for a real measurement.
    """

    tick: int
    digital_inputs: tuple[bool | None, ...]
    analogue_inputs_uv: tuple[int | None, ...]
    pwm_inputs: tuple[PWMMeasurement | None, ...]
    condition: TickCondition = TickCondition.OK
    problem_detail: int | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.tick, name="tick")
        _require_enum(self.condition, TickCondition, name="condition")
        if self.problem_detail is not None:
            _non_negative_int(self.problem_detail, name="problem_detail")

        _exact_length(
            self.digital_inputs,
            DIGITAL_INPUT_CHANNEL_COUNT,
            name="digital_inputs",
        )
        _exact_length(
            self.analogue_inputs_uv,
            ANALOGUE_INPUT_CHANNEL_COUNT,
            name="analogue_inputs_uv",
        )
        _exact_length(self.pwm_inputs, PWM_INPUT_CHANNEL_COUNT, name="pwm_inputs")

        if self.condition is TickCondition.EXECUTION_PROBLEM:
            object.__setattr__(
                self,
                "digital_inputs",
                (None,) * DIGITAL_INPUT_CHANNEL_COUNT,
            )
            object.__setattr__(
                self,
                "analogue_inputs_uv",
                (None,) * ANALOGUE_INPUT_CHANNEL_COUNT,
            )
            object.__setattr__(self, "pwm_inputs", (None,) * PWM_INPUT_CHANNEL_COUNT)
            return

        for index, value in enumerate(self.digital_inputs):
            if not isinstance(value, bool):
                raise TypeError(f"digital_inputs[{index}] must be a bool")
        for index, value in enumerate(self.analogue_inputs_uv):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"analogue_inputs_uv[{index}] must be an integer")
        for index, value in enumerate(self.pwm_inputs):
            if not isinstance(value, PWMMeasurement):
                raise TypeError(f"pwm_inputs[{index}] must be a PWMMeasurement")

    @classmethod
    def execution_problem(cls, *, tick: int, problem_detail: int | None = None) -> TickResult:
        """Construct a tick whose fixed measurements are all invalid."""
        return cls(
            tick=tick,
            digital_inputs=(None,) * DIGITAL_INPUT_CHANNEL_COUNT,
            analogue_inputs_uv=(None,) * ANALOGUE_INPUT_CHANNEL_COUNT,
            pwm_inputs=(None,) * PWM_INPUT_CHANNEL_COUNT,
            condition=TickCondition.EXECUTION_PROBLEM,
            problem_detail=problem_detail,
        )


@dataclass(frozen=True, slots=True)
class CommunicationResult:
    """One raw variable-length communication capture associated with a tick."""

    tick: int
    peripheral: CommunicationPeripheral
    channel: int
    payload: bytes
    ordinal: int | None = None

    def __post_init__(self) -> None:
        _non_negative_int(self.tick, name="tick")
        _require_enum(self.peripheral, CommunicationPeripheral, name="peripheral")
        _non_negative_int(self.channel, name="channel")
        if not isinstance(self.payload, bytes):
            raise TypeError("payload must be bytes")
        if self.ordinal is not None:
            _non_negative_int(self.ordinal, name="ordinal")


@dataclass(frozen=True, slots=True)
class ApplicationErrorRecord:
    """An application-layer diagnostic retained alongside captured evidence."""

    category: str
    detail: str
    recoverable: bool
    tick: int | None = None
    diagnostic_data: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("category must be a non-empty string")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be a string")
        if not isinstance(self.recoverable, bool):
            raise TypeError("recoverable must be a bool")
        if self.tick is not None:
            _non_negative_int(self.tick, name="tick")
        if not isinstance(self.diagnostic_data, bytes):
            raise TypeError("diagnostic_data must be bytes")


@dataclass(frozen=True, slots=True)
class RunReportRecord:
    """Application-neutral copy of the firmware's terminal Run Report."""

    schema_version: int
    valid_sections: int
    run_outcome: str
    execution_outcome: str
    result_status: str
    expected_tick_count: int
    tick_period_us: int
    last_completed_boundary: int | None
    result_ticks_emitted: int
    failure_source: str
    failure_stage: str
    failure_reason: str
    isr_timing: dict[str, int] | None
    instruction_buffer: dict[str, int] | None
    result_buffer: dict[str, int] | None
    flash: dict[str, int] | None
    extension_data: bytes = b""
    raw_bytes: bytes = b""

    def __post_init__(self) -> None:
        for name in (
            "schema_version",
            "valid_sections",
            "expected_tick_count",
            "tick_period_us",
            "result_ticks_emitted",
        ):
            _non_negative_int(getattr(self, name), name=name)
        if self.last_completed_boundary is not None:
            _non_negative_int(self.last_completed_boundary, name="last_completed_boundary")
        for name in (
            "run_outcome",
            "execution_outcome",
            "result_status",
            "failure_source",
            "failure_stage",
            "failure_reason",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("isr_timing", "instruction_buffer", "result_buffer", "flash"):
            values = getattr(self, name)
            if values is not None and (
                not isinstance(values, dict)
                or any(
                    not isinstance(key, str)
                    or not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                    for key, value in values.items()
                )
            ):
                raise TypeError(f"{name} must be a mapping of strings to non-negative integers")
        if not isinstance(self.extension_data, bytes):
            raise TypeError("extension_data must be bytes")
        if not isinstance(self.raw_bytes, bytes):
            raise TypeError("raw_bytes must be bytes")


@dataclass(frozen=True, slots=True)
class RigStatusRecord:
    """One queried or unsolicited firmware lifecycle status event."""

    origin: str
    state: str
    flags: int
    application_test_id: int | None
    schema_version: int
    failure_source: str
    failure_stage: str
    failure_reason: str
    observed_at: str

    def __post_init__(self) -> None:
        for name in ("origin", "state", "failure_source", "failure_stage", "failure_reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        _non_negative_int(self.flags, name="flags")
        _non_negative_int(self.schema_version, name="schema_version")
        if self.application_test_id is not None:
            _bounded_int(
                self.application_test_id,
                minimum=0,
                maximum=(1 << 128) - 1,
                name="application_test_id",
            )
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("observed_at must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ApplicationResponseRecord:
    """A correlated lifecycle response retained as supporting evidence."""

    scope: str
    outcome: str
    reason: str
    detail: int
    tick: int | None = None
    control_command: str | None = None
    global_control_command: str | None = None

    def __post_init__(self) -> None:
        for name in ("scope", "outcome", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        _non_negative_int(self.detail, name="detail")
        if self.tick is not None:
            _non_negative_int(self.tick, name="tick")


@dataclass(frozen=True, slots=True)
class CollectionIntegrityIssue:
    """A host-side evidence collection problem that does not rewrite firmware truth."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be a non-empty string")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("detail must be a non-empty string")


@dataclass(frozen=True, slots=True)
class CapturedRunMetadata:
    """Small immutable summary read from a capture database."""

    schema_version: str
    test_id: int
    application_test_id: int
    run_id: int
    test_name: str
    tick_period_ns: int
    expected_tick_count: int
    received_tick_count: int
    first_tick: int | None
    last_tick: int | None
    status: CaptureStatus
    created_at: str
    finalized_at: str | None
    application_protocol_version: str | None
    firmware_version: str | None
    attempt_number: int = 1
    started_at: str | None = None
    completed_at: str | None = None

    @property
    def test_id_hex(self) -> str:
        """Return the immutable definition identifier as fixed-width hexadecimal."""
        return f"{self.test_id:032x}"

    @property
    def application_test_id_hex(self) -> str:
        """Return the upload's Application Test ID as fixed-width hexadecimal."""
        return f"{self.application_test_id:032x}"

    @property
    def run_id_hex(self) -> str:
        """Return the 128-bit run identifier as fixed-width hexadecimal."""
        return f"{self.run_id:032x}"

    @property
    def missing_tick_count(self) -> int:
        """Return how many expected fixed tick rows have not been stored."""
        return max(0, self.expected_tick_count - self.received_tick_count)


@dataclass(frozen=True, slots=True)
class CapturedAssertionSet:
    """Immutable compiled assertion snapshot stored with one captured run."""

    assertion_set_id: str
    name: str
    compiled_ir_version: str
    created_at: str
    groups: tuple[CompiledAssertionGroup, ...]
    stimuli: tuple[CompiledInstruction, ...]
    assertions: tuple[CompiledAssertion, ...]

    @property
    def group_count(self) -> int:
        """Return the number of named groups in this assertion snapshot."""
        return len(self.groups)

    @property
    def assertion_count(self) -> int:
        """Return the number of assertion definitions in this set."""
        return len(self.assertions)

    @property
    def stimulus_count(self) -> int:
        """Return the number of grouped stimulus definitions in this set."""
        return len(self.stimuli)


@dataclass(frozen=True, slots=True)
class CapturedTickResult:
    """One fixed tick row read back from the capture database."""

    tick: int
    digital_inputs: tuple[bool | None, ...]
    analogue_inputs_uv: tuple[int | None, ...]
    pwm_inputs: tuple[PWMMeasurement | None, ...]
    condition: TickCondition
    problem_detail: int | None

    @property
    def valid(self) -> bool:
        """Return whether the fixed measurements on this tick are usable."""
        return self.condition is not TickCondition.EXECUTION_PROBLEM


@dataclass(frozen=True, slots=True)
class DigitalInputSample:
    """One digital channel sample with its validity context."""

    tick: int
    time_ns: int
    value: bool | None
    condition: TickCondition
    problem_detail: int | None

    @property
    def valid(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class AnalogueInputSample:
    """One analogue channel sample in integer microvolts."""

    tick: int
    time_ns: int
    microvolts: int | None
    condition: TickCondition
    problem_detail: int | None

    @property
    def valid(self) -> bool:
        return self.microvolts is not None


@dataclass(frozen=True, slots=True)
class PWMInputSample:
    """One PWM channel measurement reported for a tick."""

    tick: int
    time_ns: int
    measurement: PWMMeasurement | None
    condition: TickCondition
    problem_detail: int | None

    @property
    def valid(self) -> bool:
        return self.measurement is not None


@dataclass(frozen=True, slots=True)
class CommunicationCapture:
    """One communication payload read from persistent capture storage."""

    capture_id: int
    tick: int
    time_ns: int
    ordinal: int
    peripheral: CommunicationPeripheral
    channel: int
    payload: bytes

    @property
    def payload_size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class CapturedApplicationError:
    """One stored application-layer diagnostic."""

    error_id: int
    category: str
    detail: str
    recoverable: bool
    tick: int | None
    diagnostic_data: bytes


def _non_negative_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _bounded_int(value: object, *, minimum: int, maximum: int, name: str) -> int:
    integer = _non_negative_int(value, name=name)
    if not minimum <= integer <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return integer


def _exact_length(value: object, expected: int, *, name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")
    if len(value) != expected:
        raise ValueError(f"{name} must contain exactly {expected} values")


def _require_enum(value: object, enum_type: type[Enum], *, name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be a {enum_type.__name__} value")
