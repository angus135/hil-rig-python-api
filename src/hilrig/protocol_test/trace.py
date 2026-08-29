"""Machine-readable JSONL evidence and final JSON summaries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .models import ENVELOPE_VERSION, PROTOCOL_COMMIT, PYTHON_API_BRANCH_POINT_COMMIT


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def payload_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, bytes):
        return {"sha256": payload_hash(value), "length": len(value)}
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def source_identifier() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "WORKING_TREE_FROM_SUPPLIED_ZIP"


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


class TraceWriter:
    """Flush each evidence record immediately so failures retain useful context."""

    def __init__(self, output_dir: Path, scenario: str, *, seed: int) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
        self.scenario = scenario
        self.seed = seed
        self.start_time = utc_now()
        self.trace_path = output_dir / f"{self.run_id}.jsonl"
        self.summary_path = output_dir / f"{self.run_id}.summary.json"
        self._file = self.trace_path.open("w", encoding="utf-8")
        self.record(
            "run_start",
            scenario=scenario,
            seed=seed,
            python_api_source=source_identifier(),
            python_api_branch_point=PYTHON_API_BRANCH_POINT_COMMIT,
            protocol_commit=PROTOCOL_COMMIT,
            python_version=sys.version,
            operating_system=platform.platform(),
            pyserial_version=package_version("pyserial"),
            test_envelope_version=ENVELOPE_VERSION,
        )

    def record(self, kind: str, **fields: Any) -> None:
        record = {
            "kind": kind,
            "run_id": self.run_id,
            "utc_time": utc_now(),
            **fields,
        }
        self._file.write(json.dumps(_jsonable(record), sort_keys=True) + "\n")
        self._file.flush()

    def finish(
        self,
        *,
        passed: bool,
        failure_reason: str | None,
        diagnostics: dict[str, object] | None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "start_time": self.start_time,
            "end_time": utc_now(),
            "passed": passed,
            "failure_reason": failure_reason,
            "python_api_source": source_identifier(),
            "python_api_branch_point": PYTHON_API_BRANCH_POINT_COMMIT,
            "protocol_commit": PROTOCOL_COMMIT,
            "python_version": sys.version,
            "operating_system": platform.platform(),
            "pyserial_version": package_version("pyserial"),
            "test_envelope_version": ENVELOPE_VERSION,
            "seed": self.seed,
            "diagnostics": diagnostics,
            "trace_file": self.trace_path.name,
        }
        if extra:
            summary.update(extra)
        self.record("run_end", passed=passed, failure_reason=failure_reason)
        self.summary_path.write_text(
            json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._file.close()
        return summary
