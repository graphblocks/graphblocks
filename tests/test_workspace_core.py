from __future__ import annotations

import pytest

from graphblocks.evaluation import ChangeSet, ResourceSnapshotRef
from graphblocks.policy import PrincipalRef
from graphblocks.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceMutationDeniedError,
    WorkspaceMutationPolicy,
    WorkspaceSnapshot,
    WorkspaceSnapshotConflictError,
)


def test_workspace_snapshot_digest_ignores_record_identity_and_sorts_resources() -> None:
    left = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-a",
        revision=1,
        resources=(
            ResourceSnapshotRef("b.txt", "sha256:b", resource_kind="file"),
            ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),
        ),
        created_at="2026-06-24T00:00:00Z",
    )
    right = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-b",
        revision=2,
        resources=tuple(reversed(left.resources)),
        created_at="2026-06-24T00:01:00Z",
    )

    assert left.content_digest() == right.content_digest()
    assert [resource.resource_id for resource in left.resources] == ["a.txt", "b.txt"]


def test_workspace_fork_preserves_base_snapshot_digest() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),),
        created_at="2026-06-24T00:00:00Z",
    )

    fork = base.fork("workspace-branch", "snapshot-branch", "2026-06-24T00:02:00Z")

    assert fork.workspace_id == "workspace-branch"
    assert fork.base_snapshot_id == "snapshot-1"
    assert fork.base_snapshot_digest == base.content_digest()
    assert fork.revision == 1


def test_workspace_mutation_policy_requires_allowed_kind_and_reviewer() -> None:
    policy = WorkspaceMutationPolicy(
        policy_id="policy-1",
        allowed_resource_kinds=("file", "test"),
        denied_operations=("process.exec",),
        required_review_scopes=("quality",),
    )
    principal = PrincipalRef("author-1")
    change_set = ChangeSet(
        change_set_id="change-1",
        base=ResourceSnapshotRef("workspace", "sha256:base", resource_kind="workspace"),
        candidate=ResourceSnapshotRef("workspace", "sha256:candidate", resource_kind="workspace"),
        operations=[
            {"op": "file.write", "resource_kind": "file"},
            {"op": "process.exec", "resource_kind": "process"},
        ],
    )

    decision = policy.evaluate(change_set, principal, review_scopes=("quality",))

    assert not decision.allowed
    assert decision.reason_codes == ("workspace.operation_denied", "workspace.resource_kind_denied")

    allowed = policy.evaluate(
        ChangeSet(
            change_set_id="change-2",
            base=change_set.base,
            candidate=change_set.candidate,
            operations=[{"op": "file.write", "resource_kind": "file"}],
        ),
        principal,
        review_scopes=("quality",),
    )

    assert allowed.allowed


def test_workspace_store_compare_and_swap_commit_updates_revision() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),),
        created_at="2026-06-24T00:00:00Z",
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)

    committed = store.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(
            ResourceSnapshotRef("a.txt", "sha256:a2", resource_kind="file"),
            ResourceSnapshotRef("b.txt", "sha256:b", resource_kind="file"),
        ),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
    )

    assert committed.snapshot.revision == 2
    assert committed.previous_snapshot_id == "snapshot-1"
    assert committed.snapshot.resources[0].resource_id == "a.txt"
    assert store.current("workspace-1").snapshot_id == "snapshot-2"

    with pytest.raises(WorkspaceSnapshotConflictError) as error:
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-1",
            new_snapshot_id="snapshot-3",
            resources=(),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:06:00Z",
            change_set_id="change-2",
        )

    assert error.value.expected_snapshot_id == "snapshot-1"
    assert error.value.actual_snapshot_id == "snapshot-2"


def test_workspace_store_rejects_policy_denied_commit() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(),
        created_at="2026-06-24T00:00:00Z",
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)
    policy = WorkspaceMutationPolicy(
        policy_id="policy-1",
        allowed_resource_kinds=("file",),
        denied_operations=(),
        required_review_scopes=("quality",),
    )

    with pytest.raises(WorkspaceMutationDeniedError) as error:
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-1",
            new_snapshot_id="snapshot-2",
            resources=(ResourceSnapshotRef("proc", "sha256:proc", resource_kind="process"),),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:06:00Z",
            change_set_id="change-1",
            policy=policy,
            review_scopes=("quality",),
            operations=[{"op": "process.exec", "resource_kind": "process"}],
        )

    assert error.value.reason_codes == ("workspace.resource_kind_denied",)
