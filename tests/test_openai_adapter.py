from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import ContentPart, Message, ToolCallError, ToolDefinition, UsageAmount


ROOT = Path(__file__).parents[1]


def _add_openai_package_paths(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-openai" / "src"))


def test_openai_chat_request_encodes_messages_tools_and_options(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")
    tool = ToolDefinition(
        name="knowledge.search",
        description="Search support docs.",
        input_schema="schemas/SearchRequest@1",
        output_schema="schemas/SearchResult@1",
        tags=frozenset({"search"}),
    )

    request = graphblocks_openai.openai_chat_completion_request(
        model="gpt-test",
        messages=(
            Message(
                message_id="msg-system",
                role="system",
                parts=(ContentPart(kind="text", text="You are precise."),),
            ),
            Message(
                message_id="msg-user",
                role="user",
                parts=(
                    ContentPart(kind="text", text="Find the refund policy."),
                    ContentPart(kind="json", data={"tenant": "acme"}),
                ),
            ),
        ),
        tools=(tool,),
        tool_choice="auto",
        temperature=0.2,
        max_tokens=128,
        stream=True,
        metadata={"run_id": "run-1"},
        extra_body={"parallel_tool_calls": True},
    )

    assert request.request_contract() == {
        "endpoint": "/chat/completions",
        "body": {
            "max_tokens": 128,
            "messages": [
                {"role": "system", "content": "You are precise."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Find the refund policy."},
                        {"type": "text", "text": '{"tenant":"acme"}'},
                    ],
                },
            ],
            "model": "gpt-test",
            "parallel_tool_calls": True,
            "stream": True,
            "temperature": 0.2,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "knowledge.search",
                        "description": "Search support docs.",
                        "parameters": {"$ref": "schemas/SearchRequest@1"},
                    },
                }
            ],
        },
        "metadata": {"run_id": "run-1"},
    }


def test_openai_chat_request_rejects_invalid_contract_inputs(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="model"):
        graphblocks_openai.openai_chat_completion_request(
            model=" ",
            messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
        )

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="max_tokens"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
            max_tokens=0,
        )


def test_openai_response_maps_text_choice_to_content_parts_and_usage(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    response = graphblocks_openai.openai_chat_response_from_provider(
        {
            "id": "chatcmpl-1",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Refunds are available."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
    )

    assert response.response_id == "chatcmpl-1"
    assert response.finish_reason == "stop"
    assert response.parts == (
        ContentPart(
            kind="text",
            text="Refunds are available.",
            metadata={"choice_index": 0, "provider": "openai-compatible"},
        ),
    )
    assert response.response_contract() == {
        "response_id": "chatcmpl-1",
        "model": "gpt-test",
        "finish_reason": "stop",
        "parts": [
            {
                "kind": "text",
                "text": "Refunds are available.",
                "metadata": {"choice_index": 0, "provider": "openai-compatible"},
            }
        ],
        "tool_calls": [],
        "usage": {"completion_tokens": 5, "prompt_tokens": 20, "total_tokens": 25},
    }


def test_openai_response_preserves_tool_call_arguments_as_drafts(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    response = graphblocks_openai.openai_chat_response_from_provider(
        {
            "id": "chatcmpl-2",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "knowledge.search",
                                    "arguments": '{"query":"refund"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    assert response.parts == ()
    assert response.tool_calls == [
        {
            "arguments": '{"query":"refund"}',
            "id": "call-1",
            "name": "knowledge.search",
            "type": "function",
        }
    ]
    assert response.finish_reason == "tool_calls"

    drafts = graphblocks_openai.openai_tool_call_drafts_from_response(response)

    assert len(drafts) == 1
    assert drafts[0].response_id == "chatcmpl-2"
    assert drafts[0].tool_call_id == "call-1"
    assert drafts[0].tool_name == "knowledge.search"
    assert drafts[0].argument_fragments == ('{"query":"refund"}',)
    assert drafts[0].status == "arguments_complete"


def test_openai_stream_chunk_normalizes_content_delta(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Ref"},
                    "finish_reason": None,
                }
            ],
        },
        sequence=7,
    )

    assert delta.delta_contract() == {
        "response_id": "chatcmpl-1",
        "sequence": 7,
        "choice_index": 0,
        "content_delta": "Ref",
        "tool_call_deltas": [],
        "finish_reason": None,
        "usage_delta": {},
    }


def test_openai_stream_chunk_normalizes_usage_only_final_chunk(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-usage",
            "choices": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        },
        sequence=99,
    )

    assert delta.delta_contract() == {
        "response_id": "chatcmpl-usage",
        "sequence": 99,
        "choice_index": None,
        "content_delta": None,
        "tool_call_deltas": [],
        "finish_reason": None,
        "usage_delta": {"completion_tokens": 5, "prompt_tokens": 20, "total_tokens": 25},
    }


def test_openai_provider_usage_converts_to_usage_record(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")
    response = graphblocks_openai.openai_chat_response_from_provider(
        {
            "id": "chatcmpl-usage",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Done."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
    )

    record = graphblocks_openai.openai_usage_record_from_response(
        response,
        record_id="usage-final",
        run_id="run-1",
        attempt_id="attempt-1",
        occurred_at="2026-06-23T00:00:02Z",
        execution_scope="turn:turn-1/model:gpt-test",
    )

    assert record.record_id == "usage-final"
    assert record.source == "provider_reported"
    assert record.confidence == "provider_exact"
    assert record.run_id == "run-1"
    assert record.attempt_id == "attempt-1"
    assert record.provider_response_id == "chatcmpl-usage"
    assert record.execution_scope == "turn:turn-1/model:gpt-test"
    assert record.metadata == {
        "finish_reason": "stop",
        "model": "gpt-test",
        "provider": "openai-compatible",
    }
    assert record.amounts == (
        UsageAmount(
            "model_input_tokens",
            20,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
        UsageAmount(
            "model_output_tokens",
            5,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
        UsageAmount(
            "model_total_tokens",
            25,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
    )


def test_openai_stream_usage_converts_to_reconciliation_record(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")
    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-late",
            "choices": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
        },
        sequence=42,
    )

    record = graphblocks_openai.openai_usage_record_from_delta(
        delta,
        record_id="usage-reconciled",
        run_id="run-1",
        attempt_id="attempt-1",
        model="gpt-test",
        occurred_at="2026-06-23T00:01:00Z",
        reconciliation_of="usage-provisional",
    )

    assert record.source == "reconciled"
    assert record.confidence == "exact"
    assert record.provider_response_id == "chatcmpl-late"
    assert record.reconciliation_of == "usage-provisional"
    assert record.metadata == {
        "model": "gpt-test",
        "provider": "openai-compatible",
        "stream_sequence": 42,
    }
    assert record.amounts == (
        UsageAmount(
            "model_input_tokens",
            30,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
        UsageAmount(
            "model_output_tokens",
            8,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
        UsageAmount(
            "model_total_tokens",
            38,
            "tokens",
            dimensions={"model": "gpt-test", "provider": "openai-compatible"},
        ),
    )


def test_openai_streaming_tool_call_deltas_assemble_graphblocks_drafts(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()

    first = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-3",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "knowledge.search",
                                    "arguments": '{"query"',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        sequence=1,
    )
    second = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-3",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": ':"refund"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        sequence=2,
    )

    first_drafts = assembler.apply_delta(first)
    second_drafts = assembler.apply_delta(second)

    assert first_drafts[0].status == "arguments_streaming"
    with pytest.raises(ToolCallError, match="tool arguments are not complete"):
        first_drafts[0].into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")
    assert second_drafts[0].argument_fragments == ('{"query"', ':"refund"}')
    assert second_drafts[0].status == "arguments_streaming"

    completed = assembler.complete_all()
    call = completed[0].into_tool_call("resolved-tool-1", created_at="2026-06-23T00:00:00Z")

    assert completed[0].status == "arguments_complete"
    assert call.arguments == {"query": "refund"}
    assert call.status == "validated"


def test_openai_stream_chunk_rejects_non_string_tool_argument_delta(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")

    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="provider chunk tool_call function arguments must be a string",
    ):
        graphblocks_openai.openai_chat_delta_from_chunk(
            {
                "id": "chatcmpl-arguments",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "knowledge.search",
                                        "arguments": {"query": "refund"},
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
            sequence=1,
        )


def test_openai_streaming_tool_call_assembler_rejects_unstable_identity(monkeypatch) -> None:
    _add_openai_package_paths(monkeypatch)
    graphblocks_openai = importlib.import_module("graphblocks_openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="requires an id"):
        assembler.apply_delta(
            graphblocks_openai.OpenAIChatDelta(
                response_id="chatcmpl-4",
                sequence=1,
                choice_index=0,
                tool_call_deltas=[
                    {
                        "index": 0,
                        "name": "knowledge.search",
                        "arguments_delta": "{}",
                    }
                ],
            )
        )

    assembler.apply_delta(
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-4",
            sequence=2,
            choice_index=0,
            tool_call_deltas=[
                {
                    "index": 0,
                    "id": "call-1",
                    "name": "knowledge.search",
                    "arguments_delta": "{",
                }
            ],
        )
    )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="changed id"):
        assembler.apply_delta(
            graphblocks_openai.OpenAIChatDelta(
                response_id="chatcmpl-4",
                sequence=3,
                choice_index=0,
                tool_call_deltas=[
                    {
                        "index": 0,
                        "id": "call-2",
                        "arguments_delta": "}",
                    }
                ],
            )
        )
