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
