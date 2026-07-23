from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
from graphblocks.canonical import canonical_dumps, canonical_hash, canonical_loads
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
_AUDIT_OUTBOX_STATUSES = frozenset({"pending", "published", "failed"})
_MAX_AUDIT_ATTEMPTS = (1 << 63) - 1


class _FrozenAuditList(tuple[object, ...]):
    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)

    def __setitem__(self, index: object, value: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def __delitem__(self, index: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def append(self, item: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def clear(self) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def extend(self, items: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def insert(self, index: int, item: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def pop(self, index: int = -1) -> object:
        raise TypeError("frozen audit list cannot be mutated")

    def remove(self, item: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def reverse(self) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def sort(self, *args: object, **kwargs: object) -> None:
        raise TypeError("frozen audit list cannot be mutated")

    def __iadd__(self, items: object) -> _FrozenAuditList:
        raise TypeError("frozen audit list cannot be mutated")

    def __imul__(self, multiplier: int) -> _FrozenAuditList:
        raise TypeError("frozen audit list cannot be mutated")


def _normalize_audit_json_value(
    value: object,
    *,
    active_containers: set[int],
) -> object:
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError("audit payload must not contain cyclic values")
        active_containers.add(container_id)
        try:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("audit payload object keys must be strings")
                if key in normalized:
                    raise ValueError(f"audit payload contains duplicate key {key!r}")
                normalized[key] = _normalize_audit_json_value(
                    item,
                    active_containers=active_containers,
                )
            return normalized
        finally:
            active_containers.remove(container_id)
    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_containers:
            raise ValueError("audit payload must not contain cyclic values")
        active_containers.add(container_id)
        try:
            return [
                _normalize_audit_json_value(
                    item,
                    active_containers=active_containers,
                )
                for item in value
            ]
        finally:
            active_containers.remove(container_id)
    return value


def _snapshot_audit_payload(
    payload: object,
    *,
    field_name: str = "payload",
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"audit {field_name} must be an object")
    try:
        normalized = _normalize_audit_json_value(payload, active_containers=set())
        canonical_payload = canonical_dumps(normalized)
        decoded = canonical_loads(canonical_payload)
    except (RecursionError, TypeError, ValueError, RuntimeError) as error:
        if isinstance(error, ValueError) and str(error).startswith("audit payload"):
            raise
        raise ValueError(f"audit {field_name} must contain strict canonical JSON") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"audit {field_name} must be an object")
    return decoded


def _freeze_audit_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_audit_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenAuditList(_freeze_audit_value(item) for item in value)
    return value


def _freeze_audit_snapshot(
    payload: dict[str, object],
) -> MappingProxyType[str, object]:
    return MappingProxyType(
        {key: _freeze_audit_value(value) for key, value in payload.items()}
    )


def _freeze_audit_payload(payload: object) -> MappingProxyType[str, object]:
    return _freeze_audit_snapshot(_snapshot_audit_payload(payload))


def _validate_audit_string(field_name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"audit outbox {field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"audit outbox {field_name} must not contain surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"audit outbox {field_name} must not contain control characters")
    return value


def _loads_strict_json(field_name: str, value: object) -> object:
    if not isinstance(value, str):
        raise ValueError(f"audit outbox {field_name} must be valid strict JSON")
    try:
        decoded = canonical_loads(value)
        if canonical_dumps(decoded) != value:
            raise ValueError("non-canonical JSON encoding")
        return decoded
    except (TypeError, ValueError) as error:
        raise ValueError(f"audit outbox {field_name} must be valid strict JSON") from error


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
        record_id = _validate_audit_string("record_id", self.record_id)
        record_type = _validate_audit_string("record_type", self.record_type)
        payload_digest = _validate_audit_string("payload_digest", self.payload_digest)
        occurred_at = _validate_audit_string("occurred_at", self.occurred_at)
        if not isinstance(self.status, str) or self.status not in _AUDIT_OUTBOX_STATUSES:
            raise ValueError("audit outbox status must be pending, published, or failed")
        if (
            not isinstance(self.attempts, int)
            or isinstance(self.attempts, bool)
            or not 0 <= self.attempts <= _MAX_AUDIT_ATTEMPTS
        ):
            raise ValueError("audit outbox attempts must be a non-negative integer")
        published_at = (
            None
            if self.published_at is None
            else _validate_audit_string("published_at", self.published_at)
        )
        last_error = (
            None
            if self.last_error is None
            else _validate_audit_string("last_error", self.last_error)
        )
        payload = _snapshot_audit_payload(self.payload)
        if canonical_hash(payload) != payload_digest:
            raise ValueError("audit outbox payload_digest does not match payload")
        if self.status == "pending":
            if self.attempts != 0:
                raise ValueError("pending audit outbox record must have zero attempts")
            if last_error is not None:
                raise ValueError("pending audit outbox record must not define last_error")
        elif self.status == "failed":
            if self.attempts == 0:
                raise ValueError("failed audit outbox record requires a positive attempt count")
            if last_error is None:
                raise ValueError("failed audit outbox record requires last_error")
        elif last_error is not None:
            raise ValueError("published audit outbox record must not define last_error")
        if self.status == "published":
            if published_at is None:
                raise ValueError("published audit outbox record requires published_at")
        elif published_at is not None:
            raise ValueError("unpublished audit outbox record must not define published_at")
        object.__setattr__(self, "record_id", record_id)
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "payload", _freeze_audit_snapshot(payload))
        object.__setattr__(self, "payload_digest", payload_digest)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "last_error", last_error)


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
        record_type = _validate_audit_string("record_type", record_type)
        occurred_at = _validate_audit_string("occurred_at", occurred_at)
        payload_value = _snapshot_audit_payload(payload)
        payload_json = canonical_dumps(payload_value)
        payload_digest = canonical_hash(payload_value)
        actual_record_id = (
            f"audit:{payload_digest}"
            if record_id is None
            else _validate_audit_string("record_id", record_id)
        )
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
            self._connection.rollback()
            existing = self.get(actual_record_id)
            if (
                existing.record_type == record_type
                and canonical_dumps(_snapshot_audit_payload(existing.payload)) == payload_json
                and existing.payload_digest == payload_digest
                and existing.occurred_at == occurred_at
            ):
                return existing
            raise AuditOutboxConflictError(f"audit outbox record {actual_record_id!r} already exists") from error
        return self.get(actual_record_id)

    def get(self, record_id: str) -> AuditOutboxRecord:
        record_id = _validate_audit_string("record_id", record_id)
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
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError(
                    "audit outbox pending limit must be a non-negative integer"
                )
            sql += " LIMIT ?"
            parameters = (limit,)
        rows = self._connection.execute(sql, parameters).fetchall()
        return [self._record_from_row(row) for row in rows]

    def mark_published(self, record_id: str, *, published_at: str) -> AuditOutboxRecord:
        record_id = _validate_audit_string("record_id", record_id)
        published_at = _validate_audit_string("published_at", published_at)
        if self._connection.execute(
            """
            UPDATE audit_outbox_records
            SET status = ?, published_at = ?, last_error = NULL
            WHERE record_id = ? AND status IN ('pending', 'failed')
            """,
            ("published", published_at, record_id),
        ).rowcount == 0:
            self._connection.rollback()
            current = self.get(record_id)
            if current.status == "published" and current.published_at == published_at:
                return current
            raise AuditOutboxError(f"audit outbox record {record_id!r} is already published")
        self._connection.commit()
        return self.get(record_id)

    def mark_failed(self, record_id: str, *, error: str) -> AuditOutboxRecord:
        record_id = _validate_audit_string("record_id", record_id)
        error = _validate_audit_string("last_error", error)
        if self._connection.execute(
            """
            UPDATE audit_outbox_records
            SET status = ?, attempts = attempts + 1, last_error = ?
            WHERE record_id = ?
              AND status IN ('pending', 'failed')
              AND attempts < ?
            """,
            ("failed", error, record_id, _MAX_AUDIT_ATTEMPTS),
        ).rowcount == 0:
            self._connection.rollback()
            current = self.get(record_id)
            if current.attempts == _MAX_AUDIT_ATTEMPTS:
                raise AuditOutboxError(
                    f"audit outbox record {record_id!r} exhausted its attempt counter"
                )
            raise AuditOutboxError(f"audit outbox record {record_id!r} is already published")
        self._connection.commit()
        return self.get(record_id)

    def _record_from_row(self, row: sqlite3.Row) -> AuditOutboxRecord:
        payload = _loads_strict_json("payload_json", row["payload_json"])
        if not isinstance(payload, Mapping):
            raise ValueError("audit outbox payload_json must decode to an object")
        if canonical_hash(payload) != row["payload_digest"]:
            raise ValueError("audit outbox payload_digest does not match payload_json")
        return AuditOutboxRecord(
            record_id=row["record_id"],
            record_type=row["record_type"],
            payload=payload,
            payload_digest=row["payload_digest"],
            occurred_at=row["occurred_at"],
            status=row["status"],
            attempts=row["attempts"],
            published_at=row["published_at"],
            last_error=row["last_error"],
        )


@dataclass(frozen=True, slots=True)
class ToolEffectPrecondition:
    payload: Mapping[str, object]
    digest: str

    def __post_init__(self) -> None:
        try:
            payload = _snapshot_audit_payload(self.payload, field_name="precondition payload")
        except ValueError as error:
            raise ToolEffectAuditError("tool effect precondition payload must be strict JSON") from error
        try:
            digest = _validate_audit_string("precondition digest", self.digest)
        except ValueError as error:
            raise ToolEffectAuditError(
                "tool effect precondition digest must be an exact non-empty string"
            ) from error
        if canonical_hash(payload) != digest:
            raise ToolEffectAuditError(
                "tool effect precondition digest does not match payload"
            )
        object.__setattr__(self, "payload", _freeze_audit_snapshot(payload))
        object.__setattr__(self, "digest", digest)

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
        try:
            event_id = _validate_audit_string("event_id", self.event_id)
            target_kind = _validate_audit_string("target_kind", self.target_kind)
            occurred_at = _validate_audit_string("occurred_at", self.occurred_at)
        except ValueError as error:
            raise ToolEffectAuditError(str(error)) from error
        if target_kind not in {"tool_effect", "destructive_effect"}:
            raise ToolEffectAuditError(
                "tool effect audit target_kind must be tool_effect or destructive_effect"
            )
        if not isinstance(self.actor, PrincipalRef):
            raise ToolEffectAuditError(
                "tool effect audit actor must be a PrincipalRef"
            )
        if not isinstance(self.resource, ResourceRef):
            raise ToolEffectAuditError(
                "tool effect audit resource must be a ResourceRef"
            )
        if isinstance(self.reason_codes, str):
            raise ToolEffectAuditError(
                "tool effect audit reason_codes must be a collection of strings"
            )
        try:
            reason_codes = tuple(self.reason_codes)
        except TypeError as error:
            raise ToolEffectAuditError(
                "tool effect audit reason_codes must be a collection of strings"
            ) from error
        if not reason_codes:
            raise ToolEffectAuditError(
                "tool effect audit reason_codes must not be empty"
            )
        try:
            normalized_reason_codes = tuple(
                _validate_audit_string("reason_codes item", reason_code)
                for reason_code in reason_codes
            )
        except ValueError as error:
            raise ToolEffectAuditError(str(error)) from error
        if len(set(normalized_reason_codes)) != len(normalized_reason_codes):
            raise ToolEffectAuditError(
                "tool effect audit reason_codes must not contain duplicates"
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "target_kind", target_kind)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "reason_codes", normalized_reason_codes)
        object.__setattr__(self, "payload", _freeze_audit_payload(self.payload))

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
