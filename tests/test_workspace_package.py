from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks.evaluation import ResourceSnapshotRef


ROOT = Path(__file__).parents[1]


def test_workspace_package_reexports_snapshot_and_store_contracts(monkeypatch) -> None:
    graphblocks_workspace = importlib.import_module("graphblocks.workspace")

    snapshot = graphblocks_workspace.WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),),
        created_at="2026-06-24T00:00:00Z",
    )
    store = graphblocks_workspace.InMemoryWorkspaceStore().put_snapshot(snapshot)

    assert store.current("workspace-1") == snapshot
    assert snapshot.content_digest().startswith("sha256:")
