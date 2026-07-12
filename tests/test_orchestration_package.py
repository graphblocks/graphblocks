from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import graphblocks
from graphblocks.orchestration import VALID_CONTEXT_ACCESS_MODES
from graphblocks.policy import ResourceRef


ROOT = Path(__file__).parents[1]


def test_orchestration_package_preserves_retired_facade_wildcard_exports() -> None:
    graphblocks_orchestration = importlib.import_module("graphblocks.orchestration")
    exported: dict[str, object] = {}

    exec("from graphblocks.orchestration import *", exported)

    assert "ContextAccessMode" in graphblocks_orchestration.__all__
    assert exported["ContextAccessMode"] is graphblocks_orchestration.ContextAccessMode


def test_orchestration_package_reexports_task_and_pool_contracts(monkeypatch) -> None:
    graphblocks_orchestration = importlib.import_module("graphblocks.orchestration")

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
    assert graphblocks_orchestration.VALID_CONTEXT_ACCESS_MODES is VALID_CONTEXT_ACCESS_MODES
    assert "VALID_CONTEXT_ACCESS_MODES" in graphblocks_orchestration.__all__
    assert graphblocks.VALID_CONTEXT_ACCESS_MODES is VALID_CONTEXT_ACCESS_MODES
    assert "VALID_CONTEXT_ACCESS_MODES" in graphblocks.__all__


def test_orchestration_package_lazy_native_helpers_delegate_to_runtime(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def evaluate_scheduler(nodes: object, operations: object) -> dict[str, object]:
        calls.append(("scheduler", (nodes, operations)))
        return {"kind": "scheduler", "nodes": nodes, "operations": operations}

    def evaluate_cancellation_scope(root: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("cancellation", (root, operations)))
        return {"kind": "cancellation", "root": root, "operations": operations}

    def evaluate_task_group(group: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("task_group", (group, operations)))
        return {"kind": "task_group", "group": group, "operations": operations}

    def evaluate_node_lifecycle(state: dict[str, object], operations: object) -> dict[str, object]:
        calls.append(("node_lifecycle", (state, operations)))
        return {"kind": "node_lifecycle", "state": state, "operations": operations}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(
            evaluate_cancellation_scope=evaluate_cancellation_scope,
            evaluate_node_lifecycle=evaluate_node_lifecycle,
            evaluate_scheduler=evaluate_scheduler,
            evaluate_task_group=evaluate_task_group,
        ),
    )
    graphblocks_orchestration = importlib.import_module("graphblocks.orchestration")

    scheduler = graphblocks_orchestration.evaluate_native_scheduler(
        [{"nodeId": "render"}],
        [{"op": "admit_run"}],
    )
    cancellation = graphblocks_orchestration.evaluate_native_cancellation_scope(
        {"scope": "run", "guarantee": "cooperative"},
        [{"op": "cancel", "reason": "policy"}],
    )
    task_group = graphblocks_orchestration.evaluate_native_task_group(
        {"children": ["a", "b"], "minimumSuccesses": 1},
        [{"op": "child_failed", "childId": "a", "error": "boom"}],
    )
    lifecycle = graphblocks_orchestration.evaluate_native_node_lifecycle(
        {"status": "pending"},
        [{"op": "start"}],
    )

    assert scheduler == {
        "kind": "scheduler",
        "nodes": [{"nodeId": "render"}],
        "operations": [{"op": "admit_run"}],
    }
    assert cancellation == {
        "kind": "cancellation",
        "root": {"scope": "run", "guarantee": "cooperative"},
        "operations": [{"op": "cancel", "reason": "policy"}],
    }
    assert task_group == {
        "kind": "task_group",
        "group": {"children": ["a", "b"], "minimumSuccesses": 1},
        "operations": [{"op": "child_failed", "childId": "a", "error": "boom"}],
    }
    assert lifecycle == {
        "kind": "node_lifecycle",
        "state": {"status": "pending"},
        "operations": [{"op": "start"}],
    }
    assert calls == [
        ("scheduler", ([{"nodeId": "render"}], [{"op": "admit_run"}])),
        (
            "cancellation",
            (
                {"scope": "run", "guarantee": "cooperative"},
                [{"op": "cancel", "reason": "policy"}],
            ),
        ),
        (
            "task_group",
            (
                {"children": ["a", "b"], "minimumSuccesses": 1},
                [{"op": "child_failed", "childId": "a", "error": "boom"}],
            ),
        ),
        ("node_lifecycle", ({"status": "pending"}, [{"op": "start"}])),
    ]
    assert "evaluate_native_cancellation_scope" in graphblocks_orchestration.__all__
    assert "evaluate_native_node_lifecycle" in graphblocks_orchestration.__all__
    assert "evaluate_native_scheduler" in graphblocks_orchestration.__all__
    assert "evaluate_native_task_group" in graphblocks_orchestration.__all__
