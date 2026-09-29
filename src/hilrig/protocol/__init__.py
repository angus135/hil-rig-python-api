"""Application protocol and direct serial integration for the HIL-RIG host API."""

from hilrig.protocol.application import (
    FLAG_COMPLETE_TICK,
    FLAG_HAS_MORE_CHUNKS,
    FixedIOProtocolAdapter,
    FixedIOUploadMessages,
    ProtocolFamily,
    ResponseCorrelation,
    UploadOperation,
    UploadOperationKind,
    UploadPlan,
    VariableIOProtocolAdapter,
    VariableIOUploadMessages,
    application_test_id_from_bytes,
    application_test_id_to_bytes,
    chunk_update_instruction,
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
    "FLAG_COMPLETE_TICK",
    "FLAG_HAS_MORE_CHUNKS",
    "FixedIOProtocolAdapter",
    "FixedIOProtocolConnection",
    "FixedIOUploadMessages",
    "ManualApplicationMessage",
    "ManualMessageDefinitionError",
    "ManualSendResult",
    "ProtocolFamily",
    "ProtocolServiceReport",
    "ProtocolWorkflowState",
    "RigSystemInfo",
    "ResponseCorrelation",
    "SerialConnectionSettings",
    "UploadAdvanceMode",
    "UploadOperation",
    "UploadOperationKind",
    "UploadPlan",
    "VariableIOProtocolAdapter",
    "VariableIOUploadMessages",
    "application_test_id_from_bytes",
    "application_test_id_to_bytes",
    "chunk_update_instruction",
    "discover_serial_port",
    "monotonic_now_ms",
    "open_serial_port",
    "load_manual_message",
]
