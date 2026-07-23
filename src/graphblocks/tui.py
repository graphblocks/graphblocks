from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json

from .client import ApplicationProtocolEvent


_MAX_U64 = (1 << 64) - 1


class TuiContractError(ValueError):
    """Raised when a TUI screen contract is invalid."""


def _non_empty_string(field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise TuiContractError(f"{field_name} must be a string")
    if not value.strip():
        raise TuiContractError(f"{field_name} must not be empty")
    if value != value.strip():
        raise TuiContractError(
            f"{field_name} must not contain surrounding whitespace"
        )
    return value


def _string_tuple(field_name: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TuiContractError(f"{field_name} must be a sequence")
    normalized = tuple(
        _non_empty_string(f"{field_name} item", value)
        for value in values
    )
    return tuple(dict.fromkeys(normalized))


def _canonical_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sorted_str_mapping(values: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TuiContractError("rows must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[_non_empty_string("row key", key)] = str(value)
    return dict(sorted(normalized.items()))


def _content_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TuiSection:
    title: str
    rows: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty_string("section title", self.title)
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
        _non_empty_string("command label", self.label)
        _non_empty_string("command action", self.action)
        if self.key is not None:
            _non_empty_string("command key", self.key)

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
        _non_empty_string("screen name", self.name)
        _non_empty_string("screen title", self.title)
        if not isinstance(self.sections, (list, tuple)):
            raise TuiContractError("screen sections must be a sequence")
        if not isinstance(self.commands, (list, tuple)):
            raise TuiContractError("screen commands must be a sequence")
        sections = tuple(self.sections)
        commands = tuple(self.commands)
        if any(not isinstance(section, TuiSection) for section in sections):
            raise TuiContractError("screen sections must contain TuiSection records")
        if any(not isinstance(command, TuiCommand) for command in commands):
            raise TuiContractError("screen commands must contain TuiCommand records")
        object.__setattr__(self, "sections", sections)
        object.__setattr__(self, "commands", commands)

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
        if not isinstance(self.screens, (list, tuple)):
            raise TuiContractError("session screens must be a sequence")
        screens = tuple(self.screens)
        if any(not isinstance(screen, TuiScreen) for screen in screens):
            raise TuiContractError(
                "session screens must contain TuiScreen records"
            )
        object.__setattr__(self, "screens", screens)

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
    counters: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty_string("run_id", self.run_id)
        _non_empty_string("protocol_version", self.protocol_version)
        _non_empty_string("status", self.status)
        if not isinstance(self.last_event, str):
            raise TuiContractError("last_event must be a string")
        if self.last_event and self.last_event != self.last_event.strip():
            raise TuiContractError(
                "last_event must not contain surrounding whitespace"
            )
        if not isinstance(self.last_sequence, int) or isinstance(
            self.last_sequence,
            bool,
        ):
            raise TuiContractError("last_sequence must be an integer")
        if self.last_sequence < -1:
            raise TuiContractError("last_sequence must be at least -1")
        if self.last_sequence > _MAX_U64:
            raise TuiContractError(
                "last_sequence must fit an unsigned 64-bit integer"
            )
        if self.last_sequence == -1 and self.last_event:
            raise TuiContractError(
                "last_event must be empty when last_sequence is -1"
            )
        if self.last_sequence >= 0 and not self.last_event:
            raise TuiContractError(
                "last_event must not be empty after an event sequence"
            )
        if not isinstance(self.assistant_text, str):
            raise TuiContractError("assistant_text must be a string")
        _non_empty_string("assistant_state", self.assistant_state)
        object.__setattr__(
            self,
            "pending_actions",
            _string_tuple("pending_actions", self.pending_actions),
        )
        object.__setattr__(
            self,
            "artifacts",
            _string_tuple("artifacts", self.artifacts),
        )
        object.__setattr__(self, "tool_progress", _sorted_str_mapping(self.tool_progress))
        if not isinstance(self.counters, Mapping):
            raise TuiContractError("counters must be a mapping")
        counters: dict[str, int] = {}
        for key, value in self.counters.items():
            key_text = _non_empty_string("counter key", key)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise TuiContractError(
                    "counter values must be non-negative integers"
                )
            if value > _MAX_U64:
                raise TuiContractError(
                    "counter values must fit unsigned 64-bit integers"
                )
            counters[key_text] = value
        object.__setattr__(
            self,
            "counters",
            dict(sorted(counters.items())),
        )

    def apply(self, event: ApplicationProtocolEvent) -> TuiProtocolSession:
        if not isinstance(event, ApplicationProtocolEvent):
            raise TuiContractError(
                "event must be an ApplicationProtocolEvent"
            )
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
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "running"
            )
        elif event.kind == "RunCompleted":
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "completed"
            )
        elif event.kind == "RunFailed":
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "failed"
            )
        elif event.kind == "RunCancelled":
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "cancelled"
            )
        elif event.kind == "RunPolicyStopped":
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "policy_stopped"
            )
        elif event.kind == "RunExpired":
            event_status = payload.get("status")
            status = (
                event_status
                if isinstance(event_status, str) and event_status.strip()
                else "expired"
            )
        elif event.kind == "BudgetExhausted":
            status = "budget_exhausted"
        elif event.kind == "ExecutionDegraded":
            status = "degraded"
        elif event.kind == "OutputCutoff":
            terminal_reason = payload.get("terminal_reason", payload.get("terminalReason"))
            status = (
                terminal_reason
                if isinstance(terminal_reason, str) and terminal_reason.strip()
                else "cutoff"
            )
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
            artifact_id = artifact.get("artifact_id") if isinstance(artifact, Mapping) else payload.get("artifact_id")
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
                    if isinstance(output, (list, tuple)):
                        text_parts = []
                        for part in output:
                            if isinstance(part, Mapping):
                                text = part.get("text")
                                if isinstance(text, str) and text:
                                    text_parts.append(text)
                        summary = " ".join(text_parts) if text_parts else None
                if not isinstance(summary, str) or not summary.strip():
                    sequence = payload.get("tool_result_sequence", payload.get("toolResultSequence"))
                    summary = (
                        f"sequence {sequence}"
                        if isinstance(sequence, int) and not isinstance(sequence, bool)
                        else "updated"
                    )
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
    _non_empty_string("run_id", run_id)
    _non_empty_string("state", state)
    _non_empty_string("last_event", last_event)
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


def admission_ticket_screen(ticket: Mapping[str, object]) -> TuiScreen:
    """Project an admission ticket contract without opening a TUI session."""

    if not isinstance(ticket, Mapping):
        raise TuiContractError("admission ticket must be a mapping")
    contract = dict(ticket)

    ticket_id = contract.get("ticketId")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        raise TuiContractError("admission ticket ticketId must be a non-empty string")
    run_id = contract.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise TuiContractError("admission ticket runId must be a non-empty string")
    state = contract.get("state")
    valid_states = ("queued", "admitted", "running", "completed", "failed", "cancelled", "expired")
    if state not in valid_states:
        raise TuiContractError(
            "admission ticket state must be queued, admitted, running, completed, failed, cancelled, or expired"
        )

    limiter_id = contract.get("limiterId")
    if limiter_id is not None and (not isinstance(limiter_id, str) or not limiter_id.strip()):
        raise TuiContractError("admission ticket limiterId must be a non-empty string")
    retry_after_ms = contract.get("retryAfterMs")
    if retry_after_ms is not None and (
        not isinstance(retry_after_ms, int) or isinstance(retry_after_ms, bool) or retry_after_ms < 0
    ):
        raise TuiContractError("admission ticket retryAfterMs must be a non-negative integer")
    queue_position = contract.get("queuePosition")
    if queue_position is not None and (
        not isinstance(queue_position, int) or isinstance(queue_position, bool) or queue_position <= 0
    ):
        raise TuiContractError("admission ticket queuePosition must be a positive integer")
    queue_name = contract.get("queueName")
    if queue_name is not None and (not isinstance(queue_name, str) or not queue_name.strip()):
        raise TuiContractError("admission ticket queueName must be a non-empty string")

    ticket_rows: dict[str, object] = {"ticket_id": ticket_id}
    if limiter_id is not None:
        ticket_rows["limiter_id"] = limiter_id
    sections = [
        TuiSection("Ticket", ticket_rows),
        TuiSection("Run", {"run_id": run_id, "state": state}),
    ]
    queue_rows: dict[str, object] = {}
    if queue_name is not None:
        queue_rows["name"] = queue_name
    if queue_position is not None:
        queue_rows["position"] = queue_position
    if retry_after_ms is not None:
        queue_rows["retry_after_ms"] = retry_after_ms
    if queue_rows:
        sections.append(TuiSection("Queue", queue_rows))

    return TuiScreen(
        name="admission-ticket",
        title=f"Admission ticket {ticket_id}",
        sections=tuple(sections),
        commands=(
            TuiCommand("Refresh", "refresh", "r"),
            TuiCommand("Cancel", "cancel", "c"),
        ),
    )


def workspace_assistant_screen(state: TuiProtocolSession) -> TuiScreen:
    if not isinstance(state, TuiProtocolSession):
        raise TuiContractError("state must be a TuiProtocolSession")
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
    "admission_ticket_screen",
    "run_status_screen",
    "workspace_assistant_screen",
]
