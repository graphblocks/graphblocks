from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .migration import GRAPH_API_VERSION, migrate_document

PSEUDO_NODES = {"$input", "$output", "$state", "$context", "$execution"}


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def normalize_graph(document: dict[str, Any]) -> dict[str, Any]:
    graph = migrate_document(document)
    if graph.get("kind") != "Graph":
        return deepcopy(graph)

    normalized = deepcopy(graph)
    normalized["apiVersion"] = GRAPH_API_VERSION
    spec = normalized.setdefault("spec", {})
    if not isinstance(spec, dict):
        return normalized

    nodes = spec.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
        spec["nodes"] = nodes

    edges: list[dict[str, str]] = []
    existing_edges = spec.get("edges", [])
    if isinstance(existing_edges, list):
        for edge in existing_edges:
            if isinstance(edge, dict) and isinstance(edge.get("from"), str) and isinstance(edge.get("to"), str):
                edges.append({"from": edge["from"], "to": edge["to"]})

    for node_name in sorted(nodes):
        node = nodes[node_name]
        if not isinstance(node, dict):
            continue
        inputs = node.pop("inputs", None)
        if isinstance(inputs, dict):
            stack: list[tuple[str, Any]] = [(key, value) for key, value in inputs.items()]
            while stack:
                port_path, value = stack.pop()
                if isinstance(value, str):
                    edges.append({"from": value, "to": f"{node_name}.{port_path}"})
                elif isinstance(value, dict):
                    for key, nested in value.items():
                        stack.append((f"{port_path}.{key}", nested))
                elif isinstance(value, list):
                    for index, nested in enumerate(value):
                        stack.append((f"{port_path}.{index}", nested))
        outputs = node.pop("outputs", None)
        if isinstance(outputs, dict):
            stack = [(key, value) for key, value in outputs.items()]
            while stack:
                port_path, value = stack.pop()
                if isinstance(value, str):
                    edges.append({"from": f"{node_name}.{port_path}", "to": value})
                elif isinstance(value, dict):
                    for key, nested in value.items():
                        stack.append((f"{port_path}.{key}", nested))
                elif isinstance(value, list):
                    for index, nested in enumerate(value):
                        stack.append((f"{port_path}.{index}", nested))
        connection = node.pop("connection", None)
        if connection is not None and "bindings" not in node:
            node["bindings"] = {"default": connection}

    spec["nodes"] = {name: nodes[name] for name in sorted(nodes)}
    spec["edges"] = sorted(edges, key=lambda item: (item["from"], item["to"]))
    return normalized

