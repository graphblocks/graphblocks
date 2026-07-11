from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
    response_mode: str = "sync"
    occurred_at: str = field(default_factory=_utc_now_iso)

    def __post_init__(self) -> None:
        graph = _canonical_json_mapping("run graph command", "graph", self.graph)
        inputs = _canonical_json_mapping("run graph command", "inputs", self.inputs)
        for field_name, value in (
            ("run_id", self.run_id),
            ("response_id", self.response_id),
            ("release_id", self.release_id),
            ("policy_snapshot_id", self.policy_snapshot_id),
            ("response_mode", self.response_mode),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"run graph command {field_name} must be a non-empty string")
        if self.response_mode not in {"sync", "accepted", "background"}:
            raise ValueError("run graph command response_mode must be one of sync, accepted, or background")
        if self.turn_id is not None and (not isinstance(self.turn_id, str) or not self.turn_id.strip()):
            raise ValueError("run graph command turn_id must be a non-empty string")
        if not isinstance(self.occurred_at, str):
            raise ValueError("run graph command occurred_at must be a string")
        if not self.occurred_at.strip():
            raise ValueError("run graph command occurred_at must be a non-empty string")
        object.__setattr__(self, "graph", graph)
        object.__setattr__(self, "inputs", inputs)


@dataclass(frozen=True, slots=True)
class RunGraphResponse:
    run_id: str
    status: str
    outputs: dict[str, object]
    events: tuple[ApplicationEvent, ...]
    event_stream: ApplicationEventStreamState
    event_stream_url: str | None = None
    websocket_url: str | None = None
    cancel_url: str | None = None
    initial_cursor: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("run_id", "status"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"run graph response {field_name} must be a non-empty string")
        if not isinstance(self.outputs, Mapping):
            raise ValueError("run graph response outputs must be a JSON object")
        object.__setattr__(self, "outputs", deepcopy(dict(self.outputs)))
        object.__setattr__(self, "events", tuple(self.events))
        for field_name in ("event_stream_url", "websocket_url", "cancel_url", "initial_cursor"):
            value = getattr(self, field_name)
            if value is None:
                if self.status in {"accepted", "background"}:
                    raise ValueError(f"run graph response {self.status} requires {field_name}")
                continue
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"run graph response {field_name} must be a non-empty string")
            if field_name == "initial_cursor":
                _validate_run_cursor("run graph response", field_name, self.run_id, value)


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


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        redirected_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected_request is None:
            return None
        source_url = urlsplit(req.full_url)
        target_url = urlsplit(redirected_request.full_url)
        source_scheme = source_url.scheme.lower()
        target_scheme = target_url.scheme.lower()
        source_origin = (
            source_scheme,
            source_url.hostname,
            source_url.port or {"http": 80, "https": 443}.get(source_scheme),
        )
        target_origin = (
            target_scheme,
            target_url.hostname,
            target_url.port or {"http": 80, "https": 443}.get(target_scheme),
        )
        if source_origin != target_origin:
            redirected_request.remove_header("Authorization")
        return redirected_request


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
        arguments = _remote_invocation_arguments(self.arguments_json)
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
            "arguments": _remote_invocation_arguments(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
            "idempotency_key": self.idempotency_key,
        }


def _remote_invocation_arguments(arguments_json: str) -> dict[str, object]:
    try:
        arguments = json.loads(
            arguments_json,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
        )
    except (ValueError, json.JSONDecodeError) as error:
        raise RemoteToolAdapterError("remote invocation arguments_json must be valid JSON") from error
    if not isinstance(arguments, Mapping):
        raise RemoteToolAdapterError("remote invocation arguments_json must decode to an object")
    return dict(arguments)


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
            metadata["adapter"] = "remote"
            metadata["trust_designation"] = "untrusted_external"
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
            metadata["adapter"] = "remote"
            metadata["trust_designation"] = "untrusted_external"
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
        if command.response_mode != "sync":
            raise ValueError("LocalGraphBlocksClient supports only sync response_mode")
        result = InProcessRuntime(self.registry).run(command.graph, command.inputs, run_id=command.run_id)
        start_payload = result.journal.records[0].payload if result.journal.records else {}
        start_event = ApplicationEvent.new(
            "RunStarted",
            ApplicationEventMetadata(
                event_id=f"{result.run_id}:run-started",
                run_id=result.run_id,
                response_id=command.response_id,
                turn_id=command.turn_id,
                cursor=f"{result.run_id}:1",
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
                cursor=f"{result.run_id}:2",
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

    def _request_json(self, request: Request, label: str) -> dict[str, object]:
        try:
            if self.transport is None:
                response = build_opener(_SameOriginRedirectHandler()).open(request, timeout=self.timeout)
            else:
                response = self.transport(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        return _read_json_response(response, label)

    def _request_run_json(
        self,
        request: Request,
        label: str,
        expected_run_id: str,
    ) -> dict[str, object]:
        payload = self._request_json(request, label)
        _validate_response_run_id(
            label,
            expected_run_id,
            _payload_string(payload, label, "run_id", "runId", "run_id"),
        )
        return payload

    def health(self) -> dict[str, object]:
        request = Request(
            f"{self.base_url.rstrip('/')}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        return self._request_json(request, "GraphBlocks health response")

    def list_runs(self) -> dict[str, object]:
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs",
            headers=headers,
            method="GET",
        )
        return self._request_json(request, "GraphBlocks list runs response")

    def cancel_run(self, run_id: str) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/cancel",
            data=b"",
            headers=headers,
            method="POST",
        )
        return self._request_run_json(request, "GraphBlocks cancel response", requested_run_id)

    def pause_run(
        self,
        run_id: object,
        *,
        pause_kind: object = "operator",
        reason: object | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {
            "pauseKind": _http_non_empty_string("pause_kind", pause_kind),
        }
        if reason is not None:
            body["reason"] = _http_non_empty_string("reason", reason)
        return self._run_control(run_id, action="pause", body=body, label="GraphBlocks pause response")

    def resume_run(self, run_id: object) -> dict[str, object]:
        return self._run_control(run_id, action="resume", body={}, label="GraphBlocks resume response")

    def expire_run(self, run_id: object, *, reason: object | None = None) -> dict[str, object]:
        body: dict[str, object] = {}
        if reason is not None:
            body["reason"] = _http_non_empty_string("reason", reason)
        return self._run_control(run_id, action="expire", body=body, label="GraphBlocks expire response")

    def _run_control(
        self,
        run_id: object,
        *,
        action: str,
        body: Mapping[str, object],
        label: str,
    ) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/{action}",
            data=json.dumps(dict(body), separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return self._request_run_json(request, label, requested_run_id)

    def run_status(self, run_id: str) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}",
            headers=headers,
            method="GET",
        )
        return self._request_run_json(request, "GraphBlocks run status response", requested_run_id)

    def submit_async_callback(
        self,
        *,
        operation_id: object,
        callback_id: object,
        idempotency_key: object,
        payload: object,
        run_id: object | None = None,
        node_id: object | None = None,
        attempt_id: object | None = None,
        provider_operation_id: object | None = None,
    ) -> dict[str, object]:
        requested_operation_id = _http_non_empty_string("operation_id", operation_id)
        operation_id = quote(requested_operation_id, safe="")
        callback_id = _http_non_empty_string("callback_id", callback_id)
        idempotency_key = _http_non_empty_string("idempotency_key", idempotency_key)
        payload = _http_canonical_json_mapping("callback payload", payload)
        expected_payload_digest = canonical_hash(payload)
        body: dict[str, object] = {
            "callbackId": callback_id,
            "payload": payload,
        }
        requested_run_id = None
        if run_id is not None:
            requested_run_id = _http_non_empty_string("run_id", run_id)
            body["runId"] = requested_run_id
        requested_node_id = None
        if node_id is not None:
            requested_node_id = _http_non_empty_string("node_id", node_id)
            body["nodeId"] = requested_node_id
        requested_attempt_id = None
        if attempt_id is not None:
            requested_attempt_id = _http_non_empty_string("attempt_id", attempt_id)
            body["attemptId"] = requested_attempt_id
        requested_provider_operation_id = None
        if provider_operation_id is not None:
            requested_provider_operation_id = _http_non_empty_string(
                "provider_operation_id",
                provider_operation_id,
            )
            body["providerOperationId"] = requested_provider_operation_id
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "GraphBlocks-Idempotency-Key": idempotency_key,
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/callbacks/{operation_id}",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response_payload = self._request_json(request, "GraphBlocks async callback response")
        response_operation_id = _payload_string(
            response_payload,
            "GraphBlocks async callback response",
            "operation_id",
            "operationId",
            "operation_id",
        )
        if response_operation_id != requested_operation_id:
            raise ValueError(
                "GraphBlocks async callback response operation_id "
                f"must match requested operation {requested_operation_id!r}"
            )
        response_callback_id = _payload_string(
            response_payload,
            "GraphBlocks async callback response",
            "callback_id",
            "callbackId",
            "callback_id",
        )
        if response_callback_id != callback_id:
            raise ValueError(
                "GraphBlocks async callback response callback_id "
                f"must match requested callback {callback_id!r}"
            )
        response_idempotency_key = _payload_string(
            response_payload,
            "GraphBlocks async callback response",
            "idempotency_key",
            "idempotencyKey",
            "idempotency_key",
        )
        if response_idempotency_key != idempotency_key:
            raise ValueError(
                "GraphBlocks async callback response idempotency_key "
                f"must match requested idempotency key {idempotency_key!r}"
            )
        response_payload_digest = _payload_string(
            response_payload,
            "GraphBlocks async callback response",
            "payload_digest",
            "payloadDigest",
            "payload_digest",
        )
        if response_payload_digest != expected_payload_digest:
            raise ValueError(
                "GraphBlocks async callback response payload_digest "
                f"must match submitted payload {expected_payload_digest!r}"
            )
        if requested_run_id is not None:
            _validate_response_run_id(
                "GraphBlocks async callback response",
                requested_run_id,
                _payload_string(
                    response_payload,
                    "GraphBlocks async callback response",
                    "run_id",
                    "runId",
                    "run_id",
                ),
            )
        if requested_node_id is not None:
            response_node_id = _payload_string(
                response_payload,
                "GraphBlocks async callback response",
                "node_id",
                "nodeId",
                "node_id",
            )
            if response_node_id != requested_node_id:
                raise ValueError(
                    "GraphBlocks async callback response node_id "
                    f"must match requested node {requested_node_id!r}"
                )
        if requested_attempt_id is not None:
            response_attempt_id = _payload_string(
                response_payload,
                "GraphBlocks async callback response",
                "attempt_id",
                "attemptId",
                "attempt_id",
            )
            if response_attempt_id != requested_attempt_id:
                raise ValueError(
                    "GraphBlocks async callback response attempt_id "
                    f"must match requested attempt {requested_attempt_id!r}"
                )
        if requested_provider_operation_id is not None:
            response_provider_operation_id = _payload_string(
                response_payload,
                "GraphBlocks async callback response",
                "provider_operation_id",
                "providerOperationId",
                "provider_operation_id",
            )
            if response_provider_operation_id != requested_provider_operation_id:
                raise ValueError(
                    "GraphBlocks async callback response provider_operation_id "
                    f"must match requested provider operation {requested_provider_operation_id!r}"
                )
        return response_payload

    def run_events(self, run_id: str, *, cursor: object | None = None) -> tuple[ApplicationEvent, ...]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        url = f"{self.base_url.rstrip('/')}/runs/{run_id}/events"
        if cursor is not None:
            cursor = _validate_run_cursor(
                "GraphBlocks HTTP",
                "cursor",
                requested_run_id,
                _http_non_empty_string("cursor", cursor),
            )
            url = f"{url}?{urlencode({'cursor': cursor})}"
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            url,
            headers=headers,
            method="GET",
        )
        payload = self._request_json(request, "GraphBlocks run events response")
        return _events_from_payload(
            payload,
            "GraphBlocks run events response",
            expected_run_id=requested_run_id,
        )

    def run_stream(self, run_id: str) -> RunStreamSnapshot:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
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
        payload = self._request_json(request, "GraphBlocks run stream response")
        stream_payload = _payload_object(payload, "GraphBlocks run stream response", "stream", "stream")
        events = _events_from_payload(
            payload,
            "GraphBlocks run stream response",
            expected_run_id=requested_run_id,
        )
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunStreamSnapshot(
            run_id=_validate_response_run_id(
                "GraphBlocks run stream response",
                requested_run_id,
                _payload_string(payload, "GraphBlocks run stream response", "run_id", "runId", "run_id"),
            ),
            stream=stream_payload,
            events=events,
            event_stream=stream_state,
        )

    def attach_to_run(
        self,
        run_id: str,
        *,
        last_cursor: object | None = None,
        capabilities: Iterable[object] = (),
    ) -> RunStreamSnapshot:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        if last_cursor is not None:
            last_cursor = _validate_run_cursor(
                "GraphBlocks HTTP",
                "last_cursor",
                requested_run_id,
                _http_non_empty_string("last_cursor", last_cursor),
            )
        if isinstance(capabilities, str):
            raise ValueError("GraphBlocks HTTP capabilities must be a sequence")
        try:
            capability_values = tuple(capabilities)
        except TypeError as error:
            raise ValueError("GraphBlocks HTTP capabilities must be a sequence") from error
        normalized_capabilities: list[str] = []
        for capability in capability_values:
            normalized_capabilities.append(_http_non_empty_string("capability", capability))
        body: dict[str, object] = {"capabilities": normalized_capabilities}
        if last_cursor is not None:
            body["lastCursor"] = last_cursor
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/attach",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._request_json(request, "GraphBlocks attach response")
        events = _events_from_payload(
            payload,
            "GraphBlocks attach response",
            expected_run_id=requested_run_id,
        )
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunStreamSnapshot(
            run_id=_validate_response_run_id(
                "GraphBlocks attach response",
                requested_run_id,
                _payload_string(payload, "GraphBlocks attach response", "run_id", "runId", "run_id"),
            ),
            stream={key: deepcopy(value) for key, value in payload.items() if key != "events"},
            events=events,
            event_stream=stream_state,
        )

    def detach_from_run(
        self,
        run_id: str,
        *,
        client_id: object,
        reason: object | None = None,
    ) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        body: dict[str, object] = {"clientId": _http_non_empty_string("client_id", client_id)}
        if reason is not None:
            body["reason"] = _http_non_empty_string("reason", reason)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/detach",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return self._request_run_json(request, "GraphBlocks detach response", requested_run_id)

    def subscribe_events(
        self,
        run_id: str,
        *,
        subscription_id: object | None = None,
        event_filter: object | None = None,
        delivery: object,
        replay_from_cursor: object | None = None,
        failure_policy: object = "retry_then_dead_letter",
    ) -> RunStreamSnapshot:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        if event_filter is None:
            event_filter = {}
        body: dict[str, object] = {
            "eventFilter": _http_canonical_json_mapping("event_filter", event_filter),
            "delivery": _http_canonical_json_mapping("delivery", delivery),
            "failurePolicy": _http_non_empty_string("failure_policy", failure_policy),
        }
        if subscription_id is not None:
            body["subscriptionId"] = _http_non_empty_string("subscription_id", subscription_id)
        if replay_from_cursor is not None:
            body["replayFromCursor"] = _validate_run_cursor(
                "GraphBlocks HTTP",
                "replay_from_cursor",
                requested_run_id,
                _http_non_empty_string("replay_from_cursor", replay_from_cursor),
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/subscriptions",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._request_json(request, "GraphBlocks subscribe response")
        events = _events_from_payload(
            payload,
            "GraphBlocks subscribe response",
            expected_run_id=requested_run_id,
        )
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        return RunStreamSnapshot(
            run_id=_validate_response_run_id(
                "GraphBlocks subscribe response",
                requested_run_id,
                _payload_string(payload, "GraphBlocks subscribe response", "run_id", "runId", "run_id"),
            ),
            stream={key: deepcopy(value) for key, value in payload.items() if key != "events"},
            events=events,
            event_stream=stream_state,
        )

    def unsubscribe_events(self, run_id: str, subscription_id: object) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        requested_subscription_id = _http_non_empty_string("subscription_id", subscription_id)
        subscription_id = quote(requested_subscription_id, safe="")
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/subscriptions/{subscription_id}",
            headers=headers,
            method="DELETE",
        )
        payload = self._request_run_json(request, "GraphBlocks unsubscribe response", requested_run_id)
        _validate_response_subscription_id(
            "GraphBlocks unsubscribe response",
            requested_subscription_id,
            _payload_string(
                payload,
                "GraphBlocks unsubscribe response",
                "subscription_id",
                "subscriptionId",
                "subscription_id",
            ),
        )
        return payload

    def ack_event(
        self,
        run_id: str,
        subscription_id: object,
        *,
        event_id: object | None = None,
        cursor: object | None = None,
    ) -> dict[str, object]:
        requested_run_id = _http_non_empty_string("run_id", run_id)
        run_id = quote(requested_run_id, safe="")
        requested_subscription_id = _http_non_empty_string("subscription_id", subscription_id)
        subscription_id = quote(requested_subscription_id, safe="")
        if event_id is None and cursor is None:
            raise ValueError("GraphBlocks HTTP ack requires event_id or cursor")
        body: dict[str, object] = {}
        if event_id is not None:
            body["eventId"] = _http_non_empty_string("event_id", event_id)
        if cursor is not None:
            body["cursor"] = _validate_run_cursor(
                "GraphBlocks HTTP",
                "cursor",
                requested_run_id,
                _http_non_empty_string("cursor", cursor),
            )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/runs/{run_id}/subscriptions/{subscription_id}/ack",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._request_run_json(request, "GraphBlocks ack response", requested_run_id)
        _validate_response_subscription_id(
            "GraphBlocks ack response",
            requested_subscription_id,
            _payload_string(
                payload,
                "GraphBlocks ack response",
                "subscription_id",
                "subscriptionId",
                "subscription_id",
            ),
        )
        return payload

    def register_callback(
        self,
        *,
        subscription_id: object | None = None,
        scope: object,
        scope_id: object,
        event_filter: object | None = None,
        delivery: object,
        replay_from_cursor: object | None = None,
        failure_policy: object = "retry_then_dead_letter",
        dead_letter_policy: object | None = None,
    ) -> RunStreamSnapshot:
        if event_filter is None:
            event_filter = {}
        scope_value = _http_non_empty_string("scope", scope)
        scope_id_value = _http_non_empty_string("scope_id", scope_id)
        body: dict[str, object] = {
            "scope": scope_value,
            "scopeId": scope_id_value,
            "eventFilter": _http_canonical_json_mapping("event_filter", event_filter),
            "delivery": _http_canonical_json_mapping("delivery", delivery),
            "failurePolicy": _http_non_empty_string("failure_policy", failure_policy),
        }
        if subscription_id is not None:
            body["subscriptionId"] = _http_non_empty_string("subscription_id", subscription_id)
        if replay_from_cursor is not None:
            replay_cursor = _http_non_empty_string("replay_from_cursor", replay_from_cursor)
            if scope_value == "run":
                replay_cursor = _validate_run_cursor(
                    "GraphBlocks HTTP",
                    "replay_from_cursor",
                    scope_id_value,
                    replay_cursor,
                )
            body["replayFromCursor"] = replay_cursor
        if dead_letter_policy is not None:
            body["deadLetterPolicy"] = _http_non_empty_string("dead_letter_policy", dead_letter_policy)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/callbacks/register",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._request_json(request, "GraphBlocks callback registration response")
        events = _events_from_payload(
            payload,
            "GraphBlocks callback registration response",
            expected_run_id=scope_id_value if scope_value == "run" else None,
        )
        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        response_scope = _payload_string(payload, "GraphBlocks callback registration response", "scope", "scope")
        if response_scope != scope_value:
            raise ValueError(
                f"GraphBlocks callback registration response scope must match requested scope {scope_value!r}"
            )
        response_scope_id = _payload_string(
            payload,
            "GraphBlocks callback registration response",
            "scope_id",
            "scopeId",
            "scope_id",
        )
        if response_scope_id != scope_id_value:
            raise ValueError(
                "GraphBlocks callback registration response scope_id "
                f"must match requested scope id {scope_id_value!r}"
            )
        return RunStreamSnapshot(
            run_id=response_scope_id if response_scope == "run" else "",
            stream={key: deepcopy(value) for key, value in payload.items() if key != "events"},
            events=events,
            event_stream=stream_state,
        )

    def revoke_callback(self, subscription_id: object) -> dict[str, object]:
        requested_subscription_id = _http_non_empty_string("subscription_id", subscription_id)
        subscription_id = quote(requested_subscription_id, safe="")
        headers = {"Accept": "application/json"}
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/callbacks/{subscription_id}",
            headers=headers,
            method="DELETE",
        )
        payload = self._request_json(request, "GraphBlocks callback revoke response")
        _validate_response_subscription_id(
            "GraphBlocks callback revoke response",
            requested_subscription_id,
            _payload_string(
                payload,
                "GraphBlocks callback revoke response",
                "subscription_id",
                "subscriptionId",
                "subscription_id",
            ),
        )
        return payload

    def redrive_callback_delivery(
        self,
        delivery_id: object,
        *,
        reason: object,
        operator: object | None = None,
    ) -> dict[str, object]:
        return self._callback_delivery_control(
            delivery_id,
            operator=operator,
            reason=reason,
            action="redrive",
            label="GraphBlocks callback delivery redrive response",
        )

    def move_callback_to_dead_letter(
        self,
        delivery_id: object,
        *,
        reason: object,
        operator: object | None = None,
    ) -> dict[str, object]:
        return self._callback_delivery_control(
            delivery_id,
            operator=operator,
            reason=reason,
            action="dead-letter",
            label="GraphBlocks callback delivery dead-letter response",
        )

    def _callback_delivery_control(
        self,
        delivery_id: object,
        *,
        reason: object,
        operator: object | None,
        action: str,
        label: str,
    ) -> dict[str, object]:
        requested_delivery_id = _http_non_empty_string("delivery_id", delivery_id)
        delivery_id = quote(requested_delivery_id, safe="")
        body = {
            "reason": _http_non_empty_string("reason", reason),
        }
        if operator is not None:
            body["operator"] = _http_non_empty_string("operator", operator)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url.rstrip('/')}/callbacks/deliveries/{delivery_id}/{action}",
            data=json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        payload = self._request_json(request, label)
        response_delivery_id = _payload_string(payload, label, "delivery_id", "deliveryId", "delivery_id")
        if response_delivery_id != requested_delivery_id:
            raise ValueError(f"{label} delivery_id must match requested delivery {requested_delivery_id!r}")
        return payload

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
                "responseMode": command.response_mode,
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
        payload = self._request_json(request, "GraphBlocks HTTP response")

        events = _events_from_payload(
            payload,
            "GraphBlocks HTTP response",
            expected_run_id=command.run_id,
        )

        stream_state = ApplicationEventStreamState()
        for event in events:
            stream_state.accept(event)
        status = _payload_string(payload, "GraphBlocks HTTP response", "status", "status")
        run_id = _validate_response_run_id(
            "GraphBlocks HTTP response",
            command.run_id,
            _payload_string(payload, "GraphBlocks HTTP response", "run_id", "runId", "run_id"),
        )
        response_kwargs: dict[str, object] = {}
        for field_name, payload_key in (
            ("event_stream_url", "eventStream"),
            ("websocket_url", "websocket"),
            ("cancel_url", "cancel"),
            ("initial_cursor", "initialCursor"),
        ):
            value = payload.get(payload_key)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"GraphBlocks HTTP response {payload_key} must be a non-empty string")
                if payload_key == "initialCursor":
                    value = _validate_run_cursor("GraphBlocks HTTP response", payload_key, run_id, value)
                response_kwargs[field_name] = value
            elif status in {"accepted", "background"}:
                raise ValueError(f"GraphBlocks HTTP response {status} run handle requires {payload_key}")
        return RunGraphResponse(
            run_id=run_id,
            status=status,
            outputs=_payload_object(payload, "GraphBlocks HTTP response", "outputs", "outputs"),
            events=tuple(events),
            event_stream=stream_state,
            **response_kwargs,
        )


def _http_run_id(run_id: object) -> str:
    return _http_path_segment("run_id", run_id)


def _http_path_segment(field_name: str, value: object) -> str:
    return quote(_http_non_empty_string(field_name, value), safe="")


def _http_non_empty_string(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GraphBlocks HTTP {field_name} must be a non-empty string")
    return value


def _validate_run_cursor(label: str, field_name: str, run_id: str, value: str) -> str:
    prefix = f"{run_id}:"
    if not value.startswith(prefix):
        raise ValueError(f"{label} {field_name} must belong to run {run_id!r}")
    sequence_text = value[len(prefix) :]
    if not sequence_text.isdecimal():
        raise ValueError(f"{label} {field_name} must use '<run_id>:<sequence>' with a non-negative integer sequence")
    return value


def _validate_response_run_id(label: str, expected_run_id: str, value: str) -> str:
    if value != expected_run_id:
        raise ValueError(f"{label} run_id must match requested run {expected_run_id!r}")
    return value


def _validate_response_subscription_id(label: str, expected_subscription_id: str, value: str) -> str:
    if value != expected_subscription_id:
        raise ValueError(f"{label} subscription_id must match requested subscription {expected_subscription_id!r}")
    return value


def _http_canonical_json_mapping(field_name: str, value: object) -> dict[str, object]:
    return _canonical_json_mapping("GraphBlocks HTTP", field_name, value)


def _canonical_json_mapping(label: str, field_name: str, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} {field_name} must be a JSON object")
    pending_values: list[object] = [value]
    while pending_values:
        current_value = pending_values.pop()
        if isinstance(current_value, Mapping):
            for key, child_value in current_value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{label} {field_name} object keys must be non-empty strings")
                pending_values.append(child_value)
        elif isinstance(current_value, tuple):
            raise ValueError(f"{label} {field_name} arrays must be lists")
        elif isinstance(current_value, list):
            pending_values.extend(current_value)
    try:
        canonical_dumps(dict(value))
    except (TypeError, ValueError):
        raise ValueError(f"{label} {field_name} must contain canonical JSON values") from None
    return deepcopy(dict(value))


def _read_json_response(response: object, label: str) -> dict[str, object]:
    try:
        try:
            payload = json.loads(
                response.read().decode("utf-8"),
                parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            )
        except ValueError as error:
            raise ValueError(f"{label} must be valid JSON") from error
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
    finally:
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()


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


def _events_from_payload(
    payload: Mapping[str, object],
    label: str,
    *,
    expected_run_id: str | None = None,
) -> tuple[ApplicationEvent, ...]:
    if "events" not in payload:
        return ()
    event_payloads = payload["events"]
    if not isinstance(event_payloads, list):
        raise ValueError(f"{label} events must be a JSON array")
    events = _application_events_from_payloads(event_payloads)
    if expected_run_id is not None:
        for event in events:
            if event.metadata.run_id != expected_run_id:
                raise ValueError(f"{label} event run_id must match requested run {expected_run_id!r}")
    return events


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
    if normalized != value or len(normalized) <= 10 or normalized[10] != "T":
        raise RemoteToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime")
    suffix = normalized[19:]
    suffix_valid = False
    if suffix.startswith("."):
        offset_start = min(
            (
                position
                for position in (
                    suffix.find("Z"),
                    suffix.find("+"),
                    suffix.find("-"),
                )
                if position >= 0
            ),
            default=-1,
        )
        if offset_start > 1 and suffix[1:offset_start].isdigit():
            suffix = suffix[offset_start:]
    if suffix == "Z":
        suffix_valid = True
    elif (
        len(suffix) == 6
        and suffix[0] in "+-"
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
        and 0 <= int(suffix[1:3]) <= 23
        and 0 <= int(suffix[4:6]) <= 59
    ):
        suffix_valid = True
    if not suffix_valid:
        raise RemoteToolAdapterError(f"{owner} tool invocation {field} must be an ISO datetime")
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
