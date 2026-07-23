from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml


_MAX_DOCUMENT_DEPTH = 64
_MAX_DOCUMENT_NODES = 10_000


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


def load_documents(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as stream:
            documents = [
                document
                for document in yaml.load_all(stream, Loader=_DuplicateKeySafeLoader)
                if document is not None
            ]
    except RecursionError as error:
        raise ValueError(
            f"{source}: invalid YAML: document nesting exceeds parser limit"
        ) from error
    except yaml.YAMLError as error:
        raise ValueError(f"{source}: invalid YAML: {error}") from error
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"{source}:{index + 1}: expected a YAML mapping document")
        _validate_document_value(source, index + 1, document)
    return documents


def load_composed_documents(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    from .composition import compose_documents

    return list(compose_documents(path, root=root).mutable_documents())


__all__ = ["load_composed_documents", "load_documents"]
