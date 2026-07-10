from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from .migration import GRAPH_API_VERSION, migrate_document

PSEUDO_NODES = {"$input", "$output", "$state", "$context", "$execution"}


def canonical_loads(value: str | bytes | bytearray) -> Any:
    return json.loads(
        value,
        parse_float=lambda token: (
            float(token)
            if math.isfinite(float(token))
            and canonical_dumps(Decimal(token)) == canonical_dumps(float(token))
            else Decimal(token)
        ),
        parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
    )


def canonical_dumps(value: Any) -> str:
    pending_values: list[Any] = [value]
    occupied_strings: set[str] = set()
    has_decimal = False
    while pending_values:
        current_value = pending_values.pop()
        if isinstance(current_value, Mapping):
            for key, child_value in current_value.items():
                if not isinstance(key, str):
                    raise TypeError("canonical JSON object keys must be strings")
                occupied_strings.add(key)
                pending_values.append(child_value)
        elif isinstance(current_value, list | tuple):
            pending_values.extend(current_value)
        elif isinstance(current_value, str):
            occupied_strings.add(current_value)
        elif isinstance(current_value, Decimal):
            has_decimal = True
    if not has_decimal:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    root: list[Any] = [None]
    pending_copies: list[tuple[Any, dict[str, Any] | list[Any], str | int]] = [(value, root, 0)]
    decimal_tokens: dict[str, str] = {}
    token_index = 0
    while pending_copies:
        current_value, parent, parent_key = pending_copies.pop()
        if isinstance(current_value, Decimal):
            if not current_value.is_finite():
                raise ValueError("Out of range decimal values are not JSON compliant")
            number_tuple = current_value.as_tuple()
            digits = "".join(str(digit) for digit in number_tuple.digits)
            significant = digits.lstrip("0")
            if not significant:
                canonical_number = "-0.0" if number_tuple.sign else "0.0"
            else:
                exponent = int(number_tuple.exponent) + len(significant) - 1
                coefficient = significant.rstrip("0")
                sign = "-" if number_tuple.sign else ""
                if -4 <= exponent < 16:
                    decimal_point = exponent + 1
                    if decimal_point <= 0:
                        canonical_number = sign + "0." + ("0" * -decimal_point) + coefficient
                    elif decimal_point >= len(coefficient):
                        canonical_number = (
                            sign
                            + coefficient
                            + ("0" * (decimal_point - len(coefficient)))
                            + ".0"
                        )
                    else:
                        canonical_number = (
                            sign
                            + coefficient[:decimal_point]
                            + "."
                            + coefficient[decimal_point:]
                        )
                else:
                    canonical_number = sign + coefficient[0]
                    if len(coefficient) > 1:
                        canonical_number += f".{coefficient[1:]}"
                    exponent_magnitude = str(abs(exponent)).rjust(2, "0")
                    canonical_number += f"e{'-' if exponent < 0 else '+'}{exponent_magnitude}"
            token = f"\x00graphblocks-decimal-{token_index}\x00"
            while token in occupied_strings:
                token_index += 1
                token = f"\x00graphblocks-decimal-{token_index}\x00"
            token_index += 1
            occupied_strings.add(token)
            decimal_tokens[token] = canonical_number
            parent[parent_key] = token
        elif isinstance(current_value, Mapping):
            copied_mapping: dict[str, Any] = {}
            parent[parent_key] = copied_mapping
            for key, child_value in reversed(tuple(current_value.items())):
                pending_copies.append((child_value, copied_mapping, key))
        elif isinstance(current_value, list | tuple):
            copied_list: list[Any] = [None] * len(current_value)
            parent[parent_key] = copied_list
            for index in range(len(current_value) - 1, -1, -1):
                pending_copies.append((current_value[index], copied_list, index))
        else:
            parent[parent_key] = current_value

    encoded = json.dumps(
        root[0],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    for token, canonical_number in decimal_tokens.items():
        encoded = encoded.replace(
            json.dumps(token, ensure_ascii=False, separators=(",", ":")),
            canonical_number,
        )
    return encoded


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
