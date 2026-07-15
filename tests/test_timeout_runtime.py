from __future__ import annotations

import math
from typing import Any

import pytest

import graphblocks.runtime as runtime_module
from graphblocks.compiler import compile_graph
from graphblocks.plugins import BlockCatalog
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry, parse_duration_seconds


def test_parse_duration_seconds_supports_common_units() -> None:
    assert parse_duration_seconds("250ms") == 0.25
    assert parse_duration_seconds("2s") == 2
    assert parse_duration_seconds("3m") == 180
    assert parse_duration_seconds("1h") == 3600


def test_parse_duration_seconds_rejects_unsupported_values() -> None:
    for value in (
        True,
        False,
        "soon",
        {"seconds": 1},
        0,
        -1,
        "0s",
        "-1s",
        math.nan,
        math.inf,
        "nan",
        "inf",
        "1_000ms",
        "1_0e1s",
        "١s",
        10**1000,
    ):
        assert parse_duration_seconds(value) is None


def test_runtime_provides_timeout_deadline_to_block_context() -> None:
    seen_deadline = {"value": None}
    registry = RuntimeRegistry(allow_untyped=True)

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


def test_runtime_fails_node_that_exceeds_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runtime_module.time, "perf_counter", lambda: now["value"])
    registry = RuntimeRegistry(allow_untyped=True)

    def slow_block(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        now["value"] += 0.002
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


def test_runtime_exposes_expired_timeout_through_attempt_cancellation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runtime_module.time, "perf_counter", lambda: now["value"])
    seen: dict[str, object] = {}
    registry = RuntimeRegistry(allow_untyped=True)

    def cooperative_block(
        inputs: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        now["value"] += 0.002
        token = context["cancellation_token"]
        seen["cancelled"] = token.cancelled
        seen["reason"] = token.reason
        return {"value": "late"}

    registry.register("test.cooperative-timeout@1", cooperative_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "cooperative-timeout"},
        "spec": {
            "nodes": {
                "slow": {
                    "block": "test.cooperative-timeout@1",
                    "flow": {"timeout": "1ms"},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert seen == {
        "cancelled": True,
        "reason": "node 'slow' exceeded timeout 1ms",
    }


@pytest.mark.parametrize(
    "timeout",
    (
        "soon",
        "0s",
        "-1s",
        "nan",
        "inf",
        "1_000ms",
        "١s",
        pytest.param(10**1000, id="overflowing-integer"),
    ),
)
def test_compile_and_runtime_reject_invalid_timeout_before_invoking_block(
    timeout: object,
) -> None:
    invoked = False
    registry = RuntimeRegistry(allow_untyped=True)

    def observes_deadline(inputs: dict[str, Any], config: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
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
                    "flow": {"timeout": timeout},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    plan = compile_graph(
        graph,
        block_catalog=BlockCatalog({}, allow_unknown_blocks=True),
    )

    assert [item.code for item in plan.diagnostics.diagnostics if item.severity == "error"] == ["GB1019"]
    with pytest.raises(ValueError, match="GB1019.*flow.timeout must be a positive finite duration"):
        InProcessRuntime(registry).run(graph, {})
    assert invoked is False
