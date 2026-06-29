from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from graphblocks import (
    AdmittedToolCall,
    ArtifactRef,
    ContentPart,
    McpToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    ToolResult,
    ToolResultEvent,
    ToolResultValidationError,
    ToolSchemaRegistry,
    ToolSchemaValidationError,
    canonical_dumps,
    canonical_hash,
    validate_tool_result_for_model,
)


class McpToolAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpToolInvocation:
    binding_id: str
    resolved_tool_id: str
    tool_name: str
    tool_call_id: str
    server: str
    remote_name: str
    arguments_json: str
    arguments_digest: str
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    idempotency_key: str | None = None

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "mcp",
            "binding_id": self.binding_id,
            "resolved_tool_id": self.resolved_tool_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "server": self.server,
            "remote_name": self.remote_name,
            "arguments": json.loads(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
            "idempotency_key": self.idempotency_key,
        }


def define_mcp_tool(
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


def bind_mcp_tool(
    *,
    binding_id: str,
    tool_name: str,
    server: str,
    remote_name: str,
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
        implementation=McpToolImplementation(server=server, remote_name=remote_name),
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


def prepare_mcp_tool_invocation(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
) -> McpToolInvocation:
    implementation = resolved_tool.binding.implementation
    if not isinstance(implementation, McpToolImplementation):
        raise McpToolAdapterError("MCP tool invocation requires an MCP tool binding")
    if admitted.call.status != "admitted":
        raise McpToolAdapterError(f"tool call {admitted.call.tool_call_id} is not admitted")
    if admitted.call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise McpToolAdapterError("tool call references a different resolved tool")
    if admitted.call.name != resolved_tool.definition.name:
        raise McpToolAdapterError("tool call name does not match resolved tool")
    try:
        actual_arguments_digest = canonical_hash(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise McpToolAdapterError("tool arguments must be canonical JSON") from error
    if actual_arguments_digest != admitted.call.arguments_digest:
        raise McpToolAdapterError("tool arguments digest does not match arguments")

    try:
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise McpToolAdapterError("tool arguments must be canonical JSON") from error

    return McpToolInvocation(
        binding_id=resolved_tool.binding.binding_id,
        resolved_tool_id=resolved_tool.resolved_tool_id,
        tool_name=resolved_tool.definition.name,
        tool_call_id=admitted.call.tool_call_id,
        server=implementation.server,
        remote_name=implementation.remote_name,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        definition_digest=resolved_tool.definition_digest,
        binding_digest=resolved_tool.binding_digest,
        effective_policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
        idempotency_key=admitted.idempotency_key,
    )


def mcp_tool_result_from_response(
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
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    if not isinstance(output, Mapping):
        raise McpToolAdapterError("MCP tool response output must be an object")

    try:
        result = ToolResult.completed(
            admitted.call.tool_call_id,
            (
                ContentPart(
                    kind="json",
                    data=dict(output),
                    metadata={"adapter": "mcp", "trust_designation": "untrusted_external"},
                ),
            ),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise McpToolAdapterError("MCP tool result has an invalid effect outcome") from error

    prepare_mcp_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    return result


def mcp_tool_result_started(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    started_at: str,
) -> ToolResultEvent:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    return ToolResultEvent.started(admitted.call.tool_call_id, sequence, started_at=started_at)


def mcp_tool_result_delta(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    output: Iterable[ContentPart | Mapping[str, object] | str],
) -> ToolResultEvent:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    return ToolResultEvent.delta(
        admitted.call.tool_call_id,
        sequence,
        _stream_content_parts(output, owner="MCP"),
    )


def mcp_tool_result_artifact_ready(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    sequence: int,
    artifact: ArtifactRef | Mapping[str, object],
) -> ToolResultEvent:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    return ToolResultEvent.artifact_ready(
        admitted.call.tool_call_id,
        sequence,
        _artifact_ref(artifact, owner="MCP"),
    )


def prepare_mcp_tool_result_for_model(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    result: ToolResult,
    *,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> tuple[ContentPart, ...]:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
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
        raise McpToolAdapterError("MCP tool result failed validation") from error


def mcp_tool_result_from_error(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise McpToolAdapterError("MCP tool error must be an object")

    try:
        return ToolResult.failed(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise McpToolAdapterError("MCP tool result has an invalid effect outcome") from error


def mcp_tool_result_policy_stopped(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise McpToolAdapterError("MCP tool policy stop error must be an object")

    try:
        return ToolResult.policy_stopped(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise McpToolAdapterError("MCP tool result has an invalid effect outcome") from error


def mcp_tool_result_cancelled(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    try:
        return ToolResult.cancelled(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise McpToolAdapterError("MCP tool result has an invalid effect outcome") from error


def mcp_tool_result_incomplete(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_mcp_tool_invocation(admitted, resolved_tool)
    try:
        return ToolResult.incomplete(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise McpToolAdapterError("MCP tool result has an invalid effect outcome") from error


def _stream_content_parts(
    output: Iterable[ContentPart | Mapping[str, object] | str],
    *,
    owner: str,
) -> tuple[ContentPart, ...]:
    if isinstance(output, str):
        raise McpToolAdapterError(f"{owner} tool result delta output must be a sequence")
    try:
        raw_parts = tuple(output)
    except TypeError as error:
        raise McpToolAdapterError(f"{owner} tool result delta output must be a sequence") from error
    parts: list[ContentPart] = []
    for raw_part in raw_parts:
        if isinstance(raw_part, ContentPart):
            parts.append(raw_part)
        elif isinstance(raw_part, str):
            parts.append(ContentPart(kind="text", text=raw_part, metadata={"adapter": "mcp"}))
        elif isinstance(raw_part, Mapping):
            parts.append(_content_part(raw_part, owner=owner))
        else:
            raise McpToolAdapterError(f"{owner} tool result delta output entries must be content parts")
    return tuple(parts)


def _content_part(raw_part: Mapping[str, object], *, owner: str) -> ContentPart:
    kind = raw_part.get("kind")
    metadata = raw_part.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise McpToolAdapterError(f"{owner} tool result delta metadata must be an object")
    if kind is None:
        kind = "text" if "text" in raw_part else "json" if "data" in raw_part else None
    if kind == "text":
        text = raw_part.get("text")
        if not isinstance(text, str):
            raise McpToolAdapterError(f"{owner} text delta output requires string text")
        return ContentPart(kind="text", text=text, metadata=dict(metadata))
    if kind in {"json", "artifact_ref"}:
        data = raw_part.get("data")
        if not isinstance(data, Mapping):
            raise McpToolAdapterError(f"{owner} {kind} delta output requires object data")
        return ContentPart(kind=kind, data=dict(data), metadata=dict(metadata))  # type: ignore[arg-type]
    raise McpToolAdapterError(f"{owner} tool result delta has unknown content kind {kind!r}")


def _artifact_ref(artifact: ArtifactRef | Mapping[str, object], *, owner: str) -> ArtifactRef:
    if isinstance(artifact, ArtifactRef):
        return artifact
    if not isinstance(artifact, Mapping):
        raise McpToolAdapterError(f"{owner} tool result artifact must be an ArtifactRef or object")
    artifact_id = artifact.get("artifact_id", artifact.get("artifactId"))
    uri = artifact.get("uri")
    if not isinstance(artifact_id, str) or not isinstance(uri, str):
        raise McpToolAdapterError(f"{owner} tool result artifact requires artifact_id and uri")
    metadata = artifact.get("metadata", {})
    if not isinstance(metadata, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items()
    ):
        raise McpToolAdapterError(f"{owner} tool result artifact metadata must be a string object")
    try:
        return ArtifactRef(
            artifact_id=artifact_id,
            uri=uri,
            media_type=_optional_string(artifact, "media_type", "mediaType", owner=owner),
            size_bytes=_optional_integer(artifact, "size_bytes", "sizeBytes", owner=owner),
            checksum=_optional_string(artifact, "checksum", owner=owner),
            etag=_optional_string(artifact, "etag", owner=owner),
            version=_optional_string(artifact, "version", owner=owner),
            filename=_optional_string(artifact, "filename", owner=owner),
            metadata=dict(metadata),
        )
    except ValueError as error:
        raise McpToolAdapterError(f"{owner} tool result artifact is invalid") from error


def _optional_string(artifact: Mapping[str, object], *names: str, owner: str) -> str | None:
    for name in names:
        if name in artifact:
            value = artifact[name]
            if value is None or isinstance(value, str):
                return value
            raise McpToolAdapterError(f"{owner} tool result artifact {name} must be a string")
    return None


def _optional_integer(artifact: Mapping[str, object], *names: str, owner: str) -> int | None:
    for name in names:
        if name in artifact:
            value = artifact[name]
            if value is None:
                return None
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise McpToolAdapterError(f"{owner} tool result artifact {name} must be an integer")
    return None


__all__ = [
    "McpToolAdapterError",
    "McpToolInvocation",
    "bind_mcp_tool",
    "define_mcp_tool",
    "mcp_tool_result_artifact_ready",
    "mcp_tool_result_cancelled",
    "mcp_tool_result_delta",
    "mcp_tool_result_from_error",
    "mcp_tool_result_from_response",
    "mcp_tool_result_incomplete",
    "mcp_tool_result_policy_stopped",
    "mcp_tool_result_started",
    "prepare_mcp_tool_invocation",
    "prepare_mcp_tool_result_for_model",
]
