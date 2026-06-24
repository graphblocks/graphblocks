from __future__ import annotations

from dataclasses import dataclass, field

from graphblocks.application_event import (
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventKind,
    ApplicationEventMetadata,
    ApplicationEventStreamState,
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
        object.__setattr__(self, "graph", dict(self.graph))
        object.__setattr__(self, "inputs", dict(self.inputs))


@dataclass(frozen=True, slots=True)
class RunGraphResponse:
    run_id: str
    status: str
    outputs: dict[str, object]
    events: tuple[ApplicationEvent, ...]
    event_stream: ApplicationEventStreamState

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", dict(self.outputs))
        object.__setattr__(self, "events", tuple(self.events))


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


__all__ = [
    "STANDARD_APPLICATION_EVENT_KINDS",
    "TOOL_APPLICATION_EVENT_KINDS",
    "ApplicationEvent",
    "ApplicationEventError",
    "ApplicationEventKind",
    "ApplicationEventMetadata",
    "ApplicationEventStreamState",
    "LocalGraphBlocksClient",
    "RunGraphCommand",
    "RunGraphResponse",
]
