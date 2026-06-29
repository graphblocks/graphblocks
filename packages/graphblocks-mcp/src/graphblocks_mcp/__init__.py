from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from graphblocks import (
    AdmittedToolCall,
    ContentPart,
    McpToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    ToolResult,
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


__all__ = [
    "McpToolAdapterError",
    "McpToolInvocation",
    "bind_mcp_tool",
    "define_mcp_tool",
    "mcp_tool_result_cancelled",
    "mcp_tool_result_from_error",
    "mcp_tool_result_from_response",
    "mcp_tool_result_incomplete",
    "mcp_tool_result_policy_stopped",
    "prepare_mcp_tool_invocation",
    "prepare_mcp_tool_result_for_model",
]
