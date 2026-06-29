from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json

from graphblocks_client import ApplicationProtocolEvent


class TuiContractError(ValueError):
    """Raised when a TUI screen contract is invalid."""


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sorted_str_mapping(values: Mapping[str, object]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(dict(values).items())}


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TuiSection:
    title: str
    rows: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise TuiContractError("section title must not be empty")
        object.__setattr__(self, "rows", _sorted_str_mapping(self.rows))

    def section_contract(self) -> dict[str, object]:
        return {
            "title": self.title,
            "rows": deepcopy(dict(self.rows)),
        }


@dataclass(frozen=True, slots=True)
class TuiCommand:
    label: str
    action: str
    key: str | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise TuiContractError("command label must not be empty")
        if not self.action.strip():
            raise TuiContractError("command action must not be empty")
        if self.key is not None and not self.key.strip():
            raise TuiContractError("command key must not be empty")

    def command_contract(self) -> dict[str, str]:
        contract = {"label": self.label, "action": self.action}
        if self.key is not None:
            contract["key"] = self.key
        return contract


@dataclass(frozen=True, slots=True)
class TuiScreen:
    name: str
    title: str
    sections: tuple[TuiSection, ...] = field(default_factory=tuple)
    commands: tuple[TuiCommand, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise TuiContractError("screen name must not be empty")
        if not self.title.strip():
            raise TuiContractError("screen title must not be empty")
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "commands", tuple(self.commands))

    def screen_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "sections": [section.section_contract() for section in self.sections],
            "commands": [command.command_contract() for command in self.commands],
        }


@dataclass(frozen=True, slots=True)
class TuiSessionSnapshot:
    screens: tuple[TuiScreen, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "screens", tuple(self.screens))

    def snapshot_contract(self) -> dict[str, object]:
        return {"screens": [screen.screen_contract() for screen in self.screens]}

    def content_digest(self) -> str:
        return _content_digest(self.snapshot_contract())


@dataclass(frozen=True, slots=True)
class TuiProtocolSession:
    run_id: str
    protocol_version: str = "graphblocks.app.v1"
    status: str = "idle"
    last_event: str = ""
    last_sequence: int = -1
    assistant_text: str = ""
    assistant_state: str = "empty"
    pending_actions: tuple[str, ...] = field(default_factory=tuple)
    artifacts: tuple[str, ...] = field(default_factory=tuple)
    tool_progress: Mapping[str, object] = field(default_factory=dict)
    counters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise TuiContractError("run_id must not be empty")
        if not self.protocol_version.strip():
            raise TuiContractError("protocol_version must not be empty")
        if not self.status.strip():
            raise TuiContractError("status must not be empty")
        if self.last_sequence < -1:
            raise TuiContractError("last_sequence must be at least -1")
        object.__setattr__(self, "pending_actions", tuple(dict.fromkeys(str(item) for item in self.pending_actions)))
        object.__setattr__(self, "artifacts", tuple(dict.fromkeys(str(item) for item in self.artifacts)))
        object.__setattr__(self, "tool_progress", _sorted_str_mapping(self.tool_progress))
        object.__setattr__(
            self,
            "counters",
            {str(key): int(value) for key, value in sorted(dict(self.counters).items())},
        )

    def apply(self, event: ApplicationProtocolEvent) -> TuiProtocolSession:
        if event.metadata.run_id != self.run_id:
            raise TuiContractError("event run_id mismatch")
        if event.metadata.protocol_version != self.protocol_version:
            raise TuiContractError("event protocol_version mismatch")
        if event.metadata.sequence <= self.last_sequence:
            return self

        payload = dict(event.payload)
        status = self.status
        assistant_text = self.assistant_text
        assistant_state = self.assistant_state
        pending_actions = list(self.pending_actions)
        artifacts = list(self.artifacts)
        tool_progress = dict(self.tool_progress)
        counters = dict(self.counters)
        counters[event.kind] = counters.get(event.kind, 0) + 1

        if event.kind == "RunStarted":
            status = str(payload.get("status", "running"))
        elif event.kind == "RunCompleted":
            status = str(payload.get("status", "completed"))
        elif event.kind == "RunFailed":
            status = str(payload.get("status", "failed"))
        elif event.kind == "RunCancelled":
            status = str(payload.get("status", "cancelled"))
        elif event.kind == "BudgetExhausted":
            status = "budget_exhausted"
        elif event.kind == "ExecutionDegraded":
            status = "degraded"
        elif event.kind == "OutputCutoff":
            terminal_reason = payload.get("terminal_reason", payload.get("terminalReason"))
            status = str(terminal_reason or "cutoff")
            draft_disposition = payload.get("draft_disposition", payload.get("draftDisposition"))
            if draft_disposition == "retract":
                assistant_state = "retracted"
            elif draft_disposition == "mark_incomplete":
                assistant_state = "incomplete"

        if event.kind == "AssistantDraftStarted":
            assistant_state = "drafting"
            text = payload.get("text")
            if isinstance(text, str):
                assistant_text = text
        elif event.kind == "AssistantDraftDelta":
            assistant_state = "drafting"
            delta = payload.get("delta", payload.get("text"))
            if isinstance(delta, str):
                assistant_text += delta
        elif event.kind == "AssistantCommitted":
            assistant_state = "committed"
            text = payload.get("text")
            if isinstance(text, str):
                assistant_text = text
        elif event.kind == "AssistantIncomplete":
            assistant_state = "incomplete"
        elif event.kind == "AssistantRetracted":
            assistant_state = "retracted"

        if event.kind in {"ApprovalRequested", "ToolCallApprovalRequested", "PolicyDecisionRequired"}:
            action_id = (
                payload.get("approval_id")
                or payload.get("approvalId")
                or payload.get("tool_call_id")
                or payload.get("toolCallId")
                or payload.get("decision_id")
                or payload.get("decisionId")
                or payload.get("request_id")
                or payload.get("requestId")
            )
            if isinstance(action_id, str) and action_id.strip() and action_id not in pending_actions:
                pending_actions.append(action_id)

        if event.kind == "ArtifactReady":
            artifact = payload.get("artifact")
            artifact_id = artifact.get("artifact_id") if isinstance(artifact, dict) else payload.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id.strip() and artifact_id not in artifacts:
                artifacts.append(artifact_id)

        if event.kind == "JobProgress":
            progress_id = (
                payload.get("tool_call_id")
                or payload.get("toolCallId")
                or payload.get("job_id")
                or payload.get("jobId")
                or "progress"
            )
            if isinstance(progress_id, str) and progress_id.strip():
                summary = payload.get("message")
                if not isinstance(summary, str):
                    summary = payload.get("delta")
                if not isinstance(summary, str):
                    output = payload.get("output")
                    if isinstance(output, list):
                        text_parts = []
                        for part in output:
                            if isinstance(part, dict):
                                text = part.get("text")
                                if isinstance(text, str) and text:
                                    text_parts.append(text)
                        summary = " ".join(text_parts) if text_parts else None
                if not isinstance(summary, str) or not summary.strip():
                    sequence = payload.get("tool_result_sequence", payload.get("toolResultSequence"))
                    summary = f"sequence {sequence}" if isinstance(sequence, int) else "updated"
                tool_progress[progress_id] = summary

        return replace(
            self,
            status=status,
            last_event=event.kind,
            last_sequence=event.metadata.sequence,
            assistant_text=assistant_text,
            assistant_state=assistant_state,
            pending_actions=tuple(pending_actions),
            artifacts=tuple(artifacts),
            tool_progress=tool_progress,
            counters=counters,
        )

    def apply_all(self, events: Iterable[ApplicationProtocolEvent]) -> TuiProtocolSession:
        state = self
        for event in events:
            state = state.apply(event)
        return state


def run_status_screen(
    *,
    run_id: str,
    state: str,
    last_event: str,
    counters: Mapping[str, object] | None = None,
) -> TuiScreen:
    if not run_id.strip():
        raise TuiContractError("run_id must not be empty")
    if not state.strip():
        raise TuiContractError("state must not be empty")
    if not last_event.strip():
        raise TuiContractError("last_event must not be empty")
    return TuiScreen(
        name="run-status",
        title=f"Run {run_id}",
        sections=(
            TuiSection("State", {"state": state, "last_event": last_event}),
            TuiSection("Counters", counters or {}),
        ),
        commands=(
            TuiCommand("Refresh", "refresh", "r"),
            TuiCommand("Cancel", "cancel", "c"),
        ),
    )


def workspace_assistant_screen(state: TuiProtocolSession) -> TuiScreen:
    commands = [
        TuiCommand("Refresh", "refresh", "r"),
        TuiCommand("Cancel", "cancel", "c"),
    ]
    if state.pending_actions:
        commands.extend(
            [
                TuiCommand("Approve", "approve", "a"),
                TuiCommand("Deny", "deny", "d"),
            ]
        )
    sections = [
        TuiSection(
            "Run",
            {
                "status": state.status,
                "last_event": state.last_event,
                "sequence": state.last_sequence,
            },
        ),
        TuiSection(
            "Assistant",
            {
                "state": state.assistant_state,
                "text": state.assistant_text,
            },
        ),
        TuiSection(
            "Pending",
            {
                "actions": ", ".join(state.pending_actions),
                "artifacts": ", ".join(state.artifacts),
            },
        ),
    ]
    if state.tool_progress:
        sections.append(TuiSection("Progress", state.tool_progress))
    sections.append(TuiSection("Counters", state.counters))

    return TuiScreen(
        name="workspace-assistant",
        title=f"Workspace {state.run_id}",
        sections=tuple(sections),
        commands=tuple(commands),
    )


__all__ = [
    "TuiCommand",
    "TuiContractError",
    "TuiProtocolSession",
    "TuiScreen",
    "TuiSection",
    "TuiSessionSnapshot",
    "run_status_screen",
    "workspace_assistant_screen",
]
