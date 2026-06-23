from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import BudgetPermit, UsageAmount
from graphblocks.orchestration import (
    ChildBudgetDelegation,
    ModelPool,
    ModelPoolMismatchError,
    ModelProfile,
    ModelSelectionRequest,
    ModelSensitivityAboveCeilingError,
    ModelToolNotAllowedError,
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
