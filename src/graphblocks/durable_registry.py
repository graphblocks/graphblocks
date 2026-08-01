from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .plugins import (
    BlockCatalog,
    builtin_block_catalog,
    builtin_block_implementations,
)
from .runtime import (
    BlockCallable,
    RuntimeRegistry,
    is_full_stdlib_registry,
)
from .stdlib_governance import (
    check_run_suite_block,
    gate_evaluate_block,
    result_bundle_block,
    review_request_block,
    structured_generate_block,
)
from .stdlib_rag import (
    answer_validate_grounding,
    context_build,
    rank_documents,
    retrieve_execute_plan,
    retrieve_fuse,
)
from .stdlib_runtime_handlers import (
    _control_map_handler,
    async_await_callback,
    async_cancel_operation,
    async_complete_operation,
    async_expire_operation,
    async_poll_operation,
    async_start_operation,
    begin_turn,
    commit_turn,
    control_select,
    policy_stop_turn,
    prompt_render,
    resolve_tools,
    scripted_agent_run,
    scripted_generate,
)


@dataclass(frozen=True, slots=True)
class _DurableHandlerSpec:
    implementation_id: str
    handler: BlockCallable | None


_DURABLE_INTENT_HANDLER_SPECS: Mapping[str, _DurableHandlerSpec] = MappingProxyType(
    {
        "prompt.render@1": _DurableHandlerSpec(
            "graphblocks.stdlib.prompt.render",
            prompt_render,
        ),
        "model.generate@1": _DurableHandlerSpec(
            "graphblocks.stdlib.scripted_model.generate",
            scripted_generate,
        ),
        "model.structured_generate@1": _DurableHandlerSpec(
            "graphblocks.stdlib.model.structured_generate",
            structured_generate_block,
        ),
        "tools.resolve@1": _DurableHandlerSpec(
            "graphblocks.stdlib.tools.resolve",
            resolve_tools,
        ),
        "agent.run@1": _DurableHandlerSpec(
            "graphblocks.stdlib.agent.run",
            scripted_agent_run,
        ),
        "conversation.begin_turn@1": _DurableHandlerSpec(
            "graphblocks.stdlib.conversation.begin_turn",
            begin_turn,
        ),
        "conversation.commit_turn@1": _DurableHandlerSpec(
            "graphblocks.stdlib.conversation.commit_turn",
            commit_turn,
        ),
        "conversation.policy_stop_turn@1": _DurableHandlerSpec(
            "graphblocks.stdlib.conversation.policy_stop_turn",
            policy_stop_turn,
        ),
        "async.start_operation@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.start_operation",
            async_start_operation,
        ),
        "async.await_callback@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.await_callback",
            async_await_callback,
        ),
        "async.poll_operation@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.poll_operation",
            async_poll_operation,
        ),
        "async.complete_operation@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.complete_operation",
            async_complete_operation,
        ),
        "async.cancel_operation@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.cancel_operation",
            async_cancel_operation,
        ),
        "async.expire_operation@1": _DurableHandlerSpec(
            "graphblocks.stdlib.async.expire_operation",
            async_expire_operation,
        ),
        "control.map@2": _DurableHandlerSpec(
            "graphblocks.stdlib.control.map",
            None,
        ),
        "control.select@1": _DurableHandlerSpec(
            "graphblocks.stdlib.control.select",
            control_select,
        ),
        "retrieve.fuse@1": _DurableHandlerSpec(
            "graphblocks.stdlib.retrieve.fuse",
            retrieve_fuse,
        ),
        "retrieve.execute_plan@1": _DurableHandlerSpec(
            "graphblocks.stdlib.retrieve.execute_plan",
            retrieve_execute_plan,
        ),
        "rank.documents@1": _DurableHandlerSpec(
            "graphblocks.stdlib.rank.documents",
            rank_documents,
        ),
        "context.build@1": _DurableHandlerSpec(
            "graphblocks.stdlib.context.build",
            context_build,
        ),
        "answer.validate_grounding@1": _DurableHandlerSpec(
            "graphblocks.stdlib.answer.validate_grounding",
            answer_validate_grounding,
        ),
        "check.run_suite@1": _DurableHandlerSpec(
            "graphblocks.stdlib.check.run_suite",
            check_run_suite_block,
        ),
        "gate.evaluate@1": _DurableHandlerSpec(
            "graphblocks.stdlib.gate.evaluate",
            gate_evaluate_block,
        ),
        "review.request@1": _DurableHandlerSpec(
            "graphblocks.stdlib.review.request",
            review_request_block,
        ),
        "result.bundle@1": _DurableHandlerSpec(
            "graphblocks.stdlib.result.bundle",
            result_bundle_block,
        ),
    }
)
_DURABLE_INTENT_IMPLEMENTATIONS: Mapping[str, str] = MappingProxyType(
    {
        block_id: spec.implementation_id
        for block_id, spec in _DURABLE_INTENT_HANDLER_SPECS.items()
    }
)
_DURABLE_INTENT_REGISTRY_MARKER = object()


def _implementation_inventory_mismatches() -> tuple[str, ...]:
    try:
        manifest_bindings = dict(builtin_block_implementations())
    except (TypeError, RuntimeError, ValueError):
        return ("builtin implementation inventory is not a stable mapping",)
    return tuple(
        f"{block_id}: expected {expected!r}, got {manifest_bindings.get(block_id)!r}"
        for block_id, expected in _DURABLE_INTENT_IMPLEMENTATIONS.items()
        if manifest_bindings.get(block_id) != expected
    )


def durable_intent_registry() -> RuntimeRegistry:
    """Build the closed, transport-free registry for the default durable worker."""

    mismatches = _implementation_inventory_mismatches()
    if mismatches:
        raise RuntimeError(
            "durable intent-only implementation inventory changed: "
            + "; ".join(mismatches)
        )

    preview_catalog = builtin_block_catalog(profile="preview")
    missing_descriptors = sorted(
        set(_DURABLE_INTENT_IMPLEMENTATIONS) - set(preview_catalog.descriptors)
    )
    if missing_descriptors:
        raise RuntimeError(
            "durable intent-only blocks are absent from the preview catalog: "
            + ", ".join(missing_descriptors)
        )
    registry = RuntimeRegistry(
        block_catalog=BlockCatalog(
            {
                block_id: preview_catalog.descriptors[block_id]
                for block_id in _DURABLE_INTENT_IMPLEMENTATIONS
            }
        )
    )

    for block_id, spec in _DURABLE_INTENT_HANDLER_SPECS.items():
        handler = spec.handler
        if handler is None:
            if block_id != "control.map@2":
                raise RuntimeError(
                    "durable intent-only handler builder is not declared for "
                    f"{block_id}"
                )
            handler = _control_map_handler(registry.resolve)
        registry.register(block_id, handler)

    registry._profile_marker = _DURABLE_INTENT_REGISTRY_MARKER
    registry._profile_blocks = tuple(registry.blocks.items())
    registry._profile_block_catalog = registry.block_catalog
    registry._profile_allow_untyped = registry.allow_untyped
    return registry


def is_durable_intent_registry(registry: object) -> bool:
    """Return whether a registry is an unmodified durable factory result."""

    approved_block_ids = set(_DURABLE_INTENT_IMPLEMENTATIONS)
    if (
        type(registry) is not RuntimeRegistry
        or _implementation_inventory_mismatches()
        or registry._profile_marker is not _DURABLE_INTENT_REGISTRY_MARKER
        or type(registry.blocks) is not dict
        or type(registry.block_catalog) is not BlockCatalog
        or registry.block_catalog is not registry._profile_block_catalog
        or registry.allow_untyped is not registry._profile_allow_untyped
        or set(registry.blocks) != approved_block_ids
        or set(registry.block_catalog.descriptors) != approved_block_ids
        or len(registry.blocks) != len(registry._profile_blocks)
    ):
        return False
    return all(
        registry.blocks.get(block_id) is handler
        for block_id, handler in registry._profile_blocks
    )


def is_durable_worker_compatible_registry(registry: object) -> bool:
    """Return whether parent compilation matches the default durable child."""

    if type(registry) is not RuntimeRegistry or _implementation_inventory_mismatches():
        return False
    if is_durable_intent_registry(registry):
        return True
    approved_block_ids = set(_DURABLE_INTENT_IMPLEMENTATIONS)
    return (
        is_full_stdlib_registry(registry)
        and set(registry.blocks) == approved_block_ids
        and set(registry.block_catalog.descriptors) == approved_block_ids
    )


__all__ = [
    "durable_intent_registry",
    "is_durable_intent_registry",
    "is_durable_worker_compatible_registry",
]
