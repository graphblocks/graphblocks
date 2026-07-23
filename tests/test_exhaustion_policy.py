from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from collections.abc import Iterator
import pickle

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
from graphblocks.output_policy import OutputCutoff as CanonicalOutputCutoff
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


class _ExplodingRequestedUsage:
    def __iter__(self) -> Iterator[UsageAmount]:
        raise RuntimeError("requested usage exploded")


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
    current_epoch_cleanup = controller.admit("cleanup", work_epoch=7)

    assert already_admitted.allowed is True
    assert finalization.allowed is True
    assert optional_task.allowed is False
    assert optional_task.reason == "forbidden_work"
    assert second_finalization.allowed is False
    assert second_finalization.reason == "max_additional_steps_exceeded"
    assert current_epoch_cleanup.allowed is True


def test_exhaustion_boundaries_reject_ambiguous_inputs_and_are_pickle_safe() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(
            max_additional_usage=[_tokens("100")],
            max_additional_steps=1,
        ),
    )
    controller = ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
        continuation_permit=_permit(),
    )

    assert pickle.loads(pickle.dumps(_permit())) == _permit()
    with pytest.raises(ValueError, match="requested_usage"):
        controller.admit(
            "declared_finalization",
            work_epoch=8,
            requested_usage=0,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="requested_usage"):
        controller.admit(
            "declared_finalization",
            work_epoch=8,
            requested_usage=_ExplodingRequestedUsage(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="supported integer range"):
        ExhaustionController(
            policy,
            atomic_unit_id="turn:1",
            admission_epoch=1 << 64,
        )
    with pytest.raises(ValueError, match="Unicode scalar"):
        ExhaustionController(
            policy,
            atomic_unit_id="\ud800",
            admission_epoch=7,
        )
    with pytest.raises(ValueError, match="policy must be an ExhaustionPolicy"):
        validate_exhaustion_policy(object())  # type: ignore[arg-type]


def test_hard_stop_blocks_new_work_and_late_output_delivery() -> None:
    policy = ExhaustionPolicy.from_preset("hard_stop", unit="provider_call")
    controller = ExhaustionController(policy, atomic_unit_id="call-1", admission_epoch=2)
    cutoff = OutputCutoff(
        stream_id="stream-1",
        response_id="response-1",
        last_generated_sequence=5,
        last_client_delivered_sequence=5,
        terminal_reason="budget_exhausted",
        draft_disposition="mark_incomplete",
        durable_result="incomplete",
        occurred_at="2026-06-23T00:00:00Z",
    )

    cleanup = controller.admit("cleanup", work_epoch=2)
    provider_call = controller.admit("current_provider_call", work_epoch=2)

    assert cleanup.allowed is True
    assert provider_call.allowed is False
    assert cutoff.accepts_sequence(5) is True
    assert cutoff.accepts_sequence(6) is False


def test_checkpoint_and_pause_allows_safety_work_without_topup_permit() -> None:
    policy = ExhaustionPolicy.from_preset("checkpoint_and_pause", unit="run")
    controller = ExhaustionController(policy, atomic_unit_id="run:1", admission_epoch=4)

    checkpoint = controller.admit("checkpoint", work_epoch=5)
    cleanup = controller.admit("cleanup", work_epoch=6)
    finalization = controller.admit("declared_finalization", work_epoch=7)
    provider = controller.admit("unreserved_provider_call", work_epoch=7)

    assert checkpoint.allowed is True
    assert cleanup.allowed is True
    assert finalization.allowed is False
    assert finalization.reason == "new_work_denied"
    assert provider.allowed is False
    assert provider.reason == "new_work_denied"


def test_degrade_then_finalize_allows_best_effort_finalization_without_topup_permit() -> None:
    policy = ExhaustionPolicy.from_preset("degrade_then_finalize", unit="run")
    controller = ExhaustionController(policy, atomic_unit_id="run:1", admission_epoch=4)

    finalization = controller.admit("declared_finalization", work_epoch=5)
    cleanup = controller.admit("cleanup", work_epoch=6)
    optional_task = controller.admit("optional_task", work_epoch=7)
    provider = controller.admit("unreserved_provider_call", work_epoch=7)

    assert finalization.allowed is True
    assert cleanup.allowed is True
    assert optional_task.allowed is False
    assert optional_task.reason == "forbidden_work"
    assert provider.allowed is False
    assert provider.reason == "new_work_denied"


def test_exhaustion_output_cutoff_uses_canonical_output_policy_contract() -> None:
    assert OutputCutoff is CanonicalOutputCutoff


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


def test_continuation_permit_must_not_be_expired_at_validation_time() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("100")], max_additional_steps=1),
    )
    controller = ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
        validation_time="2026-06-22T01:00:00Z",
    )

    decision = controller.admit("declared_finalization", work_epoch=8, permit=_permit())

    assert decision.allowed is False
    assert decision.reason == "invalid_permit"


def test_continuation_permit_expiration_uses_datetime_comparison() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("100")], max_additional_steps=1),
    )
    permit = replace(_permit(), expires_at="2026-06-21T20:00:00-05:00")
    allowed_controller = ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
        validation_time="2026-06-22T00:59:59Z",
    )

    allowed = allowed_controller.admit("declared_finalization", work_epoch=8, permit=permit)

    assert allowed.allowed is True
    assert allowed.reason == "allowed"

    expired_controller = ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
        validation_time="2026-06-22T01:00:01Z",
    )

    expired = expired_controller.admit("declared_finalization", work_epoch=8, permit=permit)

    assert expired.allowed is False
    assert expired.reason == "invalid_permit"


def test_controller_level_continuation_permit_must_match_policy() -> None:
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
    controller = ExhaustionController(
        policy,
        atomic_unit_id="turn:1",
        admission_epoch=7,
        continuation_permit=wrong_profile,
    )

    decision = controller.admit("declared_finalization", work_epoch=8)

    assert decision.allowed is False
    assert decision.reason == "invalid_permit"


def test_continuation_usage_must_fit_permit_authorized_amounts() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("200")], max_additional_steps=2),
    )
    controller = ExhaustionController(policy, atomic_unit_id="turn:1", admission_epoch=7, continuation_permit=_permit())

    denied = controller.admit("declared_finalization", work_epoch=8, requested_usage=[_tokens("101")])
    allowed = controller.admit("declared_finalization", work_epoch=8, requested_usage=[_tokens("100")])

    assert denied.allowed is False
    assert denied.reason == "usage_exceeds_permit"
    assert allowed.allowed is True


def test_continuation_usage_accumulates_against_envelope_bound() -> None:
    policy = ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=ContinuationEnvelope(max_additional_usage=[_tokens("100")], max_additional_steps=3),
    )
    controller = ExhaustionController(policy, atomic_unit_id="turn:1", admission_epoch=7, continuation_permit=_permit())

    first = controller.admit("declared_finalization", work_epoch=8, requested_usage=[_tokens("60")])
    denied = controller.admit("checkpoint", work_epoch=8, requested_usage=[_tokens("41")])
    second = controller.admit("cleanup", work_epoch=8, requested_usage=[_tokens("40")])

    assert first.allowed is True
    assert denied.allowed is False
    assert denied.reason == "max_additional_usage_exceeded"
    assert second.allowed is True


def test_exhaustion_constructors_freeze_limits_and_reject_coercion() -> None:
    allowed = {"cleanup"}
    usage = [_tokens("10")]
    envelope = ContinuationEnvelope(
        allowed_work=allowed,
        max_additional_usage=usage,
        max_additional_steps=1,
    )
    allowed.add("checkpoint")
    usage.append(_tokens("20"))

    assert envelope.allowed_work == frozenset({"cleanup"})
    assert envelope.max_additional_usage == (_tokens("10"),)

    with pytest.raises(ValueError, match="max_additional_steps must be an integer"):
        ContinuationEnvelope(max_additional_steps=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="deny_new_work must be a boolean"):
        ExhaustionPolicy(
            preset="hard_stop",
            in_flight="cancel_immediately",
            unit="run",
            deny_new_work=1,  # type: ignore[arg-type]
        )


def test_exhaustion_admission_rejects_unknown_work_and_usage_types() -> None:
    policy = ExhaustionPolicy.from_preset("hard_stop", unit="run")
    controller = ExhaustionController(
        policy,
        atomic_unit_id="run:1",
        admission_epoch=1,
    )

    with pytest.raises(ValueError, match="invalid exhaustion work kind"):
        controller.admit("invented", work_epoch=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requested_usage"):
        controller.admit(
            "cleanup",
            work_epoch=1,
            requested_usage=[object()],  # type: ignore[list-item]
        )


def test_exhaustion_state_cannot_reset_limits_and_deadlines_are_enforced() -> None:
    deadline_policy = ExhaustionPolicy.from_preset(
        "checkpoint_and_pause",
        unit="run",
        continuation=ContinuationEnvelope(
            deadline="2026-06-23T00:00:00Z",
        ),
    )
    assert validate_exhaustion_policy(deadline_policy, production=True) == []
    with pytest.raises(ValueError, match="requires validation_time"):
        ExhaustionController(
            deadline_policy,
            atomic_unit_id="run:1",
            admission_epoch=1,
        )

    controller = ExhaustionController(
        deadline_policy,
        atomic_unit_id="run:1",
        admission_epoch=1,
        validation_time="2026-06-23T00:00:00Z",
    )
    decision = controller.admit("checkpoint", work_epoch=2)
    assert decision.allowed is False
    assert decision.reason == "continuation_deadline_exceeded"

    bounded = ExhaustionController(
        ExhaustionPolicy.from_preset(
            "checkpoint_and_pause",
            unit="run",
            continuation=ContinuationEnvelope(max_additional_steps=1),
        ),
        atomic_unit_id="run:1",
        admission_epoch=1,
    )
    assert bounded.admit("checkpoint", work_epoch=2).allowed
    with pytest.raises(AttributeError):
        bounded.used_additional_steps = 0
    with pytest.raises(AttributeError):
        bounded.used_additional_usage = ()
    assert (
        bounded.admit("checkpoint", work_epoch=2).reason
        == "max_additional_steps_exceeded"
    )
