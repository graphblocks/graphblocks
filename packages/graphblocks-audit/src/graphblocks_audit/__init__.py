from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Literal

from graphblocks.application_event import (
    STANDARD_APPLICATION_EVENT_KINDS,
    TOOL_APPLICATION_EVENT_KINDS,
    ApplicationEvent,
    ApplicationEventError,
    ApplicationEventKind,
    ApplicationEventMetadata,
)
from graphblocks.approval import ApprovalRecord, ApprovalRequest, ApprovalStatus, VALID_APPROVAL_STATUSES
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


class AuditOutboxError(RuntimeError):
    pass


class AuditOutboxConflictError(AuditOutboxError):
    pass


class AuditOutboxRecordNotFoundError(AuditOutboxError):
    pass


AuditOutboxStatus = Literal["pending", "published", "failed"]


def record_native_tool_effect_precondition(
    resolved_tool: Mapping[str, object],
    call: Mapping[str, object],
    *,
    effect_key: str | None = None,
    idempotency_key: str | None = None,
    policy_decision_id: str | None = None,
    execution_target: str | None = None,
    sandbox_id: str | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import record_tool_effect_precondition

    return record_tool_effect_precondition(
        dict(resolved_tool),
        dict(call),
        effect_key=effect_key,
        idempotency_key=idempotency_key,
        policy_decision_id=policy_decision_id,
        execution_target=execution_target,
        sandbox_id=sandbox_id,
    )


def record_native_tool_effect_audit_event(
    *,
    event_id: str,
    occurred_at: str,
    actor: Mapping[str, object],
    resolved_tool: Mapping[str, object],
    call: Mapping[str, object],
    result: Mapping[str, object],
    effect_key: str | None = None,
    precondition_digest: str | None = None,
    idempotency_key: str | None = None,
    policy_decision_id: str | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import record_tool_effect_audit_event

    return record_tool_effect_audit_event(
        event_id=event_id,
        occurred_at=occurred_at,
        actor=dict(actor),
        resolved_tool=dict(resolved_tool),
        call=dict(call),
        result=dict(result),
        effect_key=effect_key,
        precondition_digest=precondition_digest,
        idempotency_key=idempotency_key,
        policy_decision_id=policy_decision_id,
    )


@dataclass(frozen=True, slots=True)
class AuditOutboxRecord:
    record_id: str
    record_type: str
    payload: Mapping[str, object]
    payload_digest: str
    occurred_at: str
    status: AuditOutboxStatus = "pending"
    attempts: int = 0
    published_at: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(slots=True)
class SQLiteAuditOutbox:
    path: str | Path
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_outbox_records (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              record_id TEXT NOT NULL UNIQUE,
              record_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              payload_digest TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL,
              published_at TEXT,
              last_error TEXT
            )
            """
        )
        self._connection.commit()

    @classmethod
    def in_memory(cls) -> SQLiteAuditOutbox:
        return cls(":memory:")

    def close(self) -> None:
        self._connection.close()

    def append(
        self,
        record_type: str,
        payload: Mapping[str, object],
        *,
        occurred_at: str,
        record_id: str | None = None,
    ) -> AuditOutboxRecord:
        payload_json = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))
        payload_digest = canonical_hash(json.loads(payload_json))
        actual_record_id = record_id or f"audit:{payload_digest}"
        try:
            self._connection.execute(
                """
                INSERT INTO audit_outbox_records (
                  record_id,
                  record_type,
                  payload_json,
                  payload_digest,
                  occurred_at,
                  status,
                  attempts,
                  published_at,
                  last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actual_record_id,
                    record_type,
                    payload_json,
                    payload_digest,
                    occurred_at,
                    "pending",
                    0,
                    None,
                    None,
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise AuditOutboxConflictError(f"audit outbox record {actual_record_id!r} already exists") from error
        return self.get(actual_record_id)

    def get(self, record_id: str) -> AuditOutboxRecord:
        row = self._connection.execute(
            "SELECT * FROM audit_outbox_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise AuditOutboxRecordNotFoundError(f"audit outbox record {record_id!r} does not exist")
        return self._record_from_row(row)

    def pending(self, *, limit: int | None = None) -> list[AuditOutboxRecord]:
        sql = "SELECT * FROM audit_outbox_records WHERE status IN ('pending', 'failed') ORDER BY sequence"
        parameters: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._connection.execute(sql, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def mark_published(self, record_id: str, *, published_at: str) -> AuditOutboxRecord:
        if self._connection.execute(
            """
            UPDATE audit_outbox_records
            SET status = ?, published_at = ?, last_error = NULL
            WHERE record_id = ?
            """,
            ("published", published_at, record_id),
        ).rowcount == 0:
            raise AuditOutboxRecordNotFoundError(f"audit outbox record {record_id!r} does not exist")
        self._connection.commit()
        return self.get(record_id)

    def mark_failed(self, record_id: str, *, error: str) -> AuditOutboxRecord:
        current = self.get(record_id)
        if current.status == "published":
            raise AuditOutboxError(f"audit outbox record {record_id!r} is already published")
        if self._connection.execute(
            """
            UPDATE audit_outbox_records
            SET status = ?, attempts = attempts + 1, last_error = ?
            WHERE record_id = ?
            """,
            ("failed", error, record_id),
        ).rowcount == 0:
            raise AuditOutboxRecordNotFoundError(f"audit outbox record {record_id!r} does not exist")
        self._connection.commit()
        return self.get(record_id)

    def _record_from_row(self, row: sqlite3.Row) -> AuditOutboxRecord:
        return AuditOutboxRecord(
            record_id=row["record_id"],
            record_type=row["record_type"],
            payload=json.loads(row["payload_json"]),
            payload_digest=row["payload_digest"],
            occurred_at=row["occurred_at"],
            status=row["status"],
            attempts=int(row["attempts"]),
            published_at=row["published_at"],
            last_error=row["last_error"],
        )


@dataclass(frozen=True, slots=True)
class ToolEffectPrecondition:
    payload: Mapping[str, object]
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def from_admitted_call(
        cls,
        *,
        resolved_tool: ResolvedTool,
        call: ToolCall,
        effect_key: str | None = None,
        idempotency_key: str | None = None,
        policy_decision_id: str | None = None,
        execution_target: str | None = None,
        sandbox_id: str | None = None,
    ) -> ToolEffectPrecondition:
        _validate_tool_effect_context(resolved_tool, call)
        if call.status != "admitted":
            raise ToolEffectAuditError(
                f"tool call {call.tool_call_id} must be admitted before recording an effect precondition"
            )

        payload = {
            "tool_call_id": call.tool_call_id,
            "response_id": call.response_id,
            "resolved_tool_id": resolved_tool.resolved_tool_id,
            "binding_id": resolved_tool.binding.binding_id,
            "tool_name": resolved_tool.definition.name,
            "tool_call_revision": call.revision,
            "arguments_digest": call.arguments_digest,
            "definition_digest": resolved_tool.definition_digest,
            "binding_digest": resolved_tool.binding_digest,
            "effective_policy_snapshot_id": resolved_tool.effective_policy_snapshot_id,
            "effects": sorted(resolved_tool.binding.effects),
            "effect_key": effect_key,
            "idempotency_key": idempotency_key,
            "policy_decision_id": policy_decision_id,
            "execution_target": execution_target,
            "sandbox_id": sandbox_id,
            "admitted_at": call.admitted_at,
        }
        return cls(payload=payload, digest=canonical_hash(payload))


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
        _validate_tool_effect_context(resolved_tool, call)
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


def _validate_tool_effect_context(resolved_tool: ResolvedTool, call: ToolCall) -> None:
    if call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise ToolEffectAuditError(
            f"tool call resolved tool {call.resolved_tool_id} does not match "
            f"audited resolved tool {resolved_tool.resolved_tool_id}"
        )
    if call.name != resolved_tool.definition.name:
        raise ToolEffectAuditError(
            f"tool call name {call.name} does not match audited tool {resolved_tool.definition.name}"
        )


__all__ = [
    "STANDARD_APPLICATION_EVENT_KINDS",
    "TOOL_APPLICATION_EVENT_KINDS",
    "ApplicationEvent",
    "ApplicationEventError",
    "ApplicationEventKind",
    "ApplicationEventMetadata",
    "AuditOutboxConflictError",
    "AuditOutboxError",
    "AuditOutboxRecord",
    "AuditOutboxRecordNotFoundError",
    "AuditOutboxStatus",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalStatus",
    "VALID_APPROVAL_STATUSES",
    "PolicyDecision",
    "PolicyEnforcementRecord",
    "PrincipalRef",
    "ResourceRef",
    "SQLiteAuditOutbox",
    "ToolEffectAuditError",
    "ToolEffectAuditRecord",
    "ToolEffectPrecondition",
    "ToolApprovalRecord",
    "ToolApprovalRequest",
    "ToolApprovalStatus",
    "record_native_tool_effect_audit_event",
    "record_native_tool_effect_precondition",
]
