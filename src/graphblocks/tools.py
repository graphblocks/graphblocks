from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from .canonical import canonical_hash
from .conversation import ContentPart
from .documents import ArtifactRef
from .policy import PolicyDecision, PolicyRequest, PrincipalRef, ResourceRef as PolicyResourceRef
from .schema import SchemaId, SchemaIdError


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
ToolResultEventKind = Literal[
    "started",
    "delta",
    "artifact_ready",
    "completed",
    "failed",
    "denied",
    "cancelled",
    "policy_stopped",
    "incomplete",
]
ToolEffectOutcome = Literal["no_external_effect", "committed", "not_committed", "unknown"]
ToolExecutionFailurePolicy = Literal["fail_fast", "collect", "return_failures_to_model"]
ToolExecutionCancellationPolicy = Literal["cancel_dependents", "cancel_all", "allow_independent_calls"]
ToolExecutionState = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "denied",
    "cancelled",
    "expired",
    "skipped",
]
PendingToolCallsDisposition = Literal["keep", "deny", "cancel_admitted"]
JsonSchemaType = Literal["null", "boolean", "integer", "number", "string", "array", "object"]
ToolApprovalStatus = Literal["requested", "approved", "denied", "invalidated"]

VALID_TOOL_EFFECTS = frozenset(
    {
        "none",
        "external_read",
        "external_write",
        "filesystem_read",
        "filesystem_write",
        "process",
        "network",
        "destructive",
    }
)
VALID_TOOL_APPROVALS = frozenset({"never", "policy", "always"})
VALID_TOOL_IDEMPOTENCIES = frozenset({"not_applicable", "optional", "required"})
VALID_TOOL_CANCELLATIONS = frozenset({"unsupported", "cooperative", "force_terminable"})
VALID_TOOL_RESULT_MODES = frozenset({"value", "incremental", "bounded_sequence", "artifact_reference"})
VALID_TOOL_CALL_DRAFT_STATUSES = frozenset({"proposed", "arguments_streaming", "arguments_complete"})
VALID_TOOL_CALL_STATUSES = frozenset(
    {
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
    }
)
VALID_TOOL_RESULT_STATUSES = frozenset(
    {"completed", "failed", "denied", "cancelled", "policy_stopped", "incomplete"}
)
VALID_TOOL_RESULT_EVENT_KINDS = frozenset(
    {
        "started",
        "delta",
        "artifact_ready",
        "completed",
        "failed",
        "denied",
        "cancelled",
        "policy_stopped",
        "incomplete",
    }
)
FINAL_TOOL_RESULT_EVENT_STATUSES = {
    "completed": "completed",
    "failed": "failed",
    "denied": "denied",
    "cancelled": "cancelled",
    "policy_stopped": "policy_stopped",
    "incomplete": "incomplete",
}
VALID_TOOL_EFFECT_OUTCOMES = frozenset({"no_external_effect", "committed", "not_committed", "unknown"})
VALID_TOOL_EXECUTION_FAILURE_POLICIES = frozenset({"fail_fast", "collect", "return_failures_to_model"})
VALID_TOOL_EXECUTION_CANCELLATION_POLICIES = frozenset(
    {"cancel_dependents", "cancel_all", "allow_independent_calls"}
)
VALID_TOOL_APPROVAL_STATUSES = frozenset({"requested", "approved", "denied", "invalidated"})


class ToolCallError(RuntimeError):
    pass


class ToolAdmissionError(RuntimeError):
    pass


class ToolExecutionPlanError(RuntimeError):
    pass


class ToolResolutionError(RuntimeError):
    pass


class ToolApprovalError(RuntimeError):
    pass


class ToolSchemaRegistryError(RuntimeError):
    pass


class ToolSchemaValidationError(RuntimeError):
    pass


class ToolResultValidationError(RuntimeError):
    pass


class FrozenJsonDict(dict):
    def __readonly(self, *args: object, **kwargs: object) -> None:
        raise TypeError("tool call arguments are immutable")

    __setitem__ = __readonly
    __delitem__ = __readonly
    clear = __readonly
    pop = __readonly
    popitem = __readonly
    setdefault = __readonly
    update = __readonly


class FrozenJsonList(tuple):
    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return FrozenJsonDict({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return FrozenJsonList(_freeze_json_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class JsonSchemaNode:
    expected_type: JsonSchemaType | None = None
    properties: dict[str, JsonSchemaNode] = field(default_factory=dict)
    required: frozenset[str] = field(default_factory=frozenset)
    items: JsonSchemaNode | None = None

    @classmethod
    def any(cls) -> JsonSchemaNode:
        return cls()

    @classmethod
    def string(cls) -> JsonSchemaNode:
        return cls(expected_type="string")

    @classmethod
    def integer(cls) -> JsonSchemaNode:
        return cls(expected_type="integer")

    @classmethod
    def number(cls) -> JsonSchemaNode:
        return cls(expected_type="number")

    @classmethod
    def boolean(cls) -> JsonSchemaNode:
        return cls(expected_type="boolean")

    @classmethod
    def object(cls) -> JsonSchemaNode:
        return cls(expected_type="object")

    @classmethod
    def array(cls, items: JsonSchemaNode) -> JsonSchemaNode:
        return cls(expected_type="array", items=items)

    def property(self, name: str, schema: JsonSchemaNode) -> JsonSchemaNode:
        properties = dict(self.properties)
        properties[name] = schema
        return replace(self, properties=properties)

    def required_property(self, name: str, schema: JsonSchemaNode) -> JsonSchemaNode:
        properties = dict(self.properties)
        properties[name] = schema
        return replace(self, properties=properties, required=frozenset((*self.required, name)))


@dataclass(frozen=True, slots=True)
class JsonSchema:
    schema_id: str
    root: JsonSchemaNode


@dataclass(frozen=True, slots=True)
class ToolSchemaRegistry:
    schemas: tuple[JsonSchema, ...]
    _schemas_by_id: dict[str, JsonSchema] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schemas", tuple(self.schemas))
        schemas_by_id: dict[str, JsonSchema] = {}
        for schema in self.schemas:
            try:
                SchemaId.parse(schema.schema_id)
            except SchemaIdError as error:
                raise ToolSchemaRegistryError(f"invalid schema id {schema.schema_id}: {error}") from error
            if schema.schema_id in schemas_by_id:
                raise ToolSchemaRegistryError(f"duplicate schema {schema.schema_id}")
            schemas_by_id[schema.schema_id] = schema
        object.__setattr__(self, "_schemas_by_id", schemas_by_id)

    def validate(self, schema_id: str, value: object) -> None:
        schema = self._schemas_by_id.get(schema_id)
        if schema is None:
            raise ToolSchemaValidationError(f"schema {schema_id} is not registered")
        self._validate_node(schema_id, schema.root, value, "$")

    def _validate_node(self, schema_id: str, schema: JsonSchemaNode, value: object, path: str) -> None:
        if schema.expected_type is not None:
            matches = (
                (schema.expected_type == "null" and value is None)
                or (schema.expected_type == "boolean" and isinstance(value, bool))
                or (schema.expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool))
                or (
                    schema.expected_type == "number"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                )
                or (schema.expected_type == "string" and isinstance(value, str))
                or (schema.expected_type == "array" and isinstance(value, (list, tuple)))
                or (schema.expected_type == "object" and isinstance(value, dict))
            )
            if not matches:
                raise ToolSchemaValidationError(f"{schema_id} expected {schema.expected_type} at {path}")

        if schema.expected_type == "object" and isinstance(value, dict):
            for required in sorted(schema.required):
                if required not in value:
                    raise ToolSchemaValidationError(f"{schema_id} missing required property {required} at {path}")
            for property_name, property_schema in sorted(schema.properties.items()):
                if property_name in value:
                    self._validate_node(schema_id, property_schema, value[property_name], f"{path}.{property_name}")

        if schema.expected_type == "array" and schema.items is not None and isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self._validate_node(schema_id, schema.items, item, f"{path}[{index}]")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_mapping", MappingProxyType(dict(self.input_mapping)))
        object.__setattr__(self, "output_mapping", MappingProxyType(dict(self.output_mapping)))

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_mapping", MappingProxyType(dict(self.input_mapping)))
        object.__setattr__(self, "output_mapping", MappingProxyType(dict(self.output_mapping)))

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
        effects = frozenset(self.effects)
        invalid_effects = sorted(effect for effect in effects if effect not in VALID_TOOL_EFFECTS)
        if invalid_effects:
            raise ValueError(f"invalid tool effect {invalid_effects[0]}")
        if self.approval not in VALID_TOOL_APPROVALS:
            raise ValueError(f"invalid tool approval {self.approval}")
        if self.idempotency not in VALID_TOOL_IDEMPOTENCIES:
            raise ValueError(f"invalid tool idempotency {self.idempotency}")
        if self.cancellation not in VALID_TOOL_CANCELLATIONS:
            raise ValueError(f"invalid tool cancellation {self.cancellation}")
        if self.result_mode not in VALID_TOOL_RESULT_MODES:
            raise ValueError(f"invalid tool result mode {self.result_mode}")
        if self.timeout_ms is not None and (
            not isinstance(self.timeout_ms, int) or isinstance(self.timeout_ms, bool) or self.timeout_ms < 0
        ):
            raise ValueError("tool timeout_ms must be non-negative")
        object.__setattr__(self, "effects", effects)

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
        if binding.tool_name != definition.name:
            raise ToolResolutionError(
                f"tool binding {binding.binding_id} references {binding.tool_name}, not {definition.name}"
            )
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
class ToolApprovalRequest:
    approval_id: str
    tool_call_id: str
    tool_name: str
    revision: int
    definition_digest: str
    binding_digest: str
    arguments_digest: str
    policy_snapshot_id: str
    principal_id: str
    requested_at: int
    expires_at: int

    def __post_init__(self) -> None:
        for field_name in (
            "approval_id",
            "tool_call_id",
            "tool_name",
            "definition_digest",
            "binding_digest",
            "arguments_digest",
            "policy_snapshot_id",
            "principal_id",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"approval {field_name} must not be empty")
        if self.revision < 1:
            raise ValueError("approval revision must be positive")
        if self.requested_at < 0:
            raise ValueError("approval requested_at must be non-negative")
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiration must be after request time")

    @classmethod
    def for_call(
        cls,
        approval_id: str,
        resolved_tool: ResolvedTool,
        call: ToolCall,
        *,
        principal_id: str,
        requested_at: int,
        expires_at: int,
    ) -> ToolApprovalRequest:
        if expires_at <= requested_at:
            raise ToolApprovalError("approval expiration must be after request time")
        if call.resolved_tool_id != resolved_tool.resolved_tool_id:
            raise ToolApprovalError("tool call references a different resolved tool")
        if call.name != resolved_tool.definition.name:
            raise ToolApprovalError("tool call name does not match resolved tool")
        return cls(
            approval_id=approval_id,
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            revision=call.revision,
            definition_digest=resolved_tool.definition_digest,
            binding_digest=resolved_tool.binding_digest,
            arguments_digest=call.arguments_digest,
            policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
            principal_id=principal_id,
            requested_at=requested_at,
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class ToolApprovalRecord:
    approval_id: str
    request: ToolApprovalRequest
    status: ToolApprovalStatus
    approver_id: str | None = None
    decided_at: int | None = None
    invalidated_at: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_TOOL_APPROVAL_STATUSES:
            raise ValueError(f"invalid tool approval status {self.status}")
        if self.approval_id != self.request.approval_id:
            raise ValueError("approval record id must match request approval_id")

    @classmethod
    def requested(cls, request: ToolApprovalRequest) -> ToolApprovalRecord:
        return cls(approval_id=request.approval_id, request=request, status="requested")

    @classmethod
    def approve(cls, request: ToolApprovalRequest, *, approver_id: str, decided_at: int) -> ToolApprovalRecord:
        return cls(
            approval_id=request.approval_id,
            request=request,
            status="approved",
            approver_id=approver_id,
            decided_at=decided_at,
        )

    @classmethod
    def deny(
        cls,
        request: ToolApprovalRequest,
        *,
        approver_id: str,
        decided_at: int,
        reason: str,
    ) -> ToolApprovalRecord:
        return cls(
            approval_id=request.approval_id,
            request=request,
            status="denied",
            approver_id=approver_id,
            decided_at=decided_at,
            reason=reason,
        )

    def invalidate(self, invalidated_at: int) -> ToolApprovalRecord:
        return replace(self, status="invalidated", invalidated_at=invalidated_at)

    def is_valid_for(self, resolved_tool: ResolvedTool, call: ToolCall, *, principal_id: str, now: int) -> bool:
        return (
            self.status == "approved"
            and self.approval_id == self.request.approval_id
            and self.invalidated_at is None
            and now <= self.request.expires_at
            and self.request.tool_call_id == call.tool_call_id
            and self.request.tool_name == call.name
            and self.request.revision == call.revision
            and self.request.definition_digest == resolved_tool.definition_digest
            and self.request.binding_digest == resolved_tool.binding_digest
            and self.request.arguments_digest == call.arguments_digest
            and self.request.policy_snapshot_id == resolved_tool.effective_policy_snapshot_id
            and self.request.principal_id == principal_id
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
            try:
                SchemaId.parse(definition.input_schema)
            except SchemaIdError as error:
                raise ToolResolutionError(
                    f"tool {definition.name} has invalid schema id {definition.input_schema}: {error}"
                ) from error
            if definition.output_schema is not None:
                try:
                    SchemaId.parse(definition.output_schema)
                except SchemaIdError as error:
                    raise ToolResolutionError(
                        f"tool {definition.name} has invalid schema id {definition.output_schema}: {error}"
                    ) from error
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

    def __post_init__(self) -> None:
        if self.status not in VALID_TOOL_CALL_DRAFT_STATUSES:
            raise ValueError(f"invalid tool call draft status {self.status}")
        if self.sequence < 0:
            raise ValueError("tool call draft sequence must be non-negative")
        object.__setattr__(self, "argument_fragments", tuple(self.argument_fragments))

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
            arguments = json.loads(
                "".join(self.argument_fragments),
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant {constant}")
                ),
            )
            arguments_digest = canonical_hash(arguments)
        except (json.JSONDecodeError, ValueError) as error:
            raise ToolCallError("tool arguments are invalid JSON") from error

        return ToolCall(
            tool_call_id=self.tool_call_id,
            response_id=self.response_id,
            resolved_tool_id=resolved_tool_id,
            name=self.tool_name,
            arguments=arguments,
            arguments_digest=arguments_digest,
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
        for field_name in (
            "tool_call_id",
            "response_id",
            "resolved_tool_id",
            "name",
            "arguments_digest",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"tool call {field_name} must not be empty")
        if self.status not in VALID_TOOL_CALL_STATUSES:
            raise ValueError(f"invalid tool call status {self.status}")
        if self.revision < 1:
            raise ValueError("tool call revision must be positive")
        object.__setattr__(self, "arguments", _freeze_json_value(self.arguments))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    def revise_arguments(self, arguments: object) -> ToolCall:
        if self.status != "validated":
            raise ToolCallError("tool arguments cannot be revised after validation")
        try:
            arguments_digest = canonical_hash(arguments)
        except (TypeError, ValueError) as error:
            raise ToolCallError("tool arguments are invalid JSON") from error
        return replace(
            self,
            arguments=arguments,
            arguments_digest=arguments_digest,
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
class AdmittedToolCall:
    call: ToolCall
    idempotency_key: str | None = None


def admit_tool_call(
    call: ToolCall,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    policy_decision: PolicyDecision,
    approval: ToolApprovalRecord | None = None,
    principal_id: str,
    idempotency_key: str | None = None,
    admitted_at: str,
    now: int,
) -> AdmittedToolCall:
    if call.status != "validated":
        raise ToolAdmissionError(f"tool call {call.tool_call_id} is {call.status}, not validated")
    if call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise ToolAdmissionError("tool call references a different resolved tool")
    if call.name != resolved_tool.definition.name:
        raise ToolAdmissionError("tool call name does not match resolved tool")
    try:
        actual_arguments_digest = canonical_hash(call.arguments)
    except (TypeError, ValueError) as error:
        raise ToolAdmissionError(f"tool call {call.tool_call_id} arguments are invalid JSON") from error
    if actual_arguments_digest != call.arguments_digest:
        raise ToolAdmissionError(f"tool call {call.tool_call_id} arguments digest does not match arguments")

    try:
        schema_registry.validate(resolved_tool.definition.input_schema, call.arguments)
    except ToolSchemaValidationError as error:
        raise ToolAdmissionError(f"tool call {call.tool_call_id} arguments invalid: {error}") from error

    if not resolved_tool.allowed_for_principal:
        raise ToolAdmissionError(
            f"resolved tool {resolved_tool.definition.name} is not allowed for principal {principal_id}"
        )

    if resolved_tool.valid_until is not None and admitted_at > resolved_tool.valid_until:
        raise ToolAdmissionError(f"resolved tool {resolved_tool.definition.name} expired at {resolved_tool.valid_until}")

    if not policy_decision.input_digest:
        raise ToolAdmissionError(f"policy decision {policy_decision.decision_id} has no input digest")
    if policy_decision.effect == "deny":
        reason = ", ".join(policy_decision.reason_codes) or "deny"
        raise ToolAdmissionError(
            f"policy decision {policy_decision.decision_id} denied tool call {call.tool_call_id}: {reason}"
        )
    if policy_decision.effect == "defer":
        reason = ", ".join(policy_decision.reason_codes) or "defer"
        raise ToolAdmissionError(
            f"policy decision {policy_decision.decision_id} deferred tool call {call.tool_call_id}: {reason}"
        )
    if policy_decision.effect not in {"allow", "allow_with_obligations"}:
        raise ToolAdmissionError(
            f"policy decision {policy_decision.decision_id} has unsupported effect {policy_decision.effect}"
        )

    policy_requires_approval = resolved_tool.binding.approval == "policy" and any(
        obligation.obligation_type == "require_tool_approval" for obligation in policy_decision.obligations
    )
    if resolved_tool.binding.approval == "always" or policy_requires_approval:
        if approval is None:
            raise ToolAdmissionError(f"tool call {call.tool_call_id} requires approval")
        if not approval.is_valid_for(resolved_tool, call, principal_id=principal_id, now=now):
            raise ToolAdmissionError(f"approval {approval.approval_id} is not valid for tool call {call.tool_call_id}")
    elif approval is not None and not approval.is_valid_for(resolved_tool, call, principal_id=principal_id, now=now):
        raise ToolAdmissionError(f"approval {approval.approval_id} is not valid for tool call {call.tool_call_id}")

    if resolved_tool.binding.idempotency == "required" and (
        idempotency_key is None or not idempotency_key.strip()
    ):
        raise ToolAdmissionError(f"tool call {call.tool_call_id} requires an idempotency key")

    return AdmittedToolCall(
        call=call.with_status("admitted", admitted_at=admitted_at),
        idempotency_key=idempotency_key,
    )


@dataclass(frozen=True, slots=True)
class ToolPlanCall:
    call: ToolCall
    effect_key: str | None = None
    effects: frozenset[ToolEffect] = field(default_factory=frozenset)
    cancellation: ToolCancellation = "cooperative"

    def __post_init__(self) -> None:
        effects = frozenset(self.effects)
        invalid_effects = sorted(effect for effect in effects if effect not in VALID_TOOL_EFFECTS)
        if invalid_effects:
            raise ToolExecutionPlanError(f"invalid tool effect {invalid_effects[0]}")
        if self.cancellation not in VALID_TOOL_CANCELLATIONS:
            raise ToolExecutionPlanError(f"invalid tool cancellation {self.cancellation}")
        object.__setattr__(self, "effects", effects)

    def with_effect_key_template(self, template: str) -> ToolPlanCall:
        output: list[str] = []
        rest = template
        while "{" in rest:
            open_index = rest.index("{")
            output.append(rest[:open_index])
            after_open = rest[open_index + 1 :]
            close_index = after_open.find("}")
            if close_index == -1:
                raise ToolExecutionPlanError(f"invalid effect key template {template!r}")
            placeholder = after_open[:close_index]
            if not placeholder:
                raise ToolExecutionPlanError(f"invalid effect key template {template!r}")
            if placeholder == "tool.name":
                output.append(self.call.name)
            elif placeholder.startswith("arguments."):
                argument_path = placeholder.removeprefix("arguments.")
                if not argument_path:
                    raise ToolExecutionPlanError(f"unsupported effect key template placeholder {placeholder}")
                value = self.call.arguments
                for segment in argument_path.split("."):
                    if not segment:
                        raise ToolExecutionPlanError(f"unsupported effect key template placeholder {placeholder}")
                    if not isinstance(value, Mapping) or segment not in value:
                        raise ToolExecutionPlanError(
                            f"effect key template placeholder {placeholder} has no value"
                        )
                    value = value[segment]
                if isinstance(value, str):
                    output.append(value)
                elif isinstance(value, bool):
                    output.append("true" if value else "false")
                elif isinstance(value, int | float):
                    output.append(str(value))
                elif value is None:
                    raise ToolExecutionPlanError(
                        f"effect key template placeholder {placeholder} has no value"
                    )
                else:
                    raise ToolExecutionPlanError(
                        f"effect key template placeholder {placeholder} must resolve to a scalar"
                    )
            else:
                raise ToolExecutionPlanError(f"unsupported effect key template placeholder {placeholder}")
            rest = after_open[close_index + 1 :]
        if "}" in rest:
            raise ToolExecutionPlanError(f"invalid effect key template {template!r}")
        output.append(rest)
        return replace(self, effect_key="".join(output))


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
        if self.failure_policy not in VALID_TOOL_EXECUTION_FAILURE_POLICIES:
            raise ToolExecutionPlanError(f"invalid failure policy {self.failure_policy}")
        if self.cancellation_policy not in VALID_TOOL_EXECUTION_CANCELLATION_POLICIES:
            raise ToolExecutionPlanError(f"invalid cancellation policy {self.cancellation_policy}")
        if self.maximum_parallelism <= 0:
            raise ToolExecutionPlanError("maximum_parallelism must be positive")
        self.calls = tuple(self.calls)
        self._states = {}
        self._calls_by_id = {}
        for planned_call in self.calls:
            tool_call_id = planned_call.call.tool_call_id
            if planned_call.call.response_id != self.response_id:
                raise ToolExecutionPlanError(
                    f"tool call {tool_call_id} belongs to response "
                    f"{planned_call.call.response_id}, not {self.response_id}"
                )
            if tool_call_id in self._calls_by_id:
                raise ToolExecutionPlanError(f"duplicate tool call {tool_call_id}")
            self._calls_by_id[tool_call_id] = planned_call
            self._states[tool_call_id] = "pending"
        for planned_call in self.calls:
            tool_call_id = planned_call.call.tool_call_id
            for dependency in planned_call.call.depends_on:
                if dependency not in self._calls_by_id:
                    raise ToolExecutionPlanError(
                        f"tool call {tool_call_id} depends on unknown tool call {dependency}"
                    )
        remaining_dependencies = {
            tool_call_id: set(planned_call.call.depends_on)
            for tool_call_id, planned_call in self._calls_by_id.items()
        }
        ready = [tool_call_id for tool_call_id, dependencies in remaining_dependencies.items() if not dependencies]
        while ready:
            completed_id = ready.pop(0)
            if completed_id not in remaining_dependencies:
                continue
            remaining_dependencies.pop(completed_id)
            for candidate_id, dependencies in remaining_dependencies.items():
                if completed_id in dependencies:
                    dependencies.remove(completed_id)
                    if not dependencies:
                        ready.append(candidate_id)
        if remaining_dependencies:
            for planned_call in self.calls:
                tool_call_id = planned_call.call.tool_call_id
                if tool_call_id in remaining_dependencies:
                    raise ToolExecutionPlanError(
                        f"tool execution plan has a dependency cycle involving {tool_call_id}"
                    )

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
        self._mark_blocked_dependents("skipped")
        if self.failure_policy == "fail_fast":
            for candidate_id, state in list(self._states.items()):
                if state == "pending":
                    self._states[candidate_id] = "cancelled"

    def record_denied(self, tool_call_id: str) -> None:
        self._enter_pending_terminal(tool_call_id, "denied")
        self._mark_blocked_dependents("skipped")

    def record_expired(self, tool_call_id: str) -> None:
        self._enter_pending_terminal(tool_call_id, "expired")
        self._mark_blocked_dependents("skipped")

    def record_cancelled(self, tool_call_id: str) -> None:
        self._enter_terminal(tool_call_id, "cancelled")
        if self.cancellation_policy == "cancel_dependents":
            self._mark_blocked_dependents("cancelled")
            return
        if self.cancellation_policy == "cancel_all":
            for candidate_id, state in list(self._states.items()):
                if state in {"pending", "running"}:
                    self._states[candidate_id] = "cancelled"
            return
        if self.cancellation_policy == "allow_independent_calls":
            self._mark_blocked_dependents("skipped")
            return
        raise ToolExecutionPlanError(f"unknown cancellation policy {self.cancellation_policy}")

    def _mark_blocked_dependents(self, blocked_state: ToolExecutionState) -> None:
        while True:
            blocked: list[str] = []
            for planned_call in self.calls:
                candidate_id = planned_call.call.tool_call_id
                if self._states[candidate_id] != "pending":
                    continue
                if any(
                    self._states.get(dependency) in {"failed", "denied", "cancelled", "expired", "skipped"}
                    for dependency in planned_call.call.depends_on
                ):
                    blocked.append(candidate_id)
            if not blocked:
                break
            for blocked_id in blocked:
                self._states[blocked_id] = blocked_state

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
                    can_cancel_running = planned_call.cancellation == "force_terminable" or all(
                        effect in {"none", "external_read", "filesystem_read", "network"}
                        for effect in planned_call.effects
                    )
                    if can_cancel_running:
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

    def _enter_pending_terminal(self, tool_call_id: str, terminal_state: ToolExecutionState) -> None:
        current = self._states.get(tool_call_id)
        if current is None:
            raise ToolExecutionPlanError(f"unknown tool call {tool_call_id}")
        if current != "pending":
            raise ToolExecutionPlanError(f"tool call {tool_call_id} is {current}, not pending")
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
    artifacts: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    diagnostics: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    error: Mapping[str, object] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    effect_outcome: ToolEffectOutcome = "unknown"

    def __post_init__(self) -> None:
        if not self.tool_call_id.strip():
            raise ValueError("tool result tool_call_id must not be empty")
        if self.status not in VALID_TOOL_RESULT_STATUSES:
            raise ValueError(f"invalid tool result status {self.status}")
        if self.effect_outcome not in VALID_TOOL_EFFECT_OUTCOMES:
            raise ValueError(f"invalid tool effect outcome {self.effect_outcome}")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("tool result completed_at must not be before started_at")
        object.__setattr__(self, "output", tuple(self.output))
        object.__setattr__(
            self,
            "artifacts",
            tuple(MappingProxyType(dict(artifact)) for artifact in self.artifacts),
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(MappingProxyType(dict(diagnostic)) for diagnostic in self.diagnostics),
        )
        if self.error is not None:
            object.__setattr__(self, "error", MappingProxyType(dict(self.error)))

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
        try:
            output_digest = _tool_result_output_digest(output)
        except (TypeError, ValueError) as error:
            raise ToolResultValidationError(f"tool result {tool_call_id} output is not canonical JSON") from error
        return cls(
            tool_call_id=tool_call_id,
            status="completed",
            output=output,
            output_digest=output_digest,
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
    def denied(
        cls,
        tool_call_id: str,
        *,
        error: dict[str, object],
        completed_at: str,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call_id,
            status="denied",
            error=error,
            completed_at=completed_at,
            effect_outcome="not_committed",
        )

    @classmethod
    def cancelled(
        cls,
        tool_call_id: str,
        *,
        started_at: str,
        completed_at: str,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call_id,
            status="cancelled",
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

    @classmethod
    def incomplete(
        cls,
        tool_call_id: str,
        *,
        started_at: str,
        completed_at: str,
    ) -> ToolResult:
        return cls(
            tool_call_id=tool_call_id,
            status="incomplete",
            started_at=started_at,
            completed_at=completed_at,
        )

    def with_effect_outcome(self, effect_outcome: ToolEffectOutcome) -> ToolResult:
        return replace(self, effect_outcome=effect_outcome)

    def effect_was_committed(self) -> bool:
        return self.effect_outcome == "committed"


def _tool_result_output_value(output: tuple[ContentPart, ...]) -> list[dict[str, object]]:
    return [
        {
            "kind": part.kind,
            "text": part.text,
            "data": part.data,
            "metadata": part.metadata,
        }
        for part in output
    ]


def _tool_result_output_digest(output: tuple[ContentPart, ...]) -> str:
    return canonical_hash(_tool_result_output_value(output))


def validate_tool_result_for_model(
    call: ToolCall,
    result: ToolResult,
    resolved_tool: ResolvedTool,
    schema_registry: ToolSchemaRegistry,
    *,
    max_output_bytes: int | None = None,
    redactions: tuple[dict[str, object], ...] = (),
    capture_policy: dict[str, object] | None = None,
    trust_designation: str = "untrusted_external",
    prompt_injection_label: str = "untrusted_tool_output",
    content_classification: str = "external_tool_output",
) -> tuple[ContentPart, ...]:
    if result.tool_call_id != call.tool_call_id:
        raise ToolResultValidationError(
            f"tool result {result.tool_call_id} does not match tool call {call.tool_call_id}"
        )
    if call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise ToolResultValidationError("tool call references a different resolved tool")
    if result.status != "completed":
        return ()
    try:
        actual_output_digest = _tool_result_output_digest(result.output)
    except (TypeError, ValueError) as error:
        raise ToolResultValidationError(f"tool result {result.tool_call_id} output is not canonical JSON") from error
    if result.output_digest != actual_output_digest:
        raise ToolResultValidationError(
            f"tool result {result.tool_call_id} output digest does not match output"
        )
    if resolved_tool.binding.result_mode == "artifact_reference" and any(
        part.kind != "artifact_ref" for part in result.output
    ):
        raise ToolResultValidationError(
            f"tool result {result.tool_call_id} uses artifact_reference mode but contains inline output"
        )

    output_schema = resolved_tool.definition.output_schema
    if output_schema is not None:
        json_outputs = tuple(part for part in result.output if part.kind == "json")
        if not json_outputs:
            raise ToolResultValidationError(f"tool result {result.tool_call_id} has no JSON output")
        if len(json_outputs) > 1:
            raise ToolResultValidationError(f"tool result {result.tool_call_id} has multiple JSON outputs")
        schema_registry.validate(output_schema, json_outputs[0].data)

    model_output: list[ContentPart] = []
    for part in result.output:
        metadata = dict(part.metadata)
        metadata["trust_designation"] = trust_designation
        metadata["prompt_injection_label"] = prompt_injection_label
        metadata["content_classification"] = content_classification
        model_output.append(replace(part, metadata=metadata))

    if redactions:
        redactions_by_part: dict[int, list[dict[str, object]]] = {}
        for redaction in redactions:
            path = redaction.get("path")
            if not isinstance(path, str) or not path.startswith("/parts/") or not path.endswith("/text"):
                raise ToolResultValidationError(f"invalid tool result redaction path {path!r}")
            part_index_text = path[len("/parts/") : -len("/text")]
            try:
                part_index = int(part_index_text)
            except ValueError as error:
                raise ToolResultValidationError(f"invalid tool result redaction path {path!r}") from error
            redactions_by_part.setdefault(part_index, []).append(redaction)

        for part_index, part_redactions in redactions_by_part.items():
            if part_index < 0 or part_index >= len(model_output):
                raise ToolResultValidationError(f"invalid tool result redaction path '/parts/{part_index}/text'")
            text = model_output[part_index].text
            if text is None:
                raise ToolResultValidationError(f"invalid tool result redaction path '/parts/{part_index}/text'")
            for redaction in sorted(part_redactions, key=lambda item: int(item.get("start", -1)), reverse=True):
                start = redaction.get("start")
                end = redaction.get("end")
                replacement = redaction.get("replacement")
                if (
                    not isinstance(start, int)
                    or not isinstance(end, int)
                    or not isinstance(replacement, str)
                    or start < 0
                    or end < start
                    or end > len(text)
                ):
                    raise ToolResultValidationError(
                        f"invalid tool result redaction range for {redaction.get('path')!r}"
                    )
                text = text[:start] + replacement + text[end:]
            model_output[part_index] = replace(model_output[part_index], text=text)

    if capture_policy is not None:
        mode = capture_policy.get("mode", "hash_only")
        if mode not in {"none", "hash_only", "reference_only", "redacted_preview", "full"}:
            raise ToolResultValidationError(f"invalid capture mode {mode!r}")
        retention_policy = str(capture_policy.get("retention_policy", ""))
        consent_ref = capture_policy.get("consent_ref")
        for index, part in enumerate(model_output):
            content_ref = None
            if part.kind == "text":
                content_kind = "tool_result_text"
                capture_text = part.text or ""
            elif part.kind == "json":
                content_kind = "tool_result_json"
                capture_text = json.dumps(part.data, sort_keys=True, separators=(",", ":"))
            elif part.kind == "artifact_ref":
                content_kind = "tool_result_artifact_ref"
                capture_text = json.dumps(part.data, sort_keys=True, separators=(",", ":"))
                if isinstance(part.data, dict) and isinstance(part.data.get("uri"), str):
                    content_ref = part.data["uri"]
            else:
                continue

            preview = capture_text if mode in {"redacted_preview", "full"} else None
            stored_ref = content_ref if mode == "reference_only" else None
            metadata = dict(part.metadata)
            metadata["capture"] = {
                "mode": mode,
                "content_kind": content_kind,
                "content_digest": canonical_hash(capture_text),
                "preview": preview,
                "content_ref": stored_ref,
                "retention_policy": retention_policy,
                "consent_ref": consent_ref if isinstance(consent_ref, str) else None,
                "redaction_count": 0,
                "original_bytes": len(capture_text.encode("utf-8")),
            }
            model_output[index] = replace(part, metadata=metadata)

    if max_output_bytes is not None:
        actual_bytes = 0
        for part in model_output:
            if part.text is not None:
                actual_bytes += len(part.text.encode("utf-8"))
            if part.data is not None:
                actual_bytes += len(
                    json.dumps(part.data, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
        if actual_bytes > max_output_bytes:
            raise ToolResultValidationError(
                f"tool result {result.tool_call_id} model output exceeds "
                f"{max_output_bytes} bytes (actual {actual_bytes} bytes)"
            )

    return tuple(model_output)


def build_before_tool_or_effect_policy_request(
    *,
    request_id: str,
    call: ToolCall,
    resolved_tool: ResolvedTool,
    principal: PrincipalRef,
    occurred_at: str,
    run_id: str | None = None,
    output_policy_state: dict[str, object] | None = None,
) -> PolicyRequest:
    if call.resolved_tool_id != resolved_tool.resolved_tool_id:
        raise ToolAdmissionError("tool call references a different resolved tool")
    attributes: dict[str, object] = {
        "tool_call_id": call.tool_call_id,
        "response_id": call.response_id,
        "resolved_tool_id": resolved_tool.resolved_tool_id,
        "tool_name": resolved_tool.definition.name,
        "arguments": call.arguments,
        "arguments_digest": call.arguments_digest,
        "definition_digest": resolved_tool.definition_digest,
        "binding_digest": resolved_tool.binding_digest,
        "effects": sorted(resolved_tool.binding.effects),
    }
    if output_policy_state is not None:
        attributes["output_policy_state"] = dict(output_policy_state)
    return PolicyRequest(
        request_id=request_id,
        enforcement_point="before_tool_or_effect",
        action="tool.run",
        resource=PolicyResourceRef(f"tool:{resolved_tool.definition.name}", resource_kind="tool"),
        principal=principal,
        run_id=run_id,
        attributes=attributes,
        policy_snapshot_id=resolved_tool.effective_policy_snapshot_id,
        occurred_at=occurred_at,
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

    def __post_init__(self) -> None:
        if self.kind not in VALID_TOOL_RESULT_EVENT_KINDS:
            raise ValueError(f"invalid tool result event kind {self.kind}")
        if self.sequence < 0:
            raise ValueError("tool result event sequence must be non-negative")
        object.__setattr__(self, "output", tuple(self.output))
        expected_status = FINAL_TOOL_RESULT_EVENT_STATUSES.get(self.kind)
        if expected_status is None:
            if self.result is not None:
                raise ValueError(f"tool result event {self.kind} must not carry a final result")
            return
        if self.result is None:
            raise ValueError(f"tool result event {self.kind} requires a final result")
        if self.result.tool_call_id != self.tool_call_id:
            raise ValueError(
                f"tool result event {self.kind} for {self.tool_call_id} "
                f"carries result for {self.result.tool_call_id}"
            )
        if self.result.status != expected_status:
            raise ValueError(
                f"tool result event {self.kind} requires result status "
                f"{expected_status}, got {self.result.status}"
            )

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

    @classmethod
    def failed(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="failed", tool_call_id=tool_call_id, sequence=sequence, result=result)

    @classmethod
    def denied(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="denied", tool_call_id=tool_call_id, sequence=sequence, result=result)

    @classmethod
    def cancelled(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="cancelled", tool_call_id=tool_call_id, sequence=sequence, result=result)

    @classmethod
    def policy_stopped(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="policy_stopped", tool_call_id=tool_call_id, sequence=sequence, result=result)

    @classmethod
    def incomplete(cls, tool_call_id: str, sequence: int, result: ToolResult) -> ToolResultEvent:
        return cls(kind="incomplete", tool_call_id=tool_call_id, sequence=sequence, result=result)

    def is_final_durable_result(self) -> bool:
        return self.kind in {"completed", "failed", "denied", "cancelled", "policy_stopped", "incomplete"}

    def into_result(self) -> ToolResult | None:
        return self.result if self.is_final_durable_result() else None
