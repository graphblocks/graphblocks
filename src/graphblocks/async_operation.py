from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from typing import Literal

from .canonical import canonical_hash
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


class _FrozenJsonArray(tuple[object, ...]):
    pass


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{owner} {field_name} must not be empty")
    return stripped


def _validate_sha256_digest(owner: str, field_name: str, value: object) -> str:
    digest_value = _validate_non_empty_string(owner, field_name, value)
    if not digest_value.startswith("sha256:"):
        raise ValueError(f"{owner} {field_name} must be a canonical sha256 digest")
    digest = digest_value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{owner} {field_name} must be a canonical sha256 digest")
    return digest_value


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
    if isinstance(value, _FrozenJsonArray):
        return _FrozenJsonArray(_freeze_json_value(owner, field_name, item) for item in value)
    if isinstance(value, list):
        return _FrozenJsonArray(_freeze_json_value(owner, field_name, item) for item in value)
    if isinstance(value, tuple):
        raise ValueError(f"{owner} {field_name} must contain only JSON values")
    if isinstance(value, dict):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{owner} {field_name} must contain only string object keys")
            frozen[key] = _freeze_json_value(owner, field_name, item)
        return frozen
    raise ValueError(f"{owner} {field_name} must contain only JSON values")


def _thaw_json_value(value: object) -> object:
    if isinstance(value, _FrozenJsonArray):
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


def _projection_sequence(field_name: str, value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)) or isinstance(value, Mapping):
        raise ValueError(f"async operation result {field_name} must be a sequence")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError(f"async operation result {field_name} must be a sequence") from None
    if any(not isinstance(item, Mapping) for item in items):
        raise ValueError(f"async operation result {field_name} entries must be JSON objects")
    return items


def _external_effect_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)) or isinstance(value, Mapping):
        raise ValueError("async operation result external_effects must be a sequence")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("async operation result external_effects must be a sequence") from None


def _external_callback_artifact_sequence(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)) or isinstance(value, Mapping):
        raise ValueError("external callback received artifacts must be a sequence")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("external callback received artifacts must be a sequence") from None


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
    infinite_wait_policy: str | None = None
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
            "idempotency_key",
            "created_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_empty_string("async operation", field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "resume_token_hash",
            _validate_sha256_digest("async operation", "resume_token_hash", self.resume_token_hash),
        )
        object.__setattr__(self, "kind", _validate_operation_kind(self.kind))
        object.__setattr__(self, "state", _validate_operation_state(self.state))
        for field_name in (
            "provider_operation_id",
            "callback_ref",
            "polling_ref",
            "infinite_wait_policy",
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
        if self.callback_ref is not None and self.polling_ref is not None:
            raise ValueError("async operation must not define both callback_ref and polling_ref")
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
        if self.state == "callback_received" and self.completed_at is None:
            raise ValueError("async operation callback_received state requires completed_at")
        if self.state in {"waiting_callback", "callback_received"} and self.callback_ref is None:
            raise ValueError(f"async operation {self.state} state requires callback_ref")
        if self.state == "polling" and self.polling_ref is None:
            raise ValueError("async operation polling state requires polling_ref")
        if (
            self.state in {"waiting_callback", "callback_received", "polling"}
            and self.expires_at is None
            and self.infinite_wait_policy is None
        ):
            wait_kind = "polling" if self.state == "polling" else self.state
            raise ValueError(
                f"async operation {wait_kind} state requires expires_at or explicit infinite_wait_policy"
            )
        if self.provider_operation_id is not None and self.submitted_at is None:
            raise ValueError("async operation provider_operation_id requires submitted_at")
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
        if submitted_at is not None and expires_at is not None and expires_at <= submitted_at:
            raise ValueError("async operation expires_at must be after submitted_at")
        if (
            self.state == "callback_received"
            and completed_at is not None
            and expires_at is not None
            and completed_at > expires_at
        ):
            raise ValueError("async operation callback receipt must not be after expires_at")
        if (
            self.state == "completed"
            and self.callback_ref is not None
            and completed_at is not None
            and expires_at is not None
            and completed_at > expires_at
        ):
            raise ValueError("async operation callback completion must not be after expires_at")
        if (
            self.state == "completed"
            and self.polling_ref is not None
            and completed_at is not None
            and expires_at is not None
            and completed_at > expires_at
        ):
            raise ValueError("async operation polling completion must not be after expires_at")
        if (
            self.state == "failed"
            and self.callback_ref is not None
            and completed_at is not None
            and expires_at is not None
            and completed_at > expires_at
        ):
            raise ValueError("async operation callback failure must not be after expires_at")
        if (
            self.state == "failed"
            and self.polling_ref is not None
            and completed_at is not None
            and expires_at is not None
            and completed_at > expires_at
        ):
            raise ValueError("async operation polling failure must not be after expires_at")

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
        infinite_wait_policy: str | None = None,
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
            infinite_wait_policy=infinite_wait_policy,
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
        completed_at = changes.get("completed_at")
        if state in TERMINAL_ASYNC_OPERATION_STATES and self.completed_at is not None and isinstance(completed_at, str):
            receipt_at = _parse_iso_datetime("async operation", "completed_at", self.completed_at)
            terminal_at = _parse_iso_datetime("async operation", "completed_at", completed_at)
            if terminal_at < receipt_at:
                raise ValueError("async operation terminal completed_at must not be before callback receipt")
        return replace(self, state=state, **changes)

    def _has_wait_boundary(self) -> bool:
        return self.expires_at is not None or self.infinite_wait_policy is not None

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
        if not self._has_wait_boundary():
            raise ValueError("async operation callback wait requires expires_at or explicit infinite_wait_policy")
        return self._replace_state("waiting_callback")

    def mark_callback_received(self, *, completed_at: str | None = None) -> AsyncOperation:
        if completed_at is None:
            raise ValueError("async operation callback_received state requires completed_at")
        return self._replace_state("callback_received", completed_at=completed_at)

    def start_polling(self) -> AsyncOperation:
        if self.polling_ref is None:
            raise ValueError("async operation polling_ref is required before polling")
        if not self._has_wait_boundary():
            raise ValueError("async operation polling wait requires expires_at or explicit infinite_wait_policy")
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
            "infinite_wait_policy": self.infinite_wait_policy,
            "resume_token_hash": self.resume_token_hash,
            "idempotency_key": self.idempotency_key,
            "expected_schema": self.expected_schema,
            "created_at": self.created_at,
            "submitted_at": self.submitted_at,
            "expires_at": self.expires_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class ExternalCallbackReceived:
    callback_id: str
    operation_id: str
    run_id: str
    node_id: str
    attempt_id: str
    idempotency_key: str
    payload: object
    payload_digest: str
    received_at: str
    verified_by: str
    policy_snapshot_id: str
    provider_operation_id: str | None = None
    artifacts: tuple[object, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for field_name in (
            "callback_id",
            "operation_id",
            "run_id",
            "node_id",
            "attempt_id",
            "idempotency_key",
            "received_at",
            "verified_by",
            "policy_snapshot_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_non_empty_string(
                    "external callback received",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        if self.provider_operation_id is not None:
            object.__setattr__(
                self,
                "provider_operation_id",
                _validate_non_empty_string(
                    "external callback received",
                    "provider_operation_id",
                    self.provider_operation_id,
                ),
            )
        object.__setattr__(
            self,
            "payload_digest",
            _validate_sha256_digest(
                "external callback received",
                "payload_digest",
                self.payload_digest,
            ),
        )
        object.__setattr__(
            self,
            "payload",
            _freeze_json_value(
                "external callback received",
                "payload",
                self.payload,
            ),
        )
        if canonical_hash(_thaw_json_value(self.payload)) != self.payload_digest:
            raise ValueError("external callback received payload_digest must match payload")
        artifact_refs = tuple(
            _freeze_json_value("external callback received", "artifacts", artifact)
            for artifact in _external_callback_artifact_sequence(self.artifacts)
        )
        artifact_ids: set[str] = set()
        for artifact in artifact_refs:
            if not isinstance(artifact, Mapping):
                raise ValueError("external callback received artifacts entries must be JSON objects")
            artifact_id = artifact.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise ValueError("external callback received artifacts artifact_id must be a non-empty string")
            uri = artifact.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                raise ValueError("external callback received artifacts uri must be a non-empty string")
            for field_name in ("media_type", "checksum"):
                value = artifact.get(field_name)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(
                        f"external callback received artifacts {field_name} must be a non-empty string"
                    )
            if artifact_id in artifact_ids:
                raise ValueError("external callback received artifacts must not contain duplicate artifact_id")
            artifact_ids.add(artifact_id)
        object.__setattr__(self, "artifacts", artifact_refs)
        object.__setattr__(
            self,
            "received_at",
            _parse_iso_datetime("external callback received", "received_at", self.received_at)
            .isoformat()
            .replace("+00:00", "Z"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "callback_id": self.callback_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "provider_operation_id": self.provider_operation_id,
            "idempotency_key": self.idempotency_key,
            "payload": _thaw_json_value(self.payload),
            "payload_digest": self.payload_digest,
            "artifacts": [_thaw_json_value(artifact) for artifact in self.artifacts],
            "received_at": self.received_at,
            "verified_by": self.verified_by,
            "policy_snapshot_id": self.policy_snapshot_id,
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
            value = _projection_sequence(field_name, getattr(self, field_name))
            object.__setattr__(
                self,
                field_name,
                tuple(_freeze_json_value("async operation result", field_name, item) for item in value),
            )
        object.__setattr__(self, "external_effects", _external_effect_sequence(self.external_effects))
        effect_ids: set[str] = set()
        provider_effect_ids: set[str] = set()
        for effect in self.external_effects:
            if not isinstance(effect, ExternalEffectRecord):
                raise ValueError("async operation result external_effects entries must be ExternalEffectRecord")
            if effect.effect_id in effect_ids:
                raise ValueError("async operation result external_effects must not contain duplicate effect_id")
            effect_ids.add(effect.effect_id)
            if effect.provider_effect_id is not None:
                if effect.provider_effect_id in provider_effect_ids:
                    raise ValueError(
                        "async operation result external_effects must not contain duplicate provider_effect_id"
                    )
                provider_effect_ids.add(effect.provider_effect_id)
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

    @classmethod
    def from_operation(cls, operation: AsyncOperation, output: object | None = None) -> AsyncOperationResult:
        if not isinstance(operation, AsyncOperation):
            raise ValueError("async operation result operation must be an AsyncOperation")
        if operation.state not in TERMINAL_ASYNC_OPERATION_STATES:
            raise ValueError("async operation result requires a terminal operation")
        return cls(operation_id=operation.operation_id, status=operation.state, output=output)

    @classmethod
    def from_late_callback(
        cls,
        operation: AsyncOperation,
        *,
        output: object | None = None,
        artifacts: Iterable[object] | None = None,
        diagnostics: Iterable[object] | None = None,
        metrics: Iterable[object] | None = None,
        checks: Iterable[object] | None = None,
        usage: Iterable[object] | None = None,
        external_effects: Iterable[ExternalEffectRecord] | None = None,
    ) -> AsyncOperationResult:
        if not isinstance(operation, AsyncOperation):
            raise ValueError("async operation result operation must be an AsyncOperation")
        if operation.state not in TERMINAL_ASYNC_OPERATION_STATES:
            raise ValueError("late callback result requires a terminal operation")
        result = cls.incomplete(operation.operation_id, output=output).with_projections(
            artifacts=() if artifacts is None else artifacts,
            diagnostics=() if diagnostics is None else diagnostics,
            metrics=() if metrics is None else metrics,
            checks=() if checks is None else checks,
            usage=() if usage is None else usage,
        )
        if external_effects is not None:
            result = result.with_external_effects(external_effects)
        return result

    def with_external_effects(
        self,
        external_effects: Iterable[ExternalEffectRecord],
    ) -> AsyncOperationResult:
        return replace(self, external_effects=_external_effect_sequence(external_effects))

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
            changes["artifacts"] = _projection_sequence("artifacts", artifacts)
        if diagnostics is not None:
            changes["diagnostics"] = _projection_sequence("diagnostics", diagnostics)
        if metrics is not None:
            changes["metrics"] = _projection_sequence("metrics", metrics)
        if checks is not None:
            changes["checks"] = _projection_sequence("checks", checks)
        if usage is not None:
            changes["usage"] = _projection_sequence("usage", usage)
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
