from __future__ import annotations

from copy import deepcopy
from typing import Any

GRAPH_API_VERSION = "graphblocks.ai/v1alpha3"
LEGACY_GRAPH_API_VERSIONS = {"graphblocks.ai/v1alpha1", "graphblocks.ai/v1alpha2"}


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    migrated = deepcopy(document)
    if migrated.get("kind") == "Graph" and migrated.get("apiVersion") in LEGACY_GRAPH_API_VERSIONS:
        previous = str(migrated["apiVersion"])
        migrated["apiVersion"] = GRAPH_API_VERSION
        metadata = migrated.setdefault("metadata", {})
        if isinstance(metadata, dict):
            annotations = metadata.setdefault("annotations", {})
            if isinstance(annotations, dict):
                annotations.setdefault("graphblocks.ai/migratedFrom", previous)
    return migrated

