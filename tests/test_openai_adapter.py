from __future__ import annotations

import importlib

import pytest

from graphblocks import (
    ContentPart,
    GenerationChunk,
    Message,
    ToolCallError,
    ToolDefinition,
    UsageAmount,
)


def test_openai_chat_request_encodes_messages_tools_and_options(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
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
        tool_schemas={
            "schemas/SearchRequest@1": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            }
        },
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
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
        },
        "metadata": {"run_id": "run-1"},
    }


def test_openai_chat_request_rejects_non_boolean_stream_flag(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="stream"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(
                Message(
                    message_id="msg-user",
                    role="user",
                    parts=(ContentPart(kind="text", text="hello"),),
                ),
            ),
            stream="false",  # type: ignore[arg-type]
        )

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="messages"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test", messages=(message for message in ())
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="tools"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(
                Message(
                    message_id="msg-user",
                    role="user",
                    parts=(ContentPart(kind="text", text="hello"),),
                ),
            ),
            tools=object(),
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="strict JSON"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(
                Message(
                    message_id="msg-user",
                    role="user",
                    parts=(ContentPart(kind="text", text="hello"),),
                ),
            ),
            extra_body={"custom": object()},
        )


@pytest.mark.parametrize(
    "choice",
    (
        {"index": -1, "message": {"content": "hello"}},
        {"index": 0, "finish_reason": 7, "message": {"content": "hello"}},
        {
            "index": 0,
            "message": {"content": [{"type": "image_url", "image_url": "https://x"}]},
        },
    ),
)
def test_openai_response_rejects_malformed_choice_payloads(monkeypatch, choice: object) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError):
        graphblocks_openai.openai_chat_response_from_provider(
            {"id": "response-1", "model": "gpt-test", "choices": [choice]}
        )


def test_openai_chat_request_rejects_invalid_contract_inputs(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="n must be 1"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
            extra_body={"n": 2},
        )

    tool = ToolDefinition(
        name="knowledge.search",
        description="Search support docs.",
        input_schema="schemas/SearchRequest@1",
    )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="tool_schemas"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
            tools=(tool,),
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="SearchRequest"):
        graphblocks_openai.openai_chat_completion_request(
            model="gpt-test",
            messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
            tools=(tool,),
            tool_schemas={},
        )
    for schema in (
        {"$ref": "schemas/SearchRequest@1"},
        {"allOf": [{"$dynamicRef": "https://schemas.example/SearchRequest"}]},
        {"allOf": ({"$ref": "schemas/SearchRequest@1"},)},
        {"type": "number", "maximum": float("nan")},
    ):
        with pytest.raises(
            graphblocks_openai.OpenAICompatibleAdapterError,
            match="(non-local|strict JSON)",
        ):
            graphblocks_openai.openai_chat_completion_request(
                model="gpt-test",
                messages=(
                    Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),
                ),
                tools=(tool,),
                tool_schemas={"schemas/SearchRequest@1": schema},
            )

    local_ref_request = graphblocks_openai.openai_chat_completion_request(
        model="gpt-test",
        messages=(Message("msg-1", "user", (ContentPart(kind="text", text="hello"),)),),
        tools=(tool,),
        tool_schemas={
            "schemas/SearchRequest@1": {
                "$ref": "#/$defs/request",
                "$defs": {"request": {"type": "object"}},
            }
        },
    )
    assert local_ref_request.body["tools"][0]["function"]["parameters"]["$ref"] == (
        "#/$defs/request"
    )


def test_openai_response_maps_text_choice_to_content_parts_and_usage(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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


def test_openai_response_rejects_multiple_choices_instead_of_flattening(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="requires n=1"):
        graphblocks_openai.openai_chat_response_from_provider(
            {
                "id": "chatcmpl-multiple",
                "model": "gpt-test",
                "choices": [
                    {"index": 0, "message": {"content": "first"}, "finish_reason": "stop"},
                    {"index": 1, "message": {"content": "second"}, "finish_reason": "length"},
                ],
            }
        )


def test_openai_response_preserves_tool_call_arguments_as_drafts(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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
    assert response.tool_calls == (
        {
            "arguments": '{"query":"refund"}',
            "id": "call-1",
            "name": "knowledge.search",
            "type": "function",
        },
    )
    assert response.finish_reason == "tool_calls"

    drafts = graphblocks_openai.openai_tool_call_drafts_from_response(response)

    assert len(drafts) == 1
    assert drafts[0].response_id == "chatcmpl-2"
    assert drafts[0].tool_call_id == "call-1"
    assert drafts[0].tool_name == "knowledge.search"
    assert drafts[0].argument_fragments == ('{"query":"refund"}',)
    assert drafts[0].status == "arguments_complete"


def test_openai_response_rejects_duplicate_tool_call_ids(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    normalized_calls = [
        {
            "id": "call-1",
            "type": "function",
            "name": name,
            "arguments": "{}",
        }
        for name in ("knowledge.search", "knowledge.lookup")
    ]

    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="duplicate.*tool_call id",
    ):
        graphblocks_openai.OpenAIChatResponse(
            "response-1",
            "gpt-test",
            tool_calls=normalized_calls,
        )
    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="duplicate.*tool_call id",
    ):
        graphblocks_openai.openai_chat_response_from_provider(
            {
                "id": "response-1",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "tool_calls": [
                                {
                                    "id": call["id"],
                                    "type": call["type"],
                                    "function": {
                                        "name": call["name"],
                                        "arguments": call["arguments"],
                                    },
                                }
                                for call in normalized_calls
                            ]
                        },
                    }
                ],
            }
        )


def test_openai_stream_chunk_normalizes_content_delta(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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


def test_openai_stream_chunk_trims_provider_identity(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    response = graphblocks_openai.openai_chat_response_from_provider(
        {
            "id": " chatcmpl-1 ",
            "model": " gpt-test ",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": " call-1 ",
                                "type": " function ",
                                "function": {
                                    "name": " knowledge.search ",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )
    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": " chatcmpl-2 ",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": " call-2 ",
                                "type": " function ",
                                "function": {
                                    "name": " knowledge.search ",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    },
                }
            ],
        },
        sequence=1,
    )

    assert response.response_id == "chatcmpl-1"
    assert response.model == "gpt-test"
    assert response.tool_calls == (
        {
            "id": "call-1",
            "type": "function",
            "name": "knowledge.search",
            "arguments": "{}",
        },
    )
    assert delta.response_id == "chatcmpl-2"
    assert delta.tool_call_deltas == (
        {
            "index": 0,
            "id": "call-2",
            "type": "function",
            "name": "knowledge.search",
            "arguments_delta": "{}",
        },
    )


def test_openai_wire_records_are_deeply_immutable_with_mutable_projections(
    monkeypatch,
) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    body = {"model": "gpt-test", "options": {"stops": [{"value": "done"}]}}
    request = graphblocks_openai.OpenAIChatCompletionRequest(body=body)
    response = graphblocks_openai.OpenAIChatResponse(
        "response-1",
        "gpt-test",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "name": "knowledge.search",
                "arguments": "{}",
                "metadata": {"attempt": 1},
            }
        ],
        usage={"details": {"cached_tokens": 2}},
    )
    delta = graphblocks_openai.OpenAIChatDelta(
        "response-1",
        1,
        0,
        tool_call_deltas=[
            {
                "index": 0,
                "id": "call-1",
                "name": "knowledge.search",
                "arguments_delta": "{}",
            }
        ],
        usage_delta={"details": {"cached_tokens": 2}},
    )
    body["options"]["stops"][0]["value"] = "caller-mutated"

    with pytest.raises(TypeError):
        request.body["options"]["stops"][0]["value"] = "consumer-mutated"
    with pytest.raises(TypeError):
        response.tool_calls[0]["id"] = "consumer-mutated"
    with pytest.raises(TypeError):
        response.usage["details"]["cached_tokens"] = 9
    with pytest.raises(TypeError):
        delta.tool_call_deltas[0]["id"] = "consumer-mutated"
    with pytest.raises(TypeError):
        delta.usage_delta["details"]["cached_tokens"] = 9

    request_projection = request.request_contract()
    response_projection = response.response_contract()
    delta_projection = delta.delta_contract()
    request_projection["body"]["options"]["stops"][0]["value"] = "projected"
    response_projection["tool_calls"][0]["id"] = "projected"
    delta_projection["tool_call_deltas"][0]["id"] = "projected"

    assert request.request_contract()["body"]["options"]["stops"][0]["value"] == "done"
    assert response.response_contract()["tool_calls"][0]["id"] == "call-1"
    assert delta.delta_contract()["tool_call_deltas"][0]["id"] == "call-1"


def test_openai_stream_content_delta_normalizes_to_generation_chunk(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "delta": {"content": "Ref"}}],
        },
        sequence=7,
    )

    assert graphblocks_openai.openai_generation_chunk_from_delta(delta) == GenerationChunk.text(
        "chatcmpl-1",
        "chatcmpl-1",
        7,
        "Ref",
    )
    assert graphblocks_openai.openai_generation_chunk_from_delta(
        delta,
        stream_id="stream-1",
        sequence=3,
    ) == GenerationChunk.text("stream-1", "chatcmpl-1", 3, "Ref")
    assert "openai_generation_chunk_from_delta" in graphblocks_openai.__all__


def test_openai_stream_non_content_delta_has_no_generation_chunk(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    usage_delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-usage",
            "choices": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        },
        sequence=99,
    )
    tool_delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-tool",
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
                                    "arguments": "{\"query\"",
                                },
                            }
                        ]
                    },
                }
            ],
        },
        sequence=8,
    )

    assert graphblocks_openai.openai_generation_chunk_from_delta(usage_delta) is None
    assert graphblocks_openai.openai_generation_chunk_from_delta(tool_delta) is None


def test_openai_generation_chunk_helper_rejects_invalid_inputs(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    delta = graphblocks_openai.OpenAIChatDelta(
        response_id="chatcmpl-1",
        sequence=1,
        choice_index=0,
        content_delta="hello",
    )

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="delta must be"):
        graphblocks_openai.openai_generation_chunk_from_delta("not-a-delta")
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="stream_id"):
        graphblocks_openai.openai_generation_chunk_from_delta(delta, stream_id=" ")
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="generation sequence"):
        graphblocks_openai.openai_generation_chunk_from_delta(delta, sequence=True)
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="generation sequence"):
        graphblocks_openai.openai_generation_chunk_from_delta(delta, sequence=-1)
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="generation sequence must be positive"):
        graphblocks_openai.openai_generation_chunk_from_delta(delta, sequence=0)

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="sequence must be positive"):
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-1",
            sequence=0,
            choice_index=0,
            content_delta="hello",
        )


def test_openai_stream_chunk_normalizes_usage_only_final_chunk(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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


def test_openai_stream_chunk_accepts_empty_provider_metadata_chunk(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    delta = graphblocks_openai.openai_chat_delta_from_chunk(
        {
            "id": "chatcmpl-filter-results",
            "choices": [],
            "prompt_filter_results": [{"prompt_index": 0}],
        },
        sequence=1,
    )

    assert delta.choice_index is None
    assert delta.content_delta is None
    assert delta.usage_delta == {}


def test_openai_stream_chunk_rejects_multiple_choices_instead_of_dropping_them(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="requires n=1"):
        graphblocks_openai.openai_chat_delta_from_chunk(
            {
                "id": "chatcmpl-multiple",
                "choices": [
                    {"index": 0, "delta": {"content": "first"}},
                    {"index": 1, "delta": {"content": "second"}},
                ],
            },
            sequence=1,
        )


def test_openai_provider_usage_converts_to_usage_record(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
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
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
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
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
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
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

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


def test_openai_stream_chunk_rejects_malformed_tool_call_metadata(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    cases = (
        (
            {"index": True, "id": "call-1", "function": {"name": "knowledge.search"}},
            "provider chunk tool_call index must be a non-negative integer",
        ),
        (
            {"index": -1, "id": "call-1", "function": {"name": "knowledge.search"}},
            "provider chunk tool_call index must be a non-negative integer",
        ),
        (
            {"index": 0, "id": object(), "function": {"name": "knowledge.search"}},
            "provider chunk tool_call id must be a string",
        ),
        (
            {"index": 0, "id": " ", "function": {"name": "knowledge.search"}},
            "provider chunk tool_call id must not be empty",
        ),
        (
            {"index": 0, "id": "call-1", "type": " ", "function": {"name": "knowledge.search"}},
            "provider chunk tool_call type must not be empty",
        ),
        (
            {"index": 0, "id": "call-1", "function": {"name": object()}},
            "provider chunk tool_call function name must be a string",
        ),
        (
            {"index": 0, "id": "call-1", "function": {"name": " "}},
            "provider chunk tool_call function name must not be empty",
        ),
    )

    for raw_delta, message in cases:
        with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match=message):
            graphblocks_openai.openai_chat_delta_from_chunk(
                {
                    "id": "chatcmpl-metadata",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [raw_delta]},
                        }
                    ],
                },
                sequence=1,
            )


def test_openai_delta_rejects_invalid_sequence_and_metadata(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="sequence"):
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-1",
            sequence=True,
            choice_index=0,
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="choice_index"):
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-1",
            sequence=1,
            choice_index=-1,
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="tool_call_delta id"):
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-1",
            sequence=1,
            choice_index=0,
            tool_call_deltas=[{"index": 0, "id": " ", "name": "knowledge.search"}],
        )
    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="arguments_delta"):
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-1",
            sequence=1,
            choice_index=0,
            tool_call_deltas=[{"index": 0, "arguments_delta": {"query": "refund"}}],
        )


def test_openai_streaming_tool_call_assembler_rejects_unstable_identity(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
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


def test_openai_streaming_tool_call_assembler_rejects_delta_atomically(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()
    partially_invalid = graphblocks_openai.OpenAIChatDelta(
        response_id="chatcmpl-atomic",
        sequence=1,
        choice_index=0,
        tool_call_deltas=[
            {
                "index": 0,
                "id": "call-1",
                "name": "knowledge.search",
                "arguments_delta": '{"query":"refund"}',
            },
            {
                "index": 1,
                "id": "call-2",
                "arguments_delta": "{}",
            },
        ],
    )

    with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError, match="requires a name"):
        assembler.apply_delta(partially_invalid)

    assert assembler.response_id is None
    assert assembler.drafts() == ()


def test_openai_streaming_tool_call_assembler_replays_sequences_idempotently(
    monkeypatch,
) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()
    delta = graphblocks_openai.OpenAIChatDelta(
        response_id="chatcmpl-replay",
        sequence=2,
        choice_index=0,
        tool_call_deltas=[
            {
                "index": 0,
                "id": "call-1",
                "name": "knowledge.search",
                "arguments_delta": '{"query"',
            }
        ],
    )

    first = assembler.apply_delta(delta)
    replay = assembler.apply_delta(delta)

    assert replay == first
    assert assembler.drafts()[0].argument_fragments == ('{"query"',)

    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="reused with different content",
    ):
        assembler.apply_delta(
            graphblocks_openai.OpenAIChatDelta(
                response_id="chatcmpl-replay",
                sequence=2,
                choice_index=0,
                tool_call_deltas=[
                    {
                        "index": 0,
                        "arguments_delta": ':"different"}',
                    }
                ],
            )
        )
    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="sequence must increase",
    ):
        assembler.apply_delta(
            graphblocks_openai.OpenAIChatDelta(
                response_id="chatcmpl-replay",
                sequence=1,
                choice_index=0,
            )
        )


def test_openai_streaming_tool_call_assembler_validates_restored_state(
    monkeypatch,
) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    delta = graphblocks_openai.OpenAIChatDelta(
        "chatcmpl-restored",
        1,
        0,
        tool_call_deltas=[
            {
                "index": 0,
                "id": "call-1",
                "name": "knowledge.search",
                "arguments_delta": '{"query"',
            }
        ],
    )
    original = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()
    original.apply_delta(delta)
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler.restore(
        original.applied_deltas()
    )

    assert assembler.drafts() == original.drafts()
    assert assembler.apply_delta(delta) == original.apply_delta(delta)
    assert assembler.drafts()[0].argument_fragments == ('{"query"',)
    completed = original.complete_all()
    completed_restore = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler.restore(
        original.applied_deltas(),
        completed=True,
    )
    assert completed_restore.drafts() == completed
    assert completed_restore.apply_delta(delta)[0].argument_fragments == ('{"query"',)
    assert completed_restore.drafts() == completed
    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="strictly increase",
    ):
        graphblocks_openai.OpenAIStreamingToolCallDraftAssembler.restore(
            (delta, delta)
        )

    invalid_states = (
        {
            "response_id": "chatcmpl-restored",
            "_drafts_by_index": {0: original.drafts()[0]},
            "_index_order": [0],
        },
        {
            "response_id": "chatcmpl-other",
            "_drafts_by_index": {0: original.drafts()[0]},
            "_index_order": [0],
            "_applied_deltas": original.applied_deltas(),
        },
        {
            "response_id": "chatcmpl-restored",
            "_drafts_by_index": {0: original.drafts()[0]},
            "_index_order": [0, 0],
            "_applied_deltas": original.applied_deltas(),
        },
        {
            "response_id": "chatcmpl-restored",
            "_drafts_by_index": {0: original.drafts()[0]},
            "_index_order": [],
            "_applied_deltas": original.applied_deltas(),
        },
    )
    for state in invalid_states:
        with pytest.raises(graphblocks_openai.OpenAICompatibleAdapterError):
            graphblocks_openai.OpenAIStreamingToolCallDraftAssembler(**state)


def test_openai_streaming_tool_call_assembler_rejects_duplicate_call_ids(
    monkeypatch,
) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()
    duplicate = graphblocks_openai.OpenAIChatDelta(
        response_id="chatcmpl-duplicate",
        sequence=1,
        choice_index=0,
        tool_call_deltas=[
            {
                "index": 0,
                "id": "call-1",
                "name": "knowledge.search",
                "arguments_delta": "{}",
            },
            {
                "index": 1,
                "id": "call-1",
                "name": "knowledge.lookup",
                "arguments_delta": "{}",
            },
        ],
    )

    with pytest.raises(
        graphblocks_openai.OpenAICompatibleAdapterError,
        match="must identify one index",
    ):
        assembler.apply_delta(duplicate)

    assert assembler.response_id is None
    assert assembler.drafts() == ()


def test_openai_streaming_tool_call_completion_is_atomic(monkeypatch) -> None:
    graphblocks_openai = importlib.import_module("graphblocks.integrations.openai")
    assembler = graphblocks_openai.OpenAIStreamingToolCallDraftAssembler()
    assembler.apply_delta(
        graphblocks_openai.OpenAIChatDelta(
            response_id="chatcmpl-complete",
            sequence=1,
            choice_index=0,
            tool_call_deltas=[
                {
                    "index": 0,
                    "id": "call-1",
                    "name": "knowledge.search",
                    "arguments_delta": "{}",
                },
                {
                    "index": 1,
                    "id": "call-2",
                    "name": "knowledge.lookup",
                },
            ],
        )
    )

    with pytest.raises(ValueError, match="requires argument fragments"):
        assembler.complete_all()

    assert [draft.status for draft in assembler.drafts()] == [
        "arguments_streaming",
        "proposed",
    ]
