from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from .documents import ArtifactRef


ToolFailurePolicy = Literal["return_to_model", "fail", "fallback"]
VALID_TOOL_FAILURE_POLICIES = frozenset({"return_to_model", "fail", "fallback"})
AgentLoopDisposition = Literal["continue", "finalize", "stop"]
AgentStatePatchOpKind = Literal["set", "delete"]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    model_pool: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    state_schema: str | None = None
    max_steps: int = 12
    exit_conditions: tuple[str, ...] = ("final_message",)
    tool_failure: ToolFailurePolicy = "return_to_model"
    parallel_tool_calls: bool = True
    budget_policy_ref: str | None = None
    completion_reserve_ref: str | None = None
    completion_reserve_units: int | None = None

    def __post_init__(self) -> None:
        if self.tool_failure not in VALID_TOOL_FAILURE_POLICIES:
            raise ValueError(f"invalid tool failure policy {self.tool_failure}")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "exit_conditions", tuple(self.exit_conditions))

    def with_tools(self, tools: list[str] | tuple[str, ...]) -> AgentSpec:
        return replace(self, tools=tuple(tools))

    def with_max_steps(self, max_steps: int) -> AgentSpec:
        return replace(self, max_steps=max_steps)

    def with_completion_reserve_units(self, completion_reserve_units: int) -> AgentSpec:
        return replace(self, completion_reserve_units=completion_reserve_units)


@dataclass(frozen=True, slots=True)
class AgentStateSchema:
    allowed_keys: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_keys", tuple(sorted(set(self.allowed_keys))))

    def allows(self, key: str) -> bool:
        return key in self.allowed_keys


@dataclass(frozen=True, slots=True)
class AgentStatePatchOp:
    kind: AgentStatePatchOpKind
    key: str
    value: object = None


@dataclass(frozen=True, slots=True)
class AgentStatePatch:
    ops: tuple[AgentStatePatchOp, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ops", tuple(self.ops))

    def set(self, key: str, value: object) -> AgentStatePatch:
        return replace(self, ops=(*self.ops, AgentStatePatchOp("set", key, value)))

    def delete(self, key: str) -> AgentStatePatch:
        return replace(self, ops=(*self.ops, AgentStatePatchOp("delete", key)))


class AgentStateError(ValueError):
    pass


@dataclass(slots=True)
class AgentState:
    revision: int = 0
    values: dict[str, object] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = field(default_factory=tuple)
    pending_approvals: tuple[str, ...] = field(default_factory=tuple)
    pending_reviews: tuple[str, ...] = field(default_factory=tuple)
    budget_id: str | None = None
    active_task_plan_id: str | None = None

    def __post_init__(self) -> None:
        self.values = dict(self.values)
        self.artifacts = tuple(self.artifacts)
        self.pending_approvals = tuple(self.pending_approvals)
        self.pending_reviews = tuple(self.pending_reviews)

    def apply_patch(
        self,
        expected_revision: int,
        patch: AgentStatePatch,
        *,
        schema: AgentStateSchema | None = None,
    ) -> int:
        if self.revision != expected_revision:
            raise AgentStateError(
                f"agent state is at revision {self.revision}, not expected revision {expected_revision}"
            )
        for op in patch.ops:
            if op.kind not in {"set", "delete"}:
                raise AgentStateError(f"unknown agent state patch operation {op.kind}")
            if schema is not None and not schema.allows(op.key):
                raise AgentStateError(f"agent state key {op.key!r} is not allowed")
        for op in patch.ops:
            if op.kind == "set":
                self.values[op.key] = op.value
            elif op.kind == "delete":
                self.values.pop(op.key, None)
        self.revision += 1
        return self.revision


@dataclass(frozen=True, slots=True)
class AgentLoopDecision:
    disposition: AgentLoopDisposition
    reason: str

    @classmethod
    def continue_(cls, reason: str) -> AgentLoopDecision:
        return cls("continue", reason)

    @classmethod
    def finalize(cls, reason: str) -> AgentLoopDecision:
        return cls("finalize", reason)

    @classmethod
    def stop(cls, reason: str) -> AgentLoopDecision:
        return cls("stop", reason)


@dataclass(frozen=True, slots=True)
class AgentLoopController:
    spec: AgentSpec

    def decide_next_step(self, completed_steps: int, remaining_budget_units: int) -> AgentLoopDecision:
        if completed_steps >= self.spec.max_steps:
            return AgentLoopDecision.stop("max_steps_reached")
        if (
            self.spec.completion_reserve_units is not None
            and remaining_budget_units <= self.spec.completion_reserve_units
        ):
            return AgentLoopDecision.finalize("completion_reserve_reached")
        return AgentLoopDecision.continue_("admitted")


__all__ = [
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
    "ToolFailurePolicy",
]
