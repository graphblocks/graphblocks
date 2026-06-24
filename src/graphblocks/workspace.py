from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import canonical_hash
from .evaluation import ChangeSet, ResourceSnapshotRef
from .policy import PrincipalRef


def _copy_resource_snapshot_ref(resource: ResourceSnapshotRef) -> ResourceSnapshotRef:
    return ResourceSnapshotRef(
        resource_id=resource.resource_id,
        digest=resource.digest,
        resource_kind=resource.resource_kind,
        uri=resource.uri,
        metadata=dict(resource.metadata),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    workspace_id: str
    snapshot_id: str
    revision: int
    resources: tuple[ResourceSnapshotRef, ...] = field(default_factory=tuple)
    created_at: str = ""
    base_snapshot_id: str | None = None
    base_snapshot_digest: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resources",
            tuple(
                sorted(
                    (_copy_resource_snapshot_ref(resource) for resource in self.resources),
                    key=lambda resource: resource.resource_id,
                )
            ),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "workspace_id": self.workspace_id,
                "resources": [
                    {
                        "resource_id": resource.resource_id,
                        "digest": resource.digest,
                        "resource_kind": resource.resource_kind,
                        "uri": resource.uri,
                        "metadata": dict(resource.metadata),
                    }
                    for resource in self.resources
                ],
                "base_snapshot_id": self.base_snapshot_id,
                "base_snapshot_digest": self.base_snapshot_digest,
                "metadata": self.metadata,
            }
        )

    def fork(self, workspace_id: str, snapshot_id: str, created_at: str) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            workspace_id=workspace_id,
            snapshot_id=snapshot_id,
            revision=1,
            resources=self.resources,
            created_at=created_at,
            base_snapshot_id=self.snapshot_id,
            base_snapshot_digest=self.content_digest(),
            metadata=self.metadata,
        )


def _copy_workspace_snapshot(snapshot: WorkspaceSnapshot) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        workspace_id=snapshot.workspace_id,
        snapshot_id=snapshot.snapshot_id,
        revision=snapshot.revision,
        resources=tuple(_copy_resource_snapshot_ref(resource) for resource in snapshot.resources),
        created_at=snapshot.created_at,
        base_snapshot_id=snapshot.base_snapshot_id,
        base_snapshot_digest=snapshot.base_snapshot_digest,
        metadata=dict(snapshot.metadata),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceMutationDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))


@dataclass(frozen=True, slots=True)
class WorkspaceMutationPolicy:
    policy_id: str
    allowed_resource_kinds: tuple[str, ...]
    denied_operations: tuple[str, ...] = field(default_factory=tuple)
    required_review_scopes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_resource_kinds", tuple(sorted(set(self.allowed_resource_kinds))))
        object.__setattr__(self, "denied_operations", tuple(sorted(set(self.denied_operations))))
        object.__setattr__(self, "required_review_scopes", tuple(sorted(set(self.required_review_scopes))))

    def evaluate(
        self,
        change_set: ChangeSet,
        principal: PrincipalRef,
        *,
        review_scopes: tuple[str, ...] = (),
    ) -> WorkspaceMutationDecision:
        del principal
        reasons: list[str] = []
        review_scope_set = set(review_scopes)
        if not set(self.required_review_scopes).issubset(review_scope_set):
            reasons.append("workspace.review_required")
        for operation in change_set.operations:
            operation_name = operation.get("op")
            resource_kind = operation.get("resource_kind")
            if isinstance(operation_name, str) and operation_name in self.denied_operations:
                reasons.append("workspace.operation_denied")
            if isinstance(resource_kind, str) and resource_kind not in self.allowed_resource_kinds:
                reasons.append("workspace.resource_kind_denied")
        return WorkspaceMutationDecision(allowed=not reasons, reason_codes=tuple(reasons))


class WorkspaceError(ValueError):
    pass


class WorkspaceNotFoundError(WorkspaceError):
    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"workspace {workspace_id!r} does not exist")


class WorkspaceSnapshotConflictError(WorkspaceError):
    def __init__(self, expected_snapshot_id: str, actual_snapshot_id: str) -> None:
        self.expected_snapshot_id = expected_snapshot_id
        self.actual_snapshot_id = actual_snapshot_id
        super().__init__(
            f"workspace snapshot conflict: expected {expected_snapshot_id!r}, got {actual_snapshot_id!r}"
        )


class WorkspaceMutationDeniedError(WorkspaceError):
    def __init__(self, reason_codes: tuple[str, ...]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__(f"workspace mutation denied: {', '.join(self.reason_codes)}")


@dataclass(frozen=True, slots=True)
class WorkspaceCommit:
    commit_id: str
    workspace_id: str
    previous_snapshot_id: str
    snapshot: WorkspaceSnapshot
    committed_by: PrincipalRef
    committed_at: str
    change_set_id: str


def _copy_workspace_commit(commit: WorkspaceCommit) -> WorkspaceCommit:
    return WorkspaceCommit(
        commit_id=commit.commit_id,
        workspace_id=commit.workspace_id,
        previous_snapshot_id=commit.previous_snapshot_id,
        snapshot=_copy_workspace_snapshot(commit.snapshot),
        committed_by=commit.committed_by,
        committed_at=commit.committed_at,
        change_set_id=commit.change_set_id,
    )


@dataclass(slots=True)
class InMemoryWorkspaceStore:
    _snapshots: dict[str, WorkspaceSnapshot] = field(default_factory=dict)
    _commits: list[WorkspaceCommit] = field(default_factory=list)

    def put_snapshot(self, snapshot: WorkspaceSnapshot) -> InMemoryWorkspaceStore:
        self._snapshots[snapshot.workspace_id] = _copy_workspace_snapshot(snapshot)
        return self

    def current(self, workspace_id: str) -> WorkspaceSnapshot:
        snapshot = self._snapshots.get(workspace_id)
        if snapshot is None:
            raise WorkspaceNotFoundError(workspace_id)
        return _copy_workspace_snapshot(snapshot)

    def compare_and_swap_commit(
        self,
        *,
        workspace_id: str,
        expected_snapshot_id: str,
        new_snapshot_id: str,
        resources: tuple[ResourceSnapshotRef, ...],
        committed_by: PrincipalRef,
        committed_at: str,
        change_set_id: str,
        policy: WorkspaceMutationPolicy | None = None,
        review_scopes: tuple[str, ...] = (),
        operations: list[dict[str, object]] | None = None,
    ) -> WorkspaceCommit:
        current = self.current(workspace_id)
        if current.snapshot_id != expected_snapshot_id:
            raise WorkspaceSnapshotConflictError(expected_snapshot_id, current.snapshot_id)

        candidate = WorkspaceSnapshot(
            workspace_id=workspace_id,
            snapshot_id=new_snapshot_id,
            revision=current.revision + 1,
            resources=resources,
            created_at=committed_at,
            base_snapshot_id=current.snapshot_id,
            base_snapshot_digest=current.content_digest(),
            metadata=current.metadata,
        )
        if policy is not None:
            decision = policy.evaluate(
                ChangeSet(
                    change_set_id=change_set_id,
                    base=ResourceSnapshotRef(workspace_id, current.content_digest(), resource_kind="workspace"),
                    candidate=ResourceSnapshotRef(workspace_id, candidate.content_digest(), resource_kind="workspace"),
                    operations=list(operations or []),
                ),
                committed_by,
                review_scopes=review_scopes,
            )
            if not decision.allowed:
                raise WorkspaceMutationDeniedError(decision.reason_codes)

        commit = WorkspaceCommit(
            commit_id=f"{workspace_id}:{new_snapshot_id}",
            workspace_id=workspace_id,
            previous_snapshot_id=current.snapshot_id,
            snapshot=candidate,
            committed_by=committed_by,
            committed_at=committed_at,
            change_set_id=change_set_id,
        )
        self._snapshots[workspace_id] = _copy_workspace_snapshot(candidate)
        self._commits.append(_copy_workspace_commit(commit))
        return _copy_workspace_commit(commit)


__all__ = [
    "InMemoryWorkspaceStore",
    "WorkspaceCommit",
    "WorkspaceError",
    "WorkspaceMutationDecision",
    "WorkspaceMutationDeniedError",
    "WorkspaceMutationPolicy",
    "WorkspaceNotFoundError",
    "WorkspaceSnapshot",
    "WorkspaceSnapshotConflictError",
]
