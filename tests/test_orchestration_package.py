from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks.policy import ResourceRef


ROOT = Path(__file__).parents[1]


def test_orchestration_package_reexports_task_and_pool_contracts(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-orchestration" / "src"))
    graphblocks_orchestration = importlib.import_module("graphblocks_orchestration")

    plan = graphblocks_orchestration.TaskPlan(
        plan_id="plan-1",
        objective="answer support request",
        steps=(graphblocks_orchestration.TaskStep("draft", "Draft response"),),
    )
    pool = graphblocks_orchestration.ModelPool("support-pool", "policy-1").with_models(
        [graphblocks_orchestration.ModelProfile("support", "models.support").with_capabilities(["chat"])]
    )
    request = graphblocks_orchestration.ModelSelectionRequest(
        graphblocks_orchestration.WorkerProfile("worker").with_required_capabilities(["chat"])
    )
    lease_pool = graphblocks_orchestration.LeasePool("formal-license", "eda.formal", capacity_units=1)
    leased, grant = lease_pool.acquire(
        graphblocks_orchestration.LeaseRequest(
            "formal-check",
            ResourceRef("trial:formal"),
            "eda.formal",
        ),
        lease_id="lease-1",
        acquired_at="2026-06-24T00:00:00Z",
        expires_at="2026-06-24T00:05:00Z",
    )

    assert plan.step("draft").description == "Draft response"
    assert pool.select_model(request).connection == "models.support"
    assert grant.fencing_epoch == 1
    assert leased.available_units == 0
    assert "TaskPlanIdentityError" in graphblocks_orchestration.__all__
