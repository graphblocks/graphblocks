from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from graphblocks.application_event import (
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventKind,
    ApplicationEventMetadata,
)
from graphblocks.approval import ApprovalRecord, ApprovalRequest, ApprovalStatus
from graphblocks.canonical import canonical_hash
from graphblocks.policy import PolicyDecision, PolicyEnforcementRecord, PrincipalRef, ResourceRef
from graphblocks.tools import (
    ResolvedTool,
    ToolApprovalRecord,
    ToolApprovalRequest,
    ToolApprovalStatus,
    ToolCall,
    ToolResult,
)


class ToolEffectAuditError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolEffectAuditRecord:
    event_id: str
    target_kind: str
    occurred_at: str
    actor: PrincipalRef
    resource: ResourceRef
    reason_codes: tuple[str, ...]
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def from_tool_result(
        cls,
        *,
        event_id: str,
        occurred_at: str,
        actor: PrincipalRef,
        resolved_tool: ResolvedTool,
        call: ToolCall,
        result: ToolResult,
        effect_key: str | None = None,
        precondition_digest: str | None = None,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
    ) -> ToolEffectAuditRecord:
        if call.resolved_tool_id != resolved_tool.resolved_tool_id:
            raise ToolEffectAuditError(
                f"tool call resolved tool {call.resolved_tool_id} does not match "
                f"audited resolved tool {resolved_tool.resolved_tool_id}"
            )
        if call.name != resolved_tool.definition.name:
            raise ToolEffectAuditError(
                f"tool call name {call.name} does not match audited tool {resolved_tool.definition.name}"
            )
        if result.tool_call_id != call.tool_call_id:
            raise ToolEffectAuditError(
                f"tool result {result.tool_call_id} does not match tool call {call.tool_call_id}"
            )

        target_kind = "destructive_effect" if "destructive" in resolved_tool.binding.effects else "tool_effect"
        effect_outcome = result.effect_outcome
        return cls(
            event_id=event_id,
            target_kind=target_kind,
            occurred_at=occurred_at,
            actor=actor,
            resource=ResourceRef(
                resource_id=f"tool:{resolved_tool.definition.name}",
                resource_kind="tool",
            ),
            reason_codes=(f"tool_effect.{effect_outcome}",),
            payload={
                "tool_call_id": call.tool_call_id,
                "response_id": call.response_id,
                "resolved_tool_id": resolved_tool.resolved_tool_id,
                "tool_name": resolved_tool.definition.name,
                "tool_call_revision": call.revision,
                "arguments_digest": call.arguments_digest,
                "definition_digest": resolved_tool.definition_digest,
                "binding_digest": resolved_tool.binding_digest,
                "effective_policy_snapshot_id": resolved_tool.effective_policy_snapshot_id,
                "effects": sorted(resolved_tool.binding.effects),
                "effect_key": effect_key,
                "precondition_digest": precondition_digest,
                "idempotency_key": idempotency_key,
                "policy_decision_id": policy_decision_id,
                "result_status": result.status,
                "effect_outcome": effect_outcome,
                "output_digest": result.output_digest,
                "started_at": result.started_at,
                "completed_at": result.completed_at,
            },
        )

    def payload_digest(self) -> str:
        return canonical_hash(
            {
                "target_kind": self.target_kind,
                "actor": {
                    "principal_id": self.actor.principal_id,
                    "tenant_id": self.actor.tenant_id,
                    "groups": self.actor.groups,
                    "roles": self.actor.roles,
                    "attributes": dict(self.actor.attributes),
                },
                "resource": {
                    "resource_id": self.resource.resource_id,
                    "resource_kind": self.resource.resource_kind,
                    "tenant_id": self.resource.tenant_id,
                    "attributes": dict(self.resource.attributes),
                },
                "reason_codes": self.reason_codes,
                "payload": dict(self.payload),
            }
        )


__all__ = [
    "STANDARD_APPLICATION_EVENT_KINDS",
    "TOOL_APPLICATION_EVENT_KINDS",
    "ApplicationEvent",
    "ApplicationEventError",
    "ApplicationEventKind",
    "ApplicationEventMetadata",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalStatus",
    "PolicyDecision",
    "PolicyEnforcementRecord",
    "PrincipalRef",
    "ResourceRef",
    "ToolEffectAuditError",
    "ToolEffectAuditRecord",
    "ToolApprovalRecord",
    "ToolApprovalRequest",
    "ToolApprovalStatus",
]
