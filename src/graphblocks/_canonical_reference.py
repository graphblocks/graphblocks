"""Internal access to the deterministic Python canonical reference oracle.

Public canonical identity helpers route to the normative native boundary. Python
authoring, validation, and reference implementations import this module so they
remain usable without a native wheel and never become an implicit authority
fallback for a public native facade.
"""

from .canonical import (
    MAX_CANONICAL_INTEGER_DIGITS,
    MAX_CANONICAL_JSON_DEPTH,
    PSEUDO_NODES,
    _MANUAL_INTEGER_BIT_LENGTH,
    _canonical_dumps,
    _has_unicode_surrogate,
    _normalize_graph_unchecked,
    _reject_duplicate_keys,
    canonical_dumps_reference as canonical_dumps,
    canonical_hash_reference as canonical_hash,
    canonical_loads_reference as canonical_loads,
)


def normalize_graph(document: dict[str, object]) -> dict[str, object]:
    """Normalize through the explicit Python migration reference oracle."""

    from .migration import migrate_document_reference

    return _normalize_graph_unchecked(migrate_document_reference(document))

__all__ = [
    "MAX_CANONICAL_INTEGER_DIGITS",
    "MAX_CANONICAL_JSON_DEPTH",
    "PSEUDO_NODES",
    "_MANUAL_INTEGER_BIT_LENGTH",
    "_canonical_dumps",
    "_has_unicode_surrogate",
    "_normalize_graph_unchecked",
    "_reject_duplicate_keys",
    "canonical_dumps",
    "canonical_hash",
    "canonical_loads",
    "normalize_graph",
]
