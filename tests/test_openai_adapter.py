from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from graphblocks import ContentPart, Message, ToolDefinition


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
    }
