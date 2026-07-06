from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from urllib.request import Request, urlopen

from graphblocks import (
    AdmittedToolCall,
    ArtifactRef,
    ContentPart,
    RemoteToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    ToolResult,
    ToolResultEvent,
    ToolResultStreamError,
    ToolResultStreamState,
    ToolResultValidationError,
    ToolSchemaRegistry,
    ToolSchemaValidationError,
    canonical_dumps,
    canonical_hash,
    validate_tool_result_for_model,
)
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
    ApplicationProtocolLog,
    ApplicationProtocolStreamState,
)
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry
from graphblocks.server import ApplicationProtocolCapabilities


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RunGraphCommand:
    graph: dict[str, object]
    inputs: dict[str, object] = field(default_factory=dict)
    run_id: str = "run-000001"
    response_id: str = "response-000001"
    turn_id: str | None = None
    release_id: str = "local"
    policy_snapshot_id: str = "local"
    occurred_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.graph, Mapping):
            raise ValueError("run graph command graph must be a JSON object")
        if not isinstance(self.inputs, Mapping):
            raise ValueError("run graph command inputs must be a JSON object")
        for field_name, value in (
            ("run_id", self.run_id),
            ("response_id", self.response_id),
            ("release_id", self.release_id),
            ("policy_snapshot_id", self.policy_snapshot_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"run graph command {field_name} must be a non-empty string")
        if self.turn_id is not None and (not isinstance(self.turn_id, str) or not self.turn_id.strip()):
            raise ValueError("run graph command turn_id must be a non-empty string")
        if not isinstance(self.occurred_at, str):
            raise ValueError("run graph command occurred_at must be a string")
        if not self.occurred_at.strip():
            raise ValueError("run graph command occurred_at must be a non-empty string")
        object.__setattr__(self, "graph", deepcopy(dict(self.graph)))
        object.__setattr__(self, "inputs", deepcopy(dict(self.inputs)))


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


class RemoteToolAdapterError(RuntimeError):
    pass


def evaluate_native_application_event_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_application_event_stream

    return evaluate_application_event_stream(state, operations)


def evaluate_native_application_protocol_log(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_application_protocol_log

    return evaluate_application_protocol_log(state, operations)


def evaluate_native_application_protocol_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_application_protocol_stream

    return evaluate_application_protocol_stream(state, operations)


def negotiate_native_application_protocol_capabilities(
    server: dict[str, object],
    client: dict[str, object],
) -> dict[str, object]:
    from graphblocks_runtime import negotiate_application_protocol_capabilities

    return negotiate_application_protocol_capabilities(server, client)


@dataclass(frozen=True, slots=True)
class RemoteToolInvocation:
    binding_id: str
    resolved_tool_id: str
    tool_name: str
    tool_call_id: str
    connection: str
    operation: str
    arguments_json: str
    arguments_digest: str
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "binding_id",
            "resolved_tool_id",
            "tool_name",
            "tool_call_id",
            "connection",
            "operation",
            "arguments_digest",
            "definition_digest",
            "binding_digest",
            "effective_policy_snapshot_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RemoteToolAdapterError(f"remote invocation {field_name} must not be empty")
            object.__setattr__(self, field_name, value.strip())
        if self.idempotency_key is not None:
            if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
                raise RemoteToolAdapterError("remote invocation idempotency_key must not be empty")
            object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        if not isinstance(self.arguments_json, str) or not self.arguments_json.strip():
            raise RemoteToolAdapterError("remote invocation arguments_json must not be empty")
        try:
            arguments = json.loads(self.arguments_json)
        except json.JSONDecodeError as error:
            raise RemoteToolAdapterError("remote invocation arguments_json must be valid JSON") from error
        if not isinstance(arguments, Mapping):
            raise RemoteToolAdapterError("remote invocation arguments_json must decode to an object")
        try:
            actual_digest = canonical_hash(arguments)
            canonical_arguments = canonical_dumps(arguments)
        except (TypeError, ValueError) as error:
            raise RemoteToolAdapterError("remote invocation arguments_json must be canonical JSON") from error
        if actual_digest != self.arguments_digest:
            raise RemoteToolAdapterError("remote invocation arguments digest does not match arguments_json")
        object.__setattr__(self, "arguments_json", canonical_arguments)

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "remote",
            "binding_id": self.binding_id,
            "resolved_tool_id": self.resolved_tool_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "connection": self.connection,
            "operation": self.operation,
            "arguments": json.loads(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
            "idempotency_key": self.idempotency_key,
        }


def define_remote_tool(
    *,
    name: str,
    description: str,
    input_schema: str,
    output_schema: str | None = None,
    tags: Iterable[str] = (),
    version: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        tags=frozenset(tags),
        version=version,
    )


def bind_remote_tool(
    *,
    binding_id: str,
    tool_name: str,
    connection: str,
    operation: str,
    effects: Iterable[str] = ("network",),
    approval: str = "policy",
    idempotency: str = "optional",
    cancellation: str = "cooperative",
    result_mode: str = "value",
    timeout_ms: int | None = None,
    retry_policy_ref: str | None = None,
    policy_profile_ref: str | None = None,
    execution_class: str | None = None,
) -> ToolBinding:
    return ToolBinding(
        binding_id=binding_id,
        tool_name=tool_name,
        implementation=RemoteToolImplementation(connection=connection, operation=operation),
        effects=frozenset(effects),
        approval=approval,
        idempotency=idempotency,
        cancellation=cancellation,
        result_mode=result_mode,
        timeout_ms=timeout_ms,
        retry_policy_ref=retry_policy_ref,
        policy_profile_ref=policy_profile_ref,
        execution_class=execution_class,
    )


def prepare_remote_tool_invocation(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    validation_time: str | None = None,
) -> RemoteToolInvocation:
    implementation = resolved_tool.binding.implementation
    if not isinstance(implementation, RemoteToolImplementation):
        raise RemoteToolAdapterError("remote tool invocation requires a remote tool binding")
    if admitted.call.status != "admitted":
        raise RemoteToolAdapterError(f"tool call {admitted.call.tool_call_id} is not admitted")
    if admitted.call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise RemoteToolAdapterError("tool call references a different resolved tool")
    if admitted.call.name != resolved_tool.definition.name:
        raise RemoteToolAdapterError("tool call name does not match resolved tool")
    _validate_resolved_tool_capability(
        admitted,
        resolved_tool,
        validation_time=validation_time,
        owner="remote",
    )
    if resolved_tool.binding.idempotency == "required" and admitted.idempotency_key is None:
        raise RemoteToolAdapterError(
            f"remote tool call {admitted.call.tool_call_id} requires an idempotency key before execution"
        )
    try:
        actual_arguments_digest = canonical_hash(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise RemoteToolAdapterError("tool arguments must be canonical JSON") from error
    if actual_arguments_digest != admitted.call.arguments_digest:
        raise RemoteToolAdapterError("tool arguments digest does not match arguments")

    try:
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise RemoteToolAdapterError("tool arguments must be canonical JSON") from error

    return RemoteToolInvocation(
        binding_id=resolved_tool.binding.binding_id,
        resolved_tool_id=resolved_tool.resolved_tool_id,
        tool_name=resolved_tool.definition.name,
        tool_call_id=admitted.call.tool_call_id,
        connection=implementation.connection,
        operation=implementation.operation,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        definition_digest=resolved_tool.definition_digest,
        binding_digest=resolved_tool.binding_digest,
        effective_policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
        idempotency_key=admitted.idempotency_key,
    )


def remote_tool_result_from_response(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    output: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if not isinstance(output, Mapping):
        raise RemoteToolAdapterError("remote tool response output must be an object")

    try:
        result = ToolResult.completed(
            admitted.call.tool_call_id,
            (
                ContentPart(
                    kind="json",
                    data=dict(output),
                    metadata={"adapter": "remote", "trust_designation": "untrusted_external"},
                ),
            ),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise RemoteToolAdapterError("remote tool result has an invalid effect outcome") from error

    prepare_remote_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    return result


def remote_tool_result_started(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    started_at: str,
) -> ToolResultEvent:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    return ToolResultEvent.started(admitted.call.tool_call_id, sequence, started_at=started_at)


def remote_tool_result_delta(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    output: Iterable[ContentPart | Mapping[str, object] | str],
) -> ToolResultEvent:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if isinstance(output, str):
        raise RemoteToolAdapterError("remote tool result delta output must be a sequence")
    try:
        raw_parts = tuple(output)
    except TypeError as error:
        raise RemoteToolAdapterError("remote tool result delta output must be a sequence") from error
    parts: list[ContentPart] = []
    for raw_part in raw_parts:
        if isinstance(raw_part, ContentPart):
            metadata = dict(raw_part.metadata)
            metadata.setdefault("adapter", "remote")
            metadata.setdefault("trust_designation", "untrusted_external")
            parts.append(replace(raw_part, metadata=metadata))
        elif isinstance(raw_part, str):
            parts.append(
                ContentPart(
                    kind="text",
                    text=raw_part,
                    metadata={"adapter": "remote", "trust_designation": "untrusted_external"},
                )
            )
        elif isinstance(raw_part, Mapping):
            kind = raw_part.get("kind")
            metadata = raw_part.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise RemoteToolAdapterError("remote tool result delta metadata must be an object")
            metadata = dict(metadata)
            metadata.setdefault("adapter", "remote")
            metadata.setdefault("trust_designation", "untrusted_external")
            if kind is None:
                kind = "text" if "text" in raw_part else "json" if "data" in raw_part else None
            if kind == "text":
                text = raw_part.get("text")
                if not isinstance(text, str):
                    raise RemoteToolAdapterError("remote text delta output requires string text")
                parts.append(ContentPart(kind="text", text=text, metadata=metadata))
            elif kind in {"json", "artifact_ref"}:
                data = raw_part.get("data")
                if not isinstance(data, Mapping):
                    raise RemoteToolAdapterError(f"remote {kind} delta output requires object data")
                parts.append(ContentPart(kind=kind, data=dict(data), metadata=metadata))  # type: ignore[arg-type]
            else:
                raise RemoteToolAdapterError(f"remote tool result delta has unknown content kind {kind!r}")
        else:
            raise RemoteToolAdapterError("remote tool result delta output entries must be content parts")
    return ToolResultEvent.delta(
        admitted.call.tool_call_id,
        sequence,
        tuple(parts),
    )


def remote_tool_result_artifact_ready(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    artifact: ArtifactRef | Mapping[str, object],
) -> ToolResultEvent:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if isinstance(artifact, ArtifactRef):
        artifact_ref = artifact
    else:
        if not isinstance(artifact, Mapping):
            raise RemoteToolAdapterError("remote tool result artifact must be an ArtifactRef or object")
        artifact_id = artifact.get("artifact_id", artifact.get("artifactId"))
        uri = artifact.get("uri")
        if not isinstance(artifact_id, str) or not isinstance(uri, str):
            raise RemoteToolAdapterError("remote tool result artifact requires artifact_id and uri")
        metadata = artifact.get("metadata", {})
        if not isinstance(metadata, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
        ):
            raise RemoteToolAdapterError("remote tool result artifact metadata must be a string object")
        optional_strings: dict[str, str | None] = {}
        for target_name, names in {
            "media_type": ("media_type", "mediaType"),
            "checksum": ("checksum",),
            "etag": ("etag",),
            "version": ("version",),
            "filename": ("filename",),
        }.items():
            optional_strings[target_name] = None
            for name in names:
                if name in artifact:
                    value = artifact[name]
                    if value is None or isinstance(value, str):
                        optional_strings[target_name] = value
                        break
                    raise RemoteToolAdapterError(f"remote tool result artifact {name} must be a string")
        size_bytes = None
        for name in ("size_bytes", "sizeBytes"):
            if name in artifact:
                value = artifact[name]
                if value is None:
                    break
                if isinstance(value, int) and not isinstance(value, bool):
                    size_bytes = value
                    break
                raise RemoteToolAdapterError(f"remote tool result artifact {name} must be an integer")
        try:
            artifact_ref = ArtifactRef(
                artifact_id=artifact_id,
                uri=uri,
                media_type=optional_strings["media_type"],
                size_bytes=size_bytes,
                checksum=optional_strings["checksum"],
                etag=optional_strings["etag"],
                version=optional_strings["version"],
                filename=optional_strings["filename"],
                metadata=dict(metadata),
            )
        except ValueError as error:
            raise RemoteToolAdapterError("remote tool result artifact is invalid") from error
    return ToolResultEvent.artifact_ready(
        admitted.call.tool_call_id,
        sequence,
        artifact_ref,
    )


def remote_tool_result_completed(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    sequence: int,
    result: ToolResult,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResultEvent:
    if result.status != "completed":
        raise RemoteToolAdapterError("remote completed event requires a completed tool result")
    prepare_remote_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    try:
        return ToolResultEvent.completed(admitted.call.tool_call_id, sequence, result)
    except ValueError as error:
        raise RemoteToolAdapterError("remote completed event is invalid") from error


def remote_tool_result_terminal_event(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    sequence: int,
    result: ToolResult,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> ToolResultEvent:
    if result.status == "completed":
        return remote_tool_result_completed(
            admitted,
            resolved_tool,
            schema_registry,
            sequence=sequence,
            result=result,
            max_output_bytes=max_output_bytes,
            redactions=redactions,
            capture_policy=capture_policy,
        )

    prepare_remote_tool_invocation(admitted, resolved_tool)
    constructors = {
        "failed": ToolResultEvent.failed,
        "denied": ToolResultEvent.denied,
        "cancelled": ToolResultEvent.cancelled,
        "policy_stopped": ToolResultEvent.policy_stopped,
        "incomplete": ToolResultEvent.incomplete,
    }
    constructor = constructors.get(result.status)
    if constructor is None:
        raise RemoteToolAdapterError(f"remote terminal event does not support result status {result.status}")
    try:
        return constructor(admitted.call.tool_call_id, sequence, result)
    except ValueError as error:
        raise RemoteToolAdapterError("remote terminal event is invalid") from error


def prepare_remote_tool_result_for_model(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    result: ToolResult,
    *,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> tuple[ContentPart, ...]:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    try:
        return validate_tool_result_for_model(
            admitted.call,
            result,
            resolved_tool,
            schema_registry,
            max_output_bytes=max_output_bytes,
            redactions=tuple(dict(redaction) for redaction in redactions),
            capture_policy=dict(capture_policy) if capture_policy is not None else None,
        )
    except (ToolResultValidationError, ToolSchemaValidationError) as error:
        raise RemoteToolAdapterError("remote tool result failed validation") from error


def remote_tool_result_from_error(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise RemoteToolAdapterError("remote tool error must be an object")

    try:
        return ToolResult.failed(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise RemoteToolAdapterError("remote tool result has an invalid effect outcome") from error


def remote_tool_result_denied(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    completed_at: str,
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise RemoteToolAdapterError("remote tool denial error must be an object")
    return ToolResult.denied(
        admitted.call.tool_call_id,
        error=dict(error),
        completed_at=completed_at,
    )


def remote_tool_result_policy_stopped(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise RemoteToolAdapterError("remote tool policy stop error must be an object")

    try:
        return ToolResult.policy_stopped(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise RemoteToolAdapterError("remote tool result has an invalid effect outcome") from error


def remote_tool_result_cancelled(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    try:
        return ToolResult.cancelled(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise RemoteToolAdapterError("remote tool result has an invalid effect outcome") from error


def remote_tool_result_incomplete(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_remote_tool_invocation(admitted, resolved_tool)
    try:
        return ToolResult.incomplete(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise RemoteToolAdapterError("remote tool result has an invalid effect outcome") from error


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
        run_id = _http_run_id(run_id)
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
        run_id = _http_run_id(run_id)
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
        return _events_from_payload(payload, "GraphBlocks run events response")

    def run_stream(self, run_id: str) -> RunStreamSnapshot:
        run_id = _http_run_id(run_id)
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
        stream_payload = _payload_object(payload, "GraphBlocks run stream response", "stream", "stream")
        events = _events_from_payload(payload, "GraphBlocks run stream response")
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunStreamSnapshot(
            run_id=_payload_string(payload, "GraphBlocks run stream response", "run_id", "runId", "run_id"),
            stream=stream_payload,
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

        events = _events_from_payload(payload, "GraphBlocks HTTP response")

        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunGraphResponse(
            run_id=_payload_string(payload, "GraphBlocks HTTP response", "run_id", "runId", "run_id"),
            status=_payload_string(payload, "GraphBlocks HTTP response", "status", "status"),
            outputs=_payload_object(payload, "GraphBlocks HTTP response", "outputs", "outputs"),
            events=tuple(events),
            event_stream=stream_state,
        )


def _http_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("GraphBlocks HTTP run_id must be a non-empty string")
    return run_id


def _read_json_response(response: object, label: str) -> dict[str, object]:
    payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    status_code = getattr(response, "status", getattr(response, "status_code", None))
    if status_code is not None:
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise ValueError(f"{label} status code must be an integer")
        if status_code < 100 or status_code > 599:
            raise ValueError(f"{label} status code must be a valid HTTP status")
        if status_code >= 400:
            raise GraphBlocksHttpError(status_code, payload)
    return payload


def _payload_string(payload: Mapping[str, object], label: str, field_name: str, *keys: str) -> str:
    value: object = None
    for key in keys:
        if key in payload:
            value = payload[key]
            break
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} {field_name} must be a non-empty string")
    return value


def _payload_object(payload: Mapping[str, object], label: str, field_name: str, *keys: str) -> dict[str, object]:
    value: object = {}
    for key in keys:
        if key in payload:
            value = payload[key]
            break
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} {field_name} must be a JSON object")
    return dict(value)


def _events_from_payload(payload: Mapping[str, object], label: str) -> tuple[ApplicationEvent, ...]:
    if "events" not in payload:
        return ()
    event_payloads = payload["events"]
    if not isinstance(event_payloads, list):
        raise ValueError(f"{label} events must be a JSON array")
    return _application_events_from_payloads(event_payloads)


def _validate_resolved_tool_capability(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    validation_time: str | None,
    owner: str,
) -> None:
    if not resolved_tool.allowed_for_principal:
        raise RemoteToolAdapterError(
            f"{owner} resolved tool {resolved_tool.definition.name} is not allowed for principal"
        )
    effective_time = validation_time if validation_time is not None else admitted.call.admitted_at
    if not isinstance(effective_time, str) or not effective_time.strip():
        raise RemoteToolAdapterError(f"{owner} tool invocation validation_time must be a non-empty string")
    if resolved_tool.valid_until is not None:
        effective_datetime = _parse_iso_datetime(
            effective_time,
            owner=owner,
            field="validation_time",
        )
        valid_until_datetime = _parse_iso_datetime(
            resolved_tool.valid_until,
            owner=owner,
            field="valid_until",
        )
        if effective_datetime > valid_until_datetime:
            raise RemoteToolAdapterError(
                f"{owner} resolved tool {resolved_tool.definition.name} expired at {resolved_tool.valid_until}"
            )


def _parse_iso_datetime(value: str, *, owner: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RemoteToolAdapterError(f"{owner} tool invocation {field} must be a non-empty ISO datetime")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RemoteToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _application_events_from_payloads(event_payloads: object) -> tuple[ApplicationEvent, ...]:
    if not isinstance(event_payloads, list):
        raise ValueError("GraphBlocks HTTP events must be a JSON array")
    events: list[ApplicationEvent] = []
    for event_payload in event_payloads:
        if not isinstance(event_payload, dict):
            raise ValueError("GraphBlocks HTTP event must be a JSON object")
        kind = event_payload.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("GraphBlocks HTTP event kind must be a non-empty string")
        metadata_payload = event_payload.get("metadata")
        if not isinstance(metadata_payload, dict):
            raise ValueError("GraphBlocks HTTP event metadata must be a JSON object")
        event_id = metadata_payload.get("eventId", metadata_payload.get("event_id"))
        run_id = metadata_payload.get("runId", metadata_payload.get("run_id"))
        response_id = metadata_payload.get("responseId", metadata_payload.get("response_id"))
        release_id = metadata_payload.get("releaseId", metadata_payload.get("release_id"))
        policy_snapshot_id = metadata_payload.get(
            "policySnapshotId",
            metadata_payload.get("policy_snapshot_id"),
        )
        occurred_at = metadata_payload.get("occurredAt", metadata_payload.get("occurred_at"))
        for field_name, value in (
            ("event_id", event_id),
            ("run_id", run_id),
            ("response_id", response_id),
            ("release_id", release_id),
            ("policy_snapshot_id", policy_snapshot_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"GraphBlocks HTTP event metadata {field_name} must be a non-empty string")
        if not isinstance(occurred_at, str):
            raise ValueError("GraphBlocks HTTP event metadata occurred_at must be a string")
        turn_id = metadata_payload.get("turnId", metadata_payload.get("turn_id"))
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
            raise ValueError("GraphBlocks HTTP event metadata turn_id must be a non-empty string")
        cursor = metadata_payload.get("cursor")
        graph_id = metadata_payload.get("graphId", metadata_payload.get("graph_id"))
        node_id = metadata_payload.get("nodeId", metadata_payload.get("node_id"))
        operation_id = metadata_payload.get("operationId", metadata_payload.get("operation_id"))
        for field_name, value in (
            ("cursor", cursor),
            ("graph_id", graph_id),
            ("node_id", node_id),
            ("operation_id", operation_id),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"GraphBlocks HTTP event metadata {field_name} must be a non-empty string")
        visibility = metadata_payload.get("visibility", "client")
        if visibility not in {"client", "operator", "internal", "audit_only"}:
            raise ValueError("GraphBlocks HTTP event metadata visibility must be a valid visibility value")
        sequence = metadata_payload.get("sequence", 0)
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("GraphBlocks HTTP event metadata sequence must be an integer")
        metadata = ApplicationEventMetadata(
            event_id=event_id,
            run_id=run_id,
            response_id=response_id,
            turn_id=turn_id,
            cursor=cursor,
            graph_id=graph_id,
            node_id=node_id,
            operation_id=operation_id,
            sequence=sequence,
            release_id=release_id,
            policy_snapshot_id=policy_snapshot_id,
            occurred_at=occurred_at,
            visibility=visibility,
        )
        event_body_payload = event_payload.get("payload", {})
        if not isinstance(event_body_payload, Mapping):
            raise ValueError("GraphBlocks HTTP event payload must be a JSON object")
        event_body = dict(event_body_payload)
        tool_call_id = event_payload.get("toolCallId", event_payload.get("tool_call_id"))
        if tool_call_id is not None:
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                raise ValueError("GraphBlocks HTTP event tool_call_id must be a non-empty string")
            events.append(
                ApplicationEvent.tool(
                    kind,
                    metadata,
                    tool_call_id=tool_call_id,
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
    "ApplicationProtocolCapabilities",
    "ApplicationProtocolLog",
    "ApplicationProtocolStreamState",
    "GraphBlocksHttpError",
    "HttpGraphBlocksClient",
    "LocalGraphBlocksClient",
    "RemoteToolAdapterError",
    "RemoteToolInvocation",
    "RunGraphCommand",
    "RunGraphResponse",
    "RunStreamSnapshot",
    "ToolResult",
    "ToolResultEvent",
    "ToolResultStreamError",
    "ToolResultStreamState",
    "bind_remote_tool",
    "define_remote_tool",
    "evaluate_native_application_event_stream",
    "evaluate_native_application_protocol_log",
    "evaluate_native_application_protocol_stream",
    "negotiate_native_application_protocol_capabilities",
    "prepare_remote_tool_invocation",
    "prepare_remote_tool_result_for_model",
    "remote_tool_result_artifact_ready",
    "remote_tool_result_cancelled",
    "remote_tool_result_completed",
    "remote_tool_result_denied",
    "remote_tool_result_delta",
    "remote_tool_result_from_error",
    "remote_tool_result_from_response",
    "remote_tool_result_incomplete",
    "remote_tool_result_policy_stopped",
    "remote_tool_result_started",
    "remote_tool_result_terminal_event",
]
