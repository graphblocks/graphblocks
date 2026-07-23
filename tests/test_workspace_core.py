from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
import time

import pytest

import graphblocks.workspace as workspace_module
from graphblocks.budget import BudgetPermit, UsageAmount
from graphblocks.evaluation import (
    ChangeSet,
    CheckResult,
    ResourceSnapshotRef,
    ReviewRecord,
    evaluate_gate,
)
from graphblocks.orchestration import LeasePool, LeaseRequest
from graphblocks.policy import PrincipalRef, ResourceRef
from graphblocks.workspace import (
    InMemoryWorkspaceStore,
    WorkspaceCommit,
    WorkspaceCommitAuthorizationError,
    WorkspaceError,
    WorkspaceMutationDecision,
    WorkspaceMutationDeniedError,
    WorkspaceMutationPolicy,
    WorkspaceSnapshot,
    WorkspaceSnapshotConflictError,
    WorkspaceTrialError,
    WorkspaceTrialPlan,
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
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    with pytest.raises(ValueError, match="metadata must contain canonical JSON values"):
        WorkspaceSnapshot(
            "workspace-1",
            "snapshot-1",
            1,
            metadata=recursive,
        )
    with pytest.raises(
        ValueError,
        match="base_snapshot_id and base_snapshot_digest must be provided together",
    ):
        WorkspaceSnapshot(
            "workspace-1",
            "snapshot-1",
            1,
            base_snapshot_id="snapshot-0",
        )


@pytest.mark.parametrize(
    ("factory", "expected_error"),
    (
        (
            lambda: WorkspaceSnapshot(" workspace-1", "snapshot-1", 1),
            "workspace snapshot workspace_id must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceSnapshot("workspace-1", " snapshot-1", 1),
            "workspace snapshot snapshot_id must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceSnapshot("workspace-1", "snapshot-1", 1, base_snapshot_id=" snapshot-0"),
            "workspace snapshot base_snapshot_id must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceSnapshot("workspace-1", "snapshot-1", 1, base_snapshot_digest=" sha256:base"),
            "workspace snapshot base_snapshot_digest must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceSnapshot("workspace-1", "snapshot-1", 1, metadata={" phase": "candidate"}),
            "workspace snapshot metadata key must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationDecision(False, (" workspace.denied",)),
            "workspace mutation decision reason_codes item must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy(" policy-1", ("file",)),
            "workspace mutation policy policy_id must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy("policy-1", (" file",)),
            "workspace mutation policy allowed_resource_kinds item must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy("policy-1", ("file",), denied_operations=(" process.exec",)),
            "workspace mutation policy denied_operations item must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy("policy-1", ("file",), required_review_scopes=(" quality",)),
            "workspace mutation policy required_review_scopes item must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy("policy-1", ("file",), read_only_resource_ids=(" rtl/top.sv",)),
            "workspace mutation policy read_only_resource_ids item must not contain surrounding whitespace",
        ),
        (
            lambda: WorkspaceMutationPolicy("policy-1", ("file",), read_only_resource_kinds=(" source",)),
            "workspace mutation policy read_only_resource_kinds item must not contain surrounding whitespace",
        ),
    ),
)
def test_workspace_records_reject_whitespace_wrapped_identities(factory, expected_error: str) -> None:
    with pytest.raises(ValueError, match=expected_error):
        factory()


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
        base_snapshot_id="snapshot-1",
        base_snapshot_digest="sha256:base",
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
    with pytest.raises(
        ValueError,
        match="snapshot must reference previous_snapshot_id",
    ):
        WorkspaceCommit(
            "commit-1",
            "workspace-1",
            "snapshot-other",
            snapshot,
            PrincipalRef("author-1"),
            "2026-06-24T00:05:00Z",
            "change-1",
        )


@pytest.mark.parametrize(
    ("field_name", "expected_error"),
    (
        ("commit_id", "workspace commit commit_id must not contain surrounding whitespace"),
        ("workspace_id", "workspace commit workspace_id must not contain surrounding whitespace"),
        ("previous_snapshot_id", "workspace commit previous_snapshot_id must not contain surrounding whitespace"),
        ("committed_at", "workspace commit committed_at must not contain surrounding whitespace"),
        ("change_set_id", "workspace commit change_set_id must not contain surrounding whitespace"),
    ),
)
def test_workspace_commit_rejects_whitespace_wrapped_identities(field_name: str, expected_error: str) -> None:
    snapshot = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-2",
        revision=2,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a2", resource_kind="file"),),
        created_at="2026-06-24T00:05:00Z",
        base_snapshot_id="snapshot-1",
        base_snapshot_digest="sha256:base",
    )
    values = {
        "commit_id": "commit-1",
        "workspace_id": "workspace-1",
        "previous_snapshot_id": "snapshot-1",
        "snapshot": snapshot,
        "committed_by": PrincipalRef("author-1"),
        "committed_at": "2026-06-24T00:05:00Z",
        "change_set_id": "change-1",
    }
    values[field_name] = f" {values[field_name]}"

    with pytest.raises(ValueError, match=expected_error):
        WorkspaceCommit(**values)


def test_workspace_mutation_decision_validates_boolean_and_reason_codes() -> None:
    decision = WorkspaceMutationDecision(True, ("workspace.b", "workspace.a", "workspace.a"))

    assert decision.allowed is True
    assert decision.reason_codes == ("workspace.a", "workspace.b")
    with pytest.raises(ValueError, match="workspace mutation decision allowed must be a boolean"):
        WorkspaceMutationDecision("yes", ())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="workspace mutation decision reason_codes item must not be empty"):
        WorkspaceMutationDecision(False, (" ",))


def test_workspace_mutation_policy_validates_identity_and_string_collections() -> None:
    policy = WorkspaceMutationPolicy(
        policy_id="policy-1",
        allowed_resource_kinds=("file", "source", "file"),
        denied_operations=("process.exec",),
        required_review_scopes=("quality",),
        read_only_resource_ids=("rtl/top.sv",),
        read_only_resource_kinds=("test_oracle", "source"),
    )

    assert policy.allowed_resource_kinds == ("file", "source")
    assert policy.read_only_resource_kinds == ("source", "test_oracle")
    with pytest.raises(ValueError, match="workspace mutation policy policy_id must not be empty"):
        WorkspaceMutationPolicy(" ", ("file",))
    with pytest.raises(ValueError, match="workspace mutation policy allowed_resource_kinds must be a collection"):
        WorkspaceMutationPolicy("policy-1", "file")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="workspace mutation policy denied_operations item must be a string"):
        WorkspaceMutationPolicy("policy-1", ("file",), denied_operations=(object(),))  # type: ignore[arg-type]


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


def test_workspace_mutation_policy_rejects_malformed_evaluation_context() -> None:
    policy = WorkspaceMutationPolicy("policy-1", ("file",))
    subject = ResourceSnapshotRef("workspace", "sha256:base", resource_kind="workspace")
    change_set = ChangeSet("change-1", subject, subject)

    with pytest.raises(ValueError, match="change_set must be a ChangeSet"):
        policy.evaluate(object(), PrincipalRef("author-1"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="principal must be a PrincipalRef"):
        policy.evaluate(change_set, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="review_scopes must be a collection"):
        policy.evaluate(
            change_set,
            PrincipalRef("author-1"),
            review_scopes="quality",  # type: ignore[arg-type]
        )
    duplicate_resource = ResourceSnapshotRef(
        "file.txt",
        "sha256:file",
        resource_kind="file",
    )
    with pytest.raises(ValueError, match="unique resource_id"):
        policy.evaluate(
            change_set,
            PrincipalRef("author-1"),
            base_resources=(duplicate_resource, duplicate_resource),
        )


@pytest.mark.parametrize(
    ("operation", "reason_code"),
    [
        ({"resource_kind": "file"}, "workspace.operation_denied"),
        ({"op": 7, "resource_kind": "file"}, "workspace.operation_denied"),
        ({"op": "file.write"}, "workspace.resource_kind_denied"),
        ({"op": "file.write", "resource_kind": 7}, "workspace.resource_kind_denied"),
    ],
)
def test_workspace_mutation_policy_fails_closed_for_malformed_operations(
    operation: dict[str, object],
    reason_code: str,
) -> None:
    policy = WorkspaceMutationPolicy("policy-1", ("file",))
    subject = ResourceSnapshotRef("workspace", "sha256:base", resource_kind="workspace")
    decision = policy.evaluate(
        ChangeSet("change-1", subject, subject, operations=[operation]),
        PrincipalRef("author-1"),
    )

    assert not decision.allowed
    assert reason_code in decision.reason_codes


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


def test_workspace_revision_exhaustion_does_not_mutate_head() -> None:
    maximum = (1 << 64) - 1
    base = WorkspaceSnapshot(
        "workspace-1",
        "snapshot-maximum",
        maximum,
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)

    with pytest.raises(OverflowError, match="workspace revision exhausted"):
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-maximum",
            new_snapshot_id="snapshot-overflow",
            resources=(),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:05:00Z",
            change_set_id="change-1",
        )

    assert store.current("workspace-1") == base
    with pytest.raises(
        ValueError,
        match="workspace snapshot revision exceeds storage range",
    ):
        WorkspaceSnapshot(
            "workspace-invalid",
            "snapshot-invalid",
            maximum + 1,
        )


def test_workspace_store_rejects_reused_snapshot_identity() -> None:
    store = InMemoryWorkspaceStore().put_snapshot(
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1)
    )
    store.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
    )

    with pytest.raises(WorkspaceError, match="snapshot identity 'snapshot-1' has already been used"):
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-2",
            new_snapshot_id="snapshot-1",
            resources=(),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:06:00Z",
            change_set_id="change-2",
        )

    assert store.current("workspace-1").snapshot_id == "snapshot-2"
    assert store.current("workspace-1").revision == 2


def test_workspace_store_rejects_snapshot_head_overwrite() -> None:
    original = WorkspaceSnapshot("workspace-1", "snapshot-1", 1)
    store = InMemoryWorkspaceStore().put_snapshot(original)

    assert store.put_snapshot(original) is store
    with pytest.raises(WorkspaceError, match="workspace 'workspace-1' is already initialized"):
        store.put_snapshot(WorkspaceSnapshot("workspace-1", "snapshot-2", 2))

    assert store.current("workspace-1") == original


def test_workspace_store_rejects_reused_commit_identity() -> None:
    store = InMemoryWorkspaceStore().put_snapshot(
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1)
    )
    store.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
        commit_id="commit-1",
    )

    with pytest.raises(WorkspaceError, match="commit identity 'commit-1' has already been used"):
        store.compare_and_swap_commit(
            workspace_id="workspace-1",
            expected_snapshot_id="snapshot-2",
            new_snapshot_id="snapshot-3",
            resources=(),
            committed_by=PrincipalRef("author-1"),
            committed_at="2026-06-24T00:06:00Z",
            change_set_id="change-2",
            commit_id="commit-1",
        )

    assert store.current("workspace-1").snapshot_id == "snapshot-2"
    assert store.current("workspace-1").revision == 2


def test_workspace_store_compare_and_swap_commit_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(ResourceSnapshotRef("a.txt", "sha256:a", resource_kind="file"),),
        created_at="2026-06-24T00:00:00Z",
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)
    original_copy = workspace_module._copy_workspace_snapshot

    def delayed_base_copy(snapshot: WorkspaceSnapshot) -> WorkspaceSnapshot:
        if snapshot.snapshot_id == "snapshot-1":
            time.sleep(0.02)
        return original_copy(snapshot)

    monkeypatch.setattr(workspace_module, "_copy_workspace_snapshot", delayed_base_copy)
    start = Barrier(2)

    def commit(snapshot_id: str) -> str:
        start.wait()
        try:
            store.compare_and_swap_commit(
                workspace_id="workspace-1",
                expected_snapshot_id="snapshot-1",
                new_snapshot_id=snapshot_id,
                resources=(ResourceSnapshotRef("a.txt", f"sha256:{snapshot_id}", resource_kind="file"),),
                committed_by=PrincipalRef("author-1"),
                committed_at="2026-06-24T00:05:00Z",
                change_set_id=f"change-{snapshot_id}",
            )
        except WorkspaceSnapshotConflictError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(commit, ("snapshot-a", "snapshot-b")))

    assert sorted(outcomes) == ["committed", "conflict"]
    assert len(store._commits) == 1
    assert store.current("workspace-1").revision == 2


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


def test_workspace_store_deep_copies_nested_metadata_at_boundaries() -> None:
    snapshot_metadata = {"labels": {"groups": ["trusted"]}}
    resource_metadata = {"attributes": {"owners": ["author-1"]}}
    base = WorkspaceSnapshot(
        workspace_id="workspace-1",
        snapshot_id="snapshot-1",
        revision=1,
        resources=(
            ResourceSnapshotRef(
                "a.txt",
                "sha256:a",
                resource_kind="file",
                metadata=resource_metadata,
            ),
        ),
        created_at="2026-06-24T00:00:00Z",
        metadata=snapshot_metadata,
    )
    store = InMemoryWorkspaceStore().put_snapshot(base)
    snapshot_metadata["labels"]["groups"].append("caller-mutated")
    resource_metadata["attributes"]["owners"].append("caller-mutated")

    returned = store.current("workspace-1")
    returned.metadata["labels"]["groups"].append("consumer-mutated")
    returned.resources[0].metadata["attributes"]["owners"].append(
        "consumer-mutated"
    )

    fresh = store.current("workspace-1")
    assert fresh.metadata == {"labels": {"groups": ["trusted"]}}
    assert fresh.resources[0].metadata == {
        "attributes": {"owners": ["author-1"]}
    }


def test_workspace_store_validates_and_detaches_restored_state() -> None:
    source = InMemoryWorkspaceStore().put_snapshot(
        WorkspaceSnapshot(
            "workspace-1",
            "snapshot-1",
            1,
            metadata={"labels": {"groups": ["trusted"]}},
        )
    )
    source.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
    )
    restored_snapshots = {"workspace-1": source.current("workspace-1")}
    restored_commits = list(source._commits)

    restored = InMemoryWorkspaceStore(
        _snapshots=restored_snapshots,
        _commits=restored_commits,
    )
    restored_snapshots["workspace-1"].metadata["labels"]["groups"].append(
        "snapshot-caller"
    )
    restored_commits[0].snapshot.metadata["labels"]["groups"].append(
        "commit-caller"
    )

    assert restored.current("workspace-1").metadata == {
        "labels": {"groups": ["trusted"]}
    }
    assert restored._commits[0].snapshot.metadata == {
        "labels": {"groups": ["trusted"]}
    }


def test_workspace_store_rejects_invalid_restored_state_identities() -> None:
    source = InMemoryWorkspaceStore().put_snapshot(
        WorkspaceSnapshot("workspace-1", "snapshot-1", 1)
    )
    source.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-1",
        new_snapshot_id="snapshot-2",
        resources=(),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:05:00Z",
        change_set_id="change-1",
        commit_id="commit-1",
    )
    source.compare_and_swap_commit(
        workspace_id="workspace-1",
        expected_snapshot_id="snapshot-2",
        new_snapshot_id="snapshot-3",
        resources=(),
        committed_by=PrincipalRef("author-1"),
        committed_at="2026-06-24T00:06:00Z",
        change_set_id="change-2",
        commit_id="commit-2",
    )
    head = source.current("workspace-1")
    commits = tuple(source._commits)

    with pytest.raises(ValueError, match="key must match snapshot workspace_id"):
        InMemoryWorkspaceStore(_snapshots={"wrong-workspace": head})
    with pytest.raises(ValueError, match="commit_id values must be unique"):
        InMemoryWorkspaceStore(
            _snapshots={"workspace-1": head},
            _commits=(commits[0], replace(commits[1], commit_id="commit-1")),
        )
    broken_snapshot = replace(
        commits[1].snapshot,
        base_snapshot_id="snapshot-unrelated",
    )
    broken_commit = replace(
        commits[1],
        previous_snapshot_id="snapshot-unrelated",
        snapshot=broken_snapshot,
    )
    with pytest.raises(ValueError, match="chain snapshot identities must be linked"):
        InMemoryWorkspaceStore(
            _snapshots={"workspace-1": broken_snapshot},
            _commits=(commits[0], broken_commit),
        )
    with pytest.raises(ValueError, match="restored head must match"):
        InMemoryWorkspaceStore(
            _snapshots={"workspace-1": commits[0].snapshot},
            _commits=commits,
        )


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


def test_workspace_trial_plan_materializes_and_enforces_verified_commit_request() -> None:
    base = WorkspaceSnapshot(
        workspace_id="workspace-rtl",
        snapshot_id="snapshot-base",
        revision=7,
        resources=(ResourceSnapshotRef("design.v", "sha256:base-design", resource_kind="file"),),
        created_at="2026-07-02T00:00:00Z",
    )
    candidate_resources = (
        ResourceSnapshotRef("design.v", "sha256:candidate-design", resource_kind="file"),
    )
    candidate = WorkspaceSnapshot(
        workspace_id="workspace-rtl",
        snapshot_id="snapshot-candidate",
        revision=8,
        resources=candidate_resources,
        created_at="2026-07-02T00:30:00Z",
        base_snapshot_id=base.snapshot_id,
        base_snapshot_digest=base.content_digest(),
    )
    change_set = ChangeSet(
        "changeset-rtl-1",
        base=ResourceSnapshotRef("workspace-rtl", base.content_digest(), resource_kind="workspace"),
        candidate=ResourceSnapshotRef(
            "workspace-rtl",
            candidate.content_digest(),
            resource_kind="workspace",
        ),
        operations=({"op": "file.replace", "resource_id": "design.v", "resource_kind": "file"},),
    )
    checks = (
        CheckResult("lint", change_set.candidate, "passed"),
        CheckResult("compile", change_set.candidate, "passed"),
        CheckResult("regression", change_set.candidate, "passed"),
        CheckResult("formal", change_set.candidate, "passed"),
    )
    gate = evaluate_gate(
        "rtl-quality",
        change_set.candidate,
        checks=list(checks),
        required_check_ids=["lint", "compile", "regression", "formal"],
    )
    holder = ResourceSnapshotRef("workspace-rtl", candidate.content_digest(), resource_kind="workspace")
    permit_owner = ResourceRef("trial:rtl-1")
    permit = BudgetPermit(
        permit_id="permit-formal",
        reservation_refs=("reservation-formal",),
        owner=permit_owner,
        atomic_unit=permit_owner,
        admission_epoch=1,
        authorized_amounts=[UsageAmount("licensed_resource_seconds", Decimal("900"), "second")],
        continuation_profile="default",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-07-02T00:40:00Z",
        fencing_tokens={"reservation-formal": 1},
    )
    _, lease = LeasePool("formal-license", "eda.formal", 1).acquire_with_budget_permit(
        LeaseRequest("formal-check", permit_owner, "eda.formal"),
        permit,
        [UsageAmount("licensed_resource_seconds", Decimal("300"), "second")],
        lease_id="lease-formal",
        acquired_at="2026-07-02T00:10:00Z",
        expires_at="2026-07-02T00:35:00Z",
    )
    review = ReviewRecord(
        "review-rtl-1",
        change_set.candidate,
        change_set.candidate.digest,
        "design_intent",
        PrincipalRef("reviewer-1"),
        "accept",
        created_at="2026-07-02T00:20:00Z",
    )
    plan = WorkspaceTrialPlan(
        trial_id="rtl-1",
        change_set=change_set,
        expected_base_revision=7,
        required_check_ids=("lint", "compile", "regression", "formal"),
        required_lease_kinds=("eda.formal",),
        required_review_scopes=("design_intent",),
        checks=checks,
        gate=gate,
        mutation_decision=WorkspaceMutationDecision(True),
        leases=(lease,),
        reviews=(review,),
    )

    request = plan.to_commit_request("commit-rtl-1", now="2026-07-02T00:25:00Z")
    committed = InMemoryWorkspaceStore().put_snapshot(base).compare_and_swap_commit_request(
        workspace_id="workspace-rtl",
        request=request,
        new_snapshot_id="snapshot-candidate",
        resources=candidate_resources,
        committed_by=PrincipalRef("optimizer-1"),
        committed_at="2026-07-02T00:30:00Z",
    )

    assert holder.digest == change_set.candidate.digest
    assert request.metadata == {
        "change_set_digest": change_set.content_digest(),
        "lease_ids": ["lease-formal"],
        "trial_id": "rtl-1",
    }
    assert committed.commit_id == request.commit_id == "commit-rtl-1"
    assert committed.snapshot.content_digest() == change_set.candidate.digest
    assert committed.snapshot.revision == 8

    with pytest.raises(WorkspaceCommitAuthorizationError, match="active lease kind 'eda.formal'"):
        InMemoryWorkspaceStore().put_snapshot(base).compare_and_swap_commit_request(
            workspace_id="workspace-rtl",
            request=request,
            new_snapshot_id="snapshot-candidate",
            resources=candidate_resources,
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-07-02T00:35:00Z",
        )

    with pytest.raises(WorkspaceCommitAuthorizationError, match="required review scope 'design_intent'"):
        InMemoryWorkspaceStore().put_snapshot(base).compare_and_swap_commit_request(
            workspace_id="workspace-rtl",
            request=replace(request, reviews=()),
            new_snapshot_id="snapshot-candidate",
            resources=candidate_resources,
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-07-02T00:30:00Z",
        )

    with pytest.raises(WorkspaceCommitAuthorizationError, match="required gate check 'formal'"):
        InMemoryWorkspaceStore().put_snapshot(base).compare_and_swap_commit_request(
            workspace_id="workspace-rtl",
            request=replace(
                request,
                gate=replace(
                    request.gate,
                    check_ids=[
                        check_id
                        for check_id in request.gate.check_ids
                        if check_id != "formal"
                    ],
                ),
            ),
            new_snapshot_id="snapshot-candidate",
            resources=candidate_resources,
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-07-02T00:30:00Z",
        )


def test_workspace_trial_binds_complete_candidate_evidence() -> None:
    base = WorkspaceSnapshot(
        "workspace-1",
        "snapshot-1",
        1,
        created_at="2026-07-02T00:00:00Z",
    )
    candidate = WorkspaceSnapshot(
        "workspace-1",
        "snapshot-2",
        2,
        created_at="2026-07-02T00:05:00Z",
        base_snapshot_id=base.snapshot_id,
        base_snapshot_digest=base.content_digest(),
    )
    canonical_candidate = ResourceSnapshotRef(
        "workspace-1",
        candidate.content_digest(),
        resource_kind="workspace",
        metadata={"source": "change-set"},
    )
    evidence_subject = ResourceSnapshotRef(
        "workspace-1",
        candidate.content_digest(),
        uri="file:///tmp/workspace-1",
        metadata={"source": "check"},
    )
    change_set = ChangeSet(
        "change-1",
        ResourceSnapshotRef("workspace-1", base.content_digest()),
        canonical_candidate,
    )
    check = CheckResult("lint", evidence_subject, "passed")
    gate = evaluate_gate(
        "quality",
        canonical_candidate,
        checks=[check],
        required_check_ids=["lint"],
    )

    with pytest.raises(
        WorkspaceTrialError,
        match="check 'lint' has a stale subject",
    ):
        WorkspaceTrialPlan(
            "trial-1",
            change_set,
            1,
            required_check_ids=("lint",),
            checks=(check,),
            gate=gate,
            mutation_decision=WorkspaceMutationDecision(True),
        ).to_commit_request(
            "commit-1",
            now="2026-07-02T00:04:00Z",
        )

    canonical_check = CheckResult(
        "lint",
        canonical_candidate,
        "passed",
    )
    stale_gate = evaluate_gate(
        "quality",
        ResourceSnapshotRef(
            "workspace-1",
            candidate.content_digest(),
            metadata={"source": "gate"},
        ),
        checks=[canonical_check],
        required_check_ids=["lint"],
    )
    with pytest.raises(
        WorkspaceTrialError,
        match="gate has a stale subject",
    ):
        WorkspaceTrialPlan(
            "trial-1",
            change_set,
            1,
            required_check_ids=("lint",),
            checks=(canonical_check,),
            gate=stale_gate,
            mutation_decision=WorkspaceMutationDecision(True),
        ).to_commit_request(
            "commit-1",
            now="2026-07-02T00:04:00Z",
        )


def test_workspace_trial_plan_fails_closed_when_governance_evidence_is_missing_or_stale() -> None:
    base = ResourceSnapshotRef("workspace-rtl", "sha256:base", resource_kind="workspace")
    candidate = ResourceSnapshotRef("workspace-rtl", "sha256:candidate", resource_kind="workspace")
    change_set = ChangeSet("changeset-rtl-1", base=base, candidate=candidate)
    lint = CheckResult("lint", candidate, "passed")
    gate = evaluate_gate("rtl-quality", candidate, checks=[lint], required_check_ids=["lint"])

    with pytest.raises(WorkspaceTrialError, match="required mutation decision"):
        WorkspaceTrialPlan(
            trial_id="rtl-1",
            change_set=change_set,
            expected_base_revision=7,
            required_check_ids=("lint",),
            checks=(lint,),
            gate=gate,
        ).to_commit_request("commit-rtl-1", now="2026-07-02T00:25:00Z")

    stale_review = ReviewRecord(
        "review-stale",
        ResourceSnapshotRef("workspace-rtl", "sha256:old", resource_kind="workspace"),
        "sha256:old",
        "design_intent",
        PrincipalRef("reviewer-1"),
        "accept",
        created_at="2026-07-02T00:20:00Z",
    )
    with pytest.raises(WorkspaceTrialError, match="review scope 'design_intent'"):
        WorkspaceTrialPlan(
            trial_id="rtl-1",
            change_set=change_set,
            expected_base_revision=7,
            required_check_ids=("lint",),
            required_review_scopes=("design_intent",),
            checks=(lint,),
            gate=gate,
            mutation_decision=WorkspaceMutationDecision(True),
            reviews=(stale_review,),
        ).to_commit_request("commit-rtl-1", now="2026-07-02T00:25:00Z")

    store = InMemoryWorkspaceStore().put_snapshot(
        WorkspaceSnapshot("workspace-rtl", "snapshot-base", 6, created_at="2026-07-02T00:00:00Z")
    )
    request = WorkspaceTrialPlan(
        trial_id="rtl-1",
        change_set=change_set,
        expected_base_revision=7,
        required_check_ids=("lint",),
        checks=(lint,),
        gate=gate,
        mutation_decision=WorkspaceMutationDecision(True),
    ).to_commit_request("commit-rtl-1", now="2026-07-02T00:25:00Z")
    with pytest.raises(WorkspaceCommitAuthorizationError, match="base revision"):
        store.compare_and_swap_commit_request(
            workspace_id="workspace-rtl",
            request=request,
            new_snapshot_id="snapshot-candidate",
            resources=(),
            committed_by=PrincipalRef("optimizer-1"),
            committed_at="2026-07-02T00:30:00Z",
        )
