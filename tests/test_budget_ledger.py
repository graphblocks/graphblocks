from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import (
    BudgetCompletionReserveStateError,
    BudgetCompletionReserveUnauthorizedError,
    BudgetExceededError,
    InMemoryBudgetLedger,
    BudgetReservationStateError,
    SQLiteBudgetLedger,
    UsageAmount,
)
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


def test_usage_amount_rejects_negative_amounts_and_freezes_dimensions() -> None:
    dimensions = {"model": "support"}

    amount = UsageAmount("model_total_tokens", Decimal("5"), "tokens", dimensions)
    dimensions["model"] = "mutated"

    assert amount.dimensions == {"model": "support"}
    with pytest.raises(TypeError):
        amount.dimensions["model"] = "direct"
    with pytest.raises(ValueError, match="usage amount must be non-negative"):
        UsageAmount("model_total_tokens", Decimal("-1"), "tokens")


def test_budget_ledger_reserve_reduces_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")

    reservation = ledger.reserve(
        "budget-1",
        owner=ResourceRef("run:1", resource_kind="run"),
        amounts=[_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:00:00Z",
    )

    balance = ledger.balance("budget-1")
    assert reservation.fencing_token == 1
    assert reservation.status == "reserved"
    assert balance.reserved == [_tokens("40")]
    assert balance.available == [_tokens("60")]
    assert balance.revision == 2


def test_budget_ledger_rejects_reservation_above_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("80")], purpose="provider_call", expires_at="later")

    with pytest.raises(BudgetExceededError):
        ledger.reserve("budget-1", ResourceRef("run:2"), [_tokens("30")], purpose="provider_call", expires_at="later")


def test_budget_ledger_commit_releases_unused_reservation() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("25")])

    balance = ledger.balance("budget-1")
    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert balance.reserved == []
    assert balance.committed == [_tokens("25")]
    assert balance.available == [_tokens("75")]


def test_budget_ledger_release_restores_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.release(reservation.reservation_id)

    assert settlement.released == [_tokens("40")]
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_budget_ledger_expire_restores_available_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.expire(reservation.reservation_id)

    balance = ledger.balance("budget-1")
    assert settlement.status == "expired"
    assert settlement.released == [_tokens("40")]
    assert balance.reserved == []
    assert balance.committed == []
    assert balance.available == [_tokens("100")]


def test_budget_ledger_expired_reservation_cannot_be_settled_or_authorize_permit() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    ledger.expire(reservation.reservation_id)

    with pytest.raises(BudgetReservationStateError):
        ledger.commit(reservation.reservation_id, [_tokens("1")])
    with pytest.raises(BudgetReservationStateError):
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


def test_budget_ledger_commit_over_reserved_records_overdraft() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("50")])

    balance = ledger.balance("budget-1")
    assert settlement.overdraft == [_tokens("10")]
    assert balance.committed == [_tokens("50")]
    assert balance.overdraft == [_tokens("10")]
    assert balance.available == [_tokens("50")]


def test_budget_ledger_commit_allows_overdraft_within_limit() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    settlement = ledger.commit(reservation.reservation_id, [_tokens("45")], max_overdraft=[_tokens("5")])

    assert settlement.overdraft == [_tokens("5")]
    assert ledger.balance("budget-1").committed == [_tokens("45")]


def test_budget_ledger_rejects_commit_above_overdraft_limit_without_mutating_balance() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve("budget-1", ResourceRef("run:1"), [_tokens("40")], purpose="provider_call", expires_at="later")

    with pytest.raises(BudgetExceededError):
        ledger.commit(reservation.reservation_id, [_tokens("46")], max_overdraft=[_tokens("5")])

    balance = ledger.balance("budget-1")
    assert balance.reserved == [_tokens("40")]
    assert balance.committed == []
    assert balance.overdraft == []
    assert balance.available == [_tokens("60")]


def test_completion_reserve_holds_finalization_capacity_out_of_general_budget() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")

    reserve = ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    assert reserve.status == "available"
    assert reserve.fencing_token == 1
    assert ledger.balance("budget-1").reserved == [_tokens("20")]
    assert ledger.balance("budget-1").available == [_tokens("80")]
    with pytest.raises(BudgetExceededError):
        ledger.reserve("budget-1", ResourceRef("planner"), [_tokens("90")], purpose="task", expires_at="later")


def test_completion_reserve_can_be_spent_by_authorized_finalization_work() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    reservation = ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")
    reserve = ledger.completion_reserve("finalization-reserve")

    assert reservation.purpose == "finalization"
    assert reservation.amounts == [_tokens("20")]
    assert reservation.fencing_token == reserve.fencing_token
    assert reserve.status == "spent"
    settlement = ledger.commit(reservation.reservation_id, [_tokens("15")])
    balance = ledger.balance("budget-1")
    assert settlement.committed == [_tokens("15")]
    assert settlement.released == [_tokens("5")]
    assert balance.reserved == []
    assert balance.committed == [_tokens("15")]
    assert balance.available == [_tokens("85")]


def test_completion_reserve_release_restores_held_capacity() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
    )

    reserve = ledger.release_completion_reserve("finalization-reserve")

    assert reserve.status == "released"
    assert ledger.balance("budget-1").reserved == []
    assert ledger.balance("budget-1").available == [_tokens("100")]
    with pytest.raises(BudgetCompletionReserveStateError):
        ledger.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")


def test_completion_reserve_expire_restores_held_capacity() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
        expires_at="2026-06-22T00:05:00Z",
    )

    reserve = ledger.expire_completion_reserve("finalization-reserve")

    assert reserve.status == "expired"
    assert ledger.balance("budget-1").reserved == []
    assert ledger.balance("budget-1").available == [_tokens("100")]


def test_completion_reserve_rejects_unauthorized_spender() -> None:
    ledger = InMemoryBudgetLedger()
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    ledger.create_completion_reserve(
        "cleanup-reserve",
        "budget-1",
        purpose="cleanup",
        amounts=[_tokens("10")],
        spendable_by=("cleanup.worker",),
    )

    with pytest.raises(BudgetCompletionReserveUnauthorizedError) as error:
        ledger.spend_completion_reserve("cleanup-reserve", "planner", expires_at="later")

    assert error.value.reserve_id == "cleanup-reserve"
    assert error.value.spender == "planner"
    assert ledger.completion_reserve("cleanup-reserve").status == "available"


def test_sqlite_budget_ledger_persists_reserved_balance_and_permit_spend(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reservation = ledger.reserve(
        "budget-1",
        ResourceRef("run:1", resource_kind="run"),
        [_tokens("40")],
        purpose="provider_call",
        expires_at="2026-06-22T01:00:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=ResourceRef("worker:1", resource_kind="worker"),
        atomic_unit=ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-22T02:00:00Z",
    )
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.balance("budget-1").reserved == [_tokens("40")]
    settlement = reopened.commit_with_permit_at(
        permit.permit_id,
        reservation.reservation_id,
        [_tokens("25")],
        now="2026-06-22T01:30:00Z",
    )

    assert settlement.committed == [_tokens("25")]
    assert settlement.released == [_tokens("15")]
    assert reopened.balance("budget-1").available == [_tokens("75")]
    reopened.close()


def test_sqlite_budget_ledger_persists_completion_reserve_lifecycle(tmp_path) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate("budget-1", ResourceRef("tenant:acme"), [_tokens("100")], policy_ref="policy-1")
    reserve = ledger.create_completion_reserve(
        "finalization-reserve",
        "budget-1",
        purpose="finalization",
        amounts=[_tokens("20")],
        spendable_by=("agent.finalize",),
        expires_at="2026-06-22T00:05:00Z",
    )
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.completion_reserve("finalization-reserve") == reserve
    assert reopened.balance("budget-1").available == [_tokens("80")]
    reservation = reopened.spend_completion_reserve("finalization-reserve", "agent.finalize", expires_at="later")
    assert reopened.completion_reserve("finalization-reserve").status == "spent"
    reopened.close()

    final = SQLiteBudgetLedger(path)
    assert final.completion_reserve("finalization-reserve").reservation_id == reservation.reservation_id
    assert final.commit(reservation.reservation_id, [_tokens("15")]).released == [_tokens("5")]
    assert final.balance("budget-1").available == [_tokens("85")]
    final.close()
