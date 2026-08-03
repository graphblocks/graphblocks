from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_MAX_DOCUMENT_DEPTH = 64
_MAX_DOCUMENT_NODES = 10_000


@dataclass(frozen=True, slots=True)
class InputBudget:
    max_documents: int = 256
    max_input_bytes: int = 8 * 1024 * 1024
    max_cumulative_nodes: int = 100_000
    max_files: int = 256
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name in (
            "max_documents",
            "max_input_bytes",
            "max_cumulative_nodes",
            "max_files",
            "max_total_bytes",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"input budget {field_name} must be a positive integer")
        if self.max_total_bytes < self.max_input_bytes:
            raise ValueError(
                "input budget max_total_bytes must be at least max_input_bytes"
            )


DEFAULT_INPUT_BUDGET = InputBudget()


class _DuplicateKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _DuplicateKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ValueError(f"duplicate YAML mapping key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _validate_document_value(
    source: Path,
    document_index: int,
    value: object,
    *,
    depth: int = 0,
    active_containers: set[int] | None = None,
    node_count: list[int] | None = None,
) -> None:
    if depth > _MAX_DOCUMENT_DEPTH:
        raise ValueError(
            f"{source}:{document_index}: YAML document exceeds maximum depth "
            f"{_MAX_DOCUMENT_DEPTH}"
        )
    active = set() if active_containers is None else active_containers
    count = [0] if node_count is None else node_count
    count[0] += 1
    if count[0] > _MAX_DOCUMENT_NODES:
        raise ValueError(
            f"{source}:{document_index}: YAML document exceeds maximum node count "
            f"{_MAX_DOCUMENT_NODES}"
        )

    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{source}:{document_index}: YAML document must not be recursive")
        active.add(identity)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(
                        f"{source}:{document_index}: YAML mapping keys must be strings"
                    )
                if any("\ud800" <= character <= "\udfff" for character in key):
                    raise ValueError(
                        f"{source}:{document_index}: YAML strings must contain "
                        "only Unicode scalar values"
                    )
                _validate_document_value(
                    source,
                    document_index,
                    item,
                    depth=depth + 1,
                    active_containers=active,
                    node_count=count,
                )
        finally:
            active.remove(identity)
        return

    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{source}:{document_index}: YAML document must not be recursive")
        active.add(identity)
        try:
            for item in value:
                _validate_document_value(
                    source,
                    document_index,
                    item,
                    depth=depth + 1,
                    active_containers=active,
                    node_count=count,
                )
        finally:
            active.remove(identity)
        return

    if isinstance(value, str):
        if any("\ud800" <= character <= "\udfff" for character in value):
            raise ValueError(
                f"{source}:{document_index}: YAML strings must contain "
                "only Unicode scalar values"
            )
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError(
            f"{source}:{document_index}: YAML document numbers must be finite"
        )
    raise ValueError(
        f"{source}:{document_index}: YAML document values must be JSON-compatible"
    )


def load_documents(
    path: str | Path,
    *,
    budget: InputBudget = DEFAULT_INPUT_BUDGET,
) -> list[dict[str, Any]]:
    if not isinstance(budget, InputBudget):
        raise TypeError("load_documents budget must be an InputBudget")
    source = Path(path)
    try:
        with source.open("rb") as stream:
            encoded = stream.read(budget.max_input_bytes + 1)
        if len(encoded) > budget.max_input_bytes:
            raise ValueError(
                f"{source}: YAML input exceeds maximum byte count "
                f"{budget.max_input_bytes}"
            )
        text = encoded.decode("utf-8")
        documents: list[dict[str, Any]] = []
        cumulative_nodes = 0
        for document_index, document in enumerate(
            yaml.load_all(text, Loader=_DuplicateKeySafeLoader),
            start=1,
        ):
            if document_index > budget.max_documents:
                raise ValueError(
                    f"{source}: YAML stream exceeds maximum document count "
                    f"{budget.max_documents}"
                )
            if document is None:
                continue
            if not isinstance(document, dict):
                raise ValueError(
                    f"{source}:{document_index}: expected a YAML mapping document"
                )
            document_nodes = [0]
            _validate_document_value(
                source,
                document_index,
                document,
                node_count=document_nodes,
            )
            cumulative_nodes += document_nodes[0]
            if cumulative_nodes > budget.max_cumulative_nodes:
                raise ValueError(
                    f"{source}:{document_index}: YAML stream exceeds maximum "
                    f"cumulative node count {budget.max_cumulative_nodes}"
                )
            documents.append(document)
    except RecursionError as error:
        raise ValueError(
            f"{source}: invalid YAML: document nesting exceeds parser limit"
        ) from error
    except UnicodeError as error:
        raise ValueError(
            f"{source}: invalid YAML: document is not UTF-8"
        ) from error
    except yaml.YAMLError as error:
        raise ValueError(f"{source}: invalid YAML: {error}") from error
    return documents


def load_composed_documents(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    from .composition import compose_documents

    return list(compose_documents(path, root=root).mutable_documents())


__all__ = ["DEFAULT_INPUT_BUDGET", "InputBudget", "load_composed_documents", "load_documents"]
