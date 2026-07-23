from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from .canonical import canonical_dumps, canonical_loads
from .documents import ArtifactRef, FrozenDict, FrozenList


ToolFailurePolicy = Literal["return_to_model", "fail", "fallback"]
VALID_TOOL_FAILURE_POLICIES = frozenset({"return_to_model", "fail", "fallback"})
AgentLoopDisposition = Literal["continue", "finalize", "stop"]
AgentStatePatchOpKind = Literal["set", "delete"]
_MAX_AGENT_STATE_REVISION = (1 << 64) - 1


def _validate_exact_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
    return value


def _validate_non_negative_integer(owner: str, field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{owner} {field_name} must be non-negative")
    return value


def _validate_agent_state_revision(
    owner: str,
    field_name: str,
    value: object,
) -> int:
    revision = _validate_non_negative_integer(owner, field_name, value)
    if revision > _MAX_AGENT_STATE_REVISION:
        raise ValueError(
            f"{owner} {field_name} must be at most "
            f"{_MAX_AGENT_STATE_REVISION}"
        )
    return revision


def _validate_string_tuple(
    owner: str,
    field_name: str,
    values: object,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        normalized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(
            f"{owner} {field_name} must be a collection of strings"
        ) from error
    for value in normalized:
        _validate_exact_non_empty_string(owner, f"{field_name} item", value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} {field_name} must not contain duplicates")
    return normalized


def _freeze_state_value(value: object) -> object:
    if isinstance(value, dict):
        return FrozenDict(
            {key: _freeze_state_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return FrozenList(_freeze_state_value(item) for item in value)
    return value


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
    output_policy_profile_ref: str | None = None

    def __post_init__(self) -> None:
        _validate_exact_non_empty_string("agent spec", "model_pool", self.model_pool)
        if not isinstance(self.tool_failure, str):
            raise ValueError("agent spec tool_failure must be a string")
        if self.tool_failure not in VALID_TOOL_FAILURE_POLICIES:
            raise ValueError(f"invalid tool failure policy {self.tool_failure}")
        _validate_non_negative_integer("agent spec", "max_steps", self.max_steps)
        if self.max_steps == 0:
            raise ValueError("agent spec max_steps must be positive")
        if not isinstance(self.parallel_tool_calls, bool):
            raise ValueError("agent spec parallel_tool_calls must be a boolean")
        if self.completion_reserve_units is not None:
            _validate_non_negative_integer(
                "agent spec",
                "completion_reserve_units",
                self.completion_reserve_units,
            )
        for field_name in (
            "state_schema",
            "budget_policy_ref",
            "completion_reserve_ref",
            "output_policy_profile_ref",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _validate_exact_non_empty_string("agent spec", field_name, value)
        tools = _validate_string_tuple("agent spec", "tools", self.tools)
        exit_conditions = _validate_string_tuple(
            "agent spec",
            "exit_conditions",
            self.exit_conditions,
        )
        if not exit_conditions:
            raise ValueError("agent spec exit_conditions must not be empty")
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "exit_conditions", exit_conditions)

    def with_tools(self, tools: list[str] | tuple[str, ...]) -> AgentSpec:
        return replace(self, tools=tuple(tools))

    def with_max_steps(self, max_steps: int) -> AgentSpec:
        return replace(self, max_steps=max_steps)

    def with_completion_reserve_units(self, completion_reserve_units: int) -> AgentSpec:
        return replace(self, completion_reserve_units=completion_reserve_units)

    def with_output_policy_profile_ref(self, output_policy_profile_ref: str) -> AgentSpec:
        return replace(self, output_policy_profile_ref=output_policy_profile_ref)


@dataclass(frozen=True, slots=True)
class AgentStateSchema:
    allowed_keys: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        allowed_keys = _validate_string_tuple(
            "agent state schema",
            "allowed_keys",
            self.allowed_keys,
        )
        object.__setattr__(self, "allowed_keys", tuple(sorted(allowed_keys)))

    def allows(self, key: str) -> bool:
        return key in self.allowed_keys


@dataclass(frozen=True, slots=True)
class AgentStatePatchOp:
    kind: AgentStatePatchOpKind
    key: str
    value: object = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"set", "delete"}:
            raise AgentStateError(f"unknown agent state patch operation {self.kind}")
        _validate_exact_non_empty_string("agent state patch", "key", self.key)
        if self.kind == "delete" and self.value is not None:
            raise AgentStateError("agent state delete operations must not carry a value")


@dataclass(frozen=True, slots=True)
class AgentStatePatch:
    ops: tuple[AgentStatePatchOp, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.ops, (str, bytes, bytearray, Mapping)):
            raise AgentStateError("agent state patch ops must be AgentStatePatchOp values")
        try:
            ops = tuple(self.ops)
        except TypeError as error:
            raise AgentStateError(
                "agent state patch ops must be AgentStatePatchOp values"
            ) from error
        if any(not isinstance(op, AgentStatePatchOp) for op in ops):
            raise AgentStateError("agent state patch ops must be AgentStatePatchOp values")
        object.__setattr__(self, "ops", ops)

    def set(self, key: str, value: object) -> AgentStatePatch:
        return replace(self, ops=(*self.ops, AgentStatePatchOp("set", key, value)))

    def delete(self, key: str) -> AgentStatePatch:
        return replace(self, ops=(*self.ops, AgentStatePatchOp("delete", key)))


class AgentStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentState:
    revision: int = 0
    values: dict[str, object] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = field(default_factory=tuple)
    pending_approvals: tuple[str, ...] = field(default_factory=tuple)
    pending_reviews: tuple[str, ...] = field(default_factory=tuple)
    budget_id: str | None = None
    active_task_plan_id: str | None = None

    def __post_init__(self) -> None:
        _validate_agent_state_revision("agent state", "revision", self.revision)
        if not isinstance(self.values, Mapping):
            raise AgentStateError("agent state values must be a mapping")
        try:
            values = canonical_loads(canonical_dumps(self.values))
        except (TypeError, ValueError) as error:
            raise AgentStateError("agent state values must be canonical JSON") from error
        if not isinstance(values, dict):
            raise AgentStateError("agent state values must be a mapping")
        try:
            artifacts = tuple(self.artifacts)
        except TypeError as error:
            raise AgentStateError(
                "agent state artifacts must be ArtifactRef values"
            ) from error
        if any(not isinstance(artifact, ArtifactRef) for artifact in artifacts):
            raise AgentStateError("agent state artifacts must be ArtifactRef values")
        pending_approvals = _validate_string_tuple(
            "agent state",
            "pending_approvals",
            self.pending_approvals,
        )
        pending_reviews = _validate_string_tuple(
            "agent state",
            "pending_reviews",
            self.pending_reviews,
        )
        for field_name in ("budget_id", "active_task_plan_id"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_exact_non_empty_string("agent state", field_name, value)
        object.__setattr__(self, "values", _freeze_state_value(values))
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "pending_approvals", pending_approvals)
        object.__setattr__(self, "pending_reviews", pending_reviews)

    def apply_patch(
        self,
        expected_revision: int,
        patch: AgentStatePatch,
        *,
        schema: AgentStateSchema | None = None,
    ) -> int:
        _validate_agent_state_revision(
            "agent state patch",
            "expected_revision",
            expected_revision,
        )
        if not isinstance(patch, AgentStatePatch):
            raise AgentStateError("agent state patch must be an AgentStatePatch")
        if schema is not None and not isinstance(schema, AgentStateSchema):
            raise AgentStateError("agent state schema must be an AgentStateSchema")
        if self.revision != expected_revision:
            raise AgentStateError(
                f"agent state is at revision {self.revision}, not expected revision {expected_revision}"
            )
        if self.revision == _MAX_AGENT_STATE_REVISION:
            raise AgentStateError("agent state revision is exhausted")
        set_values: dict[int, object] = {}
        for op in patch.ops:
            if schema is not None and not schema.allows(op.key):
                raise AgentStateError(f"agent state key {op.key!r} is not allowed")
            if op.kind == "set":
                try:
                    set_values[id(op)] = canonical_loads(
                        canonical_dumps(op.value)
                    )
                except (TypeError, ValueError) as error:
                    raise AgentStateError(
                        f"agent state value for {op.key!r} must be canonical JSON"
                    ) from error
        next_values = canonical_loads(canonical_dumps(self.values))
        if not isinstance(next_values, dict):
            raise AgentStateError("agent state values must be a mapping")
        for op in patch.ops:
            if op.kind == "set":
                next_values[op.key] = set_values[id(op)]
            elif op.kind == "delete":
                next_values.pop(op.key, None)
        object.__setattr__(self, "values", _freeze_state_value(next_values))
        object.__setattr__(self, "revision", self.revision + 1)
        return self.revision


@dataclass(frozen=True, slots=True)
class AgentLoopDecision:
    disposition: AgentLoopDisposition
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.disposition, str)
            or self.disposition not in {"continue", "finalize", "stop"}
        ):
            raise ValueError(f"invalid agent loop disposition {self.disposition}")
        _validate_exact_non_empty_string("agent loop decision", "reason", self.reason)

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

    def __post_init__(self) -> None:
        if not isinstance(self.spec, AgentSpec):
            raise ValueError("agent loop controller spec must be an AgentSpec")

    def decide_next_step(self, completed_steps: int, remaining_budget_units: int) -> AgentLoopDecision:
        _validate_non_negative_integer(
            "agent loop",
            "completed_steps",
            completed_steps,
        )
        _validate_non_negative_integer(
            "agent loop",
            "remaining_budget_units",
            remaining_budget_units,
        )
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
