"""Immutable, protocol-neutral compiler output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

from hilrig.models.instructions import Instruction

IR_SCHEMA_VERSION = "1.1"
IRScalar: TypeAlias = str | int | float | bool | None


def immutable_fields(fields: dict[str, IRScalar]) -> MappingProxyType[str, IRScalar]:
    """Copy a field dictionary into a read-only mapping."""
    return MappingProxyType(dict(fields))


@dataclass(frozen=True, slots=True)
class TimeSlot:
    """All instructions scheduled for one timestamp."""

    timestamp: int
    instructions: tuple[Instruction, ...]


@dataclass(frozen=True, slots=True)
class CompiledConfiguration:
    """One protocol-neutral peripheral configuration."""

    peripheral: str
    channel: int
    parameters: MappingProxyType[str, IRScalar]


@dataclass(frozen=True, slots=True)
class CompiledInstruction:
    """One chronological, protocol-neutral stimulus instruction."""

    instruction_id: int
    tick: int
    peripheral: str
    channel: int
    operation: str
    arguments: MappingProxyType[str, IRScalar]


@dataclass(frozen=True, slots=True)
class CompiledAssertion:
    """One host-side assertion retained for human-readable output."""

    peripheral: str
    channel: int
    assertion: str
    arguments: MappingProxyType[str, IRScalar]


@dataclass(frozen=True, slots=True)
class CompiledTestIR:
    """Immutable compiled snapshot from which files can be exported repeatedly.

    Assertion definitions deliberately live only on this host-side snapshot and are
    omitted from :meth:`to_dict`, :meth:`to_json`, and :meth:`write_json`. Their latest
    tick can still extend ``expected_tick_count`` so the RIG captures enough evidence.
    """

    test_id: int
    name: str
    frequency_mode: str
    frequency_hz: int
    expected_tick_count: int
    start_mode: str
    configurations: tuple[CompiledConfiguration, ...]
    instructions: tuple[CompiledInstruction, ...]
    assertions: tuple[CompiledAssertion, ...]
    time_slots: tuple[TimeSlot, ...]
    schema_version: str = IR_SCHEMA_VERSION

    @property
    def test_id_hex(self) -> str:
        """Return the 128-bit test ID as exactly 32 lowercase hexadecimal digits."""
        return f"{self.test_id:032x}"

    @property
    def tick_period_ns(self) -> int:
        """Return the duration of one configured execution tick in nanoseconds."""
        return 1_000_000_000 // self.frequency_hz

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-compatible, RIG-facing intermediate representation."""
        from hilrig.exporters.json_ir import as_machine_ir

        return as_machine_ir(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the RIG-facing intermediate representation as JSON text."""
        from hilrig.exporters.json_ir import dumps_machine_ir

        return dumps_machine_ir(self, indent=indent)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> Path:
        """Write the RIG-facing JSON IR and return its resolved path."""
        from hilrig.exporters.json_ir import write_machine_ir

        return write_machine_ir(self, path, indent=indent)

    def write_excel(self, path: str | Path) -> Path:
        """Write the four-sheet human-readable workbook and return its path."""
        from hilrig.exporters.excel import write_human_readable_workbook

        return write_human_readable_workbook(self, path)


# Kept as a compatibility alias for code written against the preliminary compiler.
ExecutionPlan = CompiledTestIR
