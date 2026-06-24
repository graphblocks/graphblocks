from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_budget_package_exposes_reservation_settlement_and_permit_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-budget" / "src"))
    graphblocks_budget = importlib.import_module("graphblocks_budget")

    requested = graphblocks_budget.UsageAmount("model_total_tokens", Decimal("40"), "tokens")
    actual = graphblocks_budget.UsageAmount("model_total_tokens", Decimal("25"), "tokens")
    ledger = graphblocks_budget.InMemoryBudgetLedger()
    ledger.allocate(
        "budget-1",
        graphblocks_budget.ResourceRef("tenant:acme"),
        [graphblocks_budget.UsageAmount("model_total_tokens", Decimal("100"), "tokens")],
        policy_ref="policy-1",
    )

    reservation = ledger.reserve(
        "budget-1",
        graphblocks_budget.ResourceRef("run:1", resource_kind="run"),
        [requested],
        purpose="provider_call",
        expires_at="2026-06-23T00:00:00Z",
    )
    permit = ledger.issue_permit(
        "permit-1",
        reservation_ids=[reservation.reservation_id],
        owner=graphblocks_budget.ResourceRef("worker:1", resource_kind="worker"),
        atomic_unit=graphblocks_budget.ResourceRef("turn:1", resource_kind="turn"),
        admission_epoch=1,
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-23T00:00:00Z",
    )
    settlement = ledger.commit_with_permit(permit.permit_id, reservation.reservation_id, [actual])

    assert reservation.status == "reserved"
    assert permit.allows([actual])
    assert settlement.committed == [actual]
    assert "BudgetPermitScopeError" in graphblocks_budget.__all__
    assert "BudgetPermitExpiredError" in graphblocks_budget.__all__
    assert ledger.balance("budget-1").available == [
        graphblocks_budget.UsageAmount("model_total_tokens", Decimal("75"), "tokens")
    ]


def test_budget_package_exposes_completion_reserve_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-budget" / "src"))
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    ledger = graphblocks_budget.InMemoryBudgetLedger()
    amount = graphblocks_budget.UsageAmount("model_total_tokens", Decimal("20"), "tokens")
    ledger.allocate(
        "budget-1",
        graphblocks_budget.ResourceRef("tenant:acme"),
        [graphblocks_budget.UsageAmount("model_total_tokens", Decimal("100"), "tokens")],
        policy_ref="policy-1",
    )

    reserve = ledger.create_completion_reserve(
        "reserve-1",
        "budget-1",
        purpose="checkpoint",
        amounts=[amount],
        spendable_by=("checkpoint.worker",),
    )
    reservation = ledger.spend_completion_reserve("reserve-1", "checkpoint.worker", expires_at="later")

    assert reserve.status == "available"
    assert ledger.completion_reserve("reserve-1").status == "spent"
    assert reservation.purpose == "finalization"
    assert reservation.amounts == [amount]
    assert "CompletionReserve" in graphblocks_budget.__all__
