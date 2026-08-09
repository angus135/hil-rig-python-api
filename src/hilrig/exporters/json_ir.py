"""JSON serialization for the RIG-facing intermediate representation."""

from __future__ import annotations

import json
from pathlib import Path

from hilrig.models.execution import CompiledTestIR


def as_machine_ir(compiled: CompiledTestIR) -> dict[str, object]:
    """Build the JSON-compatible machine IR, intentionally excluding assertions."""
    return {
        "ir_version": compiled.schema_version,
        "test": {
            "test_id": compiled.test_id_hex,
            "name": compiled.name,
            "frequency_mode": compiled.frequency_mode,
            "frequency_hz": compiled.frequency_hz,
            "start_mode": compiled.start_mode,
        },
        "configurations": [
            {
                "peripheral": item.peripheral,
                "channel": item.channel,
                "parameters": dict(item.parameters),
            }
            for item in compiled.configurations
        ],
        "instructions": [
            {
                "instruction_id": item.instruction_id,
                "tick": item.tick,
                "peripheral": item.peripheral,
                "channel": item.channel,
                "operation": item.operation,
                "arguments": dict(item.arguments),
            }
            for item in compiled.instructions
        ],
    }


def dumps_machine_ir(compiled: CompiledTestIR, *, indent: int | None = 2) -> str:
    """Serialize the machine IR deterministically."""
    return json.dumps(as_machine_ir(compiled), indent=indent, ensure_ascii=False) + "\n"


def write_machine_ir(
    compiled: CompiledTestIR,
    path: str | Path,
    *,
    indent: int | None = 2,
) -> Path:
    """Write one JSON IR file."""
    output_path = Path(path).expanduser()
    if output_path.suffix.lower() != ".json":
        raise ValueError("JSON IR path must end in .json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dumps_machine_ir(compiled, indent=indent), encoding="utf-8")
    return output_path.resolve()
