from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hilrig.protocol_test.models import PROTOCOL_COMMIT
from hilrig.protocol_test.trace import (
    CompatibilityError,
    TraceWriter,
    inspect_git_source,
    validate_protocol_compatibility,
)


def test_git_source_metadata_records_commit_and_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "abc123\n", "")
        if args[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args, 0, " M tracked.py\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    metadata = inspect_git_source(tmp_path)
    assert metadata.available is True
    assert metadata.commit == "abc123"
    assert metadata.dirty is True


def test_git_source_metadata_has_explicit_unavailable_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 128, "", "not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)
    metadata = inspect_git_source(tmp_path)
    assert metadata.available is False
    assert metadata.commit is None
    assert metadata.dirty is None


def test_protocol_revision_mismatch_is_rejected_before_trace_creation(tmp_path: Path) -> None:
    evidence = {
        "protocol_expected_commit": PROTOCOL_COMMIT,
        "protocol_observed_commit": "deadbeef",
    }
    with pytest.raises(CompatibilityError, match="revision mismatch"):
        TraceWriter(tmp_path, "unit", seed=1, source_evidence=evidence)
    assert not list(tmp_path.glob("*.jsonl"))


def test_unavailable_protocol_revision_does_not_claim_observed_commit() -> None:
    evidence = {
        "protocol_expected_commit": PROTOCOL_COMMIT,
        "protocol_observed_commit": None,
    }
    validate_protocol_compatibility(evidence)
