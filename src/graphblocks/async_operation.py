from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from typing import Literal

from .tools import ToolEffectOutcome, VALID_TOOL_EFFECT_OUTCOMES


AsyncOperationStateValue = Literal[
    "created",
    "submitted",
    "waiting_callback",
    "callback_received",
    "polling",
    "resuming",
    "completed",
    "failed",
    "cancelled",
    "expired",
]
AsyncOperationResultStatusValue = Literal["completed", "failed", "cancelled", "expired", "incomplete"]


class AsyncOperationState:
    CREATED = "created"
    SUBMITTED = "submitted"
    WAITING_CALLBACK = "waiting_callback"
    CALLBACK_RECEIVED = "callback_received"
    POLLING = "polling"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class AsyncOperationResultStatus:
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INCOMPLETE = "incomplete"


VALID_ASYNC_OPERATION_RESULT_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "expired",
    "incomplete",
})
VALID_ASYNC_OPERATION_STATES = frozenset({
    "created",
    "submitted",
    "waiting_callback",
    "callback_received",
    "polling",
    "resuming",
    "completed",
    "failed",
    "cancelled",
    "expired",
})
TERMINAL_ASYNC_OPERATION_STATES = frozenset({"completed", "failed", "cancelled", "expired"})
ASYNC_OPERATION_ALLOWED_TRANSITIONS = {
    "created": frozenset({"submitted", "cancelled", "expired"}),
    "submitted": frozenset({"waiting_callback", "polling", "cancelled", "expired"}),
    "waiting_callback": frozenset({"callback_received", "cancelled", "expired"}),
    "callback_received": frozenset({"resuming", "failed", "cancelled", "expired"}),
    "polling": frozenset({"completed", "failed", "cancelled", "expired"}),
    "resuming": frozenset({"completed", "failed", "cancelled", "expired"}),
}
VALID_ASYNC_OPERATION_KINDS = frozenset({
    "tool",
    "sandbox_task",
    "ci_job",
    "browser_task",
    "workspace_trial",
    "external_provider_job",
    "document_job",
    "research_task",
    "custom",
})


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{owner} {field_name} must not be empty")
    return stripped


def _parse_iso_datetime(owner: str, field_name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freeze_json_value(owner: str, field_name: str, value: object) -> object:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{owner} {field_name} must not contain non-finite numbers")
        return value
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze_json_value(owner, field_name, item) for item in value)
    if isinstance(value, dict):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{owner} {field_name} must contain only string object keys")
            frozen[key] = _freeze_json_value(owner, field_name, item)
        return frozen
    raise ValueError(f"{owner} {field_name} must contain only JSON values")


def _thaw_json_value(value: object) -> object:
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    return deepcopy(value)


def _validate_status(value: object) -> AsyncOperationResultStatusValue:
    status = _validate_non_empty_string("async operation result", "status", value)
    if status not in VALID_ASYNC_OPERATION_RESULT_STATUSES:
        raise ValueError(
            "async operation result status must be one of completed, failed, cancelled, expired, or incomplete"
        )
    return status  # type: ignore[return-value]


def _validate_operation_state(value: object) -> AsyncOperationStateValue:
    state = _validate_non_empty_string("async operation", "state", value)
    if state not in VALID_ASYNC_OPERATION_STATES:
        raise ValueError("async operation state must be a valid async operation state")
    return state  # type: ignore[return-value]


def _validate_operation_kind(value: object) -> str:
    kind = _validate_non_empty_string("async operation", "kind", value)
    if kind not in VALID_ASYNC_OPERATION_KINDS:
        raise ValueError("async operation kind must be a valid async operation kind")
    return kind


def _validate_effect_outcome(value: object) -> ToolEffectOutcome:
    outcome = _validate_non_empty_string("external effect", "outcome", value)
    if outcome not in VALID_TOOL_EFFECT_OUTCOMES:
        raise ValueError(
            "external effect outcome must be one of no_external_effect, committed, not_committed, or unknown"
        )
    return outcome  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class AsyncOperation:
    operation_id: str
    run_id: str
    node_id: str
    attempt_id: str
    kind: str
    state: AsyncOperationStateValue
    expected_schema: str
    resume_token_hash: str
    idempotency_key: str
    created_at: str
    provider_operation_id: str | None = None
    callback_ref: str | None = None
    polling_ref: str | None = None
    submitted_at: str | None = None
    expires_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "run_id",
            "node_id",
            "attempt_id",
            "expected_schema",
            "resume_token_hash",
            "idempotency_key",
            "created_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_empty_string("async operation", field_name, getattr(self, field_name)),
            )
        object.__setattr__(self, "kind", _validate_operation_kind(self.kind))
        object.__setattr__(self, "state", _validate_operation_state(self.state))
        for field_name in (
            "provider_operation_id",
            "callback_ref",
            "polling_ref",
            "submitted_at",
            "expires_at",
            "completed_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _validate_non_empty_string("async operation", field_name, value),
                )
        if self.state == "created" and (self.submitted_at is not None or self.completed_at is not None):
            raise ValueError("async operation created state must not have submitted_at or completed_at")
        if self.state in {
            "submitted",
            "waiting_callback",
            "callback_received",
            "polling",
            "resuming",
            "completed",
            "failed",
            "cancelled",
            "expired",
        } and self.submitted_at is None:
            raise ValueError(f"async operation {self.state} state requires submitted_at")
        if self.state in TERMINAL_ASYNC_OPERATION_STATES and self.completed_at is None:
            raise ValueError("async operation terminal state requires completed_at")
        created_at = _parse_iso_datetime("async operation", "created_at", self.created_at)
        submitted_at = (
            None
            if self.submitted_at is None
            else _parse_iso_datetime("async operation", "submitted_at", self.submitted_at)
        )
        completed_at = (
            None
            if self.completed_at is None
            else _parse_iso_datetime("async operation", "completed_at", self.completed_at)
        )
        expires_at = (
            None
            if self.expires_at is None
            else _parse_iso_datetime("async operation", "expires_at", self.expires_at)
        )
        if submitted_at is not None and submitted_at < created_at:
            raise ValueError("async operation submitted_at must not be before created_at")
        if completed_at is not None and completed_at < created_at:
            raise ValueError("async operation completed_at must not be before created_at")
        if submitted_at is not None and completed_at is not None and completed_at < submitted_at:
            raise ValueError("async operation completed_at must not be before submitted_at")
        if expires_at is not None and expires_at <= created_at:
            raise ValueError("async operation expires_at must be after created_at")

    @classmethod
    def created(
        cls,
        *,
        operation_id: str,
        run_id: str,
        node_id: str,
        attempt_id: str,
        kind: str,
        expected_schema: str,
        resume_token_hash: str,
        idempotency_key: str,
        created_at: str,
        provider_operation_id: str | None = None,
        callback_ref: str | None = None,
        polling_ref: str | None = None,
        expires_at: str | None = None,
    ) -> AsyncOperation:
        return cls(
            operation_id=operation_id,
            run_id=run_id,
            node_id=node_id,
            attempt_id=attempt_id,
            kind=kind,
            state="created",
            provider_operation_id=provider_operation_id,
            callback_ref=callback_ref,
            polling_ref=polling_ref,
            expected_schema=expected_schema,
            resume_token_hash=resume_token_hash,
            idempotency_key=idempotency_key,
            created_at=created_at,
            expires_at=expires_at,
        )

    def _replace_state(self, state: AsyncOperationStateValue, **changes: object) -> AsyncOperation:
        if self.state in TERMINAL_ASYNC_OPERATION_STATES:
            raise ValueError("async operation terminal state cannot transition")
        if state not in ASYNC_OPERATION_ALLOWED_TRANSITIONS.get(self.state, frozenset()):
            raise ValueError(f"async operation cannot transition from {self.state} to {state}")
        return replace(self, state=state, **changes)

    def mark_submitted(
        self,
        *,
        submitted_at: str,
        provider_operation_id: str | None = None,
    ) -> AsyncOperation:
        changes: dict[str, object] = {"submitted_at": submitted_at}
        if provider_operation_id is not None:
            changes["provider_operation_id"] = provider_operation_id
        return self._replace_state("submitted", **changes)

    def wait_for_callback(self) -> AsyncOperation:
        if self.callback_ref is None:
            raise ValueError("async operation callback_ref is required before waiting_callback")
        return self._replace_state("waiting_callback")

    def mark_callback_received(self, *, completed_at: str | None = None) -> AsyncOperation:
        changes: dict[str, object] = {}
        if completed_at is not None:
            changes["completed_at"] = completed_at
        return self._replace_state("callback_received", **changes)

    def start_polling(self) -> AsyncOperation:
        if self.polling_ref is None:
            raise ValueError("async operation polling_ref is required before polling")
        return self._replace_state("polling")

    def mark_resuming(self) -> AsyncOperation:
        return self._replace_state("resuming")

    def complete(self, *, completed_at: str) -> AsyncOperation:
        return self._replace_state("completed", completed_at=completed_at)

    def fail(self, *, completed_at: str) -> AsyncOperation:
        return self._replace_state("failed", completed_at=completed_at)

    def cancel(self, *, completed_at: str) -> AsyncOperation:
        return self._replace_state("cancelled", completed_at=completed_at)

    def expire(self, *, completed_at: str) -> AsyncOperation:
        return self._replace_state("expired", completed_at=completed_at)

    def to_json(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "state": self.state,
            "provider_operation_id": self.provider_operation_id,
            "callback_ref": self.callback_ref,
            "polling_ref": self.polling_ref,
            "resume_token_hash": self.resume_token_hash,
            "idempotency_key": self.idempotency_key,
            "expected_schema": self.expected_schema,
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
            "expires_at": self.expires_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class ExternalEffectRecord:
    effect_id: str
    target: str
    operation: str
    outcome: ToolEffectOutcome
    idempotency_key: str | None = None
    provider_effect_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effect_id",
            _validate_non_empty_string("external effect", "effect_id", self.effect_id),
        )
        object.__setattr__(
            self,
            "target",
            _validate_non_empty_string("external effect", "target", self.target),
        )
        object.__setattr__(
            self,
            "operation",
            _validate_non_empty_string("external effect", "operation", self.operation),
        )
        object.__setattr__(self, "outcome", _validate_effect_outcome(self.outcome))
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _validate_non_empty_string(
                    "external effect",
                    "idempotency_key",
                    self.idempotency_key,
                ),
            )
        if self.provider_effect_id is not None:
            object.__setattr__(
                self,
                "provider_effect_id",
                _validate_non_empty_string(
                    "external effect",
                    "provider_effect_id",
                    self.provider_effect_id,
                ),
            )

    def to_json(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "target": self.target,
            "operation": self.operation,
            "outcome": self.outcome,
            "idempotency_key": self.idempotency_key,
            "provider_effect_id": self.provider_effect_id,
        }


@dataclass(frozen=True, slots=True)
class AsyncOperationResult:
    operation_id: str
    status: AsyncOperationResultStatusValue
    output: object | None = None
    artifacts: tuple[object, ...] = field(default_factory=tuple)
    diagnostics: tuple[object, ...] = field(default_factory=tuple)
    metrics: tuple[object, ...] = field(default_factory=tuple)
    checks: tuple[object, ...] = field(default_factory=tuple)
    usage: tuple[object, ...] = field(default_factory=tuple)
    external_effects: tuple[ExternalEffectRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _validate_non_empty_string("async operation result", "operation_id", self.operation_id),
        )
        object.__setattr__(self, "status", _validate_status(self.status))
        object.__setattr__(self, "output", _freeze_json_value("async operation result", "output", self.output))
        for field_name in ("artifacts", "diagnostics", "metrics", "checks", "usage"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                raise ValueError(f"async operation result {field_name} must be a sequence")
            object.__setattr__(
                self,
                field_name,
                tuple(_freeze_json_value("async operation result", field_name, item) for item in value),
            )
        object.__setattr__(self, "external_effects", tuple(self.external_effects))
        for effect in self.external_effects:
            if not isinstance(effect, ExternalEffectRecord):
                raise ValueError("async operation result external_effects entries must be ExternalEffectRecord")
            if effect.provider_effect_id is not None and effect.outcome != "committed":
                raise ValueError(
                    f"external effect {effect.effect_id} has provider identity but no committed external effect"
                )

    @classmethod
    def completed(cls, operation_id: str, output: object | None = None) -> AsyncOperationResult:
        return cls(operation_id=operation_id, status="completed", output=output)

    @classmethod
    def failed(cls, operation_id: str, output: object | None = None) -> AsyncOperationResult:
        return cls(operation_id=operation_id, status="failed", output=output)

    @classmethod
    def cancelled(cls, operation_id: str) -> AsyncOperationResult:
        return cls(operation_id=operation_id, status="cancelled")

    @classmethod
    def expired(cls, operation_id: str) -> AsyncOperationResult:
        return cls(operation_id=operation_id, status="expired")

    @classmethod
    def incomplete(cls, operation_id: str, output: object | None = None) -> AsyncOperationResult:
        return cls(operation_id=operation_id, status="incomplete", output=output)

    def with_external_effects(
        self,
        external_effects: Iterable[ExternalEffectRecord],
    ) -> AsyncOperationResult:
        return replace(self, external_effects=tuple(external_effects))

    def with_projections(
        self,
        *,
        artifacts: Iterable[object] | None = None,
        diagnostics: Iterable[object] | None = None,
        metrics: Iterable[object] | None = None,
        checks: Iterable[object] | None = None,
        usage: Iterable[object] | None = None,
    ) -> AsyncOperationResult:
        changes: dict[str, object] = {}
        if artifacts is not None:
            changes["artifacts"] = tuple(artifacts)
        if diagnostics is not None:
            changes["diagnostics"] = tuple(diagnostics)
        if metrics is not None:
            changes["metrics"] = tuple(metrics)
        if checks is not None:
            changes["checks"] = tuple(checks)
        if usage is not None:
            changes["usage"] = tuple(usage)
        return replace(self, **changes)

    def external_effect_was_committed(self) -> bool:
        return any(effect.outcome == "committed" for effect in self.external_effects)

    def to_json(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "output": _thaw_json_value(self.output),
            "artifacts": [_thaw_json_value(item) for item in self.artifacts],
            "diagnostics": [_thaw_json_value(item) for item in self.diagnostics],
            "metrics": [_thaw_json_value(item) for item in self.metrics],
            "checks": [_thaw_json_value(item) for item in self.checks],
            "usage": [_thaw_json_value(item) for item in self.usage],
            "external_effects": [effect.to_json() for effect in self.external_effects],
        }
