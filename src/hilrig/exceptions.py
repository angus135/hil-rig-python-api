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
