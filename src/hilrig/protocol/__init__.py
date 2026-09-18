"""Fixed-I/O Application/Transport integration for the HIL-RIG host API."""

from hilrig.protocol.application import (
    FixedIOProtocolAdapter,
    FixedIOUploadMessages,
    ResponseCorrelation,
    UploadOperation,
    UploadOperationKind,
    UploadPlan,
    application_test_id_from_bytes,
    application_test_id_to_bytes,
)
from hilrig.protocol.connection import (
    FixedIOProtocolConnection,
    ManualSendResult,
    ProtocolServiceReport,
    ProtocolWorkflowState,
    RigSystemInfo,
    UploadAdvanceMode,
    monotonic_now_ms,
)
from hilrig.protocol.manual import (
    ManualApplicationMessage,
    ManualMessageDefinitionError,
    load_manual_message,
)
from hilrig.protocol.serial import (
    SerialConnectionSettings,
    discover_serial_port,
    open_serial_port,
)

__all__ = [
    "FixedIOProtocolAdapter",
    "FixedIOProtocolConnection",
    "FixedIOUploadMessages",
    "ManualApplicationMessage",
    "ManualMessageDefinitionError",
    "ManualSendResult",
    "ProtocolServiceReport",
    "ProtocolWorkflowState",
    "RigSystemInfo",
    "ResponseCorrelation",
    "SerialConnectionSettings",
    "UploadAdvanceMode",
    "UploadOperation",
    "UploadOperationKind",
    "UploadPlan",
    "application_test_id_from_bytes",
    "application_test_id_to_bytes",
    "discover_serial_port",
    "monotonic_now_ms",
    "open_serial_port",
    "load_manual_message",
]
