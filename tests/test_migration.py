from __future__ import annotations

from graphblocks import migrate_document


def test_legacy_graph_api_version_migrates_to_v1alpha3() -> None:
    migrated = migrate_document(
        {
            "apiVersion": "graphblocks.ai/v1alpha2",
            "kind": "Graph",
            "metadata": {"name": "legacy"},
            "spec": {"nodes": {}},
        }
    )

    assert migrated["apiVersion"] == "graphblocks.ai/v1alpha3"
    assert migrated["metadata"]["annotations"]["graphblocks.ai/migratedFrom"] == "graphblocks.ai/v1alpha2"

