from __future__ import annotations

from typing import Any

import pytest

from graphblocks._canonical_reference import canonical_dumps
from graphblocks.leases import InMemoryLeasePool
from graphblocks.run_store import InMemoryRunStore
from graphblocks.runtime import InProcessRuntime, core_stdlib_registry


class UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("error formatter failed")


class HostileMessage(str):
    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError("message encode override called")


class HostileMessageError(Exception):
    def __str__(self) -> str:
        return HostileMessage("plain error text")


@pytest.mark.parametrize("attempts", (1, 2))
@pytest.mark.parametrize(
    "error",
    (UnprintableError(), HostileMessageError(), ValueError("bad\ud800text"), ValueError("😀" * 5_000)),
)
def test_error_messages_preserve_terminal_state_and_cleanup(
    error: Exception, attempts: int
) -> None:
    registry = core_stdlib_registry()
    leases = InMemoryLeasePool({"model": 1})
    store = InMemoryRunStore()
    calls = 0

    def fail(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        nonlocal calls
        if calls == 0:
            context["lease_pool"].acquire("model", owner=context["run_id"])
        calls += 1
        raise error

    registry.replace("model.generate@1", fail)
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "error-envelope"},
        "spec": {
            "nodes": {
                "call": {
                    "block": "model.generate@1",
                    "inputs": {"prompt": "$input.value"},
                    "flow": {"retry": {"maxAttempts": attempts}},
                }
            }
        },
    }
    result = InProcessRuntime(registry, run_store=store, lease_pool=leases).run(
        graph, {"value": "prompt"}
    )

    assert result.status == store.get_run(result.run_id).status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert calls == attempts
    assert leases.available("model") == 1
    for record in result.journal.records:
        if "error" in record.payload:
            message = record.payload["error"]
            assert len(message) <= 1_024
            assert len(message.encode("utf-8")) <= 4_096
            canonical_dumps(record.payload).encode("utf-8")


@pytest.mark.parametrize(
    "error", (UnprintableError(), ValueError("bad\ud800text"), ValueError("x" * 5_000))
)
def test_map_collect_uses_safe_bounded_error_messages(error: Exception) -> None:
    registry = core_stdlib_registry()

    def fail(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        raise error

    registry.replace("model.generate@1", fail)
    result = registry.resolve("control.map@2")(
        {"items": [1, 2]},
        {"block": "model.generate@1", "onError": "collect"},
        {},
    )

    assert result["values"] == []
    assert [outcome["status"] for outcome in result["outcomes"]] == ["failed", "failed"]
    for outcome in result["outcomes"]:
        assert len(outcome["error"]) <= 1_024
    canonical_dumps(result).encode("utf-8")


def test_terminal_records_share_one_exception_text_projection() -> None:
    class StatefulError(Exception):
        calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return f"failure-{self.calls}"

    error = StatefulError()
    registry = core_stdlib_registry()

    def fail(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        raise error

    registry.replace("model.generate@1", fail)
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "stateful-error"},
        "spec": {"nodes": {"call": {"block": "model.generate@1"}}},
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert error.calls == 1
    assert result.journal.records[-2].payload["error"] == "failure-1"
    assert result.journal.records[-1].payload["error"] == "failure-1"


def test_cancelled_attempt_skips_unused_exception_formatting() -> None:
    class ErrorAfterCancellation(Exception):
        calls = 0

        def __str__(self) -> str:
            self.calls += 1
            return "unused"

    error = ErrorAfterCancellation()
    registry = core_stdlib_registry()

    def cancel_then_fail(inputs: Any, config: Any, context: Any) -> dict[str, Any]:
        context["cancellation_token"].cancel("stop")
        raise error

    registry.replace("model.generate@1", cancel_then_fail)
    graph = {
        "apiVersion": "graphblocks.ai/v1",
        "kind": "Graph",
        "metadata": {"name": "cancelled-error"},
        "spec": {"nodes": {"call": {"block": "model.generate@1"}}},
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "cancelled"
    assert error.calls == 0
