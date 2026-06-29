from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json

from graphblocks import (
    AdmittedToolCall,
    ContentPart,
    OpenApiToolImplementation,
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


class OpenApiToolAdapterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpenApiOperationInvocation:
    binding_id: str
    resolved_tool_id: str
    tool_call_id: str
    connection: str
    operation_id: str
    arguments_json: str
    arguments_digest: str
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    idempotency_key: str | None = None

    def request_contract(self) -> dict[str, object]:
        return {
            "kind": "openapi",
            "binding_id": self.binding_id,
            "resolved_tool_id": self.resolved_tool_id,
            "tool_call_id": self.tool_call_id,
            "connection": self.connection,
            "operation_id": self.operation_id,
            "arguments": json.loads(self.arguments_json),
            "arguments_digest": self.arguments_digest,
            "definition_digest": self.definition_digest,
            "binding_digest": self.binding_digest,
            "effective_policy_snapshot_id": self.effective_policy_snapshot_id,
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
        actual_arguments_digest = canonical_hash(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise OpenApiToolAdapterError("tool arguments must be canonical JSON") from error
    if actual_arguments_digest != admitted.call.arguments_digest:
        raise OpenApiToolAdapterError("tool arguments digest does not match arguments")

    try:
        arguments_json = canonical_dumps(admitted.call.arguments)
    except (TypeError, ValueError) as error:
        raise OpenApiToolAdapterError("tool arguments must be canonical JSON") from error

    return OpenApiOperationInvocation(
        binding_id=resolved_tool.binding.binding_id,
        resolved_tool_id=resolved_tool.resolved_tool_id,
        tool_call_id=admitted.call.tool_call_id,
        connection=implementation.connection,
        operation_id=implementation.operation_id,
        arguments_json=arguments_json,
        arguments_digest=admitted.call.arguments_digest,
        definition_digest=resolved_tool.definition_digest,
        binding_digest=resolved_tool.binding_digest,
        effective_policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
        idempotency_key=admitted.idempotency_key,
    )


def openapi_tool_result_from_response(
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
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(output, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation response output must be an object")

    try:
        result = ToolResult.completed(
            admitted.call.tool_call_id,
            (
                ContentPart(
                    kind="json",
                    data=dict(output),
                    metadata={"adapter": "openapi", "trust_designation": "untrusted_external"},
                ),
            ),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error

    prepare_openapi_tool_result_for_model(
        admitted,
        resolved_tool,
        schema_registry,
        result,
        max_output_bytes=max_output_bytes,
        redactions=redactions,
        capture_policy=capture_policy,
    )
    return result


def prepare_openapi_tool_result_for_model(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    result: ToolResult,
    *,
    max_output_bytes: int | None = None,
    redactions: Iterable[Mapping[str, object]] = (),
    capture_policy: Mapping[str, object] | None = None,
) -> tuple[ContentPart, ...]:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
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
        raise OpenApiToolAdapterError("OpenAPI tool result failed validation") from error


def openapi_tool_result_from_error(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation error must be an object")

    try:
        return ToolResult.failed(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_policy_stopped(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    error: Mapping[str, object],
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    if not isinstance(error, Mapping):
        raise OpenApiToolAdapterError("OpenAPI operation policy stop error must be an object")

    try:
        return ToolResult.policy_stopped(
            admitted.call.tool_call_id,
            error=dict(error),
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_cancelled(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    try:
        return ToolResult.cancelled(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


def openapi_tool_result_incomplete(
    admitted: AdmittedToolCall,
    resolved_tool: ResolvedTool,
    *,
    started_at: str,
    completed_at: str,
    effect_outcome: str = "unknown",
) -> ToolResult:
    prepare_openapi_operation_invocation(admitted, resolved_tool)
    try:
        return ToolResult.incomplete(
            admitted.call.tool_call_id,
            started_at=started_at,
            completed_at=completed_at,
        ).with_effect_outcome(effect_outcome)
    except ValueError as error:
        raise OpenApiToolAdapterError("OpenAPI tool result has an invalid effect outcome") from error


__all__ = [
    "OpenApiOperationInvocation",
    "OpenApiToolAdapterError",
    "bind_openapi_operation",
    "define_openapi_tool",
    "openapi_tool_result_cancelled",
    "openapi_tool_result_from_error",
    "openapi_tool_result_from_response",
    "openapi_tool_result_incomplete",
    "openapi_tool_result_policy_stopped",
    "prepare_openapi_operation_invocation",
    "prepare_openapi_tool_result_for_model",
]
