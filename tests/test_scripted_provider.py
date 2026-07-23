from __future__ import annotations

import importlib

import pytest


def test_scripted_provider_generates_deterministic_response_for_exact_prompt(monkeypatch) -> None:
    graphblocks_scripted = importlib.import_module("graphblocks.integrations.scripted")
    provider = graphblocks_scripted.ScriptedModelProvider(
        scripts={"Answer: Hello": "Hello from the scripted provider."},
        model="scripted-test",
        provider_id="scripted-local",
    )

    response = provider.generate("Answer: Hello", response_id="response-1", metadata={"run_id": "run-1"})

    assert response.response_contract() == {
        "response_id": "response-1",
        "provider": "scripted-local",
        "model": "scripted-test",
        "text": "Hello from the scripted provider.",
        "finish_reason": "scripted",
        "usage": {
            "input_characters": 13,
            "output_characters": 33,
        },
        "metadata": {"run_id": "run-1", "script_key": "Answer: Hello"},
    }
    assert provider.capabilities() == {
        "chat": True,
        "streaming": True,
        "tool_calling": False,
        "usage": True,
    }


def test_scripted_provider_streams_chunked_deltas_and_completion(monkeypatch) -> None:
    graphblocks_scripted = importlib.import_module("graphblocks.integrations.scripted")
    provider = graphblocks_scripted.ScriptedModelProvider(
        scripts={"prompt": "abcdef"},
        model="scripted-test",
    )

    chunks = tuple(provider.stream("prompt", response_id="response-1", chunk_size=2))

    assert [chunk.delta_contract() for chunk in chunks] == [
        {
            "response_id": "response-1",
            "sequence": 1,
            "text_delta": "ab",
            "finished": False,
            "finish_reason": None,
        },
        {
            "response_id": "response-1",
            "sequence": 2,
            "text_delta": "cd",
            "finished": False,
            "finish_reason": None,
        },
        {
            "response_id": "response-1",
            "sequence": 3,
            "text_delta": "ef",
            "finished": False,
            "finish_reason": None,
        },
        {
            "response_id": "response-1",
            "sequence": 4,
            "text_delta": "",
            "finished": True,
            "finish_reason": "scripted",
        },
    ]


def test_scripted_provider_rejects_missing_prompt_and_invalid_chunk_size(monkeypatch) -> None:
    graphblocks_scripted = importlib.import_module("graphblocks.integrations.scripted")
    provider = graphblocks_scripted.ScriptedModelProvider(scripts={"known": "response"})

    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="no scripted response"):
        provider.generate("unknown")

    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="chunk_size"):
        tuple(provider.stream("known", chunk_size=0))


def test_scripted_provider_detaches_mutable_script_and_metadata_inputs(monkeypatch) -> None:
    graphblocks_scripted = importlib.import_module("graphblocks.integrations.scripted")
    scripts = {"prompt": "response"}
    metadata = {"run_id": "run-1"}
    provider = graphblocks_scripted.ScriptedModelProvider(scripts=scripts)

    response = provider.generate("prompt", metadata=metadata)
    scripts["prompt"] = "mutated"
    metadata["run_id"] = "mutated"

    assert response.text == "response"
    assert response.metadata["run_id"] == "run-1"


def test_scripted_provider_rejects_coercive_runtime_values(monkeypatch) -> None:
    graphblocks_scripted = importlib.import_module("graphblocks.integrations.scripted")
    provider = graphblocks_scripted.ScriptedModelProvider(scripts={"known": "response"})

    for chunk_size in (True, 1.5):
        with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="chunk_size"):
            tuple(provider.stream("known", chunk_size=chunk_size))
    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="sequence"):
        graphblocks_scripted.ScriptedModelDelta("response-1", True, "delta")
    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="finished"):
        graphblocks_scripted.ScriptedModelDelta(
            "response-1", 1, "delta", finished="false"  # type: ignore[arg-type]
        )
    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="usage"):
        graphblocks_scripted.ScriptedModelResponse(
            "response-1", "scripted", "scripted", "text", usage={"tokens": True}
        )
    with pytest.raises(graphblocks_scripted.ScriptedModelProviderError, match="metadata"):
        provider.generate("known", metadata=object())
