from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json

from graphblocks import (
    AdmittedToolCall,
    McpToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    canonical_dumps,
)


class McpToolAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpToolInvocation:
    binding_id: str
    tool_call_id: str
    server: str
    remote_name: str
    arguments_json: str
    arguments_digest: str
    idempotency_key: str | None = None

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "mcp",
            "binding_id": self.binding_id,
            "tool_call_id": self.tool_call_id,
            "server": self.server,
            "remote_name": self.remote_name,
            "arguments": json.loads(self.arguments_json),
            "arguments_digest": self.arguments_digest,
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
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise McpToolAdapterError("tool arguments must be canonical JSON") from error

    return McpToolInvocation(
        binding_id=resolved_tool.binding.binding_id,
        tool_call_id=admitted.call.tool_call_id,
        server=implementation.server,
        remote_name=implementation.remote_name,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        idempotency_key=admitted.idempotency_key,
    )


__all__ = [
    "McpToolAdapterError",
    "McpToolInvocation",
    "bind_mcp_tool",
    "define_mcp_tool",
    "prepare_mcp_tool_invocation",
]
