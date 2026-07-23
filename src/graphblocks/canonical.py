from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
import hashlib
import json
import math
from copy import deepcopy
from typing import Any

from .migration import migrate_document

PSEUDO_NODES = {"$input", "$output", "$state", "$context", "$execution"}
MAX_CANONICAL_JSON_DEPTH = 64
_MANUAL_INTEGER_BIT_LENGTH = 1_024
_INTEGER_CHUNK_BASE = 1_000_000_000
_INTEGER_CHUNK_DIGITS = 9


def _depth_error() -> ValueError:
    return ValueError(
        f"canonical JSON nesting must not exceed {MAX_CANONICAL_JSON_DEPTH} levels"
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _has_unicode_surrogate(value: str) -> bool:
    return any("\ud800" <= character <= "\udfff" for character in value)


def _parse_integer(token: str) -> int:
    negative = token.startswith("-")
    digits = token[1:] if negative else token
    first_chunk_length = len(digits) % _INTEGER_CHUNK_DIGITS
    if first_chunk_length == 0:
        first_chunk_length = _INTEGER_CHUNK_DIGITS
    value = int(digits[:first_chunk_length])
    for offset in range(first_chunk_length, len(digits), _INTEGER_CHUNK_DIGITS):
        value = (
            value * _INTEGER_CHUNK_BASE
            + int(digits[offset : offset + _INTEGER_CHUNK_DIGITS])
        )
    return -value if negative else value


def _format_integer(value: int) -> str:
    if value == 0:
        return "0"
    negative = value < 0
    magnitude = -value if negative else value
    chunks: list[int] = []
    while magnitude:
        magnitude, chunk = divmod(magnitude, _INTEGER_CHUNK_BASE)
        chunks.append(chunk)
    encoded = str(chunks.pop())
    encoded += "".join(
        f"{chunk:0{_INTEGER_CHUNK_DIGITS}d}" for chunk in reversed(chunks)
    )
    return f"-{encoded}" if negative else encoded


def _canonical_snapshot(
    value: Any,
    *,
    reject_tuples: bool = False,
) -> tuple[Any, set[str], bool, bool]:
    """Copy and validate one potentially stateful Python value exactly once."""

    root: list[Any] = [None]
    pending: list[
        tuple[Any, dict[str, Any] | list[Any], str | int, int, bool]
    ] = [(value, root, 0, 0, False)]
    active_containers: set[int] = set()
    occupied_strings: set[str] = set()
    has_decimal = False
    has_large_integer = False
    while pending:
        current_value, parent, parent_key, depth, leaving = pending.pop()
        if leaving:
            active_containers.remove(id(current_value))
            continue
        if depth > MAX_CANONICAL_JSON_DEPTH:
            raise _depth_error()
        if isinstance(current_value, Mapping):
            identity = id(current_value)
            if identity in active_containers:
                raise ValueError("canonical JSON values must not be recursive")
            items = tuple(current_value.items())
            copied_mapping: dict[str, Any] = {}
            seen_keys: set[str] = set()
            for key, _child_value in items:
                if not isinstance(key, str):
                    raise TypeError("canonical JSON object keys must be strings")
                if _has_unicode_surrogate(key):
                    raise ValueError(
                        "canonical JSON strings must contain only Unicode scalar values"
                    )
                if key in seen_keys:
                    raise ValueError(f"duplicate JSON object key {key!r}")
                seen_keys.add(key)
                occupied_strings.add(key)
            parent[parent_key] = copied_mapping
            active_containers.add(identity)
            pending.append((current_value, parent, parent_key, depth, True))
            for key, child_value in reversed(items):
                pending.append(
                    (child_value, copied_mapping, key, depth + 1, False)
                )
        elif isinstance(current_value, list | tuple):
            if reject_tuples and isinstance(current_value, tuple):
                raise TypeError("canonical JSON arrays must be lists")
            identity = id(current_value)
            if identity in active_containers:
                raise ValueError("canonical JSON values must not be recursive")
            items = tuple(current_value)
            copied_list: list[Any] = [None] * len(items)
            parent[parent_key] = copied_list
            active_containers.add(identity)
            pending.append((current_value, parent, parent_key, depth, True))
            for index in range(len(items) - 1, -1, -1):
                pending.append(
                    (items[index], copied_list, index, depth + 1, False)
                )
        else:
            if isinstance(current_value, str):
                if _has_unicode_surrogate(current_value):
                    raise ValueError(
                        "canonical JSON strings must contain only Unicode scalar values"
                    )
                occupied_strings.add(current_value)
            elif isinstance(current_value, Decimal):
                has_decimal = True
            elif (
                isinstance(current_value, int)
                and not isinstance(current_value, bool)
                and current_value.bit_length() > _MANUAL_INTEGER_BIT_LENGTH
            ):
                has_large_integer = True
            parent[parent_key] = current_value
    return root[0], occupied_strings, has_decimal, has_large_integer


def canonical_loads(value: str | bytes | bytearray) -> Any:
    try:
        decoded = json.loads(
            value,
            parse_float=lambda token: (
                float(token)
                if math.isfinite(float(token))
                and canonical_dumps(Decimal(token)) == canonical_dumps(float(token))
                else Decimal(token)
            ),
            parse_int=_parse_integer,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RecursionError as error:
        raise _depth_error() from error
    snapshot, _occupied_strings, _has_decimal, _has_large_integer = (
        _canonical_snapshot(decoded)
    )
    return snapshot


def canonical_dumps(value: Any, *, _reject_tuples: bool = False) -> str:
    snapshot, occupied_strings, has_decimal, has_large_integer = (
        _canonical_snapshot(value, reject_tuples=_reject_tuples)
    )
    if not has_decimal and not has_large_integer:
        try:
            return json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except RecursionError as error:
            raise _depth_error() from error

    root: list[Any] = [None]
    pending_copies: list[tuple[Any, dict[str, Any] | list[Any], str | int]] = [
        (snapshot, root, 0)
    ]
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
        elif (
            isinstance(current_value, int)
            and not isinstance(current_value, bool)
            and current_value.bit_length() > _MANUAL_INTEGER_BIT_LENGTH
        ):
            canonical_number = _format_integer(current_value)
            token = f"\x00graphblocks-integer-{token_index}\x00"
            while token in occupied_strings:
                token_index += 1
                token = f"\x00graphblocks-integer-{token_index}\x00"
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

    try:
        encoded = json.dumps(
            root[0],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except RecursionError as error:
        raise _depth_error() from error
    for token, canonical_number in decimal_tokens.items():
        encoded = encoded.replace(
            json.dumps(token, ensure_ascii=False, separators=(",", ":")),
            canonical_number,
        )
    return encoded


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def normalize_graph(document: dict[str, Any]) -> dict[str, Any]:
    return _normalize_graph_unchecked(migrate_document(document))


def _normalize_graph_unchecked(document: dict[str, Any]) -> dict[str, Any]:
    """Normalize a graph already admitted by its caller.

    Stable public callers go through :func:`normalize_graph`. The compiler uses
    this helper to retain an alpha version when preview-only fields cannot be
    represented by the stable v1 schema, and to return structured diagnostics
    for unsupported versions instead of raising during plan construction.
    """

    graph = deepcopy(document)
    if graph.get("kind") != "Graph":
        return graph

    normalized = graph
    spec = normalized.setdefault("spec", {})
    if not isinstance(spec, dict):
        return normalized

    nodes = spec.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
        spec["nodes"] = nodes

    edges: list[dict[str, str]] = []
    input_edges: list[dict[str, str]] = []
    output_edges: list[dict[str, str]] = []
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
                    input_edges.append(
                        {"from": value, "to": f"{node_name}.{port_path}"}
                    )
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
                    output_edges.append(
                        {"from": f"{node_name}.{port_path}", "to": value}
                    )
                elif isinstance(value, dict):
                    for key, nested in value.items():
                        stack.append((f"{port_path}.{key}", nested))
                elif isinstance(value, list):
                    for index, nested in enumerate(value):
                        stack.append((f"{port_path}.{index}", nested))
        connection = node.pop("connection", None)
        if connection is not None and "bindings" not in node:
            node["bindings"] = {"default": connection}

    edge_identities = {(edge["from"], edge["to"]) for edge in edges}
    for edge in (*input_edges, *output_edges):
        identity = (edge["from"], edge["to"])
        if identity in edge_identities:
            continue
        edges.append(edge)
        edge_identities.add(identity)
    spec["nodes"] = {name: nodes[name] for name in sorted(nodes)}
    spec["edges"] = sorted(edges, key=lambda item: (item["from"], item["to"]))
    return normalized
