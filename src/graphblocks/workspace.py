from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .canonical import canonical_hash
from .evaluation import ChangeSet, ResourceSnapshotRef
from .policy import PrincipalRef


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    return value


def _validate_optional_non_empty_string(owner: str, field_name: str, value: object | None) -> str | None:
    if value is None:
        return None
    return _validate_non_empty_string(owner, field_name, value)


def _validate_string_tuple(owner: str, field_name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a collection of strings")
    try:
        items = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{owner} {field_name} must be a collection of strings") from error
    for item in items:
        _validate_non_empty_string(owner, f"{field_name} item", item)
    return tuple(sorted(set(items)))


def _copy_resource_snapshot_ref(resource: ResourceSnapshotRef) -> ResourceSnapshotRef:
    if not isinstance(resource, ResourceSnapshotRef):
        raise ValueError("workspace snapshot resources items must be ResourceSnapshotRef")
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
        _validate_non_empty_string("workspace snapshot", "workspace_id", self.workspace_id)
        _validate_non_empty_string("workspace snapshot", "snapshot_id", self.snapshot_id)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("workspace snapshot revision must be an integer")
        if self.revision <= 0:
            raise ValueError("workspace snapshot revision must be positive")
        if not isinstance(self.created_at, str):
            raise ValueError("workspace snapshot created_at must be a string")
        _validate_optional_non_empty_string("workspace snapshot", "base_snapshot_id", self.base_snapshot_id)
        _validate_optional_non_empty_string("workspace snapshot", "base_snapshot_digest", self.base_snapshot_digest)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("workspace snapshot metadata must be a mapping")
        metadata = dict(self.metadata)
        for key in metadata:
            if not isinstance(key, str):
                raise ValueError("workspace snapshot metadata keys must be strings")
            if not key.strip():
                raise ValueError("workspace snapshot metadata key must not be empty")
        resources = tuple(
            sorted(
                (_copy_resource_snapshot_ref(resource) for resource in self.resources),
                key=lambda resource: resource.resource_id,
            )
        )
        resource_ids = [resource.resource_id for resource in resources]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("workspace snapshot resource_id values must be unique")
        object.__setattr__(
            self,
            "resources",
            resources,
        )
        object.__setattr__(self, "metadata", metadata)

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
        if not isinstance(self.allowed, bool):
            raise ValueError("workspace mutation decision allowed must be a boolean")
        object.__setattr__(
            self,
            "reason_codes",
            _validate_string_tuple("workspace mutation decision", "reason_codes", self.reason_codes),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMutationPolicy:
    policy_id: str
    allowed_resource_kinds: tuple[str, ...]
    denied_operations: tuple[str, ...] = field(default_factory=tuple)
    required_review_scopes: tuple[str, ...] = field(default_factory=tuple)
    read_only_resource_ids: tuple[str, ...] = field(default_factory=tuple)
    read_only_resource_kinds: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_non_empty_string("workspace mutation policy", "policy_id", self.policy_id)
        object.__setattr__(
            self,
            "allowed_resource_kinds",
            _validate_string_tuple("workspace mutation policy", "allowed_resource_kinds", self.allowed_resource_kinds),
        )
        object.__setattr__(
            self,
            "denied_operations",
            _validate_string_tuple("workspace mutation policy", "denied_operations", self.denied_operations),
        )
        object.__setattr__(
            self,
            "required_review_scopes",
            _validate_string_tuple("workspace mutation policy", "required_review_scopes", self.required_review_scopes),
        )
        object.__setattr__(
            self,
            "read_only_resource_ids",
            _validate_string_tuple("workspace mutation policy", "read_only_resource_ids", self.read_only_resource_ids),
        )
        object.__setattr__(
            self,
            "read_only_resource_kinds",
            _validate_string_tuple("workspace mutation policy", "read_only_resource_kinds", self.read_only_resource_kinds),
        )

    def evaluate(
        self,
        change_set: ChangeSet,
        principal: PrincipalRef,
        *,
        review_scopes: tuple[str, ...] = (),
        base_resources: tuple[ResourceSnapshotRef, ...] = (),
        candidate_resources: tuple[ResourceSnapshotRef, ...] = (),
    ) -> WorkspaceMutationDecision:
        del principal
        reasons: list[str] = []
        review_scope_set = set(review_scopes)
        read_only_resource_ids = set(self.read_only_resource_ids)
        read_only_resource_kinds = set(self.read_only_resource_kinds)
        read_only_operations = {"check", "diff", "inspect", "list", "read", "stat", "validate"}
        if not set(self.required_review_scopes).issubset(review_scope_set):
            reasons.append("workspace.review_required")
        for operation in change_set.operations:
            operation_name = operation.get("op")
            resource_kind = operation.get("resource_kind")
            resource_id = operation.get("resource_id")
            if isinstance(operation_name, str) and operation_name in self.denied_operations:
                reasons.append("workspace.operation_denied")
            if isinstance(resource_kind, str) and resource_kind not in self.allowed_resource_kinds:
                reasons.append("workspace.resource_kind_denied")
            operation_action = operation_name.rsplit(".", 1)[-1].lower() if isinstance(operation_name, str) else ""
            if operation_action not in read_only_operations:
                if isinstance(resource_id, str) and resource_id in read_only_resource_ids:
                    reasons.append("workspace.read_only_resource_changed")
                if isinstance(resource_kind, str) and resource_kind in read_only_resource_kinds:
                    reasons.append("workspace.read_only_resource_kind_changed")
        if base_resources or candidate_resources:
            candidate_by_resource_id = {resource.resource_id: resource for resource in candidate_resources}
            for resource in base_resources:
                is_read_only_resource = resource.resource_id in read_only_resource_ids or (
                    resource.resource_kind is not None and resource.resource_kind in read_only_resource_kinds
                )
                if not is_read_only_resource:
                    continue
                candidate = candidate_by_resource_id.get(resource.resource_id)
                if (
                    candidate is None
                    or candidate.digest != resource.digest
                    or candidate.resource_kind != resource.resource_kind
                    or candidate.uri != resource.uri
                    or dict(candidate.metadata) != dict(resource.metadata)
                ):
                    reasons.append("workspace.read_only_resource_changed")
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

    def __post_init__(self) -> None:
        for field_name in ("commit_id", "workspace_id", "previous_snapshot_id", "committed_at", "change_set_id"):
            _validate_non_empty_string("workspace commit", field_name, getattr(self, field_name))
        if not isinstance(self.snapshot, WorkspaceSnapshot):
            raise ValueError("workspace commit snapshot must be a WorkspaceSnapshot")
        if self.snapshot.workspace_id != self.workspace_id:
            raise ValueError("workspace commit snapshot workspace_id must match workspace_id")
        if not isinstance(self.committed_by, PrincipalRef):
            raise ValueError("workspace commit committed_by must be a PrincipalRef")
        object.__setattr__(self, "snapshot", _copy_workspace_snapshot(self.snapshot))


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
                base_resources=current.resources,
                candidate_resources=candidate.resources,
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
