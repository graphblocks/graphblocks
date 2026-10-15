from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import graphblocks.runtime as runtime_module
from graphblocks.runtime import (
    CancellationToken,
    InProcessRuntime,
    core_stdlib_registry,
)


def _graph(block_id: str, config: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    input_name, output_name = "prompt", "response"
    for _ in range(depth):
        config = {
            "block": block_id,
            "inputName": input_name,
            "outputName": output_name,
            "config": config,
        }
        block_id, input_name, output_name = "control.map@2", "items", "values"
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "invocation-contract"},
        "spec": {
            "nodes": {
                "call": {
                    "block": block_id,
                    "config": config,
                    "inputs": {input_name: "$input.value"},
                    "outputs": {output_name: "$output.value"},
                }
            }
        },
    }


@pytest.mark.parametrize("depth", [0, 1, 2])
@pytest.mark.parametrize(
    ("returned", "error"),
    [
        ([], "block returned non-mapping output"),
        (
            {"response": 1, "extra": 2},
            "model.generate@1 returned undeclared output(s): extra",
        ),
        ({}, "model.generate@1 omitted required output(s): response"),
    ],
)
def test_top_level_and_nested_calls_enforce_the_same_output_contract(
    depth: int, returned: Any, error: str
) -> None:
    registry = core_stdlib_registry()
    registry.replace("model.generate@1", lambda inputs, config, context: returned)
    value: Any = "item"
    for _ in range(depth):
        value = [value]

    result = InProcessRuntime(registry).run(
        _graph("model.generate@1", {}, depth=depth), {"value": value}
    )

    assert result.status == "failed"
    assert result.journal.records[-1].payload["error"] == error


@pytest.mark.parametrize("nested", [False, True])
def test_required_outputs_use_approved_config_after_handler_mutation(
    nested: bool,
) -> None:
    registry = core_stdlib_registry()

    def mutates_config(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        config["onError"] = "fail_fast"
        config["config"]["template"] = "changed"
        return {"values": []}

    registry.replace("control.map@2", mutates_config)
    config = {
        "block": "prompt.render@1",
        "onError": "collect",
        "config": {"template": "approved"},
    }
    if nested:
        # Keep the real map as the parent, then replace only its child via a
        # separate declared ID with the same conditional output descriptor.
        from graphblocks.plugins import BlockCatalog, BlockDescriptor

        child_id = "example.conditional_map@2"
        descriptor = registry.block_catalog.descriptors["control.map@2"]
        child_descriptor = BlockDescriptor(
            "example.conditional_map",
            2,
            inputs=descriptor.inputs,
            outputs=descriptor.outputs,
            config_schema=descriptor.config_schema,
        )
        registry.block_catalog = BlockCatalog(
            {**registry.block_catalog.descriptors, child_id: child_descriptor}
        )
        registry.register(child_id, mutates_config)
        # A fresh factory supplies the real parent map bound to this registry.
        from graphblocks.stdlib_runtime_handlers import _control_map_handler

        registry.replace(
            "control.map@2", _control_map_handler(registry._resolve_invocation)
        )
        config = {
            "block": child_id,
            "inputName": "items",
            "config": config,
        }
    graph = _graph("control.map@2", config)
    graph["spec"]["nodes"]["call"]["inputs"] = {"items": "$input.value"}
    graph["spec"]["nodes"]["call"]["outputs"] = {"values": "$output.value"}
    original = deepcopy(graph)

    result = InProcessRuntime(registry).run(graph, {"value": [[]]})

    assert result.status == "failed"
    assert (
        "omitted required output(s): outcomes"
        in result.journal.records[-1].payload["error"]
    )
    assert graph == original


def test_retry_receives_fresh_nested_config_and_inputs() -> None:
    registry = core_stdlib_registry()
    seen: list[tuple[int, list[Any]]] = []

    def flaky(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        seen.append((config["script"]["counter"], list(inputs["prompt"]["parts"])))
        config["script"]["counter"] += 1
        inputs["prompt"]["parts"].append("mutated")
        if len(seen) == 1:
            raise RuntimeError("retry")
        return {"response": "ok"}

    registry.replace("model.generate@1", flaky)
    graph = _graph("model.generate@1", {"script": {"counter": 0}})
    graph["spec"]["nodes"]["call"]["flow"] = {"retry": {"maxAttempts": 2}}
    inputs = {"value": {"parts": ["approved"]}}

    result = InProcessRuntime(registry).run(graph, inputs)

    assert result.status == "succeeded"
    assert seen == [(0, ["approved"]), (0, ["approved"])]
    assert graph["spec"]["nodes"]["call"]["config"] == {"script": {"counter": 0}}
    assert inputs == {"value": {"parts": ["approved"]}}


def test_direct_map_isolates_nested_item_config_and_input_ownership() -> None:
    registry = core_stdlib_registry()
    seen: list[tuple[int, list[Any]]] = []

    def mutates(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        seen.append((config["script"]["counter"], list(inputs["prompt"]["parts"])))
        config["script"]["counter"] += 1
        inputs["prompt"]["parts"].append("mutated")
        return {"response": "ok"}

    registry.replace("model.generate@1", mutates)
    item = {"parts": ["approved"]}
    config = {
        "block": "model.generate@1",
        "inputName": "prompt",
        "config": {"script": {"counter": 0}},
    }

    result = registry.resolve("control.map@2")({"items": [item, item]}, config, {})

    assert len(result["values"]) == 2
    assert seen == [(0, ["approved"]), (0, ["approved"])]
    assert item == {"parts": ["approved"]}
    assert config["config"] == {"script": {"counter": 0}}


@pytest.mark.parametrize("nested", [False, True])
def test_map_snapshots_reused_nested_result_buffers(nested: bool) -> None:
    registry = core_stdlib_registry()
    buffer = {"response": {"parts": [0]}}

    def reuses_buffer(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        buffer["response"]["parts"][0] = inputs["prompt"]
        return buffer

    registry.replace("model.generate@1", reuses_buffer)
    config: dict[str, Any] = {
        "block": "model.generate@1",
        "inputName": "prompt",
        "outputName": "response",
        "onError": "collect",
    }
    items: Any = [1, 2, 3]
    if nested:
        config = {
            "block": "control.map@2",
            "inputName": "items",
            "config": config,
            "outputName": "values",
            "onError": "collect",
        }
        items = [[1, 2], [3]]

    result = registry.resolve("control.map@2")({"items": items}, config, {})

    expected: Any = [{"parts": [1]}, {"parts": [2]}, {"parts": [3]}]
    if nested:
        expected = [expected[:2], expected[2:]]
    assert result["values"] == expected
    assert [item["value"] for item in result["outcomes"]] == expected
    buffer["response"]["parts"][0] = 99
    assert result["values"] == expected
    result["values"][0].clear()
    assert [item["value"] for item in result["outcomes"]] == expected


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("collect", [False, True])
@pytest.mark.parametrize("raises", [False, True])
def test_map_cancellation_stops_new_calls_including_collect_and_nested_maps(
    depth: int, collect: bool, raises: bool
) -> None:
    registry = core_stdlib_registry()
    calls: list[Any] = []

    def cancels(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        calls.append(inputs["prompt"])
        context["cancellation_token"].cancel("stop map")
        if raises:
            raise RuntimeError("item failed after cancellation")
        return {"response": "ignored"}

    registry.replace("model.generate@1", cancels)
    graph = _graph("model.generate@1", {}, depth=depth)
    config = graph["spec"]["nodes"]["call"]["config"]
    for _ in range(depth):
        config["onError"] = "collect" if collect else "fail_fast"
        config = config["config"]
    value = [1, 2, 3] if depth == 1 else [[1, 2], [3, 4]]

    result = InProcessRuntime(registry, cancellation_token=CancellationToken()).run(
        graph, {"value": value}
    )

    assert result.status == "cancelled"
    assert result.outputs == {}
    assert calls == [1]
    assert result.journal.terminal_kind == "run_cancelled"


@pytest.mark.parametrize("pre_cancelled", [False, True])
def test_direct_map_does_not_collect_cancellation_or_start_another_item(
    pre_cancelled: bool,
) -> None:
    registry = core_stdlib_registry()
    token = CancellationToken()
    calls: list[Any] = []

    def cancels(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        calls.append(inputs["prompt"])
        token.cancel("stop direct map")
        return {"response": "ignored"}

    registry.replace("model.generate@1", cancels)
    if pre_cancelled:
        token.cancel("stop direct map")

    with pytest.raises(RuntimeError, match="stop direct map"):
        registry.resolve("control.map@2")(
            {"items": [1, 2, 3]},
            {"block": "model.generate@1", "inputName": "prompt", "onError": "collect"},
            {"cancellation_token": token},
        )

    assert calls == ([] if pre_cancelled else [1])


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("collect", [False, True])
def test_map_deadline_stops_new_calls_including_collect_and_nested_maps(
    monkeypatch: pytest.MonkeyPatch, depth: int, collect: bool
) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(runtime_module.time, "perf_counter", lambda: now["value"])
    registry = core_stdlib_registry()
    calls: list[Any] = []

    def expires(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        calls.append(inputs["prompt"])
        now["value"] += 0.002
        return {"response": "late"}

    registry.replace("model.generate@1", expires)
    graph = _graph("model.generate@1", {}, depth=depth)
    graph["spec"]["nodes"]["call"]["flow"] = {"timeout": "1ms"}
    config = graph["spec"]["nodes"]["call"]["config"]
    for _ in range(depth):
        config["onError"] = "collect" if collect else "fail_fast"
        config = config["config"]
    value = [1, 2, 3] if depth == 1 else [[1, 2], [3, 4]]

    result = InProcessRuntime(registry).run(graph, {"value": value})

    assert result.status == "failed"
    assert calls == [1]
    assert "timeout" in result.journal.records[-1].payload["error"]


def test_nested_map_assigns_a_distinct_child_invocation_identity() -> None:
    registry = core_stdlib_registry()
    identities: list[str] = []

    def observes(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        identities.append(context["invocation_id"])
        return {"response": inputs["prompt"]}

    registry.replace("model.generate@1", observes)

    result = InProcessRuntime(registry).run(
        _graph("model.generate@1", {}, depth=2), {"value": [[1, 2], [3]]}
    )

    assert result.status == "succeeded"
    assert identities == [
        "call/attempt-1/map-0/map-0",
        "call/attempt-1/map-0/map-1",
        "call/attempt-1/map-1/map-0",
    ]
