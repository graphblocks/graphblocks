from __future__ import annotations

from typing import Any

from graphblocks.runtime import CancellationToken, InProcessRuntime, RuntimeRegistry


def test_pre_cancelled_runtime_starts_no_nodes() -> None:
    registry = RuntimeRegistry(allow_untyped=True)
    registry.register("test.value@1", lambda inputs, config, context: {"value": "late"})
    token = CancellationToken()
    token.cancel("user")
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "pre-cancelled"},
        "spec": {
            "nodes": {
                "value": {
                    "block": "test.value@1",
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry, cancellation_token=token).run(graph, {})

    assert result.status == "cancelled"
    assert result.outputs == {}
    assert [record.kind for record in result.journal.records] == ["run_started", "run_cancelled"]


def test_runtime_stops_before_next_node_after_cancellation() -> None:
    calls: list[str] = []
    registry = RuntimeRegistry(allow_untyped=True)

    def cancels(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        calls.append("cancel")
        context["cancellation_token"].cancel("block")
        return {"value": "cancelled"}

    def late(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        calls.append("late")
        return {"value": inputs["value"]}

    registry.register("test.cancel@1", cancels)
    registry.register("test.late@1", late)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "mid-cancelled"},
        "spec": {
            "nodes": {
                "cancel": {
                    "block": "test.cancel@1",
                    "outputs": {"value": "$output.value"},
                },
                "late": {
                    "block": "test.late@1",
                    "inputs": {"value": "cancel.value"},
                    "outputs": {"value": "$output.lateValue"},
                },
            }
        },
    }

    result = InProcessRuntime(registry, cancellation_token=CancellationToken()).run(graph, {})

    assert result.status == "cancelled"
    assert result.outputs == {"value": "cancelled"}
    assert calls == ["cancel"]
    assert result.journal.terminal_kind == "run_cancelled"


def test_cancellation_token_cancel_is_idempotent() -> None:
    token = CancellationToken()

    token.cancel("first")
    token.cancel("second")

    assert token.cancelled
    assert token.reason == "first"
