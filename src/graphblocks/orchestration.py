from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

from .budget import BudgetPermit, UsageAmount
from .canonical import canonical_hash
from .policy import ResourceRef
from .worker import WorkerAdvertisement, select_worker_for_block


ContextAccessMode = Literal["read", "write", "read_write"]
VALID_CONTEXT_ACCESS_MODES = {"read", "write", "read_write"}


class TaskPlanError(ValueError):
    """Base error for task-plan operations."""


class TaskPlanIdentityError(TaskPlanError):
    def __init__(self, entity: str, field_name: str) -> None:
        self.entity = entity
        self.field_name = field_name
        super().__init__(f"task {entity} {field_name} must not be empty")


def _validate_task_identity(entity: str, field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TaskPlanIdentityError(entity, field_name)


def _parse_lease_datetime(field_name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"lease {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"lease {field_name} must not be empty")
    normalized = value
    if normalized != normalized.strip() or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError(f"lease {field_name} must be an ISO datetime")
    timezone_start = 19
    if normalized[timezone_start] == ".":
        timezone_start += 1
        while timezone_start < len(normalized) and normalized[timezone_start].isdigit():
            timezone_start += 1
        if timezone_start == 20:
            raise ValueError(f"lease {field_name} must be an ISO datetime")
    suffix = normalized[timezone_start:]
    if suffix == "Z":
        normalized = f"{normalized[:timezone_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
    ):
        offset_hours = int(suffix[1:3])
        offset_minutes = int(suffix[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"lease {field_name} must be an ISO datetime")
    else:
        raise ValueError(f"lease {field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"lease {field_name} must be an ISO datetime") from error
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class TaskStep:
    step_id: str
    description: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_task_identity("step", "step_id", self.step_id)
        _validate_task_identity("step", "description", self.description)
        for dependency_id in self.depends_on:
            _validate_task_identity("step", "depends_on", dependency_id)
        object.__setattr__(self, "depends_on", tuple(self.depends_on))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def canonical_value(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "depends_on": self.depends_on,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class TaskPlanPatch:
    patch_id: str
    base_plan_id: str
    base_revision: int
    upsert_steps: tuple[TaskStep, ...] = field(default_factory=tuple)
    remove_step_ids: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_task_identity("patch", "patch_id", self.patch_id)
        _validate_task_identity("patch", "base_plan_id", self.base_plan_id)
        object.__setattr__(self, "upsert_steps", tuple(self.upsert_steps))
        for step_id in self.remove_step_ids:
            _validate_task_identity("patch", "remove_step_ids", step_id)
        object.__setattr__(self, "remove_step_ids", tuple(sorted(set(self.remove_step_ids))))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class TaskPlanLimits:
    max_steps: int = 128
    max_dependencies_per_step: int = 16
    max_description_chars: int = 4096


@dataclass(frozen=True, slots=True)
class TaskContextAccess:
    step_id: str
    resource_id: str
    mode: ContextAccessMode
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_task_identity("context_access", "step_id", self.step_id)
        _validate_task_identity("context_access", "resource_id", self.resource_id)

    def canonical_value(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "resource_id": self.resource_id,
            "mode": self.mode,
            "reason": self.reason,
        }


class TaskPlanLimitError(TaskPlanError):
    def __init__(self, limit_name: str, limit: int, actual: int) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual
        super().__init__(f"task plan exceeds {limit_name}: limit {limit}, actual {actual}")


class TaskPlanPatchMismatchError(TaskPlanError):
    def __init__(self, expected_plan_id: str, actual_plan_id: str, expected_revision: int, actual_revision: int) -> None:
        self.expected_plan_id = expected_plan_id
        self.actual_plan_id = actual_plan_id
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            "task plan patch mismatch: "
            f"expected {expected_plan_id}@{expected_revision}, got {actual_plan_id}@{actual_revision}"
        )


class TaskStepNotFoundError(TaskPlanError):
    def __init__(self, step_id: str) -> None:
        self.step_id = step_id
        super().__init__(f"task step {step_id!r} does not exist")


class TaskPlanDuplicateStepError(TaskPlanError):
    def __init__(self, step_id: str) -> None:
        self.step_id = step_id
        super().__init__(f"task step {step_id!r} appears more than once")


class TaskPlanDependencyError(TaskPlanError):
    def __init__(self, step_id: str, dependency_id: str) -> None:
        self.step_id = step_id
        self.dependency_id = dependency_id
        super().__init__(f"task step {step_id!r} depends on missing step {dependency_id!r}")


class TaskPlanCycleError(TaskPlanError):
    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__(f"task plan dependency cycle: {' -> '.join(cycle)}")


class TaskPlanContextAccessError(TaskPlanError):
    def __init__(self, step_id: str, resource_id: str, mode: str, reason: str) -> None:
        self.step_id = step_id
        self.resource_id = resource_id
        self.mode = mode
        self.reason = reason
        super().__init__(
            f"task context access {step_id!r}:{resource_id!r}:{mode!r} is invalid: {reason}"
        )


@dataclass(frozen=True, slots=True)
class TaskPlan:
    plan_id: str
    objective: str
    steps: tuple[TaskStep, ...] = field(default_factory=tuple)
    revision: int = 1
    metadata: dict[str, object] = field(default_factory=dict)
    limits: TaskPlanLimits = field(default_factory=TaskPlanLimits)
    context_resources: tuple[str, ...] = field(default_factory=tuple)
    context_access: tuple[TaskContextAccess, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_task_identity("plan", "plan_id", self.plan_id)
        _validate_task_identity("plan", "objective", self.objective)
        object.__setattr__(self, "steps", tuple(sorted(self.steps, key=lambda step: step.step_id)))
        object.__setattr__(self, "metadata", dict(self.metadata))
        for resource_id in self.context_resources:
            _validate_task_identity("plan", "context_resources", resource_id)
        object.__setattr__(self, "context_resources", tuple(sorted(set(self.context_resources))))
        object.__setattr__(
            self,
            "context_access",
            tuple(
                sorted(
                    self.context_access,
                    key=lambda access: (access.step_id, access.resource_id, access.mode),
                )
            ),
        )
        if len(self.steps) > self.limits.max_steps:
            raise TaskPlanLimitError("max_steps", self.limits.max_steps, len(self.steps))
        steps_by_id: dict[str, TaskStep] = {}
        for step in self.steps:
            if step.step_id in steps_by_id:
                raise TaskPlanDuplicateStepError(step.step_id)
            steps_by_id[step.step_id] = step
            if len(step.depends_on) > self.limits.max_dependencies_per_step:
                raise TaskPlanLimitError(
                    "max_dependencies_per_step",
                    self.limits.max_dependencies_per_step,
                    len(step.depends_on),
                )
            if len(step.description) > self.limits.max_description_chars:
                raise TaskPlanLimitError(
                    "max_description_chars",
                    self.limits.max_description_chars,
                    len(step.description),
                )
        for step in self.steps:
            for dependency_id in step.depends_on:
                if dependency_id not in steps_by_id:
                    raise TaskPlanDependencyError(step.step_id, dependency_id)

        visiting: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = []

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise TaskPlanCycleError(tuple(stack[stack.index(step_id) :] + [step_id]))
            visiting.add(step_id)
            stack.append(step_id)
            for dependency_id in steps_by_id[step_id].depends_on:
                visit(dependency_id)
            stack.pop()
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in steps_by_id:
            visit(step_id)

        context_resource_ids = set(self.context_resources)
        for access in self.context_access:
            if access.mode not in VALID_CONTEXT_ACCESS_MODES:
                raise TaskPlanContextAccessError(
                    access.step_id,
                    access.resource_id,
                    str(access.mode),
                    "invalid_mode",
                )
            if access.step_id not in steps_by_id:
                raise TaskPlanContextAccessError(
                    access.step_id,
                    access.resource_id,
                    access.mode,
                    "unknown_step",
                )
            if access.resource_id not in context_resource_ids:
                raise TaskPlanContextAccessError(
                    access.step_id,
                    access.resource_id,
                    access.mode,
                    "unknown_resource",
                )

    def step(self, step_id: str) -> TaskStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise TaskStepNotFoundError(step_id)

    def apply_patch(self, patch: TaskPlanPatch) -> TaskPlan:
        if patch.base_plan_id != self.plan_id or patch.base_revision != self.revision:
            raise TaskPlanPatchMismatchError(self.plan_id, patch.base_plan_id, self.revision, patch.base_revision)
        remove_step_ids = set(patch.remove_step_ids)
        steps_by_id = {step.step_id: step for step in self.steps if step.step_id not in remove_step_ids}
        for step in patch.upsert_steps:
            steps_by_id[step.step_id] = step
        metadata = dict(self.metadata)
        metadata.update(patch.metadata)
        return replace(
            self,
            steps=tuple(steps_by_id.values()),
            revision=self.revision + 1,
            metadata=metadata,
        )

    def content_digest(self) -> str:
        return canonical_hash(
            {
                "objective": self.objective,
                "steps": [step.canonical_value() for step in self.steps],
                "metadata": self.metadata,
                "limits": {
                    "max_steps": self.limits.max_steps,
                    "max_dependencies_per_step": self.limits.max_dependencies_per_step,
                    "max_description_chars": self.limits.max_description_chars,
                },
                "context_resources": self.context_resources,
                "context_access": [access.canonical_value() for access in self.context_access],
            }
        )


@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: str
    connection: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    quality_tier: str = "standard"
    cost_class: str = "standard"
    latency_class: str = "standard"
    allowed_sensitivity: tuple[str, ...] = field(default_factory=tuple)
    regions: tuple[str, ...] = field(default_factory=tuple)
    supports_cancellation: bool = False
    supports_usage_report: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))
        object.__setattr__(self, "allowed_sensitivity", tuple(sorted(set(self.allowed_sensitivity))))
        object.__setattr__(self, "regions", tuple(sorted(set(self.regions))))

    def with_capabilities(self, capabilities: list[str] | tuple[str, ...]) -> ModelProfile:
        return replace(self, capabilities=tuple(capabilities))

    def with_allowed_sensitivity(self, allowed_sensitivity: list[str] | tuple[str, ...]) -> ModelProfile:
        return replace(self, allowed_sensitivity=tuple(allowed_sensitivity))

    def with_regions(self, regions: list[str] | tuple[str, ...]) -> ModelProfile:
        return replace(self, regions=tuple(regions))

    def with_quality_tier(self, quality_tier: str) -> ModelProfile:
        return replace(self, quality_tier=quality_tier)

    def with_cost_class(self, cost_class: str) -> ModelProfile:
        return replace(self, cost_class=cost_class)

    def with_latency_class(self, latency_class: str) -> ModelProfile:
        return replace(self, latency_class=latency_class)

    def with_cancellation(self, supports_cancellation: bool) -> ModelProfile:
        return replace(self, supports_cancellation=supports_cancellation)

    def with_usage_report(self, supports_usage_report: bool) -> ModelProfile:
        return replace(self, supports_usage_report=supports_usage_report)


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    profile_id: str
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    model_pool_ref: str | None = None
    sensitivity_ceiling: str | None = None
    default_budget_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(self.required_capabilities))))
        object.__setattr__(self, "allowed_tools", tuple(sorted(set(self.allowed_tools))))

    def with_required_capabilities(self, required_capabilities: list[str] | tuple[str, ...]) -> WorkerProfile:
        return replace(self, required_capabilities=tuple(required_capabilities))

    def with_allowed_tools(self, allowed_tools: list[str] | tuple[str, ...]) -> WorkerProfile:
        return replace(self, allowed_tools=tuple(allowed_tools))

    def with_model_pool_ref(self, model_pool_ref: str) -> WorkerProfile:
        return replace(self, model_pool_ref=model_pool_ref)

    def with_sensitivity_ceiling(self, sensitivity_ceiling: str) -> WorkerProfile:
        return replace(self, sensitivity_ceiling=sensitivity_ceiling)

    def with_default_budget_ref(self, default_budget_ref: str) -> WorkerProfile:
        return replace(self, default_budget_ref=default_budget_ref)


@dataclass(frozen=True, slots=True)
class ModelSelectionRequest:
    worker: WorkerProfile
    required_tools: tuple[str, ...] = field(default_factory=tuple)
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    sensitivity: str | None = None
    region: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_tools", tuple(sorted(set(self.required_tools))))
        object.__setattr__(self, "required_capabilities", tuple(sorted(set(self.required_capabilities))))

    def with_required_tools(self, required_tools: list[str] | tuple[str, ...]) -> ModelSelectionRequest:
        return replace(self, required_tools=tuple(required_tools))

    def with_required_capabilities(self, required_capabilities: list[str] | tuple[str, ...]) -> ModelSelectionRequest:
        return replace(self, required_capabilities=tuple(required_capabilities))

    def with_sensitivity(self, sensitivity: str) -> ModelSelectionRequest:
        return replace(self, sensitivity=sensitivity)

    def with_region(self, region: str) -> ModelSelectionRequest:
        return replace(self, region=region)


class ModelSelectionError(ValueError):
    """Base error for model pool selection."""


class ModelPoolMismatchError(ModelSelectionError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"worker requires model pool {expected!r}, not {actual!r}")


class ModelToolNotAllowedError(ModelSelectionError):
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(f"tool {tool_name!r} is not allowed by worker profile")


class ModelSensitivityAboveCeilingError(ModelSelectionError):
    def __init__(self, requested: str, ceiling: str) -> None:
        self.requested = requested
        self.ceiling = ceiling
        super().__init__(f"sensitivity {requested!r} exceeds worker ceiling {ceiling!r}")


class NoEligibleModelError(ModelSelectionError):
    def __init__(self, pool_id: str, reasons: list[str]) -> None:
        self.pool_id = pool_id
        self.reasons = tuple(reasons)
        super().__init__(f"no eligible model in pool {pool_id!r}: {', '.join(reasons)}")


@dataclass(frozen=True, slots=True)
class ModelPool:
    pool_id: str
    selection_policy_ref: str
    models: tuple[ModelProfile, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", tuple(self.models))

    def with_models(self, models: list[ModelProfile] | tuple[ModelProfile, ...]) -> ModelPool:
        return replace(self, models=tuple(models))

    def select_model(self, request: ModelSelectionRequest) -> ModelProfile:
        if request.worker.model_pool_ref is not None and request.worker.model_pool_ref != self.pool_id:
            raise ModelPoolMismatchError(request.worker.model_pool_ref, self.pool_id)
        for tool_name in request.required_tools:
            if tool_name not in request.worker.allowed_tools:
                raise ModelToolNotAllowedError(tool_name)
        if request.sensitivity is not None and request.worker.sensitivity_ceiling is not None:
            sensitivity_ranks = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
            if sensitivity_ranks.get(request.sensitivity, 4) > sensitivity_ranks.get(request.worker.sensitivity_ceiling, 4):
                raise ModelSensitivityAboveCeilingError(request.sensitivity, request.worker.sensitivity_ceiling)

        required_capabilities = set(request.worker.required_capabilities)
        required_capabilities.update(request.required_capabilities)
        rejection_reasons: list[str] = []
        for model in self.models:
            if not required_capabilities.issubset(set(model.capabilities)):
                rejection_reasons.append(f"{model.profile_id}:missing_capability")
                continue
            if request.sensitivity is not None and model.allowed_sensitivity and request.sensitivity not in model.allowed_sensitivity:
                rejection_reasons.append(f"{model.profile_id}:sensitivity_not_allowed")
                continue
            if request.region is not None and model.regions and request.region not in model.regions:
                rejection_reasons.append(f"{model.profile_id}:region_not_allowed")
                continue
            return model
        raise NoEligibleModelError(self.pool_id, rejection_reasons)


@dataclass(frozen=True, slots=True)
class WorkerPool:
    pool_id: str
    workers: tuple[WorkerAdvertisement, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "workers", tuple(self.workers))

    def with_workers(self, workers: list[WorkerAdvertisement] | tuple[WorkerAdvertisement, ...]) -> WorkerPool:
        return replace(self, workers=tuple(workers))

    def select_for_block(self, block: str) -> WorkerAdvertisement:
        return select_worker_for_block(self.workers, block)


class LeasePoolError(ValueError):
    """Base error for scarce-resource lease pools."""


class LeasePoolCapacityError(LeasePoolError):
    def __init__(self, field_name: str, value: int) -> None:
        self.field_name = field_name
        self.value = value
        super().__init__(f"{field_name} must be positive, got {value}")


class LeasePoolExhaustedError(LeasePoolError):
    def __init__(self, pool_id: str, requested_units: int, available_units: int) -> None:
        self.pool_id = pool_id
        self.requested_units = requested_units
        self.available_units = available_units
        super().__init__(
            f"lease pool {pool_id!r} has {available_units} units available, requested {requested_units}"
        )


class LeaseResourceKindMismatchError(LeasePoolError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"lease resource kind mismatch: expected {expected!r}, got {actual!r}")


class LeaseAlreadyExistsError(LeasePoolError):
    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"lease {lease_id!r} already exists")


class LeaseNotFoundError(LeasePoolError):
    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"lease {lease_id!r} does not exist")


class LeaseEpochMismatchError(LeasePoolError):
    def __init__(self, lease_id: str, expected_epoch: int, actual_epoch: int) -> None:
        self.lease_id = lease_id
        self.expected_epoch = expected_epoch
        self.actual_epoch = actual_epoch
        super().__init__(
            f"lease {lease_id!r} fencing epoch mismatch: expected {expected_epoch}, got {actual_epoch}"
        )


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    request_id: str
    holder: ResourceRef
    resource_kind: str
    units: int = 1
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.units <= 0:
            raise LeasePoolCapacityError("units", self.units)
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    lease_id: str
    request_id: str
    pool_id: str
    holder: ResourceRef
    resource_kind: str
    units: int
    fencing_epoch: int
    acquired_at: str
    expires_at: str
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.units <= 0:
            raise LeasePoolCapacityError("units", self.units)
        _parse_lease_datetime("acquired_at", self.acquired_at)
        _parse_lease_datetime("expires_at", self.expires_at)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def is_active_at(self, now: str) -> bool:
        try:
            return _parse_lease_datetime("expires_at", self.expires_at) > _parse_lease_datetime("now", now)
        except ValueError:
            return False


@dataclass(frozen=True, slots=True)
class LeasePool:
    pool_id: str
    resource_kind: str
    capacity_units: int
    active_leases: tuple[LeaseGrant, ...] = field(default_factory=tuple)
    next_fencing_epoch: int = 1
    policy_ref: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capacity_units <= 0:
            raise LeasePoolCapacityError("capacity_units", self.capacity_units)
        active_leases = tuple(
            sorted(
                self.active_leases,
                key=lambda lease: (lease.expires_at, lease.lease_id),
            )
        )
        seen_lease_ids: set[str] = set()
        used_units = 0
        highest_epoch = 0
        for lease in active_leases:
            if lease.pool_id != self.pool_id:
                raise LeaseResourceKindMismatchError(self.pool_id, lease.pool_id)
            if lease.resource_kind != self.resource_kind:
                raise LeaseResourceKindMismatchError(self.resource_kind, lease.resource_kind)
            if lease.lease_id in seen_lease_ids:
                raise LeaseAlreadyExistsError(lease.lease_id)
            seen_lease_ids.add(lease.lease_id)
            used_units += lease.units
            highest_epoch = max(highest_epoch, lease.fencing_epoch)
        if used_units > self.capacity_units:
            raise LeasePoolExhaustedError(self.pool_id, used_units, self.capacity_units)
        object.__setattr__(self, "active_leases", active_leases)
        object.__setattr__(self, "next_fencing_epoch", max(self.next_fencing_epoch, highest_epoch + 1))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def used_units(self) -> int:
        return sum(lease.units for lease in self.active_leases)

    @property
    def available_units(self) -> int:
        return self.capacity_units - self.used_units

    def reap_expired(self, now: str) -> LeasePool:
        _parse_lease_datetime("now", now)
        active_leases = tuple(lease for lease in self.active_leases if lease.is_active_at(now))
        if active_leases == self.active_leases:
            return self
        return replace(self, active_leases=active_leases)

    def acquire(
        self,
        request: LeaseRequest,
        *,
        lease_id: str,
        acquired_at: str,
        expires_at: str,
    ) -> tuple[LeasePool, LeaseGrant]:
        _parse_lease_datetime("acquired_at", acquired_at)
        _parse_lease_datetime("expires_at", expires_at)
        if request.resource_kind != self.resource_kind:
            raise LeaseResourceKindMismatchError(self.resource_kind, request.resource_kind)
        current = self.reap_expired(acquired_at)
        if any(lease.lease_id == lease_id for lease in current.active_leases):
            raise LeaseAlreadyExistsError(lease_id)
        if request.units > current.available_units:
            raise LeasePoolExhaustedError(self.pool_id, request.units, current.available_units)
        grant = LeaseGrant(
            lease_id=lease_id,
            request_id=request.request_id,
            pool_id=self.pool_id,
            holder=request.holder,
            resource_kind=request.resource_kind,
            units=request.units,
            fencing_epoch=current.next_fencing_epoch,
            acquired_at=acquired_at,
            expires_at=expires_at,
            metadata=request.metadata,
        )
        return replace(
            current,
            active_leases=current.active_leases + (grant,),
            next_fencing_epoch=current.next_fencing_epoch + 1,
        ), grant

    def release(self, lease_id: str, *, fencing_epoch: int) -> LeasePool:
        active_leases = list(self.active_leases)
        for index, lease in enumerate(active_leases):
            if lease.lease_id == lease_id:
                if lease.fencing_epoch != fencing_epoch:
                    raise LeaseEpochMismatchError(lease_id, lease.fencing_epoch, fencing_epoch)
                del active_leases[index]
                return replace(self, active_leases=tuple(active_leases))
        raise LeaseNotFoundError(lease_id)


class ChildBudgetDelegationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChildBudgetDelegation:
    delegation_id: str
    parent_permit: BudgetPermit
    child_owner: ResourceRef
    amounts: list[UsageAmount]
    expires_at: str
    continuation_profile: str | None = None

    def create_child_permit(self, permit_id: str) -> BudgetPermit:
        if not self.parent_permit.allows(self.amounts):
            raise ChildBudgetDelegationError(f"parent permit {self.parent_permit.permit_id!r} does not cover delegation")
        return BudgetPermit(
            permit_id=permit_id,
            reservation_refs=self.parent_permit.reservation_refs,
            owner=self.child_owner,
            atomic_unit=self.parent_permit.atomic_unit,
            admission_epoch=self.parent_permit.admission_epoch,
            authorized_amounts=list(self.amounts),
            continuation_profile=self.continuation_profile or self.parent_permit.continuation_profile,
            policy_snapshot_digest=self.parent_permit.policy_snapshot_digest,
            expires_at=self.expires_at,
            low_watermark=[],
            fencing_tokens=dict(self.parent_permit.fencing_tokens),
        )


__all__ = [
    "ChildBudgetDelegation",
    "ChildBudgetDelegationError",
    "LeaseAlreadyExistsError",
    "LeaseEpochMismatchError",
    "LeaseGrant",
    "LeaseNotFoundError",
    "LeasePool",
    "LeasePoolCapacityError",
    "LeasePoolError",
    "LeasePoolExhaustedError",
    "LeaseRequest",
    "LeaseResourceKindMismatchError",
    "ModelPool",
    "ModelPoolMismatchError",
    "ModelProfile",
    "ModelSelectionError",
    "ModelSelectionRequest",
    "ModelSensitivityAboveCeilingError",
    "ModelToolNotAllowedError",
    "NoEligibleModelError",
    "TaskContextAccess",
    "TaskPlan",
    "TaskPlanContextAccessError",
    "TaskPlanCycleError",
    "TaskPlanDependencyError",
    "TaskPlanDuplicateStepError",
    "TaskPlanError",
    "TaskPlanIdentityError",
    "TaskPlanLimitError",
    "TaskPlanLimits",
    "TaskPlanPatch",
    "TaskPlanPatchMismatchError",
    "TaskStep",
    "TaskStepNotFoundError",
    "WorkerPool",
    "WorkerProfile",
]
