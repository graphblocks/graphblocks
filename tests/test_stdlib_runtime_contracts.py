from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from graphblocks import BlockCatalog
from graphblocks.plugins import builtin_block_catalog
from graphblocks.runtime import InProcessRuntime, RuntimeRegistry, stdlib_registry


ROOT = Path(__file__).parents[1]


EXPECTED_STDLIB_PORTS = {
    "prompt.render@1": (("message",), ("prompt",)),
    "model.generate@1": (("prompt", "context"), ("response",)),
    "model.structured_generate@1": (
        ("response", "diagnosis", "prompt", "context", "candidates", "questions", "reference"),
        ("value", "response", "items", "schemaId", "schemaRef", "contentDigest", "questions", "scores"),
    ),
    "tools.resolve@1": (("principal", "conversation", "policySnapshot"), ("tools",)),
    "agent.run@1": (
        ("messages", "tools", "context", "objective", "diagnostics", "conversation"),
        ("candidate", "result", "message"),
    ),
    "conversation.begin_turn@1": (
        ("conversationId", "conversation", "message"),
        ("transaction", "snapshot", "conversation", "turn"),
    ),
    "conversation.commit_turn@1": (
        ("transaction", "candidate", "turn", "response"),
        ("answer", "result"),
    ),
    "conversation.policy_stop_turn@1": (("transaction",), ("transaction", "turn")),
    "async.start_operation@1": (("subject", "changeset"), ("operation",)),
    "async.await_callback@1": (("operation",), ("wait", "callback", "operation")),
    "async.poll_operation@1": (("operation",), ("poll",)),
    "async.complete_operation@1": (("operation", "output"), ("result",)),
    "async.cancel_operation@1": (("operation",), ("result",)),
    "async.expire_operation@1": (("operation",), ("result",)),
    "control.map@2": (("items",), ("values", "outcomes")),
    "control.select@1": (("cases",), ("value", "selected")),
    "retrieve.fuse@1": (("sources",), ("hits", "metadata")),
    "retrieve.execute_plan@1": (
        ("query", "request", "auth", "sources"),
        ("result", "sources"),
    ),
    "rank.documents@1": (("query", "hits"), ("hits", "result")),
    "context.build@1": (("history", "evidence", "hits", "currentMessage"), ("pack",)),
    "answer.validate_grounding@1": (
        ("response", "answer", "context"),
        ("candidate", "response", "result", "validation"),
    ),
    "check.run_suite@1": (
        ("subject", "evidence", "results", "lease"),
        ("results", "checks", "diagnostics", "passed", "hardGatePassed"),
    ),
    "gate.evaluate@1": (
        ("checks", "metrics", "subject"),
        ("result", "decision", "passed", "violations"),
    ),
    "review.request@1": (
        ("subject", "gate", "review", "requestedBy", "requested_by"),
        ("request", "requestDigest", "record", "accepted", "approved", "status", "waitMode"),
    ),
    "result.bundle@1": (
        (
            "inputs",
            "outputs",
            "evidence",
            "checks",
            "metrics",
            "diagnostics",
            "reviews",
            "gate",
            "artifacts",
            "usage",
            "usageRecords",
            "policyDecisionRefs",
        ),
        ("result", "bundle", "contentDigest"),
    ),
}


def _single_node_graph(block_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "runtime-contract"},
        "spec": {"nodes": {"block": {"block": block_id}}},
    }


def _catalog_for(block_id: str, outputs: list[dict[str, Any]]) -> BlockCatalog:
    type_id, version = block_id.rsplit("@", 1)
    return BlockCatalog.from_blocks(
        [{"typeId": type_id, "version": int(version), "outputs": outputs}]
    )


def test_builtin_catalog_and_python_stdlib_have_exact_port_contract_parity() -> None:
    catalog = builtin_block_catalog()
    registry = stdlib_registry()

    assert set(catalog.descriptors) == set(registry.blocks) == set(EXPECTED_STDLIB_PORTS)
    for block_id, (expected_inputs, expected_outputs) in EXPECTED_STDLIB_PORTS.items():
        descriptor = catalog.get(block_id)
        assert descriptor is not None
        assert tuple(port.name for port in descriptor.inputs) == expected_inputs
        assert tuple(port.name for port in descriptor.outputs) == expected_outputs


def test_conversation_runtime_aliases_use_canonical_internal_types() -> None:
    catalog = builtin_block_catalog()
    begin = catalog.get("conversation.begin_turn@1")
    agent = catalog.get("agent.run@1")
    commit = catalog.get("conversation.commit_turn@1")
    assert begin is not None
    assert agent is not None
    assert commit is not None

    assert {port.name: port.type_ref for port in begin.inputs} == {
        "conversationId": "graphblocks.ai/ConversationId@1",
        "conversation": "graphblocks.conversation/ConversationRef@1",
        "message": "graphblocks.conversation/Message@1",
    }
    assert {port.name: port.type_ref for port in begin.outputs} == {
        "transaction": "graphblocks.ai/ConversationTransaction@1",
        "snapshot": "graphblocks.ai/ConversationSnapshot@1",
        "conversation": "graphblocks.ai/ConversationSnapshot@1",
        "turn": "graphblocks.ai/ConversationTransaction@1",
    }
    assert {port.name: port.type_ref for port in agent.outputs}["message"] == (
        "graphblocks.ai/TurnCandidate@1"
    )
    assert {port.name: port.type_ref for port in commit.inputs}["response"] == (
        "graphblocks.ai/TurnCandidate@1"
    )


def test_acceptance_multi_turn_chat_executes_stdlib_alias_chain() -> None:
    documents = yaml.safe_load_all(
        (ROOT / "acceptance/scenarios/multi-turn-chat.yaml").read_text(encoding="utf-8")
    )
    graph = next(document for document in documents if document.get("kind") == "Graph")

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {
            "conversation": {"conversationId": "conversation-42", "messages": []},
            "message": {"role": "user", "text": "hello"},
        },
    )

    assert result.status == "succeeded"
    succeeded = {
        record.payload["node"]: tuple(record.payload["outputs"])
        for record in result.journal.records
        if record.kind == "node_succeeded"
    }
    assert succeeded == {
        "beginTurn": ("conversation", "snapshot", "transaction", "turn"),
        "respond": ("candidate", "message", "result"),
        "commitTurn": ("answer", "result"),
    }


def test_structured_generate_projects_example_specific_optional_outputs() -> None:
    block = stdlib_registry().resolve("model.structured_generate@1")

    result = block(
        {},
        {
            "outputSchema": "graphblocks.evaluation/InterviewScoreSet@1",
            "response": {"questions": ["q1"], "scores": [{"score": 1.0}]},
        },
        {},
    )

    assert result["questions"] == ["q1"]
    assert result["scores"] == [{"score": 1.0}]


def test_runtime_registry_rejects_undeclared_and_duplicate_blocks() -> None:
    block_id = "example.echo@1"
    block = lambda inputs, config, context: {"value": inputs.get("value")}

    with pytest.raises(ValueError, match="not declared in the block catalog"):
        RuntimeRegistry().register(block_id, block)
    with pytest.raises(ValueError, match="not declared in the block catalog"):
        RuntimeRegistry(blocks={block_id: block})

    registry = RuntimeRegistry(
        block_catalog=_catalog_for(block_id, [{"name": "value", "type": "Any"}])
    )
    registry.register(block_id, block)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(block_id, block)


def test_untyped_runtime_requires_explicit_opt_in() -> None:
    block_id = "example.untyped@1"
    registry = RuntimeRegistry(allow_untyped=True)
    registry.register(block_id, lambda inputs, config, context: {"value": 1})

    result = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert result.status == "succeeded"


def test_stdlib_untyped_opt_in_keeps_known_contracts_and_allows_custom_blocks() -> None:
    block_id = "example.extension@1"
    registry = stdlib_registry(allow_untyped=True)
    registry.register(block_id, lambda inputs, config, context: {"value": 1})

    result = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert result.status == "succeeded"


@pytest.mark.parametrize("catalog_allows_unknown", [False, True])
def test_strict_empty_catalog_rejects_graph_before_execution(
    catalog_allows_unknown: bool,
) -> None:
    registry = RuntimeRegistry(
        block_catalog=BlockCatalog({}, allow_unknown_blocks=catalog_allows_unknown)
    )
    with pytest.raises(ValueError, match="GB1022"):
        InProcessRuntime(registry).run(
            _single_node_graph("example.undeclared@1"),
            {},
        )


@pytest.mark.parametrize(
    ("outputs", "result", "message"),
    [
        ([], {"extra": 1}, "returned undeclared output\\(s\\): extra"),
        (
            [{"name": "required", "type": "Any"}],
            {},
            "omitted required output\\(s\\): required",
        ),
    ],
)
@pytest.mark.parametrize("allow_untyped", [False, True])
def test_catalog_backed_runtime_enforces_output_contract(
    outputs: list[dict[str, Any]],
    result: dict[str, Any],
    message: str,
    allow_untyped: bool,
) -> None:
    block_id = "example.output_contract@1"
    registry = RuntimeRegistry(
        block_catalog=_catalog_for(block_id, outputs),
        allow_untyped=allow_untyped,
    )
    registry.register(block_id, lambda inputs, config, context: result)

    run = InProcessRuntime(registry).run(_single_node_graph(block_id), {})

    assert run.status == "failed"
    failure = next(record for record in run.journal.records if record.kind == "node_failed")
    assert re.search(message, str(failure.payload["error"]))
