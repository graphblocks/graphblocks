from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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


def load_documents(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as stream:
        documents = [
            document
            for document in yaml.load_all(stream, Loader=_DuplicateKeySafeLoader)
            if document is not None
        ]
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise ValueError(f"{source}:{index + 1}: expected a YAML mapping document")
    return documents


def load_composed_documents(
    path: str | Path,
    *,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    from .composition import compose_documents

    return list(compose_documents(path, root=root).documents)


__all__ = ["load_composed_documents", "load_documents"]
