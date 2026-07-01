from __future__ import annotations

import pytest

from graphblocks.evaluation import ChangeSet, ResourceSnapshotRef
from graphblocks.policy import PrincipalRef
from graphblocks.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceCommit,
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


def test_workspace_snapshot_rejects_duplicate_resource_ids() -> None:
    with pytest.raises(ValueError, match="workspace snapshot resource_id values must be unique"):
        WorkspaceSnapshot(
            workspace_id="workspace-1",
            snapshot_id="snapshot-a",
            revision=1,
            resources=(
                ResourceSnapshotRef("a.txt", "sha256:a1", resource_kind="file"),
                ResourceSnapshotRef("a.txt", "sha256:a2", resource_kind="file"),
            ),
            created_at="2026-06-24T00:00:00Z",
        )


def test_workspace_snapshot_validates_identity_revision_resources_and_metadata() -> None:
    with pytest.raises(ValueError, match="workspace snapshot workspace_id must not be empty"):
        WorkspaceSnapshot(" ", "snapshot-1", 1)
    with pytest.raises(ValueError, match="workspace snapshot revision must be positive"):
        WorkspaceSnapshot("workspace-1", "snapshot-1", 0)
    with pytest.raises(ValueError, match="workspace snapshot resources items must be ResourceSnapshotRef"):
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1, resources=(object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="workspace snapshot metadata must be a mapping"):
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1, metadata=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="workspace snapshot metadata keys must be strings"):
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1, metadata={object(): "value"})  # type: ignore[dict-item]


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


def test_workspace_commit_validates_identity_links_and_copies_snapshot() -> None:
    snapshot = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-2",
        revision=2,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a2", resource_kind="file"),),
        created_at="2026-06-24T00:05:00Z",
        metadata={"phase": "candidate"},
    )

    commit = WorkspaceCommit(
        "commit-1",
        "workspace-1",
        "snapshot-1",
        snapshot,
        PrincipalRef("author-1"),
        "2026-06-24T00:05:00Z",
        "change-1",
    )
    snapshot.metadata["phase"] = "mutated"
    commit.snapshot.metadata["phase"] = "returned-mutated"

    assert commit.snapshot.metadata == {"phase": "returned-mutated"}
    assert snapshot.metadata == {"phase": "mutated"}
    with pytest.raises(ValueError, match="workspace commit commit_id must not be empty"):
        WorkspaceCommit(
            " ",
            "workspace-1",
            "snapshot-1",
            snapshot,
            PrincipalRef("author-1"),
            "2026-06-24T00:05:00Z",
            "change-1",
        )
    with pytest.raises(ValueError, match="workspace commit snapshot workspace_id must match workspace_id"):
        WorkspaceCommit(
            "commit-1",
            "workspace-2",
            "snapshot-1",
            snapshot,
            PrincipalRef("author-1"),
            "2026-06-24T00:05:00Z",
            "change-1",
        )
    with pytest.raises(ValueError, match="workspace commit committed_by must be a PrincipalRef"):
        WorkspaceCommit(
            "commit-1",
            "workspace-1",
            "snapshot-1",
            snapshot,
            object(),  # type: ignore[arg-type]
            "2026-06-24T00:05:00Z",
            "change-1",
        )


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


def test_workspace_mutation_policy_protects_declared_read_only_inputs() -> None:
    policy = WorkspaceMutationPolicy(
        policy_id="policy-1",
        allowed_resource_kinds=("file", "source", "test_oracle"),
        read_only_resource_ids=("rtl/top.sv",),
        read_only_resource_kinds=("test_oracle",),
    )
    principal = PrincipalRef("optimizer-1")
    change_set = ChangeSet(
        change_set_id="change-1",
        base=ResourceSnapshotRef("workspace", "sha256:base", resource_kind="workspace"),
        candidate=ResourceSnapshotRef("workspace", "sha256:candidate", resource_kind="workspace"),
        operations=[
            {"op": "file.read", "resource_id": "rtl/top.sv", "resource_kind": "source"},
            {"op": "file.write", "resource_id": "rtl/top.sv", "resource_kind": "source"},
            {"op": "file.write", "resource_id": "golden.json", "resource_kind": "test_oracle"},
        ],
    )

    decision = policy.evaluate(change_set, principal)

    assert not decision.allowed
    assert decision.reason_codes == (
        "workspace.read_only_resource_changed",
        "workspace.read_only_resource_kind_changed",
    )


def test_workspace_store_rejects_protected_snapshot_digest_change_without_operation_log() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(
            ResourceSnapshotRef("rtl/top.sv", "sha256:source-v1", resource_kind="source"),
            ResourceSnapshotRef("candidate/out.sv", "sha256:candidate-v1", resource_kind="file"),
        ),
        created_at="2026-06-24T00:00:00Z",
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)
    policy = WorkspaceMutationPolicy(
        policy_id="policy-1",
        allowed_resource_kinds=("file", "source"),
        read_only_resource_kinds=("source",),
    )

    with pytest.raises(WorkspaceMutationDeniedError) as error:
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-1",
            new_snapshot_id="snapshot-2",
            resources=(
                ResourceSnapshotRef("rtl/top.sv", "sha256:source-v2", resource_kind="source"),
                ResourceSnapshotRef("candidate/out.sv", "sha256:candidate-v2", resource_kind="file"),
            ),
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-06-24T00:05:00Z",
            change_set_id="change-1",
            policy=policy,
            operations=[],
        )

    assert error.value.reason_codes == ("workspace.read_only_resource_changed",)
    assert store.current("workspace-1").snapshot_id == "snapshot-1"


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


def test_workspace_store_rejects_duplicate_resource_ids_in_commit_candidate() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),),
        created_at="2026-06-24T00:00:00Z",
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)

    with pytest.raises(ValueError, match="workspace snapshot resource_id values must be unique"):
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-1",
            new_snapshot_id="snapshot-2",
            resources=(
                ResourceSnapshotRef("a.txt", "sha256:a2", resource_kind="file"),
                ResourceSnapshotRef("a.txt", "sha256:a3", resource_kind="file"),
            ),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:06:00Z",
            change_set_id="change-1",
        )

    assert store.current("workspace-1").snapshot_id == "snapshot-1"


def test_workspace_store_copies_snapshots_and_resource_metadata_at_boundaries() -> None:
    snapshot_metadata = {"phase": "initial"}
    resource_metadata = {"path": "original"}
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file", metadata=resource_metadata),),
        created_at="2026-06-24T00:00:00Z",
        metadata=snapshot_metadata,
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)
    snapshot_metadata["phase"] = "mutated"
    resource_metadata["path"] = "mutated"

    current = store.current("workspace-1")

    assert current.metadata == {"phase": "initial"}
    assert current.resources[0].metadata == {"path": "original"}
    current.metadata["phase"] = "returned-mutated"
    current.resources[0].metadata["path"] = "returned-mutated"

    fresh = store.current("workspace-1")
    assert fresh.metadata == {"phase": "initial"}
    assert fresh.resources[0].metadata == {"path": "original"}

    committed = store.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(ResourceSnapshotRef("b.txt", "sha256:b", resource_kind="file", metadata={"path": "committed"}),),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
    )
    committed.snapshot.metadata["phase"] = "commit-mutated"
    committed.snapshot.resources[0].metadata["path"] = "commit-mutated"

    latest = store.current("workspace-1")
    assert latest.metadata == {"phase": "initial"}
    assert latest.resources[0].metadata == {"path": "committed"}


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
