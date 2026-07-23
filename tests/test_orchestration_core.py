from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import BudgetPermit, UsageAmount
from graphblocks.orchestration import (
    ChildBudgetDelegation,
    ChildBudgetDelegationError,
    LeaseBudgetPermitError,
    LeaseEpochMismatchError,
    LeaseGrant,
    LeasePool,
    LeasePoolCapacityError,
    LeasePoolExhaustedError,
    LeaseRequest,
    ModelPool,
    ModelPoolMismatchError,
    ModelProfile,
    ModelSelectionError,
    ModelSelectionRequest,
    ModelSensitivityAboveCeilingError,
    ModelToolNotAllowedError,
    TaskContextAccess,
    TaskExecutionContract,
    TaskExecutionContractError,
    TaskPlanCycleError,
    TaskPlanContextAccessError,
    TaskPlanDependencyError,
    TaskPlanDuplicateStepError,
    TaskPlanIdentityError,
    TaskPlanLimitError,
    TaskPlanLimits,
    TaskPlan,
    TaskPlanPatch,
    TaskStep,
    WorkerPool,
    WorkerProfile,
)
from graphblocks.policy import ResourceRef
from graphblocks.worker import BlockCapability, WorkerAdvertisement


def test_task_plan_patch_is_order_stable_and_revises_steps() -> None:
    base = TaskPlan(
        plan_id="plan-1",
        objective="answer support request",
        steps=(TaskStep("draft", "Draft response"),),
        revision=1,
    )
    patch = TaskPlanPatch(
        patch_id="patch-1",
        base_plan_id="plan-1",
        base_revision=1,
        upsert_steps=(
            TaskStep("verify", "Verify answer", depends_on=("draft",)),
            TaskStep("draft", "Draft response with citations"),
        ),
        remove_step_ids=("missing",),
        created_at="2026-06-24T00:00:00Z",
    )

    updated = base.apply_patch(patch)

    assert updated.revision == 2
    assert [step.step_id for step in updated.steps] == ["draft", "verify"]
    assert updated.step("draft").description == "Draft response with citations"
    assert updated.content_digest() == updated.apply_patch(TaskPlanPatch("noop", "plan-1", 2)).content_digest()


def test_task_plan_records_reject_ambiguous_restored_collections_and_revisions() -> None:
    with pytest.raises(ValueError, match="task plan revision must be positive"):
        TaskPlan("plan-1", "answer", revision=0)
    with pytest.raises(
        ValueError,
        match="task plan patch base_revision must be positive",
    ):
        TaskPlanPatch("patch-1", "plan-1", 0)
    with pytest.raises(
        ValueError,
        match="task step depends_on must be a collection of step ids",
    ):
        TaskStep("draft", "Draft response", depends_on="source")  # type: ignore[arg-type]
    with pytest.raises(
        ValueError,
        match="task plan context_resources must be a collection of resource ids",
    ):
        TaskPlan("plan-1", "answer", context_resources="policy-doc")  # type: ignore[arg-type]
    with pytest.raises(
        TaskPlanContextAccessError,
        match="invalid_mode",
    ):
        TaskContextAccess("draft", "policy-doc", "execute")  # type: ignore[arg-type]
    with pytest.raises(
        TaskPlanContextAccessError,
        match="invalid_mode",
    ):
        TaskContextAccess("draft", "policy-doc", ["read"])  # type: ignore[arg-type]


def test_task_plan_patch_rejects_duplicate_and_conflicting_step_operations() -> None:
    with pytest.raises(TaskPlanDuplicateStepError) as duplicate:
        TaskPlanPatch(
            "patch-1",
            "plan-1",
            1,
            upsert_steps=(
                TaskStep("draft", "First draft"),
                TaskStep("draft", "Replacement draft"),
            ),
        )
    assert duplicate.value.step_id == "draft"

    with pytest.raises(
        ValueError,
        match="must not both upsert and remove step 'draft'",
    ):
        TaskPlanPatch(
            "patch-2",
            "plan-1",
            1,
            upsert_steps=(TaskStep("draft", "Draft"),),
            remove_step_ids=("draft",),
        )


def test_task_plan_records_detach_nested_metadata_from_callers() -> None:
    step_metadata = {"labels": ["draft"]}
    plan_metadata = {"audit": {"reviewers": ["alice"]}}
    patch_metadata = {"reason": {"codes": ["clarify"]}}
    step = TaskStep("draft", "Draft response", metadata=step_metadata)
    plan = TaskPlan("plan-1", "answer", steps=(step,), metadata=plan_metadata)
    patch = TaskPlanPatch("patch-1", "plan-1", 1, metadata=patch_metadata)

    step_metadata["labels"].append("mutated")
    plan_metadata["audit"]["reviewers"].append("mutated")
    patch_metadata["reason"]["codes"].append("mutated")

    assert step.metadata == {"labels": ["draft"]}
    assert plan.metadata == {"audit": {"reviewers": ["alice"]}}
    assert patch.metadata == {"reason": {"codes": ["clarify"]}}


def test_task_plan_rejects_missing_dependencies_and_patch_cycles() -> None:
    with pytest.raises(TaskPlanDependencyError) as dependency_error:
        TaskPlan(
            plan_id="plan-1",
            objective="answer support request",
            steps=(TaskStep("verify", "Verify answer", depends_on=("draft",)),),
        )

    assert dependency_error.value.step_id == "verify"
    assert dependency_error.value.dependency_id == "draft"

    base = TaskPlan(
        plan_id="plan-1",
        objective="answer support request",
        steps=(
            TaskStep("draft", "Draft response"),
            TaskStep("verify", "Verify answer", depends_on=("draft",)),
        ),
    )
    patch = TaskPlanPatch(
        patch_id="patch-cycle",
        base_plan_id="plan-1",
        base_revision=1,
        upsert_steps=(TaskStep("draft", "Draft response", depends_on=("verify",)),),
    )

    with pytest.raises(TaskPlanCycleError) as cycle_error:
        base.apply_patch(patch)

    assert cycle_error.value.cycle == ("draft", "verify", "draft")


def test_task_plan_limits_bound_steps_and_dependencies() -> None:
    with pytest.raises(TaskPlanLimitError) as step_error:
        TaskPlan(
            plan_id="plan-1",
            objective="answer support request",
            steps=(
                TaskStep("draft", "Draft response"),
                TaskStep("verify", "Verify answer"),
            ),
            limits=TaskPlanLimits(max_steps=1),
        )

    assert step_error.value.limit_name == "max_steps"
    assert step_error.value.limit == 1
    assert step_error.value.actual == 2

    with pytest.raises(TaskPlanLimitError) as dependency_error:
        TaskPlan(
            plan_id="plan-2",
            objective="answer support request",
            steps=(
                TaskStep("a", "A"),
                TaskStep("b", "B"),
                TaskStep("combine", "Combine", depends_on=("a", "b")),
            ),
            limits=TaskPlanLimits(max_dependencies_per_step=1),
        )

    assert dependency_error.value.limit_name == "max_dependencies_per_step"
    assert dependency_error.value.limit == 1
    assert dependency_error.value.actual == 2


def test_task_plan_limits_bound_dependency_depth_and_parallel_width() -> None:
    bounded = TaskPlan(
        plan_id="plan-bounded",
        objective="research with bounded fan-out",
        steps=(
            TaskStep("collect-a", "Collect source A"),
            TaskStep("collect-b", "Collect source B"),
            TaskStep("synthesize", "Synthesize", depends_on=("collect-a", "collect-b")),
        ),
        limits=TaskPlanLimits(max_depth=2, max_parallel_tasks=2),
    )

    assert bounded.execution_layers() == (("collect-a", "collect-b"), ("synthesize",))

    with pytest.raises(TaskPlanLimitError) as depth_error:
        TaskPlan(
            plan_id="plan-too-deep",
            objective="reject recursive expansion",
            steps=(
                TaskStep("one", "One"),
                TaskStep("two", "Two", depends_on=("one",)),
                TaskStep("three", "Three", depends_on=("two",)),
            ),
            limits=TaskPlanLimits(max_depth=2),
        )

    assert depth_error.value.limit_name == "max_depth"
    assert depth_error.value.actual == 3

    with pytest.raises(TaskPlanLimitError) as parallel_error:
        TaskPlan(
            plan_id="plan-too-wide",
            objective="reject excess fan-out",
            steps=(TaskStep("a", "A"), TaskStep("b", "B")),
            limits=TaskPlanLimits(max_parallel_tasks=1),
        )

    assert parallel_error.value.limit_name == "max_parallel_tasks"
    assert parallel_error.value.actual == 2


def test_task_plan_context_access_graph_is_validated_and_digest_stable() -> None:
    left = TaskPlan(
        plan_id="plan-1",
        objective="answer support request",
        steps=(
            TaskStep("draft", "Draft response"),
            TaskStep("verify", "Verify answer", depends_on=("draft",)),
        ),
        context_resources=("tenant-profile", "policy-doc"),
        context_access=(
            TaskContextAccess("verify", "tenant-profile", "read"),
            TaskContextAccess("draft", "policy-doc", "read"),
        ),
    )
    right = TaskPlan(
        plan_id="plan-2",
        objective="answer support request",
        steps=tuple(reversed(left.steps)),
        context_resources=tuple(reversed(left.context_resources)),
        context_access=tuple(reversed(left.context_access)),
    )

    assert left.content_digest() == right.content_digest()
    assert [access.step_id for access in left.context_access] == ["draft", "verify"]

    with pytest.raises(TaskPlanContextAccessError) as step_error:
        TaskPlan(
            plan_id="plan-3",
            objective="answer support request",
            steps=(TaskStep("draft", "Draft response"),),
            context_resources=("policy-doc",),
            context_access=(TaskContextAccess("missing", "policy-doc", "read"),),
        )

    assert step_error.value.reason == "unknown_step"
    assert step_error.value.step_id == "missing"

    with pytest.raises(TaskPlanContextAccessError) as resource_error:
        TaskPlan(
            plan_id="plan-4",
            objective="answer support request",
            steps=(TaskStep("draft", "Draft response"),),
            context_resources=("policy-doc",),
            context_access=(TaskContextAccess("draft", "secret-vault", "read"),),
        )

    assert resource_error.value.reason == "unknown_resource"
    assert resource_error.value.resource_id == "secret-vault"

    with pytest.raises(TaskPlanContextAccessError) as mode_error:
        TaskPlan(
            plan_id="plan-5",
            objective="answer support request",
            steps=(TaskStep("draft", "Draft response"),),
            context_resources=("policy-doc",),
            context_access=(TaskContextAccess("draft", "policy-doc", "execute"),),
        )

    assert mode_error.value.reason == "invalid_mode"
    assert mode_error.value.mode == "execute"


def test_task_plan_rejects_empty_identity_fields() -> None:
    invalid_cases = [
        (
            lambda: TaskPlan("", "answer support request"),
            ("plan", "plan_id"),
        ),
        (
            lambda: TaskPlan("plan-1", " "),
            ("plan", "objective"),
        ),
        (
            lambda: TaskStep("", "Draft response"),
            ("step", "step_id"),
        ),
        (
            lambda: TaskStep("draft", " "),
            ("step", "description"),
        ),
        (
            lambda: TaskStep("draft", "Draft response", depends_on=(" ",)),
            ("step", "depends_on"),
        ),
        (
            lambda: TaskContextAccess("", "policy-doc", "read"),
            ("context_access", "step_id"),
        ),
        (
            lambda: TaskContextAccess("draft", "", "read"),
            ("context_access", "resource_id"),
        ),
        (
            lambda: TaskPlan(
                "plan-1",
                "answer support request",
                steps=(TaskStep("draft", "Draft response"),),
                context_resources=(" ",),
            ),
            ("plan", "context_resources"),
        ),
        (
            lambda: TaskPlanPatch("", "plan-1", 1),
            ("patch", "patch_id"),
        ),
        (
            lambda: TaskPlanPatch("patch-1", "", 1),
            ("patch", "base_plan_id"),
        ),
        (
            lambda: TaskPlanPatch("patch-1", "plan-1", 1, remove_step_ids=(" ",)),
            ("patch", "remove_step_ids"),
        ),
    ]

    for factory, expected in invalid_cases:
        with pytest.raises(TaskPlanIdentityError) as error:
            factory()
        assert (error.value.entity, error.value.field_name) == expected


def test_model_pool_selects_first_profile_matching_worker_policy_and_request() -> None:
    pool = ModelPool("support-pool", "policy-1").with_models(
        [
            ModelProfile("public-only", "models.public")
            .with_capabilities(["chat", "tool_use"])
            .with_allowed_sensitivity(["public"])
            .with_regions(["us-east-1"]),
            ModelProfile("support-internal", "models.support")
            .with_capabilities(["chat", "tool_use", "json"])
            .with_allowed_sensitivity(["public", "internal"])
            .with_regions(["us-east-1", "eu-west-1"])
            .with_usage_report(True)
            .with_cancellation(True),
        ]
    )
    worker = (
        WorkerProfile("support-worker")
        .with_required_capabilities(["chat", "tool_use"])
        .with_allowed_tools(["knowledge.search"])
        .with_model_pool_ref("support-pool")
        .with_sensitivity_ceiling("internal")
    )
    request = (
        ModelSelectionRequest(worker)
        .with_required_tools(["knowledge.search"])
        .with_sensitivity("internal")
        .with_region("us-east-1")
    )

    selected = pool.select_model(request)

    assert selected.profile_id == "support-internal"
    assert selected.connection == "models.support"
    assert selected.supports_usage_report
    assert selected.supports_cancellation


def test_model_pool_rejects_worker_policy_mismatches() -> None:
    pool = ModelPool("support-pool", "policy-1")
    worker = WorkerProfile("support-worker").with_model_pool_ref("other-pool")

    with pytest.raises(ModelPoolMismatchError) as pool_error:
        pool.select_model(ModelSelectionRequest(worker))

    assert pool_error.value.expected == "other-pool"
    assert pool_error.value.actual == "support-pool"

    worker = (
        WorkerProfile("support-worker")
        .with_model_pool_ref("support-pool")
        .with_allowed_tools(["knowledge.search"])
        .with_sensitivity_ceiling("internal")
    )
    with pytest.raises(ModelToolNotAllowedError) as tool_error:
        pool.select_model(ModelSelectionRequest(worker).with_required_tools(["ticket.create"]))

    assert tool_error.value.tool_name == "ticket.create"

    with pytest.raises(ModelSensitivityAboveCeilingError) as sensitivity_error:
        pool.select_model(ModelSelectionRequest(worker).with_sensitivity("restricted"))

    assert sensitivity_error.value.requested == "restricted"
    assert sensitivity_error.value.ceiling == "internal"

    misspelled_ceiling = worker.with_sensitivity_ceiling("confidental")
    with pytest.raises(ModelSelectionError, match="unknown worker sensitivity ceiling"):
        pool.select_model(
            ModelSelectionRequest(misspelled_ceiling).with_sensitivity("public")
        )


def test_worker_pool_selects_ready_worker_for_required_block() -> None:
    pool = WorkerPool("pool-1").with_workers(
        [
            WorkerAdvertisement.new(
                "worker-z",
                "model-cpu",
                "sha256:package-lock",
                "sha256:image-z",
                [BlockCapability("model.generate@1")],
            ),
            WorkerAdvertisement.new(
                "worker-a",
                "model-cpu",
                "sha256:package-lock",
                "sha256:image-a",
                [BlockCapability("model.generate@1")],
            ).with_state("draining"),
            WorkerAdvertisement.new(
                "worker-c",
                "model-cpu",
                "sha256:package-lock",
                "sha256:image-c",
                [BlockCapability("model.generate@1")],
            ),
        ]
    )

    assert pool.select_for_block("model.generate@1").worker_id == "worker-c"


def test_child_budget_delegation_builds_scoped_permit_from_parent_permit() -> None:
    parent = BudgetPermit(
        permit_id="permit-parent",
        reservation_refs=("reservation-parent",),
        owner=ResourceRef("task:parent"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=3,
        authorized_amounts=[UsageAmount("tokens", Decimal("100"), "tokens")],
        continuation_profile="default",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-24T01:00:00Z",
        fencing_tokens={"reservation-parent": 11},
    )
    delegation = ChildBudgetDelegation(
        delegation_id="delegation-1",
        parent_permit=parent,
        child_owner=ResourceRef("task:child"),
        amounts=[UsageAmount("tokens", Decimal("40"), "tokens")],
        expires_at="2026-06-24T00:30:00Z",
    )

    permit = delegation.create_child_permit("permit-child")

    assert permit.owner == ResourceRef("task:child")
    assert permit.atomic_unit == parent.atomic_unit
    assert permit.authorized_amounts == [UsageAmount("tokens", Decimal("40"), "tokens")]
    assert permit.fencing_tokens == {"reservation-parent": 11}
    assert parent.allows(permit.authorized_amounts)


def test_child_budget_delegation_cannot_outlive_parent_permit() -> None:
    parent = BudgetPermit(
        permit_id="permit-parent",
        reservation_refs=("reservation-parent",),
        owner=ResourceRef("task:parent"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=3,
        authorized_amounts=[UsageAmount("tokens", Decimal("100"), "tokens")],
        continuation_profile="default",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-24T01:00:00Z",
        fencing_tokens={"reservation-parent": 11},
    )

    with pytest.raises(ChildBudgetDelegationError, match="outlives parent permit"):
        ChildBudgetDelegation(
            delegation_id="delegation-late",
            parent_permit=parent,
            child_owner=ResourceRef("task:child"),
            amounts=[UsageAmount("tokens", Decimal("40"), "tokens")],
            expires_at="2026-06-24T01:00:01Z",
        ).create_child_permit("permit-child")


def test_task_execution_contract_checkpoints_per_task_and_cancels_budget_pressure_priorities() -> None:
    plan = TaskPlan(
        plan_id="plan-research",
        objective="bounded research",
        steps=(
            TaskStep("optional", "Optional source", metadata={"priority": "optional"}),
            TaskStep("normal", "Normal source", metadata={"priority": "normal"}),
            TaskStep("required", "Required source", metadata={"priority": "required"}),
            TaskStep("verify", "Verify", metadata={"priority": "verification"}),
        ),
    )
    permit = BudgetPermit(
        permit_id="permit-optional",
        reservation_refs=("reservation-optional",),
        owner=ResourceRef("task:optional"),
        atomic_unit=ResourceRef("task:optional"),
        admission_epoch=1,
        authorized_amounts=[UsageAmount("tokens", Decimal("20"), "tokens")],
        continuation_profile="default",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-24T01:00:00Z",
        fencing_tokens={"reservation-optional": 1},
    )
    contract = TaskExecutionContract(
        checkpoint="each_task",
        reservation="per_task",
        cancel_priorities=("optional", "normal"),
        preserve_priorities=("required", "verification", "finalization"),
    )

    checkpoint = contract.checkpoint_completion(
        plan,
        "optional",
        permit,
        result_digest="sha256:optional-result",
        completed_at="2026-06-24T00:30:00Z",
    )

    assert checkpoint.step_id == "optional"
    assert checkpoint.plan_revision == 1
    assert checkpoint.permit_id == "permit-optional"
    assert contract.budget_pressure_cancellations(
        plan,
        active_step_ids=("verify", "required", "normal", "optional"),
    ) == ("optional", "normal")

    with pytest.raises(TaskExecutionContractError, match="task-specific budget permit"):
        contract.checkpoint_completion(
            plan,
            "normal",
            permit,
            result_digest="sha256:normal-result",
            completed_at="2026-06-24T00:30:00Z",
        )


def test_lease_pool_acquires_capacity_with_fencing_and_expiration() -> None:
    pool = LeasePool("formal-license", "eda.formal", capacity_units=2)
    request = LeaseRequest(
        request_id="formal-check",
        holder=ResourceRef("trial:formal"),
        resource_kind="eda.formal",
        units=2,
    )

    leased, grant = pool.acquire(
        request,
        lease_id="lease-1",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )

    assert grant.pool_id == "formal-license"
    assert grant.fencing_epoch == 1
    assert grant.units == 2
    assert leased.available_units == 0
    assert grant.is_active_at("2026-06-23T23:59:59Z") is False
    assert grant.is_active_at("2026-06-24T00:00:00Z") is True
    assert grant.is_active_at("2026-06-24T00:05:00Z") is False

    with pytest.raises(LeasePoolExhaustedError) as exhausted:
        leased.acquire(
            LeaseRequest("smoke-check", ResourceRef("trial:smoke"), "eda.formal"),
            lease_id="lease-2",
            acquired_at="2026-06-24T00:01:00Z",
            expires_at="2026-06-24T00:06:00Z",
        )

    assert exhausted.value.available_units == 0
    assert exhausted.value.requested_units == 1

    reaped = leased.reap_expired("2026-06-24T00:05:01Z")
    renewed, renewed_grant = reaped.acquire(
        LeaseRequest("smoke-check", ResourceRef("trial:smoke"), "eda.formal"),
        lease_id="lease-2",
        acquired_at="2026-06-24T00:05:02Z",
        expires_at="2026-06-24T00:10:00Z",
    )

    assert reaped.available_units == 2
    assert renewed.available_units == 1
    assert renewed_grant.fencing_epoch == 2


def test_lease_records_reject_boolean_and_fractional_integer_fields() -> None:
    holder = ResourceRef("trial:formal")

    for units in (True, 1.5):
        with pytest.raises(LeasePoolCapacityError, match="units must be positive"):
            LeaseRequest("formal-check", holder, "eda.formal", units=units)  # type: ignore[arg-type]

    with pytest.raises(LeasePoolCapacityError, match="capacity_units must be positive"):
        LeasePool("formal-license", "eda.formal", capacity_units=True)
    with pytest.raises(LeasePoolCapacityError, match="next_fencing_epoch must be positive"):
        LeasePool(
            "formal-license",
            "eda.formal",
            capacity_units=1,
            next_fencing_epoch=True,
        )
    with pytest.raises(LeasePoolCapacityError, match="fencing_epoch must be positive"):
        LeaseGrant(
            lease_id="lease-1",
            request_id="formal-check",
            pool_id="formal-license",
            holder=holder,
            resource_kind="eda.formal",
            units=1,
            fencing_epoch=True,
            acquired_at="2026-06-24T00:00:00Z",
            expires_at="2026-06-24T00:05:00Z",
        )


def test_lease_pool_rejects_invalid_restored_lease_entries() -> None:
    with pytest.raises(ValueError, match="active_leases must contain LeaseGrant records"):
        LeasePool(
            "formal-license",
            "eda.formal",
            capacity_units=1,
            active_leases=(object(),),  # type: ignore[arg-type]
        )


def test_lease_records_detach_nested_metadata_from_callers() -> None:
    request_metadata = {"labels": ["formal"]}
    pool_metadata = {"owner": {"teams": ["verification"]}}
    request = LeaseRequest(
        "formal-check",
        ResourceRef("trial:formal"),
        "eda.formal",
        metadata=request_metadata,
    )
    pool = LeasePool(
        "formal-license",
        "eda.formal",
        capacity_units=1,
        metadata=pool_metadata,
    )
    leased, grant = pool.acquire(
        request,
        lease_id="lease-1",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )

    request_metadata["labels"].append("caller-mutated")
    pool_metadata["owner"]["teams"].append("caller-mutated")
    request.metadata["labels"].append("request-mutated")

    assert grant.metadata == {"labels": ["formal"]}
    assert leased.metadata == {"owner": {"teams": ["verification"]}}


@pytest.mark.parametrize(
    "expires_at",
    ("2026-06-24T00:00:00Z", "2026-06-23T23:59:59Z"),
)
def test_lease_pool_rejects_non_positive_intervals(expires_at: str) -> None:
    pool = LeasePool("formal-license", "eda.formal", capacity_units=1)

    with pytest.raises(ValueError, match="lease expires_at must be later than acquired_at"):
        pool.acquire(
            LeaseRequest("formal-check", ResourceRef("trial:formal"), "eda.formal"),
            lease_id="lease-invalid",
            acquired_at="2026-06-24T00:00:00Z",
            expires_at=expires_at,
        )


def test_lease_pool_acquisition_is_bound_to_active_budget_permit() -> None:
    pool = LeasePool("formal-license", "eda.formal", capacity_units=1)
    holder = ResourceRef("trial:rtl-1")
    permit = BudgetPermit(
        permit_id="permit-formal",
        reservation_refs=("reservation-formal",),
        owner=holder,
        atomic_unit=holder,
        admission_epoch=1,
        authorized_amounts=[
            UsageAmount("licensed_resource_seconds", Decimal("900"), "second")
        ],
        continuation_profile="default",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-24T00:15:00Z",
        fencing_tokens={"reservation-formal": 7},
    )
    amounts = [UsageAmount("licensed_resource_seconds", Decimal("300"), "second")]

    leased, grant = pool.acquire_with_budget_permit(
        LeaseRequest("formal-check", holder, "eda.formal"),
        permit,
        amounts,
        lease_id="lease-formal",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )

    assert leased.available_units == 0
    assert grant.metadata["budget_permit_id"] == "permit-formal"
    assert grant.metadata["budget_reservation_refs"] == ["reservation-formal"]

    with pytest.raises(LeaseBudgetPermitError, match="holder"):
        pool.acquire_with_budget_permit(
            LeaseRequest("wrong-holder", ResourceRef("trial:other"), "eda.formal"),
            permit,
            amounts,
            lease_id="lease-other",
            acquired_at="2026-06-24T00:00:00Z",
            expires_at="2026-06-24T00:05:00Z",
        )

    with pytest.raises(LeaseBudgetPermitError, match="expires after budget permit"):
        pool.acquire_with_budget_permit(
            LeaseRequest("formal-check", holder, "eda.formal"),
            permit,
            amounts,
            lease_id="lease-late",
            acquired_at="2026-06-24T00:00:00Z",
            expires_at="2026-06-24T00:20:00Z",
        )


def test_lease_pool_reap_expired_compares_expiration_as_datetime() -> None:
    pool = LeasePool("formal-license", "eda.formal", capacity_units=1)
    leased, grant = pool.acquire(
        LeaseRequest("formal-check", ResourceRef("trial:formal"), "eda.formal"),
        lease_id="lease-1",
        acquired_at="2026-06-23T00:00:00Z",
        expires_at="2026-06-23T19:05:00-05:00",
    )

    early = leased.reap_expired("2026-06-24T00:04:59Z")
    reaped = leased.reap_expired("2026-06-24T00:05:01Z")

    assert early.active_leases == (grant,)
    assert early.available_units == 0
    assert reaped.active_leases == ()
    assert reaped.available_units == 1


def test_lease_pool_rejects_non_rfc3339_timestamps() -> None:
    pool = LeasePool("formal-license", "eda.formal", capacity_units=1)
    request = LeaseRequest("formal-check", ResourceRef("trial:formal"), "eda.formal")

    for acquired_at in (
        "2026-06-24 00:00:00Z",
        "2026-06-24T00:00:00",
        "2026-06-24T00:00:00+0000",
        "2026-06-24T00:00:00z",
        " 2026-06-24T00:00:00Z",
    ):
        with pytest.raises(ValueError, match="lease acquired_at must be an ISO datetime"):
            pool.acquire(
                request,
                lease_id="lease-invalid-acquired",
                acquired_at=acquired_at,
                expires_at="2026-06-24T00:05:00Z",
            )

    for expires_at in (
        "2026-06-24 00:05:00Z",
        "2026-06-24T00:05:00",
        "2026-06-24T00:05:00+0000",
        "2026-06-24T00:05:00z",
    ):
        with pytest.raises(ValueError, match="lease expires_at must be an ISO datetime"):
            pool.acquire(
                request,
                lease_id="lease-invalid-expires",
                acquired_at="2026-06-24T00:00:00Z",
                expires_at=expires_at,
            )

    leased, grant = pool.acquire(
        request,
        lease_id="lease-1",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )
    with pytest.raises(ValueError, match="lease now must be an ISO datetime"):
        leased.reap_expired("2026-06-24 00:04:00Z")

    assert grant.is_active_at("2026-06-24 00:04:00Z") is False


def test_lease_pool_release_requires_matching_fencing_epoch() -> None:
    pool, grant = LeasePool("synthesis-license", "eda.synthesis", capacity_units=1).acquire(
        LeaseRequest("synthesis-check", ResourceRef("trial:synthesis"), "eda.synthesis"),
        lease_id="lease-1",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )

    with pytest.raises(LeaseEpochMismatchError) as mismatch:
        pool.release("lease-1", fencing_epoch=grant.fencing_epoch + 1)

    assert mismatch.value.expected_epoch == grant.fencing_epoch
    assert mismatch.value.actual_epoch == grant.fencing_epoch + 1
    with pytest.raises(LeasePoolCapacityError, match="fencing_epoch must be positive"):
        pool.release("lease-1", fencing_epoch=True)
    assert pool.active_leases == (grant,)
    assert pool.release("lease-1", fencing_epoch=grant.fencing_epoch).available_units == 1
