from __future__ import annotations

from typing import Any

from graphblocks.runtime import InProcessRuntime, RuntimeRegistry


def test_runtime_retries_node_until_success() -> None:
    attempts = {"count": 0}
    registry = RuntimeRegistry()

    def flaky_block(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary")
        return {"value": "ok"}

    registry.register("test.flaky@1", flaky_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "retry-success"},
        "spec": {
            "nodes": {
                "flaky": {
                    "block": "test.flaky@1",
                    "flow": {"retry": {"maxAttempts": 3}},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "succeeded"
    assert result.outputs == {"value": "ok"}
    assert attempts["count"] == 3
    assert [record.kind for record in result.journal.records].count("node_retry") == 2


def test_runtime_fails_after_retry_attempts_are_exhausted() -> None:
    attempts = {"count": 0}
    registry = RuntimeRegistry()

    def always_fails(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        attempts["count"] += 1
        raise RuntimeError("still failing")

    registry.register("test.always_fails@1", always_fails)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "retry-fail"},
        "spec": {
            "nodes": {
                "flaky": {
                    "block": "test.always_fails@1",
                    "flow": {"retry": {"maxAttempts": 2}},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert attempts["count"] == 2
    assert result.journal.terminal_kind == "run_failed"

