"""Exceptions raised by the HIL-RIG host library."""


class HilRigError(Exception):
    """Base class for all library-specific exceptions."""


class ValidationError(HilRigError):
    """The complete test definition is not valid."""


class TimingError(ValidationError):
    """An instruction violates the host-side timing model."""


class FrozenTestError(HilRigError):
    """A caller attempted to change an already compiled test."""
