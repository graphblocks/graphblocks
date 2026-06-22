from __future__ import annotations

import pytest

from graphblocks.runtime import (
    ExecutionJournal,
    InProcessRuntime,
    JournalStateError,
    RuntimeRegistry,
    stdlib_registry,
)
from graphblocks.run_store import InMemoryRunStore


def test_runtime_executes_conversation_vertical_slice() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "chat-vertical-slice"},
        "spec": {
            "interface": {
                "inputs": {"message": "graphblocks.ai/Message@1"},
                "outputs": {"answer": "graphblocks.ai/Answer@1"},
            },
            "nodes": {
                "begin": {"block": "conversation.begin_turn@1"},
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Answer: {message.text}"},
                    "inputs": {"message": "$input.message"},
                },
                "generate": {
                    "block": "model.generate@1",
                    "config": {"script": {"Answer: Hello": "Hello from the scripted model."}},
                    "inputs": {"prompt": "render.prompt"},
                },
                "commit": {
                    "block": "conversation.commit_turn@1",
                    "inputs": {
                        "transaction": "begin.transaction",
                        "candidate": "generate.response",
                    },
                    "outputs": {"answer": "$output.answer"},
                },
            },
        },
    }
    runtime = InProcessRuntime(stdlib_registry())

    result = runtime.run(graph, {"message": {"text": "Hello"}})

    assert result.status == "succeeded"
    assert result.outputs == {
        "answer": {
            "conversationId": "conversation-default",
            "text": "Hello from the scripted model.",
            "turnId": "turn-000001",
        }
    }
    assert [record.kind for record in result.journal.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]


def test_journal_rejects_second_terminal_record() -> None:
    journal = ExecutionJournal("run-test")

    journal.append_terminal("run_succeeded", {"outputs": {}})

    with pytest.raises(JournalStateError):
        journal.append_terminal("run_failed", {"error": "late"})


def test_journal_rejects_output_after_terminal() -> None:
    journal = ExecutionJournal("run-test")

    journal.append_terminal("run_succeeded", {"outputs": {}})

    with pytest.raises(JournalStateError):
        journal.append("node_succeeded", {"node": "late"})


def test_runtime_fails_when_block_is_not_registered() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "missing-block"},
        "spec": {
            "nodes": {"missing": {"block": "missing.block@1"}},
            "edges": [{"from": "missing.value", "to": "$output.value"}],
        },
    }

    result = InProcessRuntime(RuntimeRegistry()).run(graph, {})

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"


def test_runtime_updates_supplied_run_store_status() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "stored-run"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Stored {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }
    store = InMemoryRunStore()

    result = InProcessRuntime(stdlib_registry(), run_store=store).run(graph, {"message": {"text": "hello"}})

    assert result.run_id == "run-000001"
    assert store.get_run(result.run_id).status == "succeeded"
