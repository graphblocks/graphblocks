from __future__ import annotations

from typing import Any

from graphblocks.leases import InMemoryLeasePool
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry


def test_runtime_releases_owner_leases_after_success() -> None:
    pool = InMemoryLeasePool({"model": 1})
    registry = RuntimeRegistry()

    def leases_resource(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        context["lease_pool"].acquire("model", owner=context["run_id"])
        return {"value": "ok"}

    registry.register("test.lease@1", leases_resource)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "lease-success"},
        "spec": {
            "nodes": {
                "lease": {
                    "block": "test.lease@1",
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry, lease_pool=pool).run(graph, {})

    assert result.status == "succeeded"
    assert pool.available("model") == 1


def test_runtime_releases_owner_leases_after_failure() -> None:
    pool = InMemoryLeasePool({"model": 1})
    registry = RuntimeRegistry()

    def leases_then_fails(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        context["lease_pool"].acquire("model", owner=context["run_id"])
        raise RuntimeError("failed")

    registry.register("test.lease_fail@1", leases_then_fails)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "lease-failure"},
        "spec": {"nodes": {"lease": {"block": "test.lease_fail@1"}}},
    }

    result = InProcessRuntime(registry, lease_pool=pool).run(graph, {})

    assert result.status == "failed"
    assert pool.available("model") == 1

