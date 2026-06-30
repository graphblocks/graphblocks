from __future__ import annotations

from graphblocks.orchestration import (
    ChildBudgetDelegation,
    ChildBudgetDelegationError,
    ContextAccessMode,
    LeaseAlreadyExistsError,
    LeaseEpochMismatchError,
    LeaseGrant,
    LeaseNotFoundError,
    LeasePool,
    LeasePoolCapacityError,
    LeasePoolError,
    LeasePoolExhaustedError,
    LeaseRequest,
    LeaseResourceKindMismatchError,
    ModelPool,
    ModelPoolMismatchError,
    ModelProfile,
    ModelSelectionError,
    ModelSelectionRequest,
    ModelSensitivityAboveCeilingError,
    ModelToolNotAllowedError,
    NoEligibleModelError,
    TaskContextAccess,
    TaskPlan,
    TaskPlanContextAccessError,
    TaskPlanCycleError,
    TaskPlanDependencyError,
    TaskPlanDuplicateStepError,
    TaskPlanError,
    TaskPlanIdentityError,
    TaskPlanLimitError,
    TaskPlanLimits,
    TaskPlanPatch,
    TaskPlanPatchMismatchError,
    TaskStep,
    TaskStepNotFoundError,
    VALID_CONTEXT_ACCESS_MODES,
    WorkerPool,
    WorkerProfile,
)


def evaluate_native_scheduler(nodes: object, operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_scheduler

    return evaluate_scheduler(nodes, operations)


def evaluate_native_cancellation_scope(root: dict[str, object], operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_cancellation_scope

    return evaluate_cancellation_scope(root, operations)


def evaluate_native_task_group(group: dict[str, object], operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_task_group

    return evaluate_task_group(group, operations)


def evaluate_native_node_lifecycle(state: dict[str, object], operations: object) -> dict[str, object]:
    from graphblocks_runtime import evaluate_node_lifecycle

    return evaluate_node_lifecycle(state, operations)


__all__ = [
    "ChildBudgetDelegation",
    "ChildBudgetDelegationError",
    "ContextAccessMode",
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
    "VALID_CONTEXT_ACCESS_MODES",
    "WorkerPool",
    "WorkerProfile",
    "evaluate_native_cancellation_scope",
    "evaluate_native_node_lifecycle",
    "evaluate_native_scheduler",
    "evaluate_native_task_group",
]
