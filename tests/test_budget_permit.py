from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import BudgetReservationStateError, InMemoryBudgetLedger, UsageAmount
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


def test_budget_ledger_issues_bounded_permit_from_reservations() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1", resource_kind="worker"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=3,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T01:00:00Z",
    )

    assert permit.permit_id == "permit-1"
    assert permit.reservation_refs == (reservation.reservation_id,)
    assert permit.authorized_amounts == [_tokens("40")]
    assert permit.fencing_tokens == {"budget-1": reservation.fencing_token}
    assert permit.owner.resource_id == "worker:1"
    assert permit.atomic_unit.resource_id == "turn:1"


def test_budget_ledger_permit_combines_multiple_reservations() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    first = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("25")], purpose="task", expires_at="later")
    second = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("15")], purpose="finalization", expires_at="later")

    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[first.reservation_id, second.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="hard_stop",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    assert permit.authorized_amounts == [_tokens("40")]
    assert permit.fencing_tokens == {"budget-1": second.fencing_token}


def test_budget_ledger_rejects_permit_for_released_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")
    ledger.release(reservation.reservation_id)

    with pytest.raises(BudgetReservationStateError):
        ledger.issue_permit(
            "permit-1",
            reservation_ids=[reservation.reservation_id],
            owner=ResourceRef("worker:1"),
            atomic_unit=ResourceRef("turn:1"),
            admission_epoch=1,
            continuation_profile="hard_stop",
            policy_snapshot_digest="sha256:policy",
            expires_at="later",
        )
