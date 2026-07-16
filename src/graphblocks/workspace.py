from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import ParamSpec, TypeVar, cast

from .canonical import canonical_hash
from .evaluation import ChangeSet, CheckResult, GateResult, ResourceSnapshotRef, ReviewRecord
from .orchestration import LeaseGrant
from .policy import PrincipalRef


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _with_workspace_lock(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        store = cast("InMemoryWorkspaceStore", args[0])
        with store._lock:
            return method(*args, **kwargs)

    return locked


def _validate_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{owner} {field_name} must not contain surrounding whitespace")
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
            if key != key.strip():
                raise ValueError("workspace snapshot metadata key must not contain surrounding whitespace")
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


class WorkspaceTrialError(WorkspaceError):
    """Raised when trial evidence cannot authorize a workspace commit."""


class WorkspaceCommitAuthorizationError(WorkspaceError):
    """Raised when a commit request is stale or no longer matches the workspace head."""


@dataclass(frozen=True, slots=True)
class WorkspaceCommitRequest:
    commit_id: str
    change_set: ChangeSet
    expected_base_revision: int
    mutation_decision: WorkspaceMutationDecision
    gate: GateResult
    reviews: tuple[ReviewRecord, ...] = field(default_factory=tuple)
    trial_id: str | None = None
    required_check_ids: tuple[str, ...] = field(default_factory=tuple)
    required_lease_kinds: tuple[str, ...] = field(default_factory=tuple)
    required_review_scopes: tuple[str, ...] = field(default_factory=tuple)
    leases: tuple[LeaseGrant, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string("workspace commit request", "commit_id", self.commit_id)
        if not isinstance(self.change_set, ChangeSet):
            raise ValueError("workspace commit request change_set must be a ChangeSet")
        if (
            not isinstance(self.expected_base_revision, int)
            or isinstance(self.expected_base_revision, bool)
            or self.expected_base_revision <= 0
        ):
            raise ValueError("workspace commit request expected_base_revision must be positive")
        if not isinstance(self.mutation_decision, WorkspaceMutationDecision):
            raise ValueError("workspace commit request mutation_decision must be a WorkspaceMutationDecision")
        if not isinstance(self.gate, GateResult):
            raise ValueError("workspace commit request gate must be a GateResult")
        reviews = tuple(self.reviews)
        if not all(isinstance(review, ReviewRecord) for review in reviews):
            raise ValueError("workspace commit request reviews must contain ReviewRecord values")
        trial_id = _validate_optional_non_empty_string(
            "workspace commit request",
            "trial_id",
            self.trial_id,
        )
        required_lease_kinds = _validate_string_tuple(
            "workspace commit request",
            "required_lease_kinds",
            self.required_lease_kinds,
        )
        required_check_ids = _validate_string_tuple(
            "workspace commit request",
            "required_check_ids",
            self.required_check_ids,
        )
        required_review_scopes = _validate_string_tuple(
            "workspace commit request",
            "required_review_scopes",
            self.required_review_scopes,
        )
        leases = tuple(self.leases)
        if not all(isinstance(lease, LeaseGrant) for lease in leases):
            raise ValueError("workspace commit request leases must contain LeaseGrant values")
        if required_lease_kinds and trial_id is None:
            raise ValueError("workspace commit request trial_id is required for lease validation")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("workspace commit request metadata must be a mapping")
        object.__setattr__(self, "reviews", reviews)
        object.__setattr__(self, "trial_id", trial_id)
        object.__setattr__(self, "required_check_ids", required_check_ids)
        object.__setattr__(self, "required_lease_kinds", required_lease_kinds)
        object.__setattr__(self, "required_review_scopes", required_review_scopes)
        object.__setattr__(self, "leases", leases)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class WorkspaceTrialPlan:
    trial_id: str
    change_set: ChangeSet
    expected_base_revision: int
    required_check_ids: tuple[str, ...] = field(default_factory=tuple)
    required_lease_kinds: tuple[str, ...] = field(default_factory=tuple)
    required_review_scopes: tuple[str, ...] = field(default_factory=tuple)
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)
    gate: GateResult | None = None
    mutation_decision: WorkspaceMutationDecision | None = None
    leases: tuple[LeaseGrant, ...] = field(default_factory=tuple)
    reviews: tuple[ReviewRecord, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_non_empty_string("workspace trial plan", "trial_id", self.trial_id)
        if not isinstance(self.change_set, ChangeSet):
            raise ValueError("workspace trial plan change_set must be a ChangeSet")
        if (
            not isinstance(self.expected_base_revision, int)
            or isinstance(self.expected_base_revision, bool)
            or self.expected_base_revision <= 0
        ):
            raise ValueError("workspace trial plan expected_base_revision must be positive")
        object.__setattr__(
            self,
            "required_check_ids",
            _validate_string_tuple("workspace trial plan", "required_check_ids", self.required_check_ids),
        )
        object.__setattr__(
            self,
            "required_lease_kinds",
            _validate_string_tuple("workspace trial plan", "required_lease_kinds", self.required_lease_kinds),
        )
        object.__setattr__(
            self,
            "required_review_scopes",
            _validate_string_tuple("workspace trial plan", "required_review_scopes", self.required_review_scopes),
        )
        checks = tuple(self.checks)
        leases = tuple(self.leases)
        reviews = tuple(self.reviews)
        if not all(isinstance(check, CheckResult) for check in checks):
            raise ValueError("workspace trial plan checks must contain CheckResult values")
        if len({check.check_id for check in checks}) != len(checks):
            raise ValueError("workspace trial plan checks must not contain duplicate check ids")
        if self.gate is not None and not isinstance(self.gate, GateResult):
            raise ValueError("workspace trial plan gate must be a GateResult")
        if self.mutation_decision is not None and not isinstance(
            self.mutation_decision,
            WorkspaceMutationDecision,
        ):
            raise ValueError("workspace trial plan mutation_decision must be a WorkspaceMutationDecision")
        if not all(isinstance(lease, LeaseGrant) for lease in leases):
            raise ValueError("workspace trial plan leases must contain LeaseGrant values")
        if not all(isinstance(review, ReviewRecord) for review in reviews):
            raise ValueError("workspace trial plan reviews must contain ReviewRecord values")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "leases", leases)
        object.__setattr__(self, "reviews", reviews)

    def to_commit_request(self, commit_id: str, *, now: str) -> WorkspaceCommitRequest:
        checks_by_id = {check.check_id: check for check in self.checks}
        for check_id in self.required_check_ids:
            check = checks_by_id.get(check_id)
            if check is None:
                raise WorkspaceTrialError(f"workspace trial is missing required check {check_id!r}")
            if check.subject != self.change_set.candidate:
                raise WorkspaceTrialError(f"workspace trial check {check_id!r} has a stale subject")
            if check.status != "passed":
                raise WorkspaceTrialError(f"workspace trial check {check_id!r} did not pass")
        if self.gate is None:
            raise WorkspaceTrialError("workspace trial is missing a required gate")
        if self.gate.subject != self.change_set.candidate:
            raise WorkspaceTrialError("workspace trial gate has a stale subject")
        if self.gate.decision != "pass":
            raise WorkspaceTrialError("workspace trial gate did not pass")
        if not set(self.required_check_ids).issubset(self.gate.check_ids):
            raise WorkspaceTrialError("workspace trial gate does not bind every required check")
        if self.mutation_decision is None:
            raise WorkspaceTrialError("workspace trial is missing a required mutation decision")
        if not self.mutation_decision.allowed:
            raise WorkspaceTrialError("workspace trial mutation decision denied the candidate")
        for resource_kind in self.required_lease_kinds:
            if not any(
                lease.resource_kind == resource_kind
                and lease.holder.resource_id == f"trial:{self.trial_id}"
                and lease.is_active_at(now)
                for lease in self.leases
            ):
                raise WorkspaceTrialError(
                    f"workspace trial is missing active lease kind {resource_kind!r}"
                )
        selected_reviews: list[ReviewRecord] = []
        for scope in self.required_review_scopes:
            matching = tuple(
                review
                for review in self.reviews
                if review.scope == scope
                and review.decision in {"accept", "accept_with_conditions"}
                and review.is_valid_for(self.change_set.candidate)
            )
            if not matching:
                raise WorkspaceTrialError(
                    f"workspace trial is missing valid review scope {scope!r}"
                )
            selected_reviews.extend(matching)
        active_lease_ids = sorted(
            {
                lease.lease_id
                for lease in self.leases
                if lease.holder.resource_id == f"trial:{self.trial_id}" and lease.is_active_at(now)
            }
        )
        selected_leases = tuple(
            sorted(
                (
                    lease
                    for lease in self.leases
                    if lease.holder.resource_id == f"trial:{self.trial_id}"
                    and lease.is_active_at(now)
                ),
                key=lambda lease: lease.lease_id,
            )
        )
        return WorkspaceCommitRequest(
            commit_id=commit_id,
            change_set=self.change_set,
            expected_base_revision=self.expected_base_revision,
            mutation_decision=self.mutation_decision,
            gate=self.gate,
            reviews=tuple(sorted(selected_reviews, key=lambda review: review.review_id)),
            trial_id=self.trial_id,
            required_check_ids=self.required_check_ids,
            required_lease_kinds=self.required_lease_kinds,
            required_review_scopes=self.required_review_scopes,
            leases=selected_leases,
            metadata={
                "change_set_digest": self.change_set.content_digest(),
                "lease_ids": active_lease_ids,
                "trial_id": self.trial_id,
            },
        )


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
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    @_with_workspace_lock
    def put_snapshot(self, snapshot: WorkspaceSnapshot) -> InMemoryWorkspaceStore:
        self._snapshots[snapshot.workspace_id] = _copy_workspace_snapshot(snapshot)
        return self

    @_with_workspace_lock
    def current(self, workspace_id: str) -> WorkspaceSnapshot:
        snapshot = self._snapshots.get(workspace_id)
        if snapshot is None:
            raise WorkspaceNotFoundError(workspace_id)
        return _copy_workspace_snapshot(snapshot)

    @_with_workspace_lock
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
        commit_id: str | None = None,
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
            commit_id=f"{workspace_id}:{new_snapshot_id}" if commit_id is None else commit_id,
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

    @_with_workspace_lock
    def compare_and_swap_commit_request(
        self,
        *,
        workspace_id: str,
        request: WorkspaceCommitRequest,
        new_snapshot_id: str,
        resources: tuple[ResourceSnapshotRef, ...],
        committed_by: PrincipalRef,
        committed_at: str,
    ) -> WorkspaceCommit:
        if not isinstance(request, WorkspaceCommitRequest):
            raise WorkspaceCommitAuthorizationError("workspace commit requires an authorized request")
        current = self.current(workspace_id)
        if current.revision != request.expected_base_revision:
            raise WorkspaceCommitAuthorizationError(
                "workspace commit base revision no longer matches the authorized request"
            )
        if (
            request.change_set.base.resource_id != workspace_id
            or request.change_set.base.digest != current.content_digest()
        ):
            raise WorkspaceCommitAuthorizationError(
                "workspace commit base digest no longer matches the authorized request"
            )
        if request.change_set.candidate.resource_id != workspace_id:
            raise WorkspaceCommitAuthorizationError("workspace commit candidate targets another workspace")
        if not request.mutation_decision.allowed:
            raise WorkspaceCommitAuthorizationError("workspace commit mutation decision is denied")
        if request.gate.decision != "pass" or request.gate.subject != request.change_set.candidate:
            raise WorkspaceCommitAuthorizationError("workspace commit gate is not valid for the candidate")
        for check_id in request.required_check_ids:
            if check_id not in request.gate.check_ids:
                raise WorkspaceCommitAuthorizationError(
                    f"workspace commit is missing required gate check {check_id!r}"
                )
        if any(
            review.decision not in {"accept", "accept_with_conditions"}
            or not review.is_valid_for(request.change_set.candidate)
            for review in request.reviews
        ):
            raise WorkspaceCommitAuthorizationError("workspace commit contains an invalid review")
        for scope in request.required_review_scopes:
            if not any(
                review.scope == scope
                and review.decision in {"accept", "accept_with_conditions"}
                and review.is_valid_for(request.change_set.candidate)
                for review in request.reviews
            ):
                raise WorkspaceCommitAuthorizationError(
                    f"workspace commit is missing required review scope {scope!r}"
                )
        for resource_kind in request.required_lease_kinds:
            if not any(
                lease.resource_kind == resource_kind
                and lease.holder.resource_id == f"trial:{request.trial_id}"
                and lease.is_active_at(committed_at)
                for lease in request.leases
            ):
                raise WorkspaceCommitAuthorizationError(
                    f"workspace commit requires active lease kind {resource_kind!r}"
                )
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
        if candidate.content_digest() != request.change_set.candidate.digest:
            raise WorkspaceCommitAuthorizationError(
                "workspace commit candidate digest does not match the authorized request"
            )
        return self.compare_and_swap_commit(
            workspace_id=workspace_id,
            expected_snapshot_id=current.snapshot_id,
            new_snapshot_id=new_snapshot_id,
            resources=resources,
            committed_by=committed_by,
            committed_at=committed_at,
            change_set_id=request.change_set.change_set_id,
            commit_id=request.commit_id,
        )


__all__ = [
    "InMemoryWorkspaceStore",
    "WorkspaceCommit",
    "WorkspaceCommitAuthorizationError",
    "WorkspaceCommitRequest",
    "WorkspaceError",
    "WorkspaceMutationDecision",
    "WorkspaceMutationDeniedError",
    "WorkspaceMutationPolicy",
    "WorkspaceNotFoundError",
    "WorkspaceSnapshot",
    "WorkspaceSnapshotConflictError",
    "WorkspaceTrialError",
    "WorkspaceTrialPlan",
]
