from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import PSEUDO_NODES, canonical_hash, normalize_graph
from .diagnostics import Diagnostic, DiagnosticSet
from .migration import GRAPH_API_VERSION, LEGACY_GRAPH_API_VERSIONS, migrate_document
from .plugins import BlockCatalog


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

    normalized = normalize_graph(migrated)
    normalized_spec = normalized.get("spec", {})
    normalized_nodes = normalized_spec.get("nodes", {}) if isinstance(normalized_spec, dict) else {}
    edges = normalized_spec.get("edges", []) if isinstance(normalized_spec, dict) else []
    produced_nodes: set[str] = set()
    consumed_nodes: set[str] = set()
    invalid_input_port_nodes: set[str] = set()

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
                source_owner, _, source_path = source.partition(".")
                target_owner, _, target_path = target.partition(".")
                if source_owner not in PSEUDO_NODES and source_owner in normalized_nodes and source_path:
                    source_node = normalized_nodes[source_owner]
                    if isinstance(source_node, dict):
                        descriptor = block_catalog.get(str(source_node.get("block")))
                        if descriptor is not None and descriptor.outputs:
                            port_name = source_path.split(".", 1)[0]
                            output_names = {port.name for port in descriptor.outputs}
                            if port_name not in output_names:
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1014",
                                        f"block {descriptor.block_id} has no output port {port_name!r}",
                                        f"$.spec.edges[{index}].from",
                                    )
                                )
                if target_owner not in PSEUDO_NODES and target_owner in normalized_nodes and target_path:
                    target_node = normalized_nodes[target_owner]
                    if isinstance(target_node, dict):
                        descriptor = block_catalog.get(str(target_node.get("block")))
                        if descriptor is not None and descriptor.inputs:
                            port_name = target_path.split(".", 1)[0]
                            input_names = {port.name for port in descriptor.inputs}
                            if port_name not in input_names:
                                invalid_input_port_nodes.add(target_owner)
                                diagnostics.append(
                                    Diagnostic(
                                        "GB1013",
                                        f"block {descriptor.block_id} has no input port {port_name!r}",
                                        f"$.spec.edges[{index}].to",
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
