"""Future USB/transport/application adapter boundary.

The protocol repositories do not yet expose their final Python interfaces. This file
therefore documents and reserves the composition boundary without guessing field names
or encoding rules that would become accidental public API.
"""

from __future__ import annotations

from collections.abc import Iterable

from hilrig.results.builder import CapturedRunBuilder


class IncomingResultAdapter:
    """Skeleton that will translate protocol output into typed builder records.

    The stable downstream target is already implemented: once a complete application
    message is available, an adapter should construct ``TickResult``, zero or more
    ``CommunicationResult`` records, or ``ApplicationErrorRecord`` and submit them to
    ``self.builder``.
    """

    def __init__(self, builder: CapturedRunBuilder) -> None:
        if not isinstance(builder, CapturedRunBuilder):
            raise TypeError("builder must be a CapturedRunBuilder")
        self.builder = builder

    def receive_usb_bytes(self, data: bytes) -> None:
        """Feed bytes through the future transport and application interfaces.

        Intended flow once the interfaces exist::

            for transport_message in self.decode_transport_bytes(data):
                for application_message in self.decode_application_messages(
                    transport_message
                ):
                    self.ingest_application_message(application_message)

        The two decoder calls may instead become teammate-provided callbacks. This
        method remains deliberately unusable until that ownership and API are final.
        """
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        raise NotImplementedError(
            "USB/transport/application integration awaits the final Python interfaces"
        )

    def decode_transport_bytes(self, data: bytes) -> Iterable[object]:
        """Return complete transport messages from a USB byte chunk (future stub)."""
        raise NotImplementedError("Transport-layer Python interface is not defined")

    def decode_application_messages(self, transport_message: object) -> Iterable[object]:
        """Return complete application messages from one transport message (future stub)."""
        raise NotImplementedError("Application-layer Python interface is not defined")

    def ingest_application_message(self, application_message: object) -> None:
        """Parse one complete application message into stable typed result records.

        Future implementation notes:

        * A TEST_RESULT-like message becomes exactly one ``TickResult``.
        * ``OK`` and ``PARTIAL`` retain all fixed digital/analogue/PWM values.
        * ``EXECUTION_PROBLEM`` uses ``TickResult.execution_problem`` so placeholder
          firmware zeros are stored as SQL NULL rather than false measurements.
        * Any communication bytes bundled into that message become separate raw
          ``CommunicationResult`` rows. Do not clean or decode payload bytes here.
        * Application ERROR-like messages become ``ApplicationErrorRecord`` rows.
        * Completion/session-loss handling calls ``builder.finalize`` with the
          corresponding ``CaptureStatus`` once the final protocol defines that signal.

        No fields are accessed yet because the Python representation of
        HIL_Application_Message_T is intentionally still unknown.
        """
        raise NotImplementedError(
            "Application-message field mapping awaits the final Python interface"
        )
