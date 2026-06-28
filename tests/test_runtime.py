from __future__ import annotations

import pytest

from graphblocks.runtime import (
    ExecutionJournal,
    InProcessRuntime,
    JournalStateError,
    RuntimeRegistry,
    SQLiteExecutionJournal,
    stdlib_registry,
)
from graphblocks.run_store import InMemoryRunStore, SQLiteRunStore


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


def test_stdlib_policy_stop_turn_blocks_late_commit() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "policy-stopped-turn"},
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
                    "config": {"script": {"Answer: Hello": "blocked answer"}},
                    "inputs": {"prompt": "render.prompt"},
                },
                "stop": {
                    "block": "conversation.policy_stop_turn@1",
                    "inputs": {"transaction": "begin.transaction"},
                },
                "commit": {
                    "block": "conversation.commit_turn@1",
                    "inputs": {
                        "transaction": "stop.transaction",
                        "candidate": "generate.response",
                    },
                    "outputs": {"answer": "$output.answer"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {"message": {"text": "Hello"}})

    assert result.status == "failed"
    assert result.outputs == {}
    assert result.journal.terminal_kind == "run_failed"
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "commit"
    assert failed[0].payload["error"] == "conversation.commit_turn@1 cannot commit policy-stopped turn"


def test_stdlib_runtime_executes_tool_resolution_and_agent_run() -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "agent-turn"},
        "spec": {
            "interface": {
                "inputs": {"messages": "graphblocks.ai/Messages@1"},
                "outputs": {
                    "candidate": "graphblocks.ai/TurnCandidate@1",
                    "tools": "graphblocks.ai/ResolvedTools@1",
                },
            },
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1",
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-search",
                                "toolName": "knowledge.search",
                                "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                "effects": ["external_read"],
                                "approval": "never",
                                "timeoutMs": 250,
                            }
                        ],
                        "scope": {"principalTools": ["knowledge.search"]},
                    },
                    "outputs": {"tools": "$output.tools"},
                },
                "agent": {
                    "block": "agent.run@1",
                    "config": {"response": "Hello from the agent."},
                    "inputs": {
                        "messages": "$input.messages",
                        "tools": "resolve.tools",
                    },
                    "outputs": {"candidate": "$output.candidate"},
                },
            },
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(
        graph,
        {"messages": [{"role": "user", "content": "Hello"}]},
    )

    assert result.status == "succeeded"
    assert result.outputs["candidate"] == {
        "text": "Hello from the agent.",
        "finishReason": "scripted",
        "toolCount": 1,
    }
    assert result.outputs["tools"][0]["definition"]["name"] == "knowledge.search"
    assert result.outputs["tools"][0]["allowed_for_principal"] is True
    assert result.outputs["tools"][0]["binding"]["timeout_ms"] == 250


@pytest.mark.parametrize("timeout_ms", [True, "250"])
def test_stdlib_tool_resolution_rejects_non_integer_timeout_ms(timeout_ms: object) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "tool-timeout-ms-validation"},
        "spec": {
            "nodes": {
                "resolve": {
                    "block": "tools.resolve@1",
                    "config": {
                        "effectivePolicySnapshotId": "policy-snapshot-1",
                        "definitions": [
                            {
                                "name": "knowledge.search",
                                "description": "Search support documentation.",
                                "inputSchema": "schemas/SearchRequest@1",
                            }
                        ],
                        "bindings": [
                            {
                                "bindingId": "binding-search",
                                "toolName": "knowledge.search",
                                "implementation": {"kind": "block", "block": "knowledge.search@1"},
                                "effects": ["external_read"],
                                "approval": "never",
                                "timeoutMs": timeout_ms,
                            }
                        ],
                        "scope": {"principalTools": ["knowledge.search"]},
                    },
                    "outputs": {"tools": "$output.tools"},
                }
            }
        },
    }

    result = InProcessRuntime(stdlib_registry()).run(graph, {})

    assert result.status == "failed"
    assert result.outputs == {}
    failed = [record for record in result.journal.records if record.kind == "node_failed"]
    assert failed[0].payload["node"] == "resolve"
    assert "tool timeout_ms must be non-negative" in failed[0].payload["error"]


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


def test_runtime_does_not_coerce_non_numeric_retry_attempts() -> None:
    attempts = {"count": 0}
    registry = RuntimeRegistry()

    def flaky_block(inputs, config, context):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"value": "ok"}

    registry.register("test.flaky@1", flaky_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "non-numeric-retry-runtime"},
        "spec": {
            "nodes": {
                "flaky": {
                    "block": "test.flaky@1",
                    "flow": {"retry": {"maxAttempts": "2"}},
                    "outputs": {"value": "$output.value"},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert attempts["count"] == 1
    assert result.status == "failed"
    assert result.outputs == {}
    assert result.journal.terminal_kind == "run_failed"
    assert "node_retry" not in [record.kind for record in result.journal.records]


def test_runtime_ignores_malformed_retry_attempts_without_crashing() -> None:
    registry = RuntimeRegistry()

    def failing_block(inputs, config, context):
        raise RuntimeError("failed once")

    registry.register("test.fail@1", failing_block)
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "malformed-retry-runtime"},
        "spec": {
            "nodes": {
                "fail": {
                    "block": "test.fail@1",
                    "flow": {"retry": {"maxAttempts": "two"}},
                }
            }
        },
    }

    result = InProcessRuntime(registry).run(graph, {})

    assert result.status == "failed"
    assert result.journal.terminal_kind == "run_failed"
    assert [record.kind for record in result.journal.records] == [
        "run_started",
        "node_started",
        "node_failed",
        "run_failed",
    ]


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


def test_runtime_updates_supplied_sqlite_run_store_status(tmp_path) -> None:
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "stored-sqlite-run"},
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
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")

    result = InProcessRuntime(stdlib_registry(), run_store=store).run(graph, {"message": {"text": "hello"}})

    assert result.run_id == "run-000001"
    assert store.get_run(result.run_id).status == "succeeded"


def test_runtime_can_persist_execution_journal_with_factory(tmp_path) -> None:
    database = tmp_path / "journal.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "persisted-journal"},
        "spec": {
            "nodes": {
                "render": {
                    "block": "prompt.render@1",
                    "config": {"template": "Journal {message.text}"},
                    "inputs": {"message": "$input.message"},
                    "outputs": {"prompt": "$output.prompt"},
                }
            }
        },
    }

    result = InProcessRuntime(
        stdlib_registry(),
        journal_factory=lambda run_id: SQLiteExecutionJournal(database, run_id),
    ).run(graph, {"message": {"text": "hello"}})
    persisted = SQLiteExecutionJournal(database, result.run_id)

    assert result.status == "succeeded"
    assert persisted.terminal_kind == "run_succeeded"
    assert [record.kind for record in persisted.records] == [
        "run_started",
        "node_started",
        "node_succeeded",
        "run_succeeded",
    ]
