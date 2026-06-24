from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import (
    BudgetExceededError,
    BudgetPermitExpiredError,
    BudgetPermitScopeError,
    BudgetReservationStateError,
    InMemoryBudgetLedger,
    UsageAmount,
)
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
    assert permit.allows([_tokens("25")]) is True
    assert permit.allows([_tokens("41")]) is False


def test_budget_permit_requires_matching_usage_dimensions() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("100"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ],
        policy_ref="policy-1",
    )
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("40"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ],
        purpose="provider_call",
        expires_at="later",
    )
    issued = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    assert issued.allows(
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("20"),
                unit="tokens",
                dimensions={"model": "small"},
            )
        ]
    )
    assert not issued.allows(
        [
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal("20"),
                unit="tokens",
                dimensions={"model": "large"},
            )
        ]
    )


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


def test_budget_ledger_commit_with_permit_settles_authorized_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    settlement = ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [_tokens("25")])

    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert ledger.balance("budget-1").available == [_tokens("75")]


def test_budget_ledger_release_with_permit_restores_authorized_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    settlement = ledger.release_with_permit(permit.permit_id, reservation.reservation_id)

    assert settlement.released == [_tokens("40")]
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_budget_ledger_commit_with_permit_rejects_usage_above_authorized_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="later",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    with pytest.raises(BudgetExceededError):
        ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [_tokens("41")])

    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.available == [_tokens("60")]


def test_budget_ledger_commit_with_expired_permit_rejects_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T00:10:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T00:05:00Z",
    )

    with pytest.raises(BudgetPermitExpiredError) as error:
        ledger.commit_with_permit_at(
            permit.permit_id,
            reservation.reservation_id,
            [_tokens("25")],
            now="2026-06-22T00:05:00Z",
        )

    assert error.value.permit_id == "permit-1"
    assert error.value.expires_at == "2026-06-22T00:05:00Z"
    assert error.value.now == "2026-06-22T00:05:00Z"
    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.available == [_tokens("60")]


def test_budget_ledger_release_with_expired_permit_rejects_without_mutating() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T00:10:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T00:05:00Z",
    )

    with pytest.raises(BudgetPermitExpiredError) as error:
        ledger.release_with_permit_at(
            permit.permit_id,
            reservation.reservation_id,
            now="2026-06-22T00:05:00Z",
        )

    assert error.value.permit_id == "permit-1"
    assert ledger.balance("budget-1").reserved == [_tokens("40")]


def test_budget_ledger_permit_cannot_settle_unreferenced_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    first = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("25")], purpose="task", expires_at="later")
    second = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("15")], purpose="task", expires_at="later")
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[first.reservation_id],
        owner=ResourceRef("worker:1"),
        atomic_unit=ResourceRef("turn:1"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="later",
    )

    with pytest.raises(BudgetPermitScopeError) as error:
        ledger.commit_with_permit(permit.permit_id, second.reservation_id, [_tokens("10")])

    assert error.value.permit_id == "permit-1"
    assert error.value.reservation_id == second.reservation_id
