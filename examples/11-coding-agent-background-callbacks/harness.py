from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from graphblocks.approval import ApprovalRecord, ApprovalRequest
from graphblocks.canonical import canonical_hash
from graphblocks.evaluation import ResourceSnapshotRef
from graphblocks.policy import PrincipalRef


@dataclass(frozen=True, slots=True)
class InstructionDocument:
    path: str
    content: str

    def evidence(self) -> dict[str, str]:
        return {
            "contentDigest": canonical_hash(self.content),
            "path": self.path,
        }


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def discover_instructions(
    workspace_root: Path,
    working_directory: Path,
) -> tuple[InstructionDocument, ...]:
    root = workspace_root.resolve()
    current = (
        working_directory.resolve()
        if working_directory.is_absolute()
        else (root / working_directory).resolve()
    )
    if not root.is_dir() or not current.is_dir() or not _inside(root, current):
        raise ValueError("OpenCode instruction discovery must stay inside the workspace")

    directories = [current]
    while directories[-1] != root:
        parent = directories[-1].parent
        if parent == directories[-1] or not _inside(root, parent):
            raise ValueError("working directory does not descend from the workspace")
        directories.append(parent)

    paths: list[Path] = []
    for directory in directories:
        agents = directory / "AGENTS.md"
        claude = directory / "CLAUDE.md"
        selected = agents if agents.is_file() else claude if claude.is_file() else None
        if selected is not None:
            resolved = selected.resolve()
            if not _inside(root, resolved):
                raise ValueError("discovered instruction escapes the workspace")
            paths.append(resolved)
            break

    config_path = root / "opencode.json"
    if config_path.is_file():
        config_path = config_path.resolve()
        if not _inside(root, config_path):
            raise ValueError("opencode.json escapes the workspace")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        patterns = config.get("instructions", [])
        if not isinstance(patterns, list) or any(
            not isinstance(pattern, str) for pattern in patterns
        ):
            raise ValueError("opencode.json instructions must be a list of strings")
        for pattern in patterns:
            if pattern.startswith(("http://", "https://")):
                raise ValueError("the offline example does not fetch remote instructions")
            matches = sorted(root.glob(pattern))
            for match in matches:
                resolved = match.resolve()
                if not _inside(root, resolved):
                    raise ValueError("configured OpenCode instruction escapes the workspace")
                if resolved.is_file() and resolved not in paths:
                    paths.append(resolved)

    return tuple(
        InstructionDocument(
            path=path.relative_to(root).as_posix(),
            content=path.read_text(encoding="utf-8"),
        )
        for path in paths
    )


class OpenCodeHarness:
    def __init__(
        self,
        workspace_root: Path,
        *,
        snapshot: ResourceSnapshotRef,
        reader: Callable[[Path], str] | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.snapshot = snapshot
        self.reader = reader or (lambda path: path.read_text(encoding="utf-8"))
        self.events: list[dict[str, object]] = []
        self.tool_calls: list[dict[str, object]] = []
        self._always_allowed_directories: set[Path] = set()
        self._consumed_approvals: set[str] = set()

    def emit(self, kind: str, **payload: object) -> None:
        self.events.append({"sequence": len(self.events) + 1, "kind": kind, **payload})

    def _logical_path(self, target: Path) -> str:
        return Path(os.path.relpath(target, self.workspace_root)).as_posix()

    def read(
        self,
        call_id: str,
        raw_path: str,
        *,
        approval: ApprovalRecord | None = None,
    ) -> tuple[dict[str, object], ApprovalRequest | None]:
        target = (
            Path(raw_path).resolve()
            if Path(raw_path).is_absolute()
            else (self.workspace_root / raw_path).resolve()
        )
        logical_path = self._logical_path(target)
        if not any(call["callId"] == call_id for call in self.tool_calls):
            self.tool_calls.append({"callId": call_id, "tool": "read", "path": logical_path})
            self.emit(
                "message.part.updated",
                callId=call_id,
                tool="read",
                state="pending",
            )

        if target.name != ".env.example" and (
            target.name == ".env"
            or target.name.endswith(".env")
            or target.name.startswith(".env.")
        ):
            self.emit(
                "message.part.updated",
                callId=call_id,
                tool="read",
                state="error",
                error="permission denied by read pattern",
            )
            return {
                "callId": call_id,
                "permission": "read",
                "state": "error",
            }, None

        if not _inside(self.workspace_root, target):
            if target.parent in self._always_allowed_directories:
                approval = None
            else:
                arguments = {"callId": call_id, "path": logical_path, "tool": "read"}
                request = ApprovalRequest.from_arguments(
                    f"approval-{call_id}",
                    run_id="run-opencode-harness-001",
                    subject=self.snapshot,
                    action="filesystem.read",
                    arguments=arguments,
                    risk="external_directory",
                    summary=f"Read outside the project worktree: {logical_path}",
                    expires_at="2026-07-13T01:00:00Z",
                    metadata={
                        "choices": ("once", "always", "reject"),
                        "patterns": (str(Path(logical_path).parent / "*"),),
                    },
                )
                if approval is None:
                    self.emit(
                        "permission.asked",
                        approvalId=request.approval_id,
                        callId=call_id,
                        permission="external_directory",
                        choices=["once", "always", "reject"],
                    )
                    return {
                        "callId": call_id,
                        "permission": "external_directory",
                        "state": "pending_approval",
                    }, request
                same_request = (
                    approval.approval_id == request.approval_id
                    and approval.request.run_id == request.run_id
                    and approval.request.action == request.action
                    and approval.request.risk == request.risk
                    and approval.request.arguments_digest == request.arguments_digest
                    and approval.request.subject == request.subject
                )
                response = approval.metadata.get("response")
                if not same_request:
                    self.emit(
                        "message.part.updated",
                        callId=call_id,
                        tool="read",
                        state="error",
                        error="approval does not match tool request",
                    )
                    return {
                        "callId": call_id,
                        "permission": "external_directory",
                        "state": "error",
                    }, request
                if approval.status == "denied" or response == "reject":
                    self.emit(
                        "permission.replied",
                        approvalId=request.approval_id,
                        callId=call_id,
                        response="reject",
                    )
                    self.emit(
                        "message.part.updated",
                        callId=call_id,
                        tool="read",
                        state="error",
                        error="permission rejected",
                    )
                    return {
                        "callId": call_id,
                        "permission": "external_directory",
                        "state": "error",
                    }, request
                if (
                    response not in {"once", "always"}
                    or approval.approval_id in self._consumed_approvals
                    or not approval.is_valid_for(
                        self.snapshot,
                        request.arguments_digest,
                        now="2026-07-13T00:02:00Z",
                    )
                ):
                    self.emit(
                        "message.part.updated",
                        callId=call_id,
                        tool="read",
                        state="error",
                        error="approval does not match tool request",
                    )
                    return {
                        "callId": call_id,
                        "permission": "external_directory",
                        "state": "error",
                    }, request
                self._consumed_approvals.add(approval.approval_id)
                if response == "always":
                    self._always_allowed_directories.add(target.parent)
                self.emit(
                    "permission.replied",
                    approvalId=request.approval_id,
                    callId=call_id,
                    response=response,
                )

            resolved_before_execution = (
                Path(raw_path).resolve()
                if Path(raw_path).is_absolute()
                else (self.workspace_root / raw_path).resolve()
            )
            if resolved_before_execution != target:
                self.emit(
                    "message.part.updated",
                    callId=call_id,
                    tool="read",
                    state="error",
                    error="tool target changed after permission evaluation",
                )
                return {
                    "callId": call_id,
                    "permission": "external_directory",
                    "state": "error",
                }, None
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="read",
            state="running",
        )
        content = self.reader(target)
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="read",
            state="completed",
        )
        return {
            "bytes": len(content.encode("utf-8")),
            "callId": call_id,
            "permission": "external_directory"
            if not _inside(self.workspace_root, target)
            else "read",
            "state": "completed",
        }, None

    def edit(self, call_id: str, raw_path: str, old: str, new: str) -> dict[str, object]:
        target = (self.workspace_root / raw_path).resolve()
        if not _inside(self.workspace_root, target):
            raise ValueError("edit requires an external_directory approval")
        self.tool_calls.append({"callId": call_id, "tool": "edit", "path": raw_path})
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="edit",
            state="pending",
        )
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="edit",
            state="running",
        )
        content = target.read_text(encoding="utf-8")
        if content.count(old) != 1:
            raise ValueError("edit requires one exact match")
        target.write_text(content.replace(old, new), encoding="utf-8")
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="edit",
            state="completed",
        )
        return {"callId": call_id, "state": "completed"}

    def check_python(self, call_id: str, raw_path: str) -> dict[str, object]:
        target = (self.workspace_root / raw_path).resolve()
        if not _inside(self.workspace_root, target):
            raise ValueError("bash check requires an external_directory approval")
        command = f"python -c compile-file {raw_path}"
        self.tool_calls.append({"callId": call_id, "tool": "bash", "command": command})
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="bash",
            state="pending",
        )
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="bash",
            state="running",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "source = Path(sys.argv[1]).read_text(encoding='utf-8'); "
                    "compile(source, sys.argv[1], 'exec')"
                ),
                raw_path,
            ],
            cwd=self.workspace_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "Python syntax check failed")
        self.emit(
            "message.part.updated",
            callId=call_id,
            tool="bash",
            state="completed",
        )
        return {"callId": call_id, "command": command, "state": "completed"}


def run_harness(fixture_root: Path) -> dict[str, object]:
    with TemporaryDirectory(prefix="graphblocks-opencode-harness-") as temporary:
        copied_fixture = Path(temporary) / "fixture"
        shutil.copytree(fixture_root, copied_fixture)
        workspace = copied_fixture / "workspace"
        instructions = discover_instructions(workspace, Path("packages/demo"))

        before_files: dict[str, str] = {}
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            logical_path = path.relative_to(workspace).as_posix()
            resolved = path.resolve()
            if not _inside(workspace, resolved):
                before_files[logical_path] = "external"
            elif path.name == ".env" or path.name.endswith(".env") or path.name.startswith(
                ".env."
            ):
                before_files[logical_path] = "redacted"
            else:
                before_files[logical_path] = "sha256:" + hashlib.sha256(
                    resolved.read_bytes()
                ).hexdigest()
        snapshot = ResourceSnapshotRef(
            resource_id="fixture-workspace",
            digest=canonical_hash(before_files),
            resource_kind="workspace",
            metadata={"working_directory": "packages/demo"},
        )
        outside_reads: list[str] = []

        def fixture_reader(path: Path) -> str:
            if not _inside(workspace, path):
                outside_reads.append(Path(os.path.relpath(path, workspace)).as_posix())
            return path.read_text(encoding="utf-8")

        harness = OpenCodeHarness(workspace, snapshot=snapshot, reader=fixture_reader)
        harness.emit("session.status", status="busy")
        harness.emit("step.started", snapshotDigest=snapshot.digest)
        harness.emit(
            "instructions.loaded",
            paths=[instruction.path for instruction in instructions],
        )

        scripted_turns = [
            {
                "callId": "tool-001",
                "tool": "read",
                "arguments": {"path": "packages/demo/main.py"},
            },
            {
                "callId": "tool-002",
                "tool": "read",
                "arguments": {"path": ".env"},
            },
            {
                "callId": "tool-003",
                "tool": "read",
                "arguments": {"path": "../external/reference.md"},
            },
            {
                "callId": "tool-004",
                "tool": "edit",
                "arguments": {
                    "path": "packages/demo/main.py",
                    "old": 'return "before"',
                    "new": 'return "after"',
                },
            },
            {
                "callId": "tool-005",
                "tool": "bash",
                "arguments": {"path": "packages/demo/main.py"},
            },
            {"text": "Implemented and verified the requested change.", "type": "final"},
        ]
        tool_results: list[dict[str, object]] = []
        request: ApprovalRequest | None = None
        approval: ApprovalRecord | None = None
        outside_read_before_approval = False
        external_read: dict[str, object] = {}
        check: dict[str, object] = {}
        for turn_number, turn in enumerate(scripted_turns, start=1):
            harness.emit(
                "model.turn",
                turn=turn_number,
                output="final" if turn.get("type") == "final" else "tool_call",
            )
            if turn.get("type") == "final":
                harness.emit(
                    "message.part.updated",
                    role="assistant",
                    state="completed",
                    textDigest=canonical_hash(str(turn["text"])),
                )
                break

            call_id = str(turn["callId"])
            tool = str(turn["tool"])
            arguments = turn["arguments"]
            if not isinstance(arguments, dict):
                raise TypeError("scripted tool arguments must be a mapping")
            if tool == "read":
                result, pending_request = harness.read(
                    call_id,
                    str(arguments["path"]),
                )
                tool_results.append(result)
                if pending_request is not None:
                    request = pending_request
                    outside_read_before_approval = bool(outside_reads)
                    approval = ApprovalRecord.approve(
                        request,
                        approver=PrincipalRef("fixture-user"),
                        decided_at="2026-07-13T00:01:00Z",
                        metadata={"response": "once"},
                    )
                    external_read, _ = harness.read(
                        call_id,
                        str(arguments["path"]),
                        approval=approval,
                    )
                    tool_results.append(external_read)
                    result = external_read
            elif tool == "edit":
                result = harness.edit(
                    call_id,
                    str(arguments["path"]),
                    str(arguments["old"]),
                    str(arguments["new"]),
                )
                tool_results.append(result)
            elif tool == "bash":
                check = harness.check_python(call_id, str(arguments["path"]))
                result = check
                tool_results.append(result)
            else:
                raise ValueError(f"unsupported scripted tool {tool!r}")
            harness.emit(
                "tool.result",
                callId=call_id,
                state=result["state"],
                turn=turn_number,
            )

        if request is None or approval is None:
            raise AssertionError("external fixture read must request approval")
        same_path_read, _ = harness.read(
            "tool-003-repeat",
            "../external/reference.md",
            approval=approval,
        )
        changed_path_read, _ = harness.read(
            "tool-003-replay",
            "../external/other.md",
            approval=approval,
        )
        tool_results.extend((same_path_read, changed_path_read))
        same_path_reuse_rejected = same_path_read["state"] == "error"
        changed_path_reuse_rejected = changed_path_read["state"] == "error"

        after_files: dict[str, str] = {}
        for path in sorted(workspace.rglob("*")):
            if not path.is_file():
                continue
            logical_path = path.relative_to(workspace).as_posix()
            resolved = path.resolve()
            if not _inside(workspace, resolved):
                after_files[logical_path] = "external"
            elif path.name == ".env" or path.name.endswith(".env") or path.name.startswith(
                ".env."
            ):
                after_files[logical_path] = "redacted"
            else:
                after_files[logical_path] = "sha256:" + hashlib.sha256(
                    resolved.read_bytes()
                ).hexdigest()
        changed_files = sorted(
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        harness.emit("patch.created", files=changed_files)
        harness.emit("step.finished", status="completed")
        harness.emit("session.status", status="idle")

        evidence: dict[str, object] = {
            "approval": {
                "argumentsDigest": request.arguments_digest,
                "changedPathReuseRejected": changed_path_reuse_rejected,
                "choices": ["once", "always", "reject"],
                "outsideReadBeforeApproval": outside_read_before_approval,
                "outsideReadCount": len(outside_reads),
                "permission": request.risk,
                "resumed": external_read["state"] == "completed",
                "samePathReuseRejected": same_path_reuse_rejected,
                "status": approval.status,
            },
            "effectiveInstructionsDigest": canonical_hash(
                [
                    {"content": instruction.content, "path": instruction.path}
                    for instruction in instructions
                ]
            ),
            "events": harness.events,
            "instructions": [instruction.evidence() for instruction in instructions],
            "result": {
                "changedFiles": changed_files,
                "check": check["state"],
                "finalStatus": "completed",
                "modelTurns": len(scripted_turns),
            },
            "toolCallOrder": [call["callId"] for call in harness.tool_calls],
            "toolResults": tool_results,
        }
        return {**evidence, "evidenceDigest": canonical_hash(evidence)}
