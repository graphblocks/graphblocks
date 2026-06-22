from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import BudgetPermit, UsageAmount
from graphblocks.exhaustion import (
    ContinuationEnvelope,
    ExhaustionController,
    ExhaustionPolicy,
    MissingExhaustionBoundaryError,
    OutputCutoff,
    validate_exhaustion_policy,
)
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_output_tokens", amount=Decimal(value), unit="tokens")


def _permit() -> BudgetPermit:
    return BudgetPermit(
        permit_id="permit-1",
        reservation_refs=("reservation-1",),
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=7,
        authorized_amounts=[_tokens("100")],
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T01:00:00Z",
        fencing_tokens={"budget-1": 1},
    )


def test_finish_current_turn_requires_bounded_continuation() -> None:
    policy = ExhaustionPolicy.from_preset("finish_current_turn", unit="turn")

    with pytest.raises(MissingExhaustionBoundaryError):
        validate_exhaustion_policy(policy, production=True)

    bounded = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("4000")], max_additional_steps=2),
    )

    assert validate_exhaustion_policy(bounded, production=True) == []


def test_finish_current_turn_allows_only_declared_continuation_work() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("4000")], max_additional_steps=1),
    )
    controller = ExhaustionController(policy, atomic_unit_id="turn:1", admission_epoch=7, continuation_permit=_permit())

    already_admitted = controller.admit("already_admitted_child_work", work_epoch=7)
    finalization = controller.admit("declared_finalization", work_epoch=8, permit=_permit())
    optional_task = controller.admit("optional_task", work_epoch=8, permit=_permit())
    second_finalization = controller.admit("declared_finalization", work_epoch=8, permit=_permit())

    assert already_admitted.allowed is True
    assert finalization.allowed is True
    assert optional_task.allowed is False
    assert optional_task.reason == "forbidden_work"
    assert second_finalization.allowed is False
    assert second_finalization.reason == "max_additional_steps_exceeded"


def test_hard_stop_blocks_new_work_and_late_output_delivery() -> None:
    policy = ExhaustionPolicy.from_preset("hard_stop", unit="provider_call")
    controller = ExhaustionController(policy, atomic_unit_id="call-1", admission_epoch=2)
    cutoff = OutputCutoff(
        stream_id="stream-1",
        last_accepted_sequence=5,
        terminal_reason="budget_exhausted",
        durable_result="mark_incomplete",
    )

    cleanup = controller.admit("cleanup", work_epoch=2)
    provider_call = controller.admit("current_provider_call", work_epoch=2)

    assert cleanup.allowed is True
    assert provider_call.allowed is False
    assert cutoff.accepts(5) is True
    assert cutoff.accepts(6) is False


def test_continuation_permit_must_match_atomic_unit_and_profile() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("100")], max_additional_steps=1),
    )
    wrong_profile = BudgetPermit(
        permit_id="permit-2",
        reservation_refs=("reservation-1",),
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=7,
        authorized_amounts=[_tokens("100")],
        continuation_profile="hard_stop",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T01:00:00Z",
        fencing_tokens={"budget-1": 1},
    )
    wrong_unit = BudgetPermit(
        permit_id="permit-3",
        reservation_refs=("reservation-1",),
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:other", resource_kind="turn"),
        admission_epoch=7,
        authorized_amounts=[_tokens("100")],
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T01:00:00Z",
        fencing_tokens={"budget-1": 1},
    )
    controller = ExhaustionController(policy, atomic_unit_id="turn:1", admission_epoch=7)

    assert controller.admit("declared_finalization", work_epoch=8, permit=wrong_profile).reason == "invalid_permit"
    assert controller.admit("declared_finalization", work_epoch=8, permit=wrong_unit).reason == "invalid_permit"
