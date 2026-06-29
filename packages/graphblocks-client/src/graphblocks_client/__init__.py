from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import json
from urllib.request import Request, urlopen

from graphblocks.application_event import (
    APPLICATION_COMMAND_KINDS,
    APPLICATION_PROTOCOL_EVENT_KINDS,
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ApplicationCommand,
    ApplicationCommandKind,
    ApplicationCommandMetadata,
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventKind,
    ApplicationEventMetadata,
    ApplicationEventStreamState,
    ApplicationProtocolError,
    ApplicationProtocolEvent,
    ApplicationProtocolEventKind,
    ApplicationProtocolEventMetadata,
)
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry


@dataclass(frozen=True, slots=True)
class RunGraphCommand:
    graph: dict[str, object]
    inputs: dict[str, object] = field(default_factory=dict)
    run_id: str = "run-000001"
    response_id: str = "response-000001"
    turn_id: str | None = None
    release_id: str = "local"
    policy_snapshot_id: str = "local"
    occurred_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph", deepcopy(self.graph))
        object.__setattr__(self, "inputs", deepcopy(self.inputs))


@dataclass(frozen=True, slots=True)
class RunGraphResponse:
    run_id: str
    status: str
    outputs: dict[str, object]
    events: tuple[ApplicationEvent, ...]
    event_stream: ApplicationEventStreamState

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", deepcopy(self.outputs))
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True, slots=True)
class RunStreamSnapshot:
    run_id: str
    stream: dict[str, object]
    events: tuple[ApplicationEvent, ...]
    event_stream: ApplicationEventStreamState

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream", deepcopy(self.stream))
        object.__setattr__(self, "events", tuple(self.events))


class GraphBlocksHttpError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self.payload = deepcopy(payload)
        super().__init__(f"GraphBlocks HTTP request failed with status {status_code}")


@dataclass(slots=True)
class LocalGraphBlocksClient:
    registry: RuntimeRegistry = field(default_factory=stdlib_registry)

    def run_graph(self, command: RunGraphCommand) -> RunGraphResponse:
        result = InProcessRuntime(self.registry).run(command.graph, command.inputs, run_id=command.run_id)
        start_payload = result.journal.records[0].payload if result.journal.records else {}
        start_event = ApplicationEvent.new(
            "RunStarted",
            ApplicationEventMetadata(
                event_id=f"{result.run_id}:run-started",
                run_id=result.run_id,
                response_id=command.response_id,
                turn_id=command.turn_id,
                sequence=1,
                release_id=command.release_id,
                policy_snapshot_id=command.policy_snapshot_id,
                occurred_at=command.occurred_at,
            ),
            payload={
                "status": "running",
                "graph_hash": str(start_payload.get("graphHash", "")),
            },
        )
        terminal_kind = {
            "succeeded": "RunSucceeded",
            "failed": "RunFailed",
            "cancelled": "RunCancelled",
        }[result.status]
        terminal_payload: dict[str, object]
        if result.status == "succeeded":
            terminal_payload = {"status": result.status, "outputs": dict(result.outputs)}
        elif result.status == "cancelled":
            terminal_payload = {"status": result.status, "reason": "cancelled"}
        else:
            terminal_record = result.journal.records[-1] if result.journal.records else None
            terminal_payload = {"status": result.status, "outputs": dict(result.outputs)}
            if terminal_record is not None:
                terminal_payload.update(dict(terminal_record.payload))
        terminal_event = ApplicationEvent.new(
            terminal_kind,
            ApplicationEventMetadata(
                event_id=f"{result.run_id}:run-terminal",
                run_id=result.run_id,
                response_id=command.response_id,
                turn_id=command.turn_id,
                sequence=2,
                release_id=command.release_id,
                policy_snapshot_id=command.policy_snapshot_id,
                occurred_at=command.occurred_at,
            ),
            payload=terminal_payload,
        )
        stream_state = ApplicationEventStreamState()
        stream_state.accept(start_event)
        stream_state.accept(terminal_event)
        return RunGraphResponse(
            run_id=result.run_id,
            status=result.status,
            outputs=result.outputs,
            events=(start_event, terminal_event),
            event_stream=stream_state,
        )


@dataclass(slots=True)
class HttpGraphBlocksClient:
    base_url: str
    bearer_token: str | None = None
    timeout: float = 30.0
    transport: Callable[..., object] | None = None

    def health(self) -> dict[str, object]:
        request = Request(
            f"{self.base_url.rstrip('/')}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        response = (self.transport or urlopen)(request, timeout=self.timeout)
        return _read_json_response(response, "GraphBlocks health response")

    def cancel_run(self, run_id: str) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/cancel",
            data=b"",
            headers=headers,
            method="POST",
        )
        response = (self.transport or urlopen)(request, timeout=self.timeout)
        return _read_json_response(response, "GraphBlocks cancel response")

    def run_events(self, run_id: str) -> tuple[ApplicationEvent, ...]:
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/events",
            headers=headers,
            method="GET",
        )
        response = (self.transport or urlopen)(request, timeout=self.timeout)
        payload = _read_json_response(response, "GraphBlocks run events response")
        return _application_events_from_payloads(payload.get("events", ()) or ())

    def run_stream(self, run_id: str) -> RunStreamSnapshot:
        headers = {
            "Accept": "application/json",
            "Connection": "Upgrade",
            "Upgrade": "websocket",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/stream",
            headers=headers,
            method="GET",
        )
        response = (self.transport or urlopen)(request, timeout=self.timeout)
        payload = _read_json_response(response, "GraphBlocks run stream response")
        stream_payload = payload.get("stream", {}) or {}
        if not isinstance(stream_payload, dict):
            raise ValueError("GraphBlocks run stream metadata must be a JSON object")
        events = _application_events_from_payloads(payload.get("events", ()) or ())
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunStreamSnapshot(
            run_id=str(payload.get("runId", payload.get("run_id", ""))),
            stream=dict(stream_payload),
            events=events,
            event_stream=stream_state,
        )

    def run_graph(self, command: RunGraphCommand) -> RunGraphResponse:
        body = json.dumps(
            {
                "graph": command.graph,
                "inputs": command.inputs,
                "runId": command.run_id,
                "responseId": command.response_id,
                "turnId": command.turn_id,
                "releaseId": command.release_id,
                "policySnapshotId": command.policy_snapshot_id,
                "occurredAt": command.occurred_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs",
            data=body,
            headers=headers,
            method="POST",
        )
        response = (self.transport or urlopen)(request, timeout=self.timeout)
        payload = _read_json_response(response, "GraphBlocks HTTP response")

        events = _application_events_from_payloads(payload.get("events", ()) or ())

        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunGraphResponse(
            run_id=str(payload.get("runId", payload.get("run_id", ""))),
            status=str(payload.get("status", "")),
            outputs=dict(payload.get("outputs", {}) or {}),
            events=tuple(events),
            event_stream=stream_state,
        )


def _read_json_response(response: object, label: str) -> dict[str, object]:
    payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    status_code = getattr(response, "status", getattr(response, "status_code", None))
    if status_code is not None and int(status_code) >= 400:
        raise GraphBlocksHttpError(int(status_code), payload)
    return payload


def _application_events_from_payloads(event_payloads: object) -> tuple[ApplicationEvent, ...]:
    events: list[ApplicationEvent] = []
    for event_payload in event_payloads:
        if not isinstance(event_payload, dict):
            raise ValueError("GraphBlocks HTTP event must be a JSON object")
        metadata_payload = event_payload.get("metadata")
        if not isinstance(metadata_payload, dict):
            raise ValueError("GraphBlocks HTTP event metadata must be a JSON object")
        metadata = ApplicationEventMetadata(
            event_id=str(metadata_payload.get("eventId", metadata_payload.get("event_id"))),
            run_id=str(metadata_payload.get("runId", metadata_payload.get("run_id"))),
            response_id=str(metadata_payload.get("responseId", metadata_payload.get("response_id"))),
            turn_id=(
                str(metadata_payload.get("turnId", metadata_payload.get("turn_id")))
                if metadata_payload.get("turnId", metadata_payload.get("turn_id")) is not None
                else None
            ),
            sequence=int(metadata_payload.get("sequence", 0)),
            release_id=str(metadata_payload.get("releaseId", metadata_payload.get("release_id"))),
            policy_snapshot_id=str(
                metadata_payload.get("policySnapshotId", metadata_payload.get("policy_snapshot_id"))
            ),
            occurred_at=str(metadata_payload.get("occurredAt", metadata_payload.get("occurred_at"))),
        )
        kind = str(event_payload.get("kind"))
        event_body = dict(event_payload.get("payload", {}) or {})
        tool_call_id = event_payload.get("toolCallId", event_payload.get("tool_call_id"))
        if tool_call_id is not None:
            events.append(
                ApplicationEvent.tool(
                    kind,
                    metadata,
                    tool_call_id=str(tool_call_id),
                    payload=event_body,
                )
            )
        else:
            events.append(ApplicationEvent.new(kind, metadata, payload=event_body))
    return tuple(events)


__all__ = [
    "APPLICATION_COMMAND_KINDS",
    "APPLICATION_PROTOCOL_EVENT_KINDS",
    "STANDARD_APPLICATION_EVENT_KINDS",
    "TOOL_APPLICATION_EVENT_KINDS",
    "ApplicationCommand",
    "ApplicationCommandKind",
    "ApplicationCommandMetadata",
    "ApplicationEvent",
    "ApplicationEventError",
    "ApplicationEventKind",
    "ApplicationEventMetadata",
    "ApplicationEventStreamState",
    "ApplicationProtocolError",
    "ApplicationProtocolEvent",
    "ApplicationProtocolEventKind",
    "ApplicationProtocolEventMetadata",
    "GraphBlocksHttpError",
    "HttpGraphBlocksClient",
    "LocalGraphBlocksClient",
    "RunGraphCommand",
    "RunGraphResponse",
    "RunStreamSnapshot",
]
