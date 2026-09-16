"""Identifiers shared by compiled definitions, uploads, and captured runs."""

from __future__ import annotations

import secrets
from dataclasses import dataclass


def validate_uint128(value: object, *, name: str) -> int:
    """Validate and return an unsigned 128-bit integer identifier."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value < 2**128:
        raise ValueError(f"{name} must be an unsigned 128-bit integer")
    return value


def application_test_id_to_bytes(application_test_id: int) -> bytes:
    """Encode an Application Test ID in stable big-endian byte order."""
    validated = validate_uint128(application_test_id, name="application_test_id")
    return validated.to_bytes(16, byteorder="big")


def application_test_id_from_bytes(value: bytes) -> int:
    """Decode a stable big-endian 16-byte Application Test ID."""
    if not isinstance(value, bytes):
        raise TypeError("Application Test ID must be bytes")
    if len(value) != 16:
        raise ValueError("Application Test ID must contain exactly 16 bytes")
    return int.from_bytes(value, byteorder="big")


@dataclass(frozen=True, slots=True)
class UploadAttempt:
    """Bind one immutable test definition to one Application Test ID."""

    definition_test_id: int
    application_test_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "definition_test_id",
            validate_uint128(self.definition_test_id, name="definition_test_id"),
        )
        object.__setattr__(
            self,
            "application_test_id",
            validate_uint128(self.application_test_id, name="application_test_id"),
        )

    @classmethod
    def create(cls, *, definition_test_id: int) -> UploadAttempt:
        """Create a fresh wire identity for one compiled test definition."""
        validated_definition_id = validate_uint128(
            definition_test_id,
            name="definition_test_id",
        )
        application_test_id = secrets.randbits(128)
        while application_test_id == validated_definition_id:
            application_test_id = secrets.randbits(128)
        return cls(
            definition_test_id=validated_definition_id,
            application_test_id=application_test_id,
        )

    def restart(self) -> UploadAttempt:
        """Create a replacement upload identity after this attempt is abandoned."""
        application_test_id = secrets.randbits(128)
        while application_test_id in (self.definition_test_id, self.application_test_id):
            application_test_id = secrets.randbits(128)
        return type(self)(
            definition_test_id=self.definition_test_id,
            application_test_id=application_test_id,
        )

    @property
    def definition_test_id_hex(self) -> str:
        """Return the immutable definition ID as fixed-width hexadecimal."""
        return f"{self.definition_test_id:032x}"

    @property
    def application_test_id_hex(self) -> str:
        """Return the Application Test ID as fixed-width hexadecimal."""
        return f"{self.application_test_id:032x}"
