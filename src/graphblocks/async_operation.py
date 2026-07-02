from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
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
        for field_name in ("artifacts", "diagnostics", "metrics", "checks", "usage"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                raise ValueError(f"async operation result {field_name} must be a sequence")
            object.__setattr__(self, field_name, tuple(value))
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

    def external_effect_was_committed(self) -> bool:
        return any(effect.outcome == "committed" for effect in self.external_effects)

    def to_json(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "output": self.output,
            "artifacts": list(self.artifacts),
            "diagnostics": list(self.diagnostics),
            "metrics": list(self.metrics),
            "checks": list(self.checks),
            "usage": list(self.usage),
            "external_effects": [effect.to_json() for effect in self.external_effects],
        }
