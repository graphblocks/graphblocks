from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True, order=True, slots=True)
class PortRef:
    node: str
    port: str


@dataclass(frozen=True, slots=True)
class Outcome:
    status: OutcomeStatus
    payload: object = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

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
    signals: dict[PortRef, Outcome] = field(default_factory=dict)

    def publish(self, port: PortRef, outcome: Outcome) -> Outcome | None:
        previous = self.signals.get(port)
        self.signals[port] = outcome
        return previous

    def signal(self, port: PortRef) -> Outcome | None:
        return self.signals.get(port)

    def readiness(self, dependencies: list[InputDependency]) -> Readiness:
        missing: list[PortRef] = []
        resolved: dict[str, ResolvedInput] = {}

        for dependency in dependencies:
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
