from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Literal

from .canonical import canonical_hash
from .conversation import ContentPart
from .documents import ArtifactRef


JsonSchemaRef = str
GraphRef = str
ResourceRef = str

ToolEffect = Literal[
    "none",
    "external_read",
    "external_write",
    "filesystem_read",
    "filesystem_write",
    "process",
    "network",
    "destructive",
]
ToolApproval = Literal["never", "policy", "always"]
ToolIdempotency = Literal["not_applicable", "optional", "required"]
ToolCancellation = Literal["unsupported", "cooperative", "force_terminable"]
ToolResultMode = Literal["value", "incremental", "bounded_sequence", "artifact_reference"]
ToolCallDraftStatus = Literal["proposed", "arguments_streaming", "arguments_complete"]
ToolCallStatus = Literal[
    "validated",
    "policy_pending",
    "approval_pending",
    "admitted",
    "running",
    "completed",
    "failed",
    "denied",
    "cancelled",
    "policy_stopped",
    "expired",
]
ToolResultStatus = Literal["completed", "failed", "denied", "cancelled", "policy_stopped", "incomplete"]
ToolResultEventKind = Literal["started", "delta", "artifact_ready", "completed"]
ToolExecutionFailurePolicy = Literal["fail_fast", "collect", "return_failures_to_model"]
ToolExecutionCancellationPolicy = Literal["cancel_dependents", "cancel_all", "allow_independent_calls"]
ToolExecutionState = Literal["pending", "running", "completed", "failed", "denied", "cancelled", "skipped"]
PendingToolCallsDisposition = Literal["keep", "deny", "cancel_admitted"]


class ToolCallError(RuntimeError):
    pass


class ToolExecutionPlanError(RuntimeError):
    pass


class ToolResolutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonSchemaRef
    output_schema: JsonSchemaRef | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", frozenset(self.tags))

    def model_contract(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "tags": sorted(self.tags),
            "version": self.version,
        }

    def digest(self) -> str:
        return canonical_hash(self.model_contract())


@dataclass(frozen=True, slots=True)
class BlockToolImplementation:
    block: str
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    kind: Literal["block"] = field(default="block", init=False)

    def canonical_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "block": self.block,
            "input_mapping": dict(sorted(self.input_mapping.items())),
            "output_mapping": dict(sorted(self.output_mapping.items())),
        }


@dataclass(frozen=True, slots=True)
class GraphToolImplementation:
    graph: GraphRef
    input_mapping: dict[str, str] = field(default_factory=dict)
    output_mapping: dict[str, str] = field(default_factory=dict)
    kind: Literal["graph"] = field(default="graph", init=False)

    def canonical_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "graph": self.graph,
            "input_mapping": dict(sorted(self.input_mapping.items())),
            "output_mapping": dict(sorted(self.output_mapping.items())),
        }


@dataclass(frozen=True, slots=True)
class RemoteToolImplementation:
    connection: ResourceRef
    operation: str
    kind: Literal["remote"] = field(default="remote", init=False)

    def canonical_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "connection": self.connection,
            "operation": self.operation,
        }


@dataclass(frozen=True, slots=True)
class McpToolImplementation:
    server: ResourceRef
    remote_name: str
    kind: Literal["mcp"] = field(default="mcp", init=False)

    def canonical_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "server": self.server,
            "remote_name": self.remote_name,
        }


@dataclass(frozen=True, slots=True)
class OpenApiToolImplementation:
    connection: ResourceRef
    operation_id: str
    kind: Literal["openapi"] = field(default="openapi", init=False)

    def canonical_value(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "connection": self.connection,
            "operation_id": self.operation_id,
        }


ToolImplementation = (
    BlockToolImplementation
    | GraphToolImplementation
    | RemoteToolImplementation
    | McpToolImplementation
    | OpenApiToolImplementation
)


@dataclass(frozen=True, slots=True)
class ToolBinding:
    binding_id: str
    tool_name: str
    implementation: ToolImplementation
    effects: frozenset[ToolEffect] = field(default_factory=frozenset)
    approval: ToolApproval = "policy"
    idempotency: ToolIdempotency = "optional"
    cancellation: ToolCancellation = "cooperative"
    result_mode: ToolResultMode = "value"
    timeout_ms: int | None = None
    retry_policy_ref: str | None = None
    policy_profile_ref: str | None = None
    execution_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", frozenset(self.effects))

    def binding_contract(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "tool_name": self.tool_name,
            "implementation": self.implementation.canonical_value(),
            "effects": sorted(self.effects),
            "approval": self.approval,
            "idempotency": self.idempotency,
            "cancellation": self.cancellation,
            "result_mode": self.result_mode,
            "timeout_ms": self.timeout_ms,
            "retry_policy_ref": self.retry_policy_ref,
            "policy_profile_ref": self.policy_profile_ref,
            "execution_class": self.execution_class,
        }

    def digest(self) -> str:
        return canonical_hash(self.binding_contract())


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    resolved_tool_id: str
    definition: ToolDefinition
    binding: ToolBinding
    definition_digest: str
    binding_digest: str
    effective_policy_snapshot_id: str
    allowed_for_principal: bool
    valid_until: str | None = None

    @classmethod
    def from_definition_and_binding(
        cls,
        *,
        resolved_tool_id: str,
        definition: ToolDefinition,
        binding: ToolBinding,
        effective_policy_snapshot_id: str,
        allowed_for_principal: bool,
        valid_until: str | None = None,
    ) -> ResolvedTool:
        return cls(
            resolved_tool_id=resolved_tool_id,
            definition=definition,
            binding=binding,
            definition_digest=definition.digest(),
            binding_digest=binding.digest(),
            effective_policy_snapshot_id=effective_policy_snapshot_id,
            allowed_for_principal=allowed_for_principal,
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ToolResolutionScope:
    application_tools: frozenset[str] | None = None
    graph_tools: frozenset[str] | None = None
    principal_tools: frozenset[str] | None = None
    tenant_policy_tools: frozenset[str] | None = None
    conversation_policy_tools: frozenset[str] | None = None
    data_classification_tools: frozenset[str] | None = None
    deployment_tools: frozenset[str] | None = None
    budget_tools: frozenset[str] | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "application_tools",
            "graph_tools",
            "principal_tools",
            "tenant_policy_tools",
            "conversation_policy_tools",
            "data_classification_tools",
            "deployment_tools",
            "budget_tools",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, frozenset(value))

    def allows(self, tool_name: str) -> bool:
        return all(
            tools is None or tool_name in tools
            for tools in (
                self.application_tools,
                self.graph_tools,
                self.principal_tools,
                self.tenant_policy_tools,
                self.conversation_policy_tools,
                self.data_classification_tools,
                self.deployment_tools,
                self.budget_tools,
            )
        )

    def contains_in_principal_scope(self, tool_name: str) -> bool:
        return self.principal_tools is None or tool_name in self.principal_tools


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    definitions: tuple[ToolDefinition, ...]
    bindings: tuple[ToolBinding, ...]
    _definitions_by_name: dict[str, ToolDefinition] = field(init=False, repr=False)
    _bindings_by_tool: dict[str, ToolBinding] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "definitions", tuple(self.definitions))
        object.__setattr__(self, "bindings", tuple(self.bindings))
        definitions_by_name: dict[str, ToolDefinition] = {}
        for definition in self.definitions:
            if definition.name in definitions_by_name:
                raise ToolResolutionError(f"duplicate tool definition {definition.name}")
            definitions_by_name[definition.name] = definition

        binding_ids: set[str] = set()
        bindings_by_tool: dict[str, ToolBinding] = {}
        for binding in self.bindings:
            if binding.binding_id in binding_ids:
                raise ToolResolutionError(f"duplicate tool binding {binding.binding_id}")
            binding_ids.add(binding.binding_id)
            if binding.tool_name not in definitions_by_name:
                raise ToolResolutionError(
                    f"tool binding {binding.binding_id} references unknown tool {binding.tool_name}"
                )
            if binding.tool_name in bindings_by_tool:
                raise ToolResolutionError(f"multiple bindings for tool {binding.tool_name}")
            bindings_by_tool[binding.tool_name] = binding

        object.__setattr__(self, "_definitions_by_name", definitions_by_name)
        object.__setattr__(self, "_bindings_by_tool", bindings_by_tool)

    def resolve(
        self,
        scope: ToolResolutionScope,
        *,
        effective_policy_snapshot_id: str,
    ) -> list[ResolvedTool]:
        resolved: list[ResolvedTool] = []
        for tool_name in sorted(self._definitions_by_name):
            if not scope.allows(tool_name):
                continue
            definition = self._definitions_by_name[tool_name]
            binding = self._bindings_by_tool.get(tool_name)
            if binding is None:
                raise ToolResolutionError(f"tool binding missing for {tool_name}")
            definition_digest = definition.digest()
            binding_digest = binding.digest()
            resolved.append(
                ResolvedTool(
                    resolved_tool_id=canonical_hash(
                        {
                            "tool_name": tool_name,
                            "definition_digest": definition_digest,
                            "binding_digest": binding_digest,
                            "policy_snapshot": effective_policy_snapshot_id,
                        }
                    ),
                    definition=definition,
                    binding=binding,
                    definition_digest=definition_digest,
                    binding_digest=binding_digest,
                    effective_policy_snapshot_id=effective_policy_snapshot_id,
                    allowed_for_principal=scope.contains_in_principal_scope(tool_name),
                )
            )
        return resolved


@dataclass(frozen=True, slots=True)
class ToolCallDraft:
    response_id: str
    tool_call_id: str
    tool_name: str
    argument_fragments: tuple[str, ...] = field(default_factory=tuple)
    sequence: int = 0
    status: ToolCallDraftStatus = "proposed"

    @classmethod
    def proposed(cls, response_id: str, tool_call_id: str, tool_name: str) -> ToolCallDraft:
        return cls(response_id=response_id, tool_call_id=tool_call_id, tool_name=tool_name)

    def append_argument_fragment(self, fragment: str) -> ToolCallDraft:
        if self.status == "arguments_complete":
            raise ToolCallError("tool arguments are already complete")
        return replace(
            self,
            argument_fragments=(*self.argument_fragments, fragment),
            sequence=self.sequence + 1,
            status="arguments_streaming",
        )

    def complete_arguments(self) -> ToolCallDraft:
        if self.status == "arguments_complete":
            raise ToolCallError("tool arguments are already complete")
        return replace(self, status="arguments_complete")

    def into_tool_call(self, resolved_tool_id: str, *, created_at: str) -> ToolCall:
        if self.status != "arguments_complete":
            raise ToolCallError("tool arguments are not complete")

        try:
            arguments = json.loads("".join(self.argument_fragments))
        except json.JSONDecodeError as error:
            raise ToolCallError("tool arguments are invalid JSON") from error

        return ToolCall(
            tool_call_id=self.tool_call_id,
            response_id=self.response_id,
            resolved_tool_id=resolved_tool_id,
            name=self.tool_name,
            arguments=arguments,
            arguments_digest=canonical_hash(arguments),
            revision=1,
            status="validated",
            depends_on=(),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_call_id: str
    response_id: str
    resolved_tool_id: str
    name: str
    arguments: object
    arguments_digest: str
    revision: int = 1
    status: ToolCallStatus = "validated"
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    created_at: str | None = None
    admitted_at: str | None = None
    completed_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    def revise_arguments(self, arguments: object) -> ToolCall:
        if self.status != "validated":
            raise ToolCallError("tool arguments cannot be revised after validation")
        return replace(
            self,
            arguments=arguments,
            arguments_digest=canonical_hash(arguments),
            revision=self.revision + 1,
            status="validated",
            admitted_at=None,
            completed_at=None,
        )

    def with_status(
        self,
        status: ToolCallStatus,
        *,
        admitted_at: str | None = None,
        completed_at: str | None = None,
    ) -> ToolCall:
        return replace(self, status=status, admitted_at=admitted_at, completed_at=completed_at)


@dataclass(frozen=True, slots=True)
class ToolPlanCall:
    call: ToolCall
    effect_key: str | None = None


@dataclass(slots=True)
class ToolExecutionPlan:
    plan_id: str
    response_id: str
    calls: tuple[ToolPlanCall, ...]
    maximum_parallelism: int
    failure_policy: ToolExecutionFailurePolicy = "return_failures_to_model"
    cancellation_policy: ToolExecutionCancellationPolicy = "cancel_dependents"
    _states: dict[str, ToolExecutionState] = field(init=False, repr=False)
    _calls_by_id: dict[str, ToolPlanCall] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.maximum_parallelism <= 0:
            raise ToolExecutionPlanError("maximum_parallelism must be positive")
        self.calls = tuple(self.calls)
        self._states = {}
        self._calls_by_id = {}
        for planned_call in self.calls:
            tool_call_id = planned_call.call.tool_call_id
            if tool_call_id in self._calls_by_id:
                raise ToolExecutionPlanError(f"duplicate tool call {tool_call_id}")
            self._calls_by_id[tool_call_id] = planned_call
            self._states[tool_call_id] = "pending"

    def state(self, tool_call_id: str) -> ToolExecutionState | None:
        return self._states.get(tool_call_id)

    def ready_call_ids(self) -> list[str]:
        running_count = self._running_count()
        if running_count >= self.maximum_parallelism:
            return []

        remaining_slots = self.maximum_parallelism - running_count
        reserved_effect_keys = self._running_effect_keys()
        ready: list[str] = []
        for planned_call in self.calls:
            if remaining_slots == 0:
                break
            tool_call_id = planned_call.call.tool_call_id
            if self._states[tool_call_id] != "pending":
                continue
            if not self._dependencies_completed(planned_call.call):
                continue
            if planned_call.effect_key is not None:
                if planned_call.effect_key in reserved_effect_keys:
                    continue
                reserved_effect_keys.add(planned_call.effect_key)

            ready.append(tool_call_id)
            remaining_slots -= 1
        return ready

    def record_started(self, tool_call_id: str) -> None:
        current = self._states.get(tool_call_id)
        if current is None:
            raise ToolExecutionPlanError(f"unknown tool call {tool_call_id}")
        if current != "pending":
            raise ToolExecutionPlanError(f"tool call {tool_call_id} is {current}, not pending")

        planned_call = self._calls_by_id[tool_call_id]
        if not self._dependencies_completed(planned_call.call):
            raise ToolExecutionPlanError(f"tool call {tool_call_id} dependencies are not ready")
        if self._running_count() >= self.maximum_parallelism:
            raise ToolExecutionPlanError("maximum parallelism is exhausted")
        if planned_call.effect_key is not None and planned_call.effect_key in self._running_effect_keys():
            raise ToolExecutionPlanError(f"effect key {planned_call.effect_key} is already running")

        self._states[tool_call_id] = "running"

    def record_completed(self, tool_call_id: str) -> None:
        self._enter_terminal(tool_call_id, "completed")

    def record_failed(self, tool_call_id: str) -> None:
        self._enter_terminal(tool_call_id, "failed")

    def apply_policy_stop(self, pending_tool_calls: PendingToolCallsDisposition) -> list[str]:
        affected: list[str] = []
        if pending_tool_calls == "keep":
            return affected
        if pending_tool_calls == "deny":
            for planned_call in self.calls:
                tool_call_id = planned_call.call.tool_call_id
                if self._states[tool_call_id] == "pending":
                    self._states[tool_call_id] = "denied"
                    affected.append(tool_call_id)
            return affected
        if pending_tool_calls == "cancel_admitted":
            for planned_call in self.calls:
                tool_call_id = planned_call.call.tool_call_id
                if self._states[tool_call_id] == "running":
                    self._states[tool_call_id] = "cancelled"
                    affected.append(tool_call_id)
                elif self._states[tool_call_id] == "pending":
                    self._states[tool_call_id] = "denied"
                    affected.append(tool_call_id)
            return affected
        raise ToolExecutionPlanError(f"unknown pending tool call disposition {pending_tool_calls}")

    def _enter_terminal(self, tool_call_id: str, terminal_state: ToolExecutionState) -> None:
        current = self._states.get(tool_call_id)
        if current is None:
            raise ToolExecutionPlanError(f"unknown tool call {tool_call_id}")
        if current != "running":
            raise ToolExecutionPlanError(f"tool call {tool_call_id} is {current}, not running")
        self._states[tool_call_id] = terminal_state

    def _running_count(self) -> int:
        return sum(1 for state in self._states.values() if state == "running")

    def _running_effect_keys(self) -> set[str]:
        return {
            planned_call.effect_key
            for planned_call in self.calls
            if planned_call.effect_key is not None and self._states[planned_call.call.tool_call_id] == "running"
        }

    def _dependencies_completed(self, call: ToolCall) -> bool:
        return all(self._states.get(dependency) == "completed" for dependency in call.depends_on)


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    status: ToolResultStatus
    output: tuple[ContentPart, ...] = field(default_factory=tuple)
    output_digest: str | None = None
    artifacts: tuple[dict[str, object], ...] = field(default_factory=tuple)
    diagnostics: tuple[dict[str, object], ...] = field(default_factory=tuple)
    error: dict[str, object] | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def completed(
        cls,
        tool_call_id: str,
        output: tuple[ContentPart, ...],
        *,
        started_at: str,
        completed_at: str,
    ) -> ToolResult:
        output = tuple(output)
        output_value = [
            {
                "kind": part.kind,
                "text": part.text,
                "data": part.data,
                "metadata": part.metadata,
            }
            for part in output
        ]
        return cls(
            tool_call_id=tool_call_id,
            status="completed",
            output=output,
            output_digest=canonical_hash(output_value),
            started_at=started_at,
            completed_at=completed_at,
        )

    @classmethod
    def failed(
        cls,
        tool_call_id: str,
        *,
        error: dict[str, object],
        started_at: str,
        completed_at: str,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call_id,
            status="failed",
            error=error,
            started_at=started_at,
            completed_at=completed_at,
        )

    @classmethod
    def policy_stopped(
        cls,
        tool_call_id: str,
        *,
        error: dict[str, object],
        started_at: str,
        completed_at: str,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call_id,
            status="policy_stopped",
            error=error,
            started_at=started_at,
            completed_at=completed_at,
        )


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    kind: ToolResultEventKind
    tool_call_id: str
    sequence: int
    started_at: str | None = None
    output: tuple[ContentPart, ...] = field(default_factory=tuple)
    artifact: ArtifactRef | None = None
    result: ToolResult | None = None

    @classmethod
    def started(cls, tool_call_id: str, sequence: int, *, started_at: str) -> ToolResultEvent:
        return cls(kind="started", tool_call_id=tool_call_id, sequence=sequence, started_at=started_at)

    @classmethod
    def delta(cls, tool_call_id: str, sequence: int, output: tuple[ContentPart, ...]) -> ToolResultEvent:
        return cls(kind="delta", tool_call_id=tool_call_id, sequence=sequence, output=tuple(output))

    @classmethod
    def artifact_ready(cls, tool_call_id: str, sequence: int, artifact: ArtifactRef) -> ToolResultEvent:
        return cls(kind="artifact_ready", tool_call_id=tool_call_id, sequence=sequence, artifact=artifact)

    @classmethod
    def completed(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="completed", tool_call_id=tool_call_id, sequence=sequence, result=result)

    def is_final_durable_result(self) -> bool:
        return self.kind == "completed"

    def into_result(self) -> ToolResult | None:
        return self.result if self.kind == "completed" else None
