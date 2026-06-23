from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import PSEUDO_NODES, canonical_hash, normalize_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .migration import GRAPH_API_VERSION, LEGACY_GRAPH_API_VERSIONS, migrate_document
from .plugins import BlockCatalog
from .schema import SchemaId, SchemaIdError

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
STATE_CHANGING_TOOL_EFFECTS = frozenset({"external_write", "filesystem_write", "process", "destructive"})


@dataclass(frozen=True, slots=True)
class Plan:
    normalized: dict[str, Any]
    graph_hash: str
    diagnostics: DiagnosticSet

    @property
    def ok(self) -> bool:
        return self.diagnostics.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.graph_hash,
            "ok": self.ok,
            "diagnostics": self.diagnostics.to_list(),
            "graph": self.normalized,
        }


def compile_graph(document: dict[str, Any], block_catalog: BlockCatalog | None = None) -> Plan:
    diagnostics: list[Diagnostic] = []
    migrated = migrate_document(document)
    if migrated.get("kind") != "Graph":
        diagnostics.append(Diagnostic("GB0001", "document kind must be Graph", "$.kind"))
        normalized = normalize_graph(migrated)
        return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))

    api_version = document.get("apiVersion")
    if api_version not in {GRAPH_API_VERSION, *LEGACY_GRAPH_API_VERSIONS}:
        diagnostics.append(
            Diagnostic("GB0002", f"unsupported Graph apiVersion {api_version!r}", "$.apiVersion")
        )

    metadata = migrated.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"]:
        diagnostics.append(Diagnostic("GB0003", "metadata.name is required", "$.metadata.name"))

    spec = migrated.get("spec")
    if not isinstance(spec, dict):
        diagnostics.append(Diagnostic("GB0004", "spec must be a mapping", "$.spec"))
        normalized = normalize_graph(migrated)
        return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))

    nodes = spec.get("nodes", {})
    if nodes is None:
        nodes = {}
    if not isinstance(nodes, dict):
        diagnostics.append(Diagnostic("GB0005", "spec.nodes must be a mapping", "$.spec.nodes"))
        nodes = {}

    interface = spec.get("interface")
    if isinstance(interface, dict):
        for direction in ("inputs", "outputs"):
            ports = interface.get(direction)
            if isinstance(ports, dict):
                for port_name, schema_id in ports.items():
                    path = f"$.spec.interface.{direction}.{port_name}"
                    if not isinstance(schema_id, str):
                        diagnostics.append(
                            Diagnostic(
                                "InvalidSchemaId",
                                f"graph interface {direction[:-1]} schema id must be a string",
                                path,
                            )
                        )
                        continue
                    try:
                        SchemaId.parse(schema_id)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidSchemaId",
                                f"graph interface {direction[:-1]} schema id is invalid: {error}",
                                path,
                            )
                        )

    for node_name, node in nodes.items():
        if not isinstance(node_name, str) or not node_name:
            diagnostics.append(Diagnostic("GB0006", "node name must be a non-empty string", "$.spec.nodes"))
            continue
        if node_name.startswith("$"):
            diagnostics.append(Diagnostic("GB0007", "node names cannot use pseudo-node prefix '$'", f"$.spec.nodes.{node_name}"))
        if not isinstance(node, dict):
            diagnostics.append(Diagnostic("GB0008", "node spec must be a mapping", f"$.spec.nodes.{node_name}"))
            continue
        block = node.get("block")
        if not isinstance(block, str) or "@" not in block or block.endswith("@"):
            diagnostics.append(Diagnostic("GB0009", "node.block must use '<type>@<major>'", f"$.spec.nodes.{node_name}.block"))
        if "connection" in node and "bindings" in node:
            diagnostics.append(
                Diagnostic(
                    "GB1006",
                    "connection shorthand cannot be combined with explicit bindings",
                    f"$.spec.nodes.{node_name}",
                )
            )
        effects = node.get("effects", [])
        if isinstance(effects, str):
            effects = [effects]
        effect_set = {str(effect) for effect in effects} if isinstance(effects, list) else set()
        flow = node.get("flow", {})
        retry = flow.get("retry", {}) if isinstance(flow, dict) else {}
        max_attempts = 1
        idempotency_key = None
        if isinstance(retry, dict):
            max_attempts = int(retry.get("maxAttempts", 1))
            idempotency_key = retry.get("idempotencyKey") or retry.get("idempotency_key")
        elif isinstance(retry, int):
            max_attempts = retry
        effect_retry_requires_key = bool(effect_set & {"external_write", "destructive", "process"})
        if effect_retry_requires_key and max_attempts > 1 and not idempotency_key:
            diagnostics.append(
                Diagnostic(
                    "GB1011",
                    "retrying effectful nodes requires an idempotency key",
                    f"$.spec.nodes.{node_name}.flow.retry",
                )
            )

    output_policy = spec.get("outputPolicy") or spec.get("output_policy")
    output_policy = output_policy if isinstance(output_policy, dict) else None
    if output_policy is not None:
        delivery = output_policy.get("delivery")
        delivery = delivery if isinstance(delivery, dict) else None
        if delivery is not None:
            mode = delivery.get("mode")
            if mode == "bounded_holdback":
                holdback_max_tokens = delivery.get("holdbackMaxTokens", delivery.get("holdback_max_tokens"))
                holdback_max_bytes = delivery.get("holdbackMaxBytes", delivery.get("holdback_max_bytes"))
                holdback_max_duration = (
                    delivery.get("holdbackMaxDuration")
                    or delivery.get("holdback_max_duration")
                    or delivery.get("holdbackMaxDurationMs")
                    or delivery.get("holdback_max_duration_ms")
                )
                has_token_bound = isinstance(holdback_max_tokens, int) and holdback_max_tokens > 0
                has_byte_bound = isinstance(holdback_max_bytes, int) and holdback_max_bytes > 0
                has_duration_bound = (
                    (isinstance(holdback_max_duration, int) and holdback_max_duration > 0)
                    or (
                        isinstance(holdback_max_duration, str)
                        and bool(holdback_max_duration.strip())
                        and holdback_max_duration != "0ms"
                    )
                )
                if not has_token_bound and not has_byte_bound and not has_duration_bound:
                    diagnostics.append(
                        Diagnostic(
                            "UnboundedPolicyHoldback",
                            "bounded_holdback output delivery requires a token, byte, or duration bound",
                            "$.spec.outputPolicy.delivery",
                        )
                    )

            if mode == "immediate_draft":
                delivered_draft_disposition = delivery.get(
                    "deliveredDraftDisposition",
                    delivery.get("delivered_draft_disposition", "retract"),
                )
                if delivered_draft_disposition == "keep":
                    diagnostics.append(
                        Diagnostic(
                            "ImmediateDraftWithoutRetractionSupport",
                            "immediate_draft output delivery requires incomplete or retracted draft semantics",
                            "$.spec.outputPolicy.delivery.deliveredDraftDisposition",
                        )
                    )

        evaluation = (
            output_policy.get("evaluation")
            or output_policy.get("outputEvaluation")
            or output_policy.get("output_evaluation")
        )
        evaluation = evaluation if isinstance(evaluation, dict) else None
        enforcement_points = None
        if evaluation is not None:
            enforcement_points = evaluation.get("enforcementPoints") or evaluation.get("enforcement_points")
        if isinstance(enforcement_points, list):
            on_generation_chunk_index = None
            before_client_delivery_index = None
            before_output_commit_index = None
            for index, enforcement_point in enumerate(enforcement_points):
                if enforcement_point == "on_generation_chunk":
                    on_generation_chunk_index = index
                elif enforcement_point == "before_client_delivery":
                    before_client_delivery_index = index
                elif enforcement_point == "before_output_commit":
                    before_output_commit_index = index
            if before_client_delivery_index is None:
                diagnostics.append(
                    Diagnostic(
                        "OutputPolicyBypass",
                        "output policy enforcement must include the before_client_delivery gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif on_generation_chunk_index is None:
                diagnostics.append(
                    Diagnostic(
                        "OutputPolicyBypass",
                        "output policy enforcement must include the on_generation_chunk gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            elif before_output_commit_index is None:
                diagnostics.append(
                    Diagnostic(
                        "OutputPolicyBypass",
                        "output policy enforcement must include the before_output_commit gate",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
            if (
                before_client_delivery_index is not None
                and on_generation_chunk_index is not None
                and before_client_delivery_index < on_generation_chunk_index
            ):
                diagnostics.append(
                    Diagnostic(
                        "PolicyGateAfterDelivery",
                        "on_generation_chunk policy evaluation must precede before_client_delivery",
                        "$.spec.outputPolicy.evaluation.enforcementPoints",
                    )
                )
        else:
            diagnostics.append(
                Diagnostic(
                    "OutputPolicyBypass",
                    "output policy enforcement must include the before_client_delivery gate",
                    "$.spec.outputPolicy.evaluation.enforcementPoints",
                )
            )

        on_violation = output_policy.get("onViolation") or output_policy.get("on_violation")
        on_violation = on_violation if isinstance(on_violation, dict) else None
        if on_violation is not None:
            disposition = on_violation.get("disposition", "abort_response")
            if disposition in {"abort_response", "abort_turn"}:
                pending_tool_calls = on_violation.get("pendingToolCalls") or on_violation.get("pending_tool_calls")
                pending_tool_calls = pending_tool_calls if isinstance(pending_tool_calls, dict) else {}
                pending_tool_calls_disposition = pending_tool_calls.get("disposition", "deny")
                if pending_tool_calls_disposition == "keep":
                    diagnostics.append(
                        Diagnostic(
                            "PendingToolCallAfterAbort",
                            "policy-aborted responses must deny or cancel pending tool calls",
                            "$.spec.outputPolicy.onViolation.pendingToolCalls.disposition",
                        )
                    )

                durable_result = on_violation.get("durableResult") or on_violation.get("durable_result")
                durable_result = durable_result if isinstance(durable_result, dict) else {}
                durable_result_disposition = durable_result.get("disposition", "none")
                if durable_result_disposition != "none":
                    diagnostics.append(
                        Diagnostic(
                            "CommitAfterPolicyStop",
                            "policy-stopped responses must not commit a durable result",
                            "$.spec.outputPolicy.onViolation.durableResult.disposition",
                        )
                    )

    bindings = spec.get("bindings")
    bindings = bindings if isinstance(bindings, dict) else None
    tools = bindings.get("tools") if bindings is not None else None
    tools = tools if isinstance(tools, dict) else None
    if tools is not None:
        tool_execution = spec.get("toolExecution") or spec.get("tool_execution")
        tool_execution = tool_execution if isinstance(tool_execution, dict) else None
        maximum_parallelism = 1
        parallel_tool_calls = False
        has_effect_serialization_key = False
        if tool_execution is not None:
            configured_parallelism = tool_execution.get(
                "maximumParallelism",
                tool_execution.get("maximum_parallelism", 1),
            )
            if isinstance(configured_parallelism, int):
                maximum_parallelism = configured_parallelism
            parallel_tool_calls = bool(
                tool_execution.get("parallelToolCalls", tool_execution.get("parallel_tool_calls", False))
            )
            effect_serialization = tool_execution.get("effectSerialization") or tool_execution.get(
                "effect_serialization"
            )
            if isinstance(effect_serialization, dict):
                key_template = effect_serialization.get("keyTemplate") or effect_serialization.get("key_template")
                has_effect_serialization_key = isinstance(key_template, str) and bool(key_template.strip())

        has_state_changing_tool = False
        for tool_key, tool in tools.items():
            if not isinstance(tool, dict):
                continue
            effects_value = tool.get("effects", [])
            if isinstance(effects_value, str):
                effects = [effects_value]
            elif isinstance(effects_value, list):
                effects = effects_value
            else:
                effects = []
                diagnostics.append(
                    Diagnostic(
                        "InvalidToolEffect",
                        "tool effects must be a string or list of strings",
                        f"$.spec.bindings.tools.{tool_key}.effects",
                    )
                )
            valid_effects: set[str] = set()
            for effect_index, effect in enumerate(effects):
                if not isinstance(effect, str) or effect not in VALID_TOOL_EFFECTS:
                    effect_path = (
                        f"$.spec.bindings.tools.{tool_key}.effects"
                        if isinstance(effects_value, str)
                        else f"$.spec.bindings.tools.{tool_key}.effects[{effect_index}]"
                    )
                    diagnostics.append(
                        Diagnostic(
                            "InvalidToolEffect",
                            f"invalid tool effect {effect}",
                            effect_path,
                        )
                    )
                    continue
                valid_effects.add(effect)
            state_changing_tool = bool(STATE_CHANGING_TOOL_EFFECTS & valid_effects)
            has_state_changing_tool = has_state_changing_tool or state_changing_tool

            retry_policy_ref = tool.get("retryPolicyRef") or tool.get("retry_policy_ref")
            has_retry_policy_ref = isinstance(retry_policy_ref, str) and bool(retry_policy_ref.strip())
            if state_changing_tool and has_retry_policy_ref and tool.get("idempotency") != "required":
                diagnostics.append(
                    Diagnostic(
                        "NonIdempotentRetry",
                        "retrying state-changing tool effects requires required idempotency",
                        f"$.spec.bindings.tools.{tool_key}.idempotency",
                    )
                )

            approval = tool.get("approval")
            if isinstance(approval, dict):
                mode = approval.get("mode", "policy")
                requires_approval = mode in {"policy", "always"}
                bind_arguments_digest = approval.get(
                    "bindArgumentsDigest",
                    approval.get("bind_arguments_digest", False),
                )
                arguments_digest = (
                    approval.get("argumentsDigest")
                    or approval.get("arguments_digest")
                    or approval.get("argumentsDigestRef")
                    or approval.get("arguments_digest_ref")
                )
                binds_arguments_digest = bool(bind_arguments_digest) or (
                    isinstance(arguments_digest, str) and bool(arguments_digest.strip())
                )
                if requires_approval and not binds_arguments_digest:
                    diagnostics.append(
                        Diagnostic(
                            "ApprovalWithoutArgumentDigest",
                            "explicit tool approval must be bound to immutable argument digest",
                            f"$.spec.bindings.tools.{tool_key}.approval",
                        )
                    )

            definition = tool.get("definition")
            if isinstance(definition, dict):
                input_schema = definition.get("inputSchema") or definition.get("input_schema")
                if not isinstance(input_schema, str) or not input_schema.strip():
                    diagnostics.append(
                        Diagnostic(
                            "ToolSchemaMissing",
                            "model-visible tool definitions require an input schema",
                            f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                        )
                    )
                else:
                    try:
                        SchemaId.parse(input_schema)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidSchemaId",
                                f"tool input schema id is invalid: {error}",
                                f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                            )
                        )
                output_schema = definition.get("outputSchema") or definition.get("output_schema")
                if isinstance(output_schema, str) and output_schema.strip():
                    try:
                        SchemaId.parse(output_schema)
                    except SchemaIdError as error:
                        diagnostics.append(
                            Diagnostic(
                                "InvalidSchemaId",
                                f"tool output schema id is invalid: {error}",
                                f"$.spec.bindings.tools.{tool_key}.definition.outputSchema",
                            )
                        )
            else:
                diagnostics.append(
                    Diagnostic(
                        "ToolSchemaMissing",
                        "model-visible tool definitions require an input schema",
                        f"$.spec.bindings.tools.{tool_key}.definition.inputSchema",
                    )
                )
            implementation = tool.get("implementation")
            if not isinstance(implementation, dict):
                diagnostics.append(
                    Diagnostic(
                        "ToolBindingMissing",
                        "model-visible tools require an executable binding implementation",
                        f"$.spec.bindings.tools.{tool_key}.implementation",
                    )
                )
            else:
                implementation_kind = implementation.get("kind")
                missing_implementation_field: str | None = None
                if implementation_kind == "block":
                    value = implementation.get("block")
                    if not isinstance(value, str) or not value.strip():
                        missing_implementation_field = "block"
                elif implementation_kind == "graph":
                    value = implementation.get("graph")
                    if not isinstance(value, str) or not value.strip():
                        missing_implementation_field = "graph"
                elif implementation_kind == "remote":
                    connection = implementation.get("connection")
                    operation = implementation.get("operation")
                    if not isinstance(connection, str) or not connection.strip():
                        missing_implementation_field = "connection"
                    elif not isinstance(operation, str) or not operation.strip():
                        missing_implementation_field = "operation"
                elif implementation_kind == "mcp":
                    server = implementation.get("server")
                    remote_name = implementation.get("remoteName") or implementation.get("remote_name")
                    if not isinstance(server, str) or not server.strip():
                        missing_implementation_field = "server"
                    elif not isinstance(remote_name, str) or not remote_name.strip():
                        missing_implementation_field = "remoteName"
                elif implementation_kind == "openapi":
                    connection = implementation.get("connection")
                    operation_id = implementation.get("operationId") or implementation.get("operation_id")
                    if not isinstance(connection, str) or not connection.strip():
                        missing_implementation_field = "connection"
                    elif not isinstance(operation_id, str) or not operation_id.strip():
                        missing_implementation_field = "operationId"
                else:
                    diagnostics.append(
                        Diagnostic(
                            "ToolBindingMissing",
                            "tool implementation kind must be one of block, graph, remote, mcp, or openapi",
                            f"$.spec.bindings.tools.{tool_key}.implementation.kind",
                        )
                    )

                if missing_implementation_field is not None:
                    diagnostics.append(
                        Diagnostic(
                            "ToolBindingMissing",
                            f"{implementation_kind} tool implementation requires {missing_implementation_field}",
                            f"$.spec.bindings.tools.{tool_key}.implementation.{missing_implementation_field}",
                        )
                    )

        if (maximum_parallelism > 1 or parallel_tool_calls) and has_state_changing_tool and not has_effect_serialization_key:
            diagnostics.append(
                Diagnostic(
                    "UnsafeParallelEffects",
                    "parallel state-changing tool execution requires an effect serialization key",
                    "$.spec.toolExecution.effectSerialization",
                )
            )

    normalized = normalize_graph(migrated)
    normalized_spec = normalized.get("spec", {})
    normalized_nodes = normalized_spec.get("nodes", {}) if isinstance(normalized_spec, dict) else {}
    edges = normalized_spec.get("edges", []) if isinstance(normalized_spec, dict) else []
    produced_nodes: set[str] = set()
    consumed_nodes: set[str] = set()
    invalid_input_port_nodes: set[str] = set()
    invalid_resource_binding_nodes: set[str] = set()

    if isinstance(edges, list):
        for index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                diagnostics.append(Diagnostic("GB0010", "edge must be a mapping", f"$.spec.edges[{index}]"))
                continue
            source = edge.get("from")
            target = edge.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                diagnostics.append(Diagnostic("GB0011", "edge.from and edge.to must be strings", f"$.spec.edges[{index}]"))
                continue
            for key, endpoint in (("from", source), ("to", target)):
                owner = endpoint.split(".", 1)[0]
                if owner in PSEUDO_NODES:
                    continue
                if owner not in normalized_nodes:
                    diagnostics.append(
                        Diagnostic(
                            "GB1002",
                            f"edge {key} endpoint references unknown node {owner!r}",
                            f"$.spec.edges[{index}].{key}",
                        )
                    )
                elif key == "from":
                    produced_nodes.add(owner)
                else:
                    consumed_nodes.add(owner)

            if block_catalog is not None:
                source_type = None
                target_type = None
                source_required = None
                target_required = None
                source_owner, _, source_path = source.partition(".")
                target_owner, _, target_path = target.partition(".")
                if source_owner not in PSEUDO_NODES and source_owner in normalized_nodes and source_path:
                    source_node = normalized_nodes[source_owner]
                    if isinstance(source_node, dict):
                        descriptor = block_catalog.get(str(source_node.get("block")))
                        if descriptor is not None and descriptor.outputs:
                            port_name = source_path.split(".", 1)[0]
                            output_ports = {port.name: port for port in descriptor.outputs}
                            if port_name not in output_ports:
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1014",
                                        f"block {descriptor.block_id} has no output port {port_name!r}",
                                        f"$.spec.edges[{index}].from",
                                    )
                                )
                            else:
                                source_port = output_ports[port_name]
                                source_type = source_port.type_ref
                                source_required = source_port.required
                if target_owner not in PSEUDO_NODES and target_owner in normalized_nodes and target_path:
                    target_node = normalized_nodes[target_owner]
                    if isinstance(target_node, dict):
                        descriptor = block_catalog.get(str(target_node.get("block")))
                        if descriptor is not None and descriptor.inputs:
                            port_name = target_path.split(".", 1)[0]
                            input_ports = {port.name: port for port in descriptor.inputs}
                            if port_name not in input_ports:
                                invalid_input_port_nodes.add(target_owner)
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1013",
                                        f"block {descriptor.block_id} has no input port {port_name!r}",
                                        f"$.spec.edges[{index}].to",
                                    )
                                )
                            else:
                                target_port = input_ports[port_name]
                                target_type = target_port.type_ref
                                target_required = target_port.required
                if source_type and target_type and source_type != "Any" and target_type != "Any" and source_type != target_type:
                    diagnostics.append(
                        Diagnostic(
                            "GB1018",
                            f"port type mismatch: {source_type} cannot feed {target_type}",
                            f"$.spec.edges[{index}]",
                        )
                    )
                if source_required is False and target_required is True:
                    diagnostics.append(
                        Diagnostic(
                            "GB1015",
                            "optional branch output cannot feed required input",
                            f"$.spec.edges[{index}]",
                        )
                    )

    if block_catalog is not None:
        inbound_by_node: dict[str, set[str]] = {name: set() for name in normalized_nodes}
        if isinstance(edges, list):
            for edge in edges:
                if not isinstance(edge, dict) or not isinstance(edge.get("to"), str):
                    continue
                target_owner, _, target_path = edge["to"].partition(".")
                if target_owner in inbound_by_node and target_path:
                    inbound_by_node[target_owner].add(target_path.split(".", 1)[0])
        for node_name, node in normalized_nodes.items():
            if not isinstance(node, dict):
                continue
            descriptor = block_catalog.get(str(node.get("block")))
            if descriptor is None:
                continue
            if descriptor.resource_slots:
                bindings = node.get("bindings", {})
                if bindings is None:
                    bindings = {}
                if not isinstance(bindings, dict):
                    diagnostics.append(
                        Diagnostic("GB1017", "node bindings must be a mapping", f"$.spec.nodes.{node_name}.bindings")
                    )
                    bindings = {}
                slot_names = {slot.name for slot in descriptor.resource_slots}
                for binding_name in bindings:
                    if binding_name not in slot_names:
                        invalid_resource_binding_nodes.add(node_name)
                        diagnostics.append(
                            Diagnostic(
                                "GB1017",
                                f"block {descriptor.block_id} has no resource slot {binding_name!r}",
                                f"$.spec.nodes.{node_name}.bindings.{binding_name}",
                            )
                        )
                for slot in descriptor.resource_slots:
                    if node_name not in invalid_resource_binding_nodes and not slot.optional and slot.name not in bindings:
                        diagnostics.append(
                            Diagnostic(
                                "GB1016",
                                f"required resource slot {slot.name!r} is not bound for node {node_name!r}",
                                f"$.spec.nodes.{node_name}.bindings",
                            )
                        )
            if node_name in invalid_input_port_nodes:
                continue
            for port in descriptor.inputs:
                if port.required and port.name not in inbound_by_node[node_name]:
                    diagnostics.append(
                        Diagnostic(
                            "GB1003",
                            f"required input {port.name!r} is never produced for node {node_name!r}",
                            f"$.spec.nodes.{node_name}",
                        )
                    )

    for node_name, node in normalized_nodes.items():
        if isinstance(node, dict) and isinstance(node.get("when"), str):
            owner = node["when"].split(".", 1)[0]
            if owner not in PSEUDO_NODES and owner not in normalized_nodes:
                diagnostics.append(
                    Diagnostic("GB1002", f"when references unknown node {owner!r}", f"$.spec.nodes.{node_name}.when")
                )
            elif owner not in PSEUDO_NODES:
                produced_nodes.add(owner)
                consumed_nodes.add(node_name)

    interface = normalized_spec.get("interface", {}) if isinstance(normalized_spec, dict) else {}
    outputs = interface.get("outputs", {}) if isinstance(interface, dict) else {}
    has_declared_output = isinstance(outputs, dict) and bool(outputs)
    output_edges = [edge for edge in edges if isinstance(edge, dict) and isinstance(edge.get("to"), str) and edge["to"].startswith("$output.")]
    if has_declared_output and not output_edges:
        diagnostics.append(
            Diagnostic(
                "GB1003",
                "graph declares outputs but no edge writes to $output",
                "$.spec.interface.outputs",
                "warning",
            )
        )

    if output_edges:
        reachable: set[str] = set()
        stack = [edge["from"].split(".", 1)[0] for edge in output_edges if isinstance(edge.get("from"), str)]
        reverse_edges: dict[str, list[str]] = {}
        for edge in edges:
            if isinstance(edge, dict) and isinstance(edge.get("from"), str) and isinstance(edge.get("to"), str):
                source_owner = edge["from"].split(".", 1)[0]
                target_owner = edge["to"].split(".", 1)[0]
                reverse_edges.setdefault(target_owner, []).append(source_owner)
        while stack:
            owner = stack.pop()
            if owner in reachable or owner in PSEUDO_NODES:
                continue
            reachable.add(owner)
            stack.extend(reverse_edges.get(owner, []))
        for node_name in sorted(normalized_nodes):
            if node_name not in reachable and node_name not in produced_nodes and node_name not in consumed_nodes:
                diagnostics.append(Diagnostic("GB1001", f"node {node_name!r} is not connected", f"$.spec.nodes.{node_name}", "warning"))

    return Plan(normalized, canonical_hash(normalized), DiagnosticSet(tuple(diagnostics)))
