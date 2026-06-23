from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Literal

from .canonical import canonical_hash
from .conversation import ContentPart


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


class ToolCallError(RuntimeError):
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
