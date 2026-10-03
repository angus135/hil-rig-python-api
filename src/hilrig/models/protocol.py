"""Protocol family enumeration for HIL-RIG message formatting."""

from __future__ import annotations

from enum import Enum


class ProtocolFamily(str, Enum):
    """Supported HIL-RIG message framing and data structure families."""

    VARIABLE = "variable"
    LEGACY = "legacy"


__all__ = ["ProtocolFamily"]
