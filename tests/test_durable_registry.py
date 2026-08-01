from __future__ import annotations

from collections.abc import Callable
from types import FunctionType
from typing import Any

import pytest

import graphblocks.durable_registry as durable_registry_module
from graphblocks.durable_registry import (
    durable_intent_registry,
    is_durable_intent_registry,
    is_durable_worker_compatible_registry,
)
from graphblocks.plugins import (
    BlockCatalog,
    builtin_block_catalog,
    builtin_block_implementations,
)
from graphblocks.runtime import (
    BlockCallable,
    RuntimeRegistry,
    is_full_stdlib_registry,
    stdlib_registry,
)
from graphblocks.stdlib_governance import GOVERNANCE_IMPLEMENTATIONS
from graphblocks.stdlib_rag import RAG_IMPLEMENTATIONS
from graphblocks.stdlib_runtime_handlers import core_stdlib_implementations


EXPECTED_DURABLE_INTENT_IMPLEMENTATIONS = {
    "prompt.render@1": "graphblocks.stdlib.prompt.render",
    "model.generate@1": "graphblocks.stdlib.scripted_model.generate",
    "model.structured_generate@1": "graphblocks.stdlib.model.structured_generate",
    "tools.resolve@1": "graphblocks.stdlib.tools.resolve",
    "agent.run@1": "graphblocks.stdlib.agent.run",
    "conversation.begin_turn@1": "graphblocks.stdlib.conversation.begin_turn",
    "conversation.commit_turn@1": "graphblocks.stdlib.conversation.commit_turn",
    "conversation.policy_stop_turn@1": (
        "graphblocks.stdlib.conversation.policy_stop_turn"
    ),
    "async.start_operation@1": "graphblocks.stdlib.async.start_operation",
    "async.await_callback@1": "graphblocks.stdlib.async.await_callback",
    "async.poll_operation@1": "graphblocks.stdlib.async.poll_operation",
    "async.complete_operation@1": "graphblocks.stdlib.async.complete_operation",
    "async.cancel_operation@1": "graphblocks.stdlib.async.cancel_operation",
    "async.expire_operation@1": "graphblocks.stdlib.async.expire_operation",
    "control.map@2": "graphblocks.stdlib.control.map",
    "control.select@1": "graphblocks.stdlib.control.select",
    "retrieve.fuse@1": "graphblocks.stdlib.retrieve.fuse",
    "retrieve.execute_plan@1": "graphblocks.stdlib.retrieve.execute_plan",
    "rank.documents@1": "graphblocks.stdlib.rank.documents",
    "context.build@1": "graphblocks.stdlib.context.build",
    "answer.validate_grounding@1": ("graphblocks.stdlib.answer.validate_grounding"),
    "check.run_suite@1": "graphblocks.stdlib.check.run_suite",
    "gate.evaluate@1": "graphblocks.stdlib.gate.evaluate",
    "review.request@1": "graphblocks.stdlib.review.request",
    "result.bundle@1": "graphblocks.stdlib.result.bundle",
}
EXPECTED_DURABLE_HANDLER_AUTHORITIES = {
    "prompt.render@1": "graphblocks.stdlib_runtime_handlers.prompt_render",
    "model.generate@1": "graphblocks.stdlib_runtime_handlers.scripted_generate",
    "model.structured_generate@1": (
        "graphblocks.stdlib_governance.structured_generate_block"
    ),
    "tools.resolve@1": "graphblocks.stdlib_runtime_handlers.resolve_tools",
    "agent.run@1": "graphblocks.stdlib_runtime_handlers.scripted_agent_run",
    "conversation.begin_turn@1": "graphblocks.stdlib_runtime_handlers.begin_turn",
    "conversation.commit_turn@1": ("graphblocks.stdlib_runtime_handlers.commit_turn"),
    "conversation.policy_stop_turn@1": (
        "graphblocks.stdlib_runtime_handlers.policy_stop_turn"
    ),
    "async.start_operation@1": (
        "graphblocks.stdlib_runtime_handlers.async_start_operation"
    ),
    "async.await_callback@1": (
        "graphblocks.stdlib_runtime_handlers.async_await_callback"
    ),
    "async.poll_operation@1": (
        "graphblocks.stdlib_runtime_handlers.async_poll_operation"
    ),
    "async.complete_operation@1": (
        "graphblocks.stdlib_runtime_handlers.async_complete_operation"
    ),
    "async.cancel_operation@1": (
        "graphblocks.stdlib_runtime_handlers.async_cancel_operation"
    ),
    "async.expire_operation@1": (
        "graphblocks.stdlib_runtime_handlers.async_expire_operation"
    ),
    "control.map@2": (
        "graphblocks.stdlib_runtime_handlers._control_map_handler.<locals>.control_map"
    ),
    "control.select@1": "graphblocks.stdlib_runtime_handlers.control_select",
    "retrieve.fuse@1": "graphblocks.stdlib_rag.retrieve_fuse",
    "retrieve.execute_plan@1": "graphblocks.stdlib_rag.retrieve_execute_plan",
    "rank.documents@1": "graphblocks.stdlib_rag.rank_documents",
    "context.build@1": "graphblocks.stdlib_rag.context_build",
    "answer.validate_grounding@1": ("graphblocks.stdlib_rag.answer_validate_grounding"),
    "check.run_suite@1": "graphblocks.stdlib_governance.check_run_suite_block",
    "gate.evaluate@1": "graphblocks.stdlib_governance.gate_evaluate_block",
    "review.request@1": "graphblocks.stdlib_governance.review_request_block",
    "result.bundle@1": "graphblocks.stdlib_governance.result_bundle_block",
}
_FUTURE_BLOCK_ID = "unsafe.provider_send@1"
_FUTURE_IMPLEMENTATION_ID = "tests.unsafe.provider_send"


def _install_expanded_preview_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> list[object]:
    preview_catalog = builtin_block_catalog(profile="preview")
    future_descriptor = BlockCatalog.from_blocks(
        [{"typeId": "unsafe.provider_send", "version": 1}]
    ).descriptors[_FUTURE_BLOCK_ID]
    expanded_catalog = BlockCatalog(
        {
            **dict(preview_catalog.descriptors),
            _FUTURE_BLOCK_ID: future_descriptor,
        }
    )
    manifest_bindings = dict(builtin_block_implementations())
    manifest_bindings[_FUTURE_BLOCK_ID] = _FUTURE_IMPLEMENTATION_ID
    calls: list[object] = []

    def provider_send(
        inputs: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del config, context
        calls.append(inputs)
        return {"sent": True}

    def expanded_catalog_factory(*, profile: str) -> BlockCatalog:
        assert profile == "preview"
        return expanded_catalog

    def expanded_implementation_inventory() -> dict[str, str]:
        return manifest_bindings

    def expanded_core_implementations(
        resolve: Callable[[str], BlockCallable],
    ) -> dict[str, BlockCallable]:
        handlers = dict(core_stdlib_implementations(resolve))
        handlers[_FUTURE_IMPLEMENTATION_ID] = provider_send
        return handlers

    monkeypatch.setattr(
        durable_registry_module,
        "builtin_block_catalog",
        expanded_catalog_factory,
    )
    monkeypatch.setattr(
        durable_registry_module,
        "builtin_block_implementations",
        expanded_implementation_inventory,
    )
    monkeypatch.setattr(
        durable_registry_module,
        "core_stdlib_implementations",
        expanded_core_implementations,
        raising=False,
    )
    return calls


def test_durable_intent_registry_has_exact_explicit_inventory() -> None:
    registry = durable_intent_registry()
    handler_authorities: dict[str, str] = {}
    for block_id, handler in registry.blocks.items():
        assert isinstance(handler, FunctionType)
        handler_authorities[block_id] = f"{handler.__module__}.{handler.__qualname__}"

    assert set(registry.blocks) == set(EXPECTED_DURABLE_INTENT_IMPLEMENTATIONS)
    assert set(registry.block_catalog.descriptors) == set(
        EXPECTED_DURABLE_INTENT_IMPLEMENTATIONS
    )
    assert {
        block_id: builtin_block_implementations()[block_id]
        for block_id in registry.blocks
    } == EXPECTED_DURABLE_INTENT_IMPLEMENTATIONS
    assert handler_authorities == EXPECTED_DURABLE_HANDLER_AUTHORITIES
    assert is_durable_intent_registry(registry)
    assert is_durable_worker_compatible_registry(registry)
    assert is_durable_worker_compatible_registry(stdlib_registry())


def test_durable_intent_registry_rejects_handler_mutation() -> None:
    registry = durable_intent_registry()
    registry.blocks["prompt.render@1"] = lambda _inputs, _config, _context: {}

    assert not is_durable_intent_registry(registry)
    assert not is_durable_worker_compatible_registry(registry)


def test_durable_intent_registry_rejects_manifest_implementation_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_bindings = dict(builtin_block_implementations())
    manifest_bindings["prompt.render@1"] = "tests.unsafe.prompt_render"
    monkeypatch.setattr(
        durable_registry_module,
        "builtin_block_implementations",
        lambda: manifest_bindings,
    )

    with pytest.raises(
        RuntimeError,
        match="durable intent-only implementation inventory changed.*prompt.render",
    ):
        durable_intent_registry()


def test_durable_intent_registry_owns_same_id_handler_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def provider_send(
        inputs: dict[str, Any],
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        del config, context
        calls.append(inputs)
        return {"sent": True}

    def rebound_core_implementations(
        resolve: Callable[[str], BlockCallable],
    ) -> dict[str, BlockCallable]:
        handlers = dict(core_stdlib_implementations(resolve))
        handlers["graphblocks.stdlib.prompt.render"] = provider_send
        return handlers

    rebound_rag = dict(RAG_IMPLEMENTATIONS)
    rebound_rag["graphblocks.stdlib.retrieve.execute_plan"] = provider_send
    rebound_governance = dict(GOVERNANCE_IMPLEMENTATIONS)
    rebound_governance["graphblocks.stdlib.model.structured_generate"] = provider_send
    monkeypatch.setattr(
        durable_registry_module,
        "core_stdlib_implementations",
        rebound_core_implementations,
        raising=False,
    )
    monkeypatch.setattr(
        durable_registry_module,
        "RAG_IMPLEMENTATIONS",
        rebound_rag,
        raising=False,
    )
    monkeypatch.setattr(
        durable_registry_module,
        "GOVERNANCE_IMPLEMENTATIONS",
        rebound_governance,
        raising=False,
    )

    registry = durable_intent_registry()

    assert registry.resolve("prompt.render@1") is not provider_send
    assert registry.resolve("retrieve.execute_plan@1") is not provider_send
    assert registry.resolve("model.structured_generate@1") is not provider_send
    assert registry.resolve("control.map@2")(
        {"items": [{"text": "hello"}]},
        {
            "block": "prompt.render@1",
            "config": {"template": "Echo {item.text}"},
            "outputName": "prompt",
        },
        {},
    ) == {"values": ["Echo hello"]}
    assert calls == []


def test_durable_intent_registry_does_not_inherit_new_preview_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_expanded_preview_inventory(monkeypatch)

    registry = durable_intent_registry()

    assert _FUTURE_BLOCK_ID not in registry.blocks
    assert _FUTURE_BLOCK_ID not in registry.block_catalog.descriptors
    with pytest.raises(ValueError, match="not declared in the block catalog"):
        registry.resolve(_FUTURE_BLOCK_ID)
    assert calls == []


def test_durable_control_map_cannot_resolve_block_outside_intent_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_expanded_preview_inventory(monkeypatch)
    registry = durable_intent_registry()

    with pytest.raises(ValueError, match="not declared in the block catalog"):
        registry.resolve("control.map@2")(
            {"items": [{"secret": "must-not-send"}]},
            {"block": _FUTURE_BLOCK_ID},
            {},
        )
    assert calls == []


def test_durable_intent_registry_rejects_resealed_future_inventory() -> None:
    registry = durable_intent_registry()
    future_descriptor = BlockCatalog.from_blocks(
        [{"typeId": "unsafe.provider_send", "version": 1}]
    ).descriptors[_FUTURE_BLOCK_ID]
    registry.block_catalog = BlockCatalog(
        {
            **dict(registry.block_catalog.descriptors),
            _FUTURE_BLOCK_ID: future_descriptor,
        }
    )
    registry.blocks[_FUTURE_BLOCK_ID] = lambda _inputs, _config, _context: {
        "sent": True
    }
    registry._profile_blocks = tuple(registry.blocks.items())
    registry._profile_block_catalog = registry.block_catalog

    assert not is_durable_intent_registry(registry)
    assert not is_durable_worker_compatible_registry(registry)


def test_durable_worker_compatible_registry_rejects_future_full_inventory() -> None:
    registry = stdlib_registry()
    future_descriptor = BlockCatalog.from_blocks(
        [{"typeId": "unsafe.provider_send", "version": 1}]
    ).descriptors[_FUTURE_BLOCK_ID]
    registry.block_catalog = BlockCatalog(
        {
            **dict(registry.block_catalog.descriptors),
            _FUTURE_BLOCK_ID: future_descriptor,
        }
    )
    registry.blocks[_FUTURE_BLOCK_ID] = lambda _inputs, _config, _context: {
        "sent": True
    }
    registry._profile_blocks = tuple(registry.blocks.items())
    registry._profile_block_catalog = registry.block_catalog

    assert is_full_stdlib_registry(registry)
    assert not is_durable_worker_compatible_registry(registry)


def test_durable_registry_predicate_rejects_runtime_registry_subclasses() -> None:
    class RuntimeRegistrySubclass(RuntimeRegistry):
        pass

    registry = durable_intent_registry()
    subclass = RuntimeRegistrySubclass(
        blocks=dict(registry.blocks),
        block_catalog=registry.block_catalog,
    )

    assert not is_durable_intent_registry(subclass)
    assert not is_durable_worker_compatible_registry(subclass)
