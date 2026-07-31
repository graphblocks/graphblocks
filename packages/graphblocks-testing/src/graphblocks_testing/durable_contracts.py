from __future__ import annotations

from typing import Literal, get_args


DurableCaseKind = Literal[
    "source_replay",
    "source_errors",
    "source_offset_reuse",
    "window_lateness",
    "window_boundary",
    "sink_idempotency",
    "checkpoint_replay",
    "tool_terminal_from_tool_result",
    "tool_terminal_policy_stop",
    "tool_terminal_effect_invariant",
    "background_run_event_stream",
    "callback_delivery_projection",
    "async_callback_resume_guards",
    "async_callback_cancel_race",
    "external_operation_reconciliation",
]

DURABLE_CASE_KINDS = frozenset(get_args(DurableCaseKind))


__all__ = ["DURABLE_CASE_KINDS", "DurableCaseKind"]
