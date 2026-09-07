"""Machine-readable JSONL evidence and final JSON summaries."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .application_hardware import (
    APPLICATION_CODEC_CONFIG,
    COMPATIBILITY_PROFILE_ID,
    PROTOCOL_VERSION,
)
from .models import ENVELOPE_VERSION


class CompatibilityError(RuntimeError):
    """Observed runtime source is incompatible with the hardware-test manifest."""


@dataclass(frozen=True, slots=True)
class GitSourceMetadata:
    """Observed Git metadata, or an explicit unavailable result."""

    commit: str | None
    dirty: bool | None
    available: bool


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


def inspect_git_source(path: Path) -> GitSourceMetadata:
    """Inspect one Git worktree without treating configured revisions as observations."""
    if not path.exists():
        return GitSourceMetadata(None, None, False)
    try:
        root_result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            root_result.returncode != 0
            or not root_result.stdout.strip()
            or Path(root_result.stdout.strip()).resolve() != path.resolve()
        ):
            return GitSourceMetadata(None, None, False)
        commit_result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return GitSourceMetadata(None, None, False)
    if commit_result.returncode != 0:
        return GitSourceMetadata(None, None, False)
    commit = commit_result.stdout.strip()
    if not commit:
        return GitSourceMetadata(None, None, False)
    try:
        status_result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return GitSourceMetadata(commit, None, True)
    dirty = None if status_result.returncode != 0 else bool(status_result.stdout.strip())
    return GitSourceMetadata(commit, dirty, True)


def _default_source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def collect_source_evidence(repo_root: Path | None = None) -> dict[str, object]:
    """Collect observable source metadata without treating Git pins as compatibility gates."""
    root = _default_source_root() if repo_root is None else repo_root
    protocol_root = root / "external" / "hil-rig-protocol"
    python_source = inspect_git_source(root)
    protocol_source = inspect_git_source(protocol_root)
    version_file = protocol_root / "VERSION"
    declared_version: str | None = None
    if version_file.is_file():
        declared_version = version_file.read_text(encoding="utf-8").strip() or None
    return {
        "python_api_observed_commit": python_source.commit,
        "python_api_working_tree_dirty": python_source.dirty,
        "python_api_git_metadata_available": python_source.available,
        "protocol_observed_commit": protocol_source.commit,
        "protocol_working_tree_dirty": protocol_source.dirty,
        "protocol_git_metadata_available": protocol_source.available,
        "protocol_declared_version": declared_version,
    }


def validate_protocol_compatibility(source_evidence: dict[str, object]) -> None:
    """Validate the protocol version declared by the files supplied with this checkout/ZIP."""
    declared = source_evidence.get("protocol_declared_version")
    expected = ".".join(str(value) for value in PROTOCOL_VERSION)
    if declared is not None and declared != expected:
        raise CompatibilityError(
            f"protocol VERSION mismatch: expected {expected}, supplied files declare {declared}"
        )


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


class TraceWriter:
    """Write important evidence immediately and a final accumulated JSON summary."""

    def __init__(
        self,
        output_dir: Path,
        scenario: str,
        *,
        seed: int,
        source_root: Path | None = None,
        source_evidence: dict[str, object] | None = None,
    ) -> None:
        self.source_evidence = (
            collect_source_evidence(source_root) if source_evidence is None else source_evidence
        )
        validate_protocol_compatibility(self.source_evidence)
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
            source_evidence=self.source_evidence,
            python_version=sys.version,
            operating_system=platform.platform(),
            pyserial_version=package_version("pyserial"),
            test_envelope_version=ENVELOPE_VERSION,
            application_codec_config=APPLICATION_CODEC_CONFIG,
            protocol_version=PROTOCOL_VERSION,
            compatibility_profile_id=COMPATIBILITY_PROFILE_ID,
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
            "source_evidence": self.source_evidence,
            "python_version": sys.version,
            "operating_system": platform.platform(),
            "pyserial_version": package_version("pyserial"),
            "test_envelope_version": ENVELOPE_VERSION,
            "application_codec_config": _jsonable(APPLICATION_CODEC_CONFIG),
            "protocol_version": list(PROTOCOL_VERSION),
            "compatibility_profile_id": COMPATIBILITY_PROFILE_ID,
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
