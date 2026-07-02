from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

from .tools import ToolEffectOutcome, VALID_TOOL_EFFECT_OUTCOMES


AsyncOperationResultStatusValue = Literal["completed", "failed", "cancelled", "expired", "incomplete"]


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


def _validate_effect_outcome(value: object) -> ToolEffectOutcome:
    outcome = _validate_non_empty_string("external effect", "outcome", value)
    if outcome not in VALID_TOOL_EFFECT_OUTCOMES:
        raise ValueError(
            "external effect outcome must be one of no_external_effect, committed, not_committed, or unknown"
        )
    return outcome  # type: ignore[return-value]


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
