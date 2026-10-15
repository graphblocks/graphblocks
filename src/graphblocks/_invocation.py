"""Private call boundary shared by local nodes and nested stdlib dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import TYPE_CHECKING, Any

from ._canonical_reference import canonical_dumps, canonical_loads

if TYPE_CHECKING:
    from .plugins import BlockDescriptor


class _InvocationInterrupted(RuntimeError):
    """Cancellation and deadlines must escape map's item-error collection."""


def _check_invocation_active(context: Mapping[str, Any]) -> None:
    token = context.get("cancellation_token")
    if token is not None and token.cancelled:
        raise _InvocationInterrupted(token.reason or "cancelled")
    deadline = context.get("deadline_monotonic")
    if deadline is not None and time.perf_counter() >= deadline:
        reason = token.reason if token is not None else None
        raise _InvocationInterrupted(reason or "block invocation exceeded deadline")


def _snapshot_object(owner: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{owner} must be a mapping")
    try:
        snapshot = canonical_loads(canonical_dumps(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{owner} must be valid strict JSON") from error
    if not isinstance(snapshot, dict):
        raise TypeError(f"{owner} must be a mapping")
    return snapshot


def _invoke_block(
    block_id: str,
    block: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]],
    descriptor: BlockDescriptor | None,
    inputs: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    _check_invocation_active(context)
    # Keep authority separate from the handler-owned mutable projection. Each
    # retry and map item starts with a fresh deep projection of the approved config.
    approved_config = _snapshot_object(f"{block_id} config", config)
    handler_config = canonical_loads(canonical_dumps(approved_config))
    handler_inputs = _snapshot_object(f"{block_id} input", inputs)
    _check_invocation_active(context)
    result = block(handler_inputs, handler_config, dict(context))
    _check_invocation_active(context)
    if not isinstance(result, Mapping):
        raise TypeError("block returned non-mapping output")
    result = _snapshot_object(f"{block_id} output", result)
    if descriptor is not None:
        declared_outputs = {port.name for port in descriptor.outputs}
        unexpected_outputs = sorted(set(result) - declared_outputs)
        if unexpected_outputs:
            raise TypeError(
                f"{block_id} returned undeclared output(s): "
                + ", ".join(unexpected_outputs)
            )
        missing_outputs = sorted(
            port.name
            for port in descriptor.outputs
            if port.required_for(approved_config, phase="initial")
            and port.name not in result
        )
        if missing_outputs:
            raise TypeError(
                f"{block_id} omitted required output(s): " + ", ".join(missing_outputs)
            )
    return result
