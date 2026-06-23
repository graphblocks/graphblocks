from __future__ import annotations

from dataclasses import dataclass, field, replace

from .budget import BudgetPermit, UsageAmount
from .canonical import canonical_hash
from .policy import ResourceRef
from .worker import WorkerAdvertisement, select_worker_for_block


@dataclass(frozen=True, slots=True)
class TaskStep:
    step_id: str
    description: str
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
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
        object.__setattr__(self, "upsert_steps", tuple(self.upsert_steps))
        object.__setattr__(self, "remove_step_ids", tuple(sorted(set(self.remove_step_ids))))
        object.__setattr__(self, "metadata", dict(self.metadata))


class TaskPlanError(ValueError):
    """Base error for task-plan operations."""


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


@dataclass(frozen=True, slots=True)
class TaskPlan:
    plan_id: str
    objective: str
    steps: tuple[TaskStep, ...] = field(default_factory=tuple)
    revision: int = 1
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(sorted(self.steps, key=lambda step: step.step_id)))
        object.__setattr__(self, "metadata", dict(self.metadata))

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
    "ModelPool",
    "ModelPoolMismatchError",
    "ModelProfile",
    "ModelSelectionError",
    "ModelSelectionRequest",
    "ModelSensitivityAboveCeilingError",
    "ModelToolNotAllowedError",
    "NoEligibleModelError",
    "TaskPlan",
    "TaskPlanError",
    "TaskPlanPatch",
    "TaskPlanPatchMismatchError",
    "TaskStep",
    "TaskStepNotFoundError",
    "WorkerPool",
    "WorkerProfile",
]
