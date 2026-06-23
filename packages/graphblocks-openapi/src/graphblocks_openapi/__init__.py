from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json

from graphblocks import (
    AdmittedToolCall,
    OpenApiToolImplementation,
    ResolvedTool,
    ToolBinding,
    ToolDefinition,
    canonical_dumps,
)


class OpenApiToolAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenApiOperationInvocation:
    binding_id: str
    tool_call_id: str
    connection: str
    operation_id: str
    arguments_json: str
    arguments_digest: str
    idempotency_key: str | None = None

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "openapi",
            "binding_id": self.binding_id,
            "tool_call_id": self.tool_call_id,
            "connection": self.connection,
            "operation_id": self.operation_id,
            "arguments": json.loads(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "idempotency_key": self.idempotency_key,
        }


def define_openapi_tool(
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


def bind_openapi_operation(
    *,
    binding_id: str,
    tool_name: str,
    connection: str,
    operation_id: str,
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
        implementation=OpenApiToolImplementation(connection=connection, operation_id=operation_id),
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


def prepare_openapi_operation_invocation(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
) -> OpenApiOperationInvocation:
    implementation = resolved_tool.binding.implementation
    if not isinstance(implementation, OpenApiToolImplementation):
        raise OpenApiToolAdapterError("OpenAPI tool invocation requires an OpenAPI tool binding")
    if admitted.call.status != "admitted":
        raise OpenApiToolAdapterError(f"tool call {admitted.call.tool_call_id} is not admitted")
    if admitted.call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise OpenApiToolAdapterError("tool call references a different resolved tool")
    if admitted.call.name != resolved_tool.definition.name:
        raise OpenApiToolAdapterError("tool call name does not match resolved tool")

    try:
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise OpenApiToolAdapterError("tool arguments must be canonical JSON") from error

    return OpenApiOperationInvocation(
        binding_id=resolved_tool.binding.binding_id,
        tool_call_id=admitted.call.tool_call_id,
        connection=implementation.connection,
        operation_id=implementation.operation_id,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        idempotency_key=admitted.idempotency_key,
    )


__all__ = [
    "OpenApiOperationInvocation",
    "OpenApiToolAdapterError",
    "bind_openapi_operation",
    "define_openapi_tool",
    "prepare_openapi_operation_invocation",
]
