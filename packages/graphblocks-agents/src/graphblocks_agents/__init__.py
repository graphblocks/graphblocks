from __future__ import annotations

from graphblocks.agent import (
    AgentLoopController,
    AgentLoopDecision,
    AgentLoopDisposition,
    AgentSpec,
    AgentState,
    AgentStateError,
    AgentStatePatch,
    AgentStatePatchOp,
    AgentStatePatchOpKind,
    AgentStateSchema,
    ToolFailurePolicy,
)
from graphblocks.conversation import ContentPart
from graphblocks.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyObligation,
    PolicyRequest,
    PrincipalRef,
)
from graphblocks.tools import (
    AdmittedToolCall,
    BlockToolImplementation,
    FINAL_TOOL_RESULT_EVENT_STATUSES,
    GraphRef,
    GraphToolImplementation,
    JsonSchema,
    JsonSchemaNode,
    JsonSchemaRef,
    McpToolImplementation,
    OpenApiToolImplementation,
    RemoteToolImplementation,
    ResolvedTool,
    ResourceRef,
    ToolAdmissionError,
    ToolApproval,
    ToolApprovalError,
    ToolApprovalRecord,
    ToolApprovalRequest,
    ToolApprovalStatus,
    ToolBinding,
    ToolCall,
    ToolCallDraft,
    ToolCallDraftStatus,
    ToolCallError,
    ToolCallStatus,
    ToolCancellation,
    ToolCatalog,
    ToolDefinition,
    ToolEffect,
    ToolEffectOutcome,
    ToolExecutionCancellationPolicy,
    ToolExecutionFailurePolicy,
    ToolExecutionPlan,
    ToolExecutionPlanError,
    ToolExecutionState,
    ToolIdempotency,
    ToolImplementation,
    ToolPlanCall,
    ToolResolutionError,
    ToolResolutionScope,
    ToolResult,
    ToolResultEvent,
    ToolResultEventKind,
    ToolResultMode,
    ToolResultStatus,
    ToolResultStreamError,
    ToolResultStreamState,
    ToolResultValidationError,
    ToolSchemaRegistry,
    ToolSchemaRegistryError,
    ToolSchemaValidationError,
    VALID_TOOL_APPROVALS,
    VALID_TOOL_APPROVAL_STATUSES,
    VALID_TOOL_CALL_DRAFT_STATUSES,
    VALID_TOOL_CALL_STATUSES,
    VALID_TOOL_CANCELLATIONS,
    VALID_TOOL_EFFECT_OUTCOMES,
    VALID_TOOL_EFFECTS,
    VALID_TOOL_EXECUTION_CANCELLATION_POLICIES,
    VALID_TOOL_EXECUTION_FAILURE_POLICIES,
    VALID_TOOL_IDEMPOTENCIES,
    VALID_TOOL_RESULT_EVENT_KINDS,
    VALID_TOOL_RESULT_MODES,
    VALID_TOOL_RESULT_STATUSES,
    admit_tool_call,
    build_before_tool_or_effect_policy_request,
    validate_tool_result_for_model,
)


def evaluate_native_tool_execution_plan(
    plan: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_tool_execution_plan

    return evaluate_tool_execution_plan(plan, operations)


def finalize_native_tool_call(
    draft: dict[str, object],
    *,
    resolved_tool_id: str,
    created_at_unix_ms: int,
) -> dict[str, object]:
    from graphblocks_runtime import finalize_tool_call

    return finalize_tool_call(
        draft,
        resolved_tool_id=resolved_tool_id,
        created_at_unix_ms=created_at_unix_ms,
    )


def prepare_native_tool_result_for_model(
    call: dict[str, object],
    result: dict[str, object],
    resolved_tool: dict[str, object],
    schema_registry: object,
    *,
    content_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import prepare_tool_result_for_model

    return prepare_tool_result_for_model(
        call,
        result,
        resolved_tool,
        schema_registry,
        content_policy=content_policy,
    )


def decide_native_agent_step(spec: dict[str, object], request: dict[str, object]) -> dict[str, object]:
    from graphblocks_runtime import decide_agent_step

    return decide_agent_step(spec, request)


def evaluate_native_sequential_tool_queue(
    queue: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_sequential_tool_queue

    return evaluate_sequential_tool_queue(queue, operations)


def evaluate_native_tool_result_stream(
    state: dict[str, object],
    operations: object,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_tool_result_stream

    return evaluate_tool_result_stream(state, operations)


def evaluate_native_tool_approval(
    record: dict[str, object],
    resolved_tool: dict[str, object],
    call: dict[str, object],
    *,
    principal_id: str,
    now_unix_ms: int,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_tool_approval

    return evaluate_tool_approval(
        record,
        resolved_tool,
        call,
        principal_id=principal_id,
        now_unix_ms=now_unix_ms,
    )


def evaluate_native_tool_admission(request: dict[str, object]) -> dict[str, object]:
    from graphblocks_runtime import evaluate_tool_admission

    return evaluate_tool_admission(request)


def evaluate_native_tool_resolution(
    catalog: dict[str, object],
    scope: dict[str, object],
    *,
    effective_policy_snapshot_id: str,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_tool_resolution

    return evaluate_tool_resolution(
        catalog,
        scope,
        effective_policy_snapshot_id=effective_policy_snapshot_id,
    )


__all__ = [
    "AdmittedToolCall",
    "AgentLoopController",
    "AgentLoopDecision",
    "AgentLoopDisposition",
    "AgentSpec",
    "AgentState",
    "AgentStateError",
    "AgentStatePatch",
    "AgentStatePatchOp",
    "AgentStatePatchOpKind",
    "AgentStateSchema",
    "BlockToolImplementation",
    "ContentPart",
    "FINAL_TOOL_RESULT_EVENT_STATUSES",
    "GraphRef",
    "GraphToolImplementation",
    "JsonSchema",
    "JsonSchemaNode",
    "JsonSchemaRef",
    "McpToolImplementation",
    "OpenApiToolImplementation",
    "PolicyDecision",
    "PolicyEffect",
    "PolicyObligation",
    "PolicyRequest",
    "PrincipalRef",
    "RemoteToolImplementation",
    "ResolvedTool",
    "ResourceRef",
    "ToolAdmissionError",
    "ToolApproval",
    "ToolApprovalError",
    "ToolApprovalRecord",
    "ToolApprovalRequest",
    "ToolApprovalStatus",
    "ToolBinding",
    "ToolCall",
    "ToolCallDraft",
    "ToolCallDraftStatus",
    "ToolCallError",
    "ToolCallStatus",
    "ToolCancellation",
    "ToolCatalog",
    "ToolDefinition",
    "ToolEffect",
    "ToolEffectOutcome",
    "ToolExecutionCancellationPolicy",
    "ToolExecutionFailurePolicy",
    "ToolExecutionPlan",
    "ToolExecutionPlanError",
    "ToolFailurePolicy",
    "ToolExecutionState",
    "ToolIdempotency",
    "ToolImplementation",
    "ToolPlanCall",
    "ToolResolutionError",
    "ToolResolutionScope",
    "ToolResult",
    "ToolResultEvent",
    "ToolResultEventKind",
    "ToolResultMode",
    "ToolResultStatus",
    "ToolResultStreamError",
    "ToolResultStreamState",
    "ToolResultValidationError",
    "ToolSchemaRegistry",
    "ToolSchemaRegistryError",
    "ToolSchemaValidationError",
    "VALID_TOOL_APPROVALS",
    "VALID_TOOL_APPROVAL_STATUSES",
    "VALID_TOOL_CALL_DRAFT_STATUSES",
    "VALID_TOOL_CALL_STATUSES",
    "VALID_TOOL_CANCELLATIONS",
    "VALID_TOOL_EFFECT_OUTCOMES",
    "VALID_TOOL_EFFECTS",
    "VALID_TOOL_EXECUTION_CANCELLATION_POLICIES",
    "VALID_TOOL_EXECUTION_FAILURE_POLICIES",
    "VALID_TOOL_IDEMPOTENCIES",
    "VALID_TOOL_RESULT_EVENT_KINDS",
    "VALID_TOOL_RESULT_MODES",
    "VALID_TOOL_RESULT_STATUSES",
    "admit_tool_call",
    "build_before_tool_or_effect_policy_request",
    "decide_native_agent_step",
    "evaluate_native_sequential_tool_queue",
    "evaluate_native_tool_admission",
    "evaluate_native_tool_approval",
    "evaluate_native_tool_execution_plan",
    "evaluate_native_tool_result_stream",
    "evaluate_native_tool_resolution",
    "finalize_native_tool_call",
    "prepare_native_tool_result_for_model",
    "validate_tool_result_for_model",
]
