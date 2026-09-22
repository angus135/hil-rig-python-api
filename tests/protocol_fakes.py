"""Small public-surface fakes for testing the optional protocol integration."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum


class PeripheralVoltage(IntEnum):
    INVALID = 0
    V_3V3 = 1
    V_5V = 2
    V_12V = 3
    V_24V = 4


class ResultCondition(IntEnum):
    OK = 0
    PARTIAL = 1
    EXECUTION_PROBLEM = 2


@dataclass(frozen=True)
class ProtocolVersion:
    major: int
    minor: int
    patch: int


PROTOCOL_VERSION = ProtocolVersion(0, 3, 0)


class ControlCommand(IntEnum):
    INVALID = 0
    START = 1
    ABORT = 2


class GlobalControlCommand(IntEnum):
    INVALID = 0
    RESET_APPLICATION = 1


class ResponseScope(IntEnum):
    TEST_CONFIGURATION = 1
    TICK = 2
    COMPLETE_TEST = 3
    EXECUTION_CONTROL = 4
    GLOBAL_CONTROL = 5


class ResponseOutcome(IntEnum):
    ACCEPTED = 1
    REJECTED = 2
    COMPLETED = 3
    FAILED = 4


class ResponseReason(IntEnum):
    NONE = 0
    UNSUPPORTED = 1
    OPERATION_NOT_ALLOWED = 2
    INCONSISTENT_TEST_ID = 3
    INVALID_TICK = 4
    LENGTH_MISMATCH = 5
    STORAGE_UNAVAILABLE = 6
    VALIDATION_FAILED = 7
    HARDWARE_NOT_READY = 8
    INTERNAL_FAILURE = 9


class ErrorCategory(IntEnum):
    HARDWARE = 1
    EXECUTION = 2
    TIMEOUT = 3
    RETAINED_DATA = 4
    PROTOCOL = 5
    INTERNAL = 6


class TransportStatus(IntEnum):
    OK = 0
    MESSAGE_TOO_LARGE = 4
    CAPACITY_EXHAUSTED = 5
    DELIVERY_FAILED = 6
    NOT_READY = 8


class Role(IntEnum):
    HOST = 0


class LinkState(IntEnum):
    DISCONNECTED = 0
    CONNECTED = 1


class OperatingMode(IntEnum):
    NORMAL = 0
    BULK_TRANSFER = 1


class SessionState(IntEnum):
    DISCONNECTED = 0
    ESTABLISHED = 2


class EventType(IntEnum):
    SESSION_ESTABLISHED = 1
    SESSION_RESET = 2
    DELIVERY_CONFIRMED = 3
    DELIVERY_FAILED = 4
    PROTOCOL_ERROR = 5


@dataclass(frozen=True)
class ApplicationConfig:
    max_encoded_message_size: int = 512
    max_variable_data_size: int = 255
    max_variable_transfers_per_tick: int = 8
    max_expected_tick_count: int = 1_000_000


@dataclass(frozen=True)
class TransportConfig:
    max_application_message_size: int = 512
    retransmit_timeout_ms: int = 0
    max_retries: int = 0


@dataclass(frozen=True)
class TestId:
    bytes: bytes


@dataclass(frozen=True)
class SystemInfoRequest:
    request_firmware_git_hash: bool = False
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


@dataclass(frozen=True)
class SystemInfoResponse:
    protocol_version: ProtocolVersion
    firmware_version: ProtocolVersion
    diagnostic_data: bytes = b""
    firmware_git_hash: bytes = b""


@dataclass(frozen=True)
class ExecutionControl:
    test_id: TestId
    command: ControlCommand
    flags: int = 0


@dataclass(frozen=True)
class FinalizeTestUpload:
    test_id: TestId
    flags: int = 0


@dataclass(frozen=True)
class GlobalControl:
    command: GlobalControlCommand
    flags: int = 0


@dataclass(frozen=True)
class ApplicationResponse:
    test_id: TestId | None
    scope: ResponseScope
    outcome: ResponseOutcome
    reason: ResponseReason = ResponseReason.NONE
    tick_number: int = 0
    control_command: ControlCommand = ControlCommand.INVALID
    global_control_command: GlobalControlCommand = GlobalControlCommand.INVALID
    detail: int = 0


@dataclass(frozen=True)
class ApplicationErrorMessage:
    test_id: TestId | None
    category: ErrorCategory
    recoverable: bool
    tick_number: int | None = None
    detail: int = 0
    diagnostic_data: bytes = b""


@dataclass(frozen=True)
class TickDuration:
    microseconds: int = 0


@dataclass(frozen=True)
class DigitalInputConfig:
    enabled: bool = False
    voltage_level: PeripheralVoltage = PeripheralVoltage.INVALID


@dataclass(frozen=True)
class DigitalOutputConfig:
    enabled: bool = False
    voltage_level: PeripheralVoltage = PeripheralVoltage.INVALID
    initial_high: bool = False


@dataclass(frozen=True)
class AnalogInputConfig:
    enabled: bool = False


@dataclass(frozen=True)
class AnalogOutputConfig:
    enabled: bool = False


@dataclass(frozen=True)
class PWMInputConfig:
    enabled: bool = False
    voltage_level: PeripheralVoltage = PeripheralVoltage.INVALID


@dataclass(frozen=True)
class PWMOutputConfig:
    enabled: bool = False
    voltage_level: PeripheralVoltage = PeripheralVoltage.INVALID
    initial_period_nanoseconds: int = 0
    initial_duty_cycle_permyriad: int = 0


@dataclass(frozen=True)
class DigitalOutputValue:
    high: bool = False


@dataclass(frozen=True)
class AnalogOutputValue:
    microvolts: int = 0


@dataclass(frozen=True)
class PWMOutputValue:
    period_nanoseconds: int = 0
    duty_cycle_permyriad: int = 0


@dataclass(frozen=True)
class DigitalInputValue:
    high: bool = False


@dataclass(frozen=True)
class AnalogInputValue:
    microvolts: int = 0


@dataclass(frozen=True)
class PWMInputValue:
    period_nanoseconds: int = 0
    duty_cycle_permyriad: int = 0


@dataclass(frozen=True)
class TestConfiguration:
    test_id: TestId
    tick_duration_us: TickDuration
    expected_tick_count: int
    flags: int = 0
    digital_in: tuple[DigitalInputConfig, ...] = (DigitalInputConfig(),) * 10
    digital_out: tuple[DigitalOutputConfig, ...] = (DigitalOutputConfig(),) * 10
    analog_in: tuple[AnalogInputConfig, ...] = (AnalogInputConfig(),) * 2
    analog_out: tuple[AnalogOutputConfig, ...] = (AnalogOutputConfig(),) * 6
    pwm_in: tuple[PWMInputConfig, ...] = (PWMInputConfig(),) * 2
    pwm_out: tuple[PWMOutputConfig, ...] = (PWMOutputConfig(),) * 2


@dataclass(frozen=True)
class TestInstruction:
    test_id: TestId
    tick_number: int = 0
    digital_outputs: tuple[DigitalOutputValue, ...] = (DigitalOutputValue(),) * 10
    analog_outputs: tuple[AnalogOutputValue, ...] = (AnalogOutputValue(),) * 6
    pwm_outputs: tuple[PWMOutputValue, ...] = (PWMOutputValue(),) * 2


@dataclass(frozen=True)
class TestResult:
    test_id: TestId
    tick_number: int = 0
    digital_inputs: tuple[DigitalInputValue, ...] = (DigitalInputValue(),) * 10
    analog_inputs: tuple[AnalogInputValue, ...] = (AnalogInputValue(),) * 2
    pwm_inputs: tuple[PWMInputValue, ...] = (PWMInputValue(),) * 2
    condition: ResultCondition = ResultCondition.OK
    problem_detail: int = 0


class ApplicationCodec:
    def __init__(self, config: ApplicationConfig) -> None:
        self.config = config
        self.encoded: dict[bytes, object] = {}

    def encode(self, message: object) -> bytes:
        data = f"application-{len(self.encoded)}".encode()
        self.encoded[data] = message
        return data

    def decode(self, data: bytes) -> object:
        return self.encoded[data]


@dataclass(frozen=True)
class ReceiveResult:
    status: TransportStatus
    bytes_consumed: int


@dataclass(frozen=True)
class TransportEvent:
    type: EventType
    status: TransportStatus = TransportStatus.OK


@dataclass(frozen=True)
class TransportSnapshot:
    session_state: SessionState = SessionState.ESTABLISHED


class FakeProtocol:
    PROTOCOL_VERSION = PROTOCOL_VERSION
    ApplicationCodec = ApplicationCodec
    ApplicationConfig = ApplicationConfig
    TransportConfig = TransportConfig
    TestId = TestId
    SystemInfoRequest = SystemInfoRequest
    SystemInfoResponse = SystemInfoResponse
    ExecutionControl = ExecutionControl
    FinalizeTestUpload = FinalizeTestUpload
    GlobalControl = GlobalControl
    ApplicationResponse = ApplicationResponse
    ApplicationErrorMessage = ApplicationErrorMessage
    ControlCommand = ControlCommand
    GlobalControlCommand = GlobalControlCommand
    ResponseScope = ResponseScope
    ResponseOutcome = ResponseOutcome
    ResponseReason = ResponseReason
    ErrorCategory = ErrorCategory
    TickDuration = TickDuration
    DigitalInputConfig = DigitalInputConfig
    DigitalOutputConfig = DigitalOutputConfig
    AnalogInputConfig = AnalogInputConfig
    AnalogOutputConfig = AnalogOutputConfig
    PWMInputConfig = PWMInputConfig
    PWMOutputConfig = PWMOutputConfig
    DigitalOutputValue = DigitalOutputValue
    AnalogOutputValue = AnalogOutputValue
    PWMOutputValue = PWMOutputValue
    DigitalInputValue = DigitalInputValue
    AnalogInputValue = AnalogInputValue
    PWMInputValue = PWMInputValue
    TestConfiguration = TestConfiguration
    TestInstruction = TestInstruction
    TestResult = TestResult
    PeripheralVoltage = PeripheralVoltage
    ResultCondition = ResultCondition
    TransportStatus = TransportStatus
    Role = Role
    LinkState = LinkState
    OperatingMode = OperatingMode
    SessionState = SessionState
    EventType = EventType

    @staticmethod
    def check_protocol_version(peer_version: ProtocolVersion) -> None:
        if peer_version != PROTOCOL_VERSION:
            raise ValueError("version mismatch")


@dataclass
class FakeSerial:
    max_write_size: int = 1_000
    received: bytearray = field(default_factory=bytearray)
    written: bytearray = field(default_factory=bytearray)
    closed: bool = False

    @property
    def in_waiting(self) -> int:
        return len(self.received)

    def read(self, size: int) -> bytes:
        data = bytes(self.received[:size])
        del self.received[:size]
        return data

    def write(self, data: bytes) -> int:
        accepted = min(len(data), self.max_write_size)
        self.written.extend(data[:accepted])
        return accepted

    def reset_input_buffer(self) -> None:
        self.received.clear()

    def reset_output_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self) -> None:
        self.events: deque[TransportEvent] = deque()
        self.application_data: deque[bytes] = deque()
        self.outputs: deque[bytes] = deque()
        self.submitted: list[bytes] = []
        self.committed = 0
        self.link_states: list[LinkState] = []
        self.closed = False

    def notify_link_state(self, state: LinkState, now_ms: int) -> TransportStatus:
        self.link_states.append(state)
        return TransportStatus.OK

    def receive_bytes(self, data: bytes | bytearray) -> ReceiveResult:
        return ReceiveResult(TransportStatus.OK, len(data))

    def process(self, now_ms: int, operating_mode: OperatingMode) -> TransportStatus:
        return TransportStatus.OK

    def read_event(self) -> TransportEvent | None:
        return self.events.popleft() if self.events else None

    def read_application_data(self) -> bytes | None:
        return self.application_data.popleft() if self.application_data else None

    def get_status(self) -> TransportSnapshot:
        return TransportSnapshot()

    def submit_application_data(self, data: bytes) -> TransportStatus:
        self.submitted.append(data)
        self.outputs.append(b"frame:" + data)
        return TransportStatus.OK

    def peek_output(self) -> bytes | None:
        return self.outputs[0] if self.outputs else None

    def commit_output(self, now_ms: int) -> TransportStatus:
        self.outputs.popleft()
        self.committed += 1
        self.events.append(TransportEvent(EventType.DELIVERY_CONFIRMED))
        return TransportStatus.OK

    def close(self) -> None:
        self.closed = True
