from __future__ import annotations

from decimal import Decimal

import pytest

from graphblocks.budget import (
    BudgetCompletionReserveUnauthorizedError,
    BudgetExceededError,
    InMemoryBudgetLedger,
    UsageAmount,
)
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_total_tokens", amount=Decimal(value), unit="tokens")


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
