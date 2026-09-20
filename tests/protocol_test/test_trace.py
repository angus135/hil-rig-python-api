from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hilrig.protocol_test.trace import (
    CompatibilityError,
    TraceWriter,
    collect_source_evidence,
    inspect_git_source,
    validate_protocol_compatibility,
)


def test_git_source_metadata_records_commit_and_dirty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[-2:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, str(tmp_path.resolve()) + "\n", "")
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


def test_protocol_version_declared_by_supplied_files_is_compatibility_gate(tmp_path: Path) -> None:
    evidence = {"protocol_declared_version": "9.9.9", "protocol_observed_commit": "anything"}
    with pytest.raises(CompatibilityError, match="VERSION mismatch"):
        TraceWriter(tmp_path, "unit", seed=1, source_evidence=evidence)
    assert not list(tmp_path.glob("*.jsonl"))


def test_observed_protocol_commit_is_evidence_not_a_gate() -> None:
    validate_protocol_compatibility(
        {"protocol_declared_version": "0.3.0", "protocol_observed_commit": "deadbeef"}
    )


def test_zip_without_git_metadata_records_protocol_version_from_files(tmp_path: Path) -> None:
    protocol = tmp_path / "external" / "hil-rig-protocol"
    protocol.mkdir(parents=True)
    (protocol / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    evidence = collect_source_evidence(tmp_path)
    assert evidence["protocol_declared_version"] == "0.2.0"
    assert evidence["protocol_git_metadata_available"] is False
    assert evidence["protocol_observed_commit"] is None


def test_trace_summary_contains_application_compatibility_metadata(tmp_path: Path) -> None:
    trace = TraceWriter(
        tmp_path,
        "unit",
        seed=1,
        source_evidence={"protocol_declared_version": "0.3.0"},
    )
    trace.finish(passed=True, failure_reason=None, diagnostics={})
    summary = json.loads(trace.summary_path.read_text(encoding="utf-8"))
    assert summary["protocol_version"] == [0, 3, 0]
    assert summary["compatibility_profile_id"] == 0x41505032
    assert summary["application_codec_config"]["max_encoded_message_size"] == 512


@pytest.mark.parametrize("nested_path", ["ordinary", "external/hil-rig-protocol"])
def test_nested_directory_cannot_borrow_parent_revision(tmp_path: Path, nested_path: str) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Initial",
        ],
        check=True,
        capture_output=True,
    )
    nested = tmp_path / nested_path
    nested.mkdir(parents=True)
    assert inspect_git_source(tmp_path).available is True
    metadata = inspect_git_source(nested)
    assert metadata.available is False
    assert metadata.commit is None
    assert metadata.dirty is None
    if nested_path == "external/hil-rig-protocol":
        (nested / "VERSION").write_text("0.2.0\n", encoding="utf-8")
        evidence = collect_source_evidence(tmp_path)
        assert evidence["python_api_git_metadata_available"] is True
        assert evidence["protocol_git_metadata_available"] is False
        assert evidence["protocol_observed_commit"] is None
        assert evidence["protocol_working_tree_dirty"] is None
        assert evidence["protocol_declared_version"] == "0.2.0"
