from __future__ import annotations

import importlib

import pytest

from graphblocks import BlockCatalog, ContentPart, Message


def test_haystack_component_block_descriptor_is_explicit_and_catalog_compatible(monkeypatch) -> None:
    graphblocks_haystack = importlib.import_module("graphblocks.integrations.haystack")

    block = graphblocks_haystack.HaystackComponentBlock(
        component_ref="support.SearchComponent",
        block_type_id="haystack.component.support_search",
        inputs={"query": "graphblocks.ai/Text@1"},
        outputs={"documents": "List<graphblocks.ai/DocumentChunk@1>"},
        metadata={"package": "support"},
    )
    descriptor = block.block_descriptor()
    catalog = BlockCatalog.from_blocks([descriptor])

    assert descriptor == {
        "typeId": "haystack.component.support_search",
        "version": 1,
        "inputs": [{"name": "query", "type": "graphblocks.ai/Text@1", "required": True}],
        "outputs": [
            {
                "name": "documents",
                "type": "List<graphblocks.ai/DocumentChunk@1>",
                "required": True,
            }
        ],
        "resourceSlots": [{"name": "component", "type": "haystack.component", "optional": False}],
        "metadata": {
            "haystack": {
                "componentRef": "support.SearchComponent",
                "descriptorSource": "explicit",
                "kind": "component",
            },
            "package": "support",
        },
    }
    assert catalog.get("haystack.component.support_search@1").inputs[0].name == "query"


def test_haystack_pipeline_block_descriptor_records_async_pipeline(monkeypatch) -> None:
    graphblocks_haystack = importlib.import_module("graphblocks.integrations.haystack")

    block = graphblocks_haystack.HaystackPipelineBlock(
        pipeline_ref="support.rag",
        block_type_id="haystack.pipeline.support_rag",
        inputs={"question": "graphblocks.ai/Text@1"},
        outputs={"answer": "graphblocks.ai/Answer@1"},
        async_pipeline=True,
    )

    assert block.block_descriptor()["metadata"]["haystack"] == {
        "asyncPipeline": True,
        "descriptorSource": "explicit",
        "kind": "pipeline",
        "pipelineRef": "support.rag",
    }


def test_haystack_dynamic_component_requires_explicit_descriptor(monkeypatch) -> None:
    graphblocks_haystack = importlib.import_module("graphblocks.integrations.haystack")

    diagnostic = graphblocks_haystack.explicit_descriptor_required(
        subject_ref="support.DynamicComponent",
        subject_kind="component",
        reason="input sockets are dynamic",
    )

    assert diagnostic.diagnostic_contract() == {
        "code": "HaystackExplicitDescriptorRequired",
        "message": "component 'support.DynamicComponent' requires an explicit GraphBlocks descriptor",
        "metadata": {"reason": "input sockets are dynamic", "subject_kind": "component"},
    }


def test_haystack_chat_message_round_trips_graphblocks_message(monkeypatch) -> None:
    graphblocks_haystack = importlib.import_module("graphblocks.integrations.haystack")
    message = Message(
        message_id="msg-1",
        role="user",
        parts=(ContentPart(kind="text", text="hello"),),
        metadata={"tenant": "acme"},
    )

    haystack = graphblocks_haystack.message_to_haystack_chat_message(message)
    restored = graphblocks_haystack.haystack_chat_message_to_message(
        {"role": "assistant", "content": "hi", "meta": {"trace_id": "trace-1"}},
        message_id="msg-2",
    )

    assert haystack == {
        "role": "user",
        "content": "hello",
        "meta": {
            "graphblocks_metadata": {"tenant": "acme"},
            "message_id": "msg-1",
        },
    }
    assert restored == Message(
        message_id="msg-2",
        role="assistant",
        parts=(ContentPart(kind="text", text="hi"),),
        metadata={"haystack_meta": {"trace_id": "trace-1"}},
    )


def test_haystack_bridge_rejects_invalid_descriptors(monkeypatch) -> None:
    graphblocks_haystack = importlib.import_module("graphblocks.integrations.haystack")

    with pytest.raises(graphblocks_haystack.HaystackBridgeError, match="block_type_id"):
        graphblocks_haystack.HaystackComponentBlock(
            component_ref="support.SearchComponent",
            block_type_id="haystack.component.invalid id",
            inputs={"query": "graphblocks.ai/Text@1"},
            outputs={"documents": "List<graphblocks.ai/DocumentChunk@1>"},
        )

    with pytest.raises(graphblocks_haystack.HaystackBridgeError, match="outputs"):
        graphblocks_haystack.HaystackPipelineBlock(
            pipeline_ref="support.rag",
            block_type_id="haystack.pipeline.support_rag",
            inputs={"question": "graphblocks.ai/Text@1"},
            outputs={},
        )
