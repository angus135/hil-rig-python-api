"""Exceptions raised by the HIL-RIG host library."""


class HilRigError(Exception):
    """Base class for all library-specific exceptions."""


class ValidationError(HilRigError):
    """The complete test definition is not valid."""


class ConfigurationError(ValidationError):
    """A test or peripheral configuration is invalid or conflicting."""


class PeripheralError(ValidationError):
    """A peripheral operation is invalid for the selected channel or mode."""


class TimingError(ValidationError):
    """An instruction or assertion violates the host-side timing model."""


class FrozenTestError(HilRigError):
    """A caller attempted to change an already compiled test."""


class CaptureError(HilRigError):
    """Base class for errors while receiving or reading a captured run."""


class CaptureStateError(CaptureError):
    """A capture operation was attempted after the builder stopped accepting data."""


class CaptureStorageError(CaptureError):
    """Captured data could not be written to or read from persistent storage."""


class CaptureSchemaError(CaptureStorageError):
    """A capture database has an absent or unsupported schema."""


class EvaluationError(HilRigError):
    """A captured run could not be evaluated safely."""


class UnsupportedAssertionError(EvaluationError):
    """No evaluator is registered for a compiled assertion definition."""
