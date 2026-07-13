from __future__ import annotations

from pathlib import Path
import sys

import pytest

from graphblocks.approval import ApprovalRecord
from graphblocks.evaluation import ResourceSnapshotRef
from graphblocks.policy import PrincipalRef


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "examples"))
sys.path.insert(0, str(EXAMPLE_ROOT))

from _test_support import assert_example_runner
from harness import OpenCodeHarness, discover_instructions, run_harness


def test_opencode_instruction_discovery_uses_closest_rule_and_config() -> None:
    workspace = EXAMPLE_ROOT / "fixtures" / "workspace"
    instructions = discover_instructions(workspace, Path("packages/demo"))

    assert [instruction.path for instruction in instructions] == [
        "packages/demo/AGENTS.md",
        "docs/development.md",
    ]
    assert "Keep Python functions typed" in instructions[0].content
    assert all(instruction.path != "AGENTS.md" for instruction in instructions)

    with pytest.raises(ValueError, match="stay inside the workspace"):
        discover_instructions(workspace, workspace.parent)


def test_opencode_instruction_discovery_falls_back_to_claude_md(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    working_directory = workspace / "src"
    working_directory.mkdir(parents=True)
    (workspace / "CLAUDE.md").write_text("Use the fallback.\n", encoding="utf-8")

    instructions = discover_instructions(workspace, Path("src"))

    assert [(item.path, item.content) for item in instructions] == [
        ("CLAUDE.md", "Use the fallback.\n")
    ]


def test_opencode_always_and_reject_permission_responses(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed_external = tmp_path / "allowed"
    rejected_external = tmp_path / "rejected"
    workspace.mkdir()
    allowed_external.mkdir()
    rejected_external.mkdir()
    first = allowed_external / "first.md"
    second = allowed_external / "second.md"
    denied = rejected_external / "denied.md"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    denied.write_text("denied\n", encoding="utf-8")
    reads: list[Path] = []
    snapshot = ResourceSnapshotRef(
        resource_id="test-workspace",
        digest="sha256:" + ("a" * 64),
        resource_kind="workspace",
    )
    harness = OpenCodeHarness(
        workspace,
        snapshot=snapshot,
        reader=lambda path: reads.append(path) or path.read_text(encoding="utf-8"),
    )

    _, always_request = harness.read("always-1", str(first))
    assert always_request is not None
    always = ApprovalRecord.approve(
        always_request,
        approver=PrincipalRef("user-1"),
        decided_at="2026-07-13T00:01:00Z",
        metadata={"response": "always"},
    )
    allowed, _ = harness.read("always-1", str(first), approval=always)
    inherited, inherited_request = harness.read("always-2", str(second))
    _, reject_request = harness.read("reject-1", str(denied))
    assert reject_request is not None
    rejection = ApprovalRecord.deny(
        reject_request,
        approver=PrincipalRef("user-1"),
        decided_at="2026-07-13T00:01:00Z",
        reason="Not needed for this task",
    )
    rejected, _ = harness.read("reject-1", str(denied), approval=rejection)

    assert allowed["state"] == inherited["state"] == "completed"
    assert inherited_request is None
    assert rejected["state"] == "error"
    assert reads == [first.resolve(), second.resolve()]


def test_opencode_approval_rejects_a_changed_symlink_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    first = external / "first.md"
    second = external / "second.md"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    link = workspace / "reference.md"
    try:
        link.symlink_to(first)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    reads: list[Path] = []
    snapshot = ResourceSnapshotRef(
        resource_id="symlink-workspace",
        digest="sha256:" + ("b" * 64),
        resource_kind="workspace",
    )
    harness = OpenCodeHarness(
        workspace,
        snapshot=snapshot,
        reader=lambda path: reads.append(path) or path.read_text(encoding="utf-8"),
    )
    _, request = harness.read("symlink-read", "reference.md")
    assert request is not None
    approval = ApprovalRecord.approve(
        request,
        approver=PrincipalRef("user-1"),
        decided_at="2026-07-13T00:01:00Z",
        metadata={"response": "once"},
    )
    link.unlink()
    link.symlink_to(second)

    result, _ = harness.read("symlink-read", "reference.md", approval=approval)

    assert result["state"] == "error"
    assert reads == []


def test_opencode_harness_requires_exact_external_path_approval() -> None:
    harness = run_harness(EXAMPLE_ROOT / "fixtures")

    assert harness["approval"] == {
        "argumentsDigest": "sha256:b9bf96c97e682ccb1a8c51f1ae39559ef4a37bb52cb87ff0424e79a9a368b98a",
        "changedPathReuseRejected": True,
        "choices": ["once", "always", "reject"],
        "outsideReadBeforeApproval": False,
        "outsideReadCount": 1,
        "permission": "external_directory",
        "resumed": True,
        "samePathReuseRejected": True,
        "status": "approved",
    }
    assert harness["toolCallOrder"] == [
        "tool-001",
        "tool-002",
        "tool-003",
        "tool-004",
        "tool-005",
        "tool-003-repeat",
        "tool-003-replay",
    ]
    assert harness["toolResults"][1] == {
        "callId": "tool-002",
        "permission": "read",
        "state": "error",
    }
    assert harness["result"] == {
        "changedFiles": ["packages/demo/main.py"],
        "check": "completed",
        "finalStatus": "completed",
        "modelTurns": 6,
    }
    events = harness["events"]
    asked = next(
        index for index, event in enumerate(events) if event["kind"] == "permission.asked"
    )
    replied = next(
        index for index, event in enumerate(events) if event["kind"] == "permission.replied"
    )
    external_running = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "message.part.updated"
        and event.get("callId") == "tool-003"
        and event.get("state") == "running"
    )
    assert asked < replied < external_running


def test_coding_agent_background_callbacks_example() -> None:
    payload = assert_example_runner(
        Path(__file__).with_name("run.py"),
        expected_checks={
            "acceptance:accepted invocation handle check",
            "acceptance:cursor replay after detach",
            "acceptance:callback journal-before-resume check",
            "acceptance:signed webhook delivery check",
        },
        expected_boundaries={"CI callback", "secret resolver", "webhook transport"},
    )
    harness = payload["harness"]
    assert [instruction["path"] for instruction in harness["instructions"]] == [
        "packages/demo/AGENTS.md",
        "docs/development.md",
    ]
    assert harness["approval"]["outsideReadBeforeApproval"] is False
    assert harness["approval"]["resumed"] is True
    assert harness["result"]["finalStatus"] == "completed"
    assert str(harness["evidenceDigest"]).startswith("sha256:")
