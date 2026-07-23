from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Literal


OutcomeStatus = Literal[
    "value",
    "absent",
    "skipped",
    "denied",
    "budget_exhausted",
    "paused",
    "failed",
    "cancelled",
]
InputMode = Literal["value", "outcome"]
ReadinessKind = Literal["ready", "waiting", "blocked"]
ResolvedInputKind = Literal["value", "outcome"]
VALID_OUTCOME_STATUSES = frozenset(
    ("value", "absent", "skipped", "denied", "budget_exhausted", "paused", "failed", "cancelled")
)
VALID_INPUT_MODES = frozenset(("value", "outcome"))
VALID_READINESS_KINDS = frozenset(("ready", "waiting", "blocked"))
VALID_RESOLVED_INPUT_KINDS = frozenset(("value", "outcome"))
_MAX_METADATA_DEPTH = 64
_MAX_METADATA_NODES = 10_000


def _contains_forbidden_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if _contains_forbidden_control(value):
        raise ValueError(f"{owner} {field_name} must not contain control characters")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _freeze_metadata(owner: str, metadata: object) -> Mapping[str, object]:
    if not isinstance(metadata, Mapping):
        raise ValueError(f"{owner} metadata must be a mapping")
    frozen = _freeze_metadata_value(
        owner,
        metadata,
        depth=0,
        active_containers=set(),
        node_count=[0],
    )
    assert isinstance(frozen, Mapping)
    return frozen


def _freeze_metadata_value(
    owner: str,
    value: object,
    *,
    depth: int,
    active_containers: set[int],
    node_count: list[int],
) -> object:
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError(f"{owner} metadata exceeds maximum depth {_MAX_METADATA_DEPTH}")
    node_count[0] += 1
    if node_count[0] > _MAX_METADATA_NODES:
        raise ValueError(f"{owner} metadata exceeds maximum node count {_MAX_METADATA_NODES}")
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ValueError(f"{owner} metadata must not be recursive")
        active_containers.add(identity)
        try:
            snapshot: dict[str, object] = {}
            try:
                for key, item in value.items():
                    key_text = _validate_non_empty_string(owner, "metadata key", key)
                    if key_text != key_text.strip():
                        raise ValueError(
                            f"{owner} metadata key must not contain surrounding whitespace"
                        )
                    snapshot[key_text] = _freeze_metadata_value(
                        owner,
                        item,
                        depth=depth + 1,
                        active_containers=active_containers,
                        node_count=node_count,
                    )
            except RuntimeError as error:
                raise ValueError(
                    f"{owner} metadata must be a stable mapping"
                ) from error
            return MappingProxyType(snapshot)
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError(f"{owner} metadata must not be recursive")
        active_containers.add(identity)
        try:
            return tuple(
                _freeze_metadata_value(
                    owner,
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                    node_count=node_count,
                )
                for item in value
            )
        finally:
            active_containers.remove(identity)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{owner} metadata numbers must be finite")
    raise ValueError(f"{owner} metadata values must be JSON-compatible")


@dataclass(frozen=True, order=True, slots=True)
class PortRef:
    node: str
    port: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node", _validate_non_empty_string("port ref", "node", self.node).strip())
        object.__setattr__(self, "port", _validate_non_empty_string("port ref", "port", self.port).strip())


@dataclass(frozen=True, slots=True)
class Outcome:
    status: OutcomeStatus
    payload: object = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in VALID_OUTCOME_STATUSES:
            raise ValueError(f"invalid outcome status {self.status}")
        object.__setattr__(self, "code", _validate_optional_non_empty_string("outcome", "code", self.code))
        object.__setattr__(self, "message", _validate_optional_non_empty_string("outcome", "message", self.message))
        if not isinstance(self.retryable, bool):
            raise ValueError("outcome retryable must be a boolean")
        if self.status in {"skipped", "denied", "budget_exhausted", "paused", "failed", "cancelled"} and self.code is None:
            raise ValueError(f"outcome status {self.status} requires code")
        if self.status not in {"failed"} and self.retryable:
            raise ValueError("only failed outcomes may be retryable")
        object.__setattr__(self, "metadata", _freeze_metadata("outcome", self.metadata))

    @classmethod
    def value(cls, value: object) -> Outcome:
        return cls(status="value", payload=value)

    @classmethod
    def absent(cls) -> Outcome:
        return cls(status="absent")

    @classmethod
    def skipped(cls, code: str, message: str | None = None) -> Outcome:
        return cls(status="skipped", code=code, message=message)

    @classmethod
    def denied(cls, decision_id: str, message: str | None = None) -> Outcome:
        return cls(status="denied", code=decision_id, message=message)

    @classmethod
    def budget_exhausted(cls, code: str, message: str | None = None) -> Outcome:
        return cls(status="budget_exhausted", code=code, message=message)

    @classmethod
    def paused(cls, code: str, message: str | None = None) -> Outcome:
        return cls(status="paused", code=code, message=message)

    @classmethod
    def failed(cls, code: str, message: str | None = None, *, retryable: bool = False) -> Outcome:
        return cls(status="failed", code=code, message=message, retryable=retryable)

    @classmethod
    def cancelled(cls, code: str, message: str | None = None) -> Outcome:
        return cls(status="cancelled", code=code, message=message)


@dataclass(frozen=True, slots=True)
class InputDependency:
    input: str
    source: PortRef
    mode: InputMode = "value"

    def __post_init__(self) -> None:
        object.__setattr__(self, "input", _validate_non_empty_string("input dependency", "input", self.input).strip())
        if not isinstance(self.source, PortRef):
            raise ValueError("input dependency source must be PortRef")
        if not isinstance(self.mode, str) or self.mode not in VALID_INPUT_MODES:
            raise ValueError(f"invalid input dependency mode {self.mode}")

    @classmethod
    def value(cls, input: str, source: PortRef) -> InputDependency:
        return cls(input=input, source=source, mode="value")

    @classmethod
    def outcome(cls, input: str, source: PortRef) -> InputDependency:
        return cls(input=input, source=source, mode="outcome")


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    kind: ResolvedInputKind
    payload: object

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in VALID_RESOLVED_INPUT_KINDS:
            raise ValueError(f"invalid resolved input kind {self.kind}")
        if self.kind == "outcome" and not isinstance(self.payload, Outcome):
            raise ValueError("resolved input outcome payload must be Outcome")

    @classmethod
    def value(cls, value: object) -> ResolvedInput:
        return cls(kind="value", payload=value)

    @classmethod
    def outcome(cls, outcome: Outcome) -> ResolvedInput:
        return cls(kind="outcome", payload=outcome)


@dataclass(frozen=True, slots=True)
class Readiness:
    kind: ReadinessKind
    inputs: dict[str, ResolvedInput] = field(default_factory=dict)
    missing: tuple[PortRef, ...] = field(default_factory=tuple)
    input: str | None = None
    source: PortRef | None = None
    outcome: Outcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in VALID_READINESS_KINDS:
            raise ValueError(f"invalid readiness kind {self.kind}")
        if not isinstance(self.inputs, Mapping):
            raise ValueError("readiness inputs must be a mapping")
        inputs: dict[str, ResolvedInput] = {}
        for key, value in self.inputs.items():
            normalized_key = _validate_non_empty_string(
                "readiness",
                "inputs key",
                key,
            ).strip()
            if normalized_key in inputs:
                raise ValueError("readiness inputs must not contain duplicate normalized keys")
            inputs[normalized_key] = _validate_resolved_input(value)
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        try:
            missing = tuple(self.missing)
        except TypeError as error:
            raise ValueError(
                "readiness missing entries must be PortRef"
            ) from error
        if any(not isinstance(port, PortRef) for port in missing):
            raise ValueError("readiness missing entries must be PortRef")
        object.__setattr__(self, "missing", missing)

        if self.input is not None:
            object.__setattr__(self, "input", _validate_non_empty_string("readiness", "input", self.input).strip())
        if self.source is not None and not isinstance(self.source, PortRef):
            raise ValueError("readiness source must be PortRef")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise ValueError("readiness outcome must be Outcome")

        if self.kind == "ready":
            if self.missing or self.input is not None or self.source is not None or self.outcome is not None:
                raise ValueError("ready readiness must not carry missing or blocked fields")
        elif self.kind == "waiting":
            if not self.missing:
                raise ValueError("waiting readiness requires missing dependencies")
            if self.inputs or self.input is not None or self.source is not None or self.outcome is not None:
                raise ValueError("waiting readiness must only carry missing dependencies")
        else:
            if self.inputs or self.missing or self.input is None or self.source is None or self.outcome is None:
                raise ValueError("blocked readiness requires input, source, and outcome only")
            if self.outcome.status == "value":
                raise ValueError("blocked readiness outcome must not be a value outcome")

    @classmethod
    def ready(cls, inputs: dict[str, ResolvedInput]) -> Readiness:
        return cls(kind="ready", inputs=dict(inputs))

    @classmethod
    def waiting(cls, missing: list[PortRef]) -> Readiness:
        return cls(kind="waiting", missing=tuple(missing))

    @classmethod
    def blocked(cls, input: str, source: PortRef, outcome: Outcome) -> Readiness:
        return cls(kind="blocked", input=input, source=source, outcome=outcome)


@dataclass(slots=True)
class ReadinessTracker:
    signals: Mapping[PortRef, Outcome] = field(default_factory=dict)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "signals" and hasattr(self, "signals"):
            raise AttributeError("readiness signals cannot be replaced")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        if not isinstance(self.signals, Mapping):
            raise ValueError("readiness signals must be a mapping")
        signals = dict(self.signals)
        if any(not isinstance(port, PortRef) for port in signals):
            raise ValueError("readiness signal keys must be PortRef")
        if any(not isinstance(outcome, Outcome) for outcome in signals.values()):
            raise ValueError("readiness signal values must be Outcome")
        object.__setattr__(self, "signals", MappingProxyType(signals))

    def publish(self, port: PortRef, outcome: Outcome) -> Outcome | None:
        if not isinstance(port, PortRef):
            raise ValueError("readiness signal port must be PortRef")
        if not isinstance(outcome, Outcome):
            raise ValueError("readiness signal outcome must be Outcome")
        previous = self.signals.get(port)
        signals = dict(self.signals)
        signals[port] = outcome
        object.__setattr__(self, "signals", MappingProxyType(signals))
        return previous

    def signal(self, port: PortRef) -> Outcome | None:
        if not isinstance(port, PortRef):
            raise ValueError("readiness signal port must be PortRef")
        return self.signals.get(port)

    def readiness(self, dependencies: list[InputDependency]) -> Readiness:
        if isinstance(dependencies, (str, bytes, bytearray)):
            raise ValueError("readiness dependencies must be a collection")
        try:
            normalized_dependencies = tuple(dependencies)
        except TypeError as error:
            raise ValueError("readiness dependencies must be a collection") from error
        if any(
            not isinstance(dependency, InputDependency)
            for dependency in normalized_dependencies
        ):
            raise ValueError("readiness dependencies must be InputDependency")
        dependency_inputs = [dependency.input for dependency in normalized_dependencies]
        if len(set(dependency_inputs)) != len(dependency_inputs):
            raise ValueError("readiness dependencies must not contain duplicate inputs")

        missing: list[PortRef] = []
        resolved: dict[str, ResolvedInput] = {}

        for dependency in normalized_dependencies:
            outcome = self.signals.get(dependency.source)
            if outcome is None:
                missing.append(dependency.source)
                continue

            if dependency.mode == "value":
                if outcome.status == "value":
                    resolved[dependency.input] = ResolvedInput.value(outcome.payload)
                else:
                    return Readiness.blocked(dependency.input, dependency.source, outcome)
            else:
                resolved[dependency.input] = ResolvedInput.outcome(outcome)

        if missing:
            return Readiness.waiting(missing)
        return Readiness.ready(resolved)


def _validate_resolved_input(value: object) -> ResolvedInput:
    if not isinstance(value, ResolvedInput):
        raise ValueError("readiness inputs values must be ResolvedInput")
    return value
