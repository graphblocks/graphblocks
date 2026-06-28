from __future__ import annotations

import time
from typing import Any

from graphblocks.runtime import InProcessRuntime, RuntimeRegistry, parse_duration_seconds


def test_parse_duration_seconds_supports_common_units() -> None:
    assert parse_duration_seconds("250ms") == 0.25
    assert parse_duration_seconds("2s") == 2
    assert parse_duration_seconds("3m") == 180
    assert parse_duration_seconds("1h") == 3600


def test_parse_duration_seconds_rejects_unsupported_values() -> None:
    assert parse_duration_seconds(True) is None
    assert parse_duration_seconds(False) is None
    assert parse_duration_seconds("soon") is None
    assert parse_duration_seconds({"seconds": 1}) is None


def test_runtime_provides_timeout_deadline_to_block_context() -> None:
    seen_deadline = {"value": None}
    registry = RuntimeRegistry()

    def observes_deadline(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        seen_deadline["value"] = context["deadline_monotonic"]
        return {"value": "ok"}

    registry.register("test.deadline@1", observes_deadline)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "deadline-context"},
        "spec": {
            "nodes": {
                "deadline": {
                    "block": "test.deadline@1",
                    "flow": {"timeout": "5s"},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "succeeded"
    assert isinstance(seen_deadline["value"], float)


def test_runtime_fails_node_that_exceeds_timeout() -> None:
    registry = RuntimeRegistry()

    def slow_block(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.02)
        return {"value": "late"}

    registry.register("test.slow@1", slow_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "timeout-failure"},
        "spec": {
            "nodes": {
                "slow": {
                    "block": "test.slow@1",
                    "flow": {"timeout": "1ms"},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    assert result.journal.terminal_kind == "run_failed"
    assert "timeout" in result.journal.records[-1].payload["error"]


def test_runtime_ignores_malformed_timeout_without_crashing() -> None:
    seen_deadline = {"value": "unset"}
    registry = RuntimeRegistry()

    def observes_deadline(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        seen_deadline["value"] = context["deadline_monotonic"]
        return {"value": "ok"}

    registry.register("test.malformed-timeout@1", observes_deadline)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-timeout"},
        "spec": {
            "nodes": {
                "deadline": {
                    "block": "test.malformed-timeout@1",
                    "flow": {"timeout": "soon"},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "succeeded"
    assert result.outputs == {"value": "ok"}
    assert seen_deadline["value"] is None
