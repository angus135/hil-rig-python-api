"""Incoming captured-run intermediate representation."""

from hilrig.results.adapter import IncomingResultAdapter
from hilrig.results.builder import CapturedRunBuilder
from hilrig.results.ir import (
    AnalogueInputSeries,
    CapturedRunIR,
    DigitalInputSeries,
    PWMInputSeries,
)
from hilrig.results.models import (
    ANALOGUE_INPUT_CHANNEL_COUNT,
    DIGITAL_INPUT_CHANNEL_COUNT,
    PWM_INPUT_CHANNEL_COUNT,
    AnalogueInputSample,
    ApplicationErrorRecord,
    CapturedApplicationError,
    CapturedRunMetadata,
    CapturedTickResult,
    CaptureStatus,
    CommunicationCapture,
    CommunicationPeripheral,
    CommunicationResult,
    DigitalInputSample,
    PWMInputSample,
    PWMMeasurement,
    TickCondition,
    TickResult,
)

__all__ = [
    "ANALOGUE_INPUT_CHANNEL_COUNT",
    "DIGITAL_INPUT_CHANNEL_COUNT",
    "PWM_INPUT_CHANNEL_COUNT",
    "AnalogueInputSample",
    "AnalogueInputSeries",
    "ApplicationErrorRecord",
    "CapturedApplicationError",
    "CapturedRunBuilder",
    "CapturedRunIR",
    "CapturedRunMetadata",
    "CapturedTickResult",
    "CaptureStatus",
    "CommunicationCapture",
    "CommunicationPeripheral",
    "CommunicationResult",
    "DigitalInputSample",
    "DigitalInputSeries",
    "IncomingResultAdapter",
    "PWMInputSample",
    "PWMInputSeries",
    "PWMMeasurement",
    "TickCondition",
    "TickResult",
]
