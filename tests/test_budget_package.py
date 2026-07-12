from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

from graphblocks.budget import (
    VALID_BUDGET_STATUSES,
    VALID_COMPLETION_RESERVE_PURPOSES,
    VALID_COMPLETION_RESERVE_STATUSES,
    VALID_RESERVATION_PURPOSES,
    VALID_RESERVATION_STATUSES,
)
from graphblocks.exhaustion import ExhaustionPolicy


ROOT = Path(__file__).parents[1]


def test_budget_package_exposes_reservation_settlement_and_permit_contract(monkeypatch) -> None:
    graphblocks_budget = importlib.import_module("graphblocks.budget")

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
    settlement = ledger.commit_with_permit(
        permit.permit_id,
        reservation.reservation_id,
        [actual],
        now="2026-06-22T00:30:00Z",
    )

    assert reservation.status == "reserved"
    assert permit.allows([actual])
    assert settlement.committed == [actual]
    assert "BudgetPermitScopeError" in graphblocks_budget.__all__
    assert "BudgetPermitExpiredError" in graphblocks_budget.__all__
    assert ledger.balance("budget-1").available == [
        graphblocks_budget.UsageAmount("model_total_tokens", Decimal("75"), "tokens")
    ]


def test_budget_package_exposes_completion_reserve_contract(monkeypatch) -> None:
    graphblocks_budget = importlib.import_module("graphblocks.budget")
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

    released = ledger.create_completion_reserve(
        "reserve-2",
        "budget-1",
        purpose="cleanup",
        amounts=[amount],
        spendable_by=("cleanup.worker",),
    )
    assert released.status == "available"
    assert ledger.release_completion_reserve("reserve-2").status == "released"


def test_budget_package_exposes_exhaustion_continuation_contract(monkeypatch) -> None:
    graphblocks_budget = importlib.import_module("graphblocks.budget")

    continuation = graphblocks_budget.ContinuationEnvelope(
        allowed_work={"declared_finalization"},
        max_additional_steps=1,
    )
    policy = graphblocks_budget.ExhaustionPolicy.from_preset(
        "finish_current_turn",
        unit="turn",
        continuation=continuation,
    )

    assert policy.continuation is not None
    assert "declared_finalization" in policy.continuation.allowed_work
    assert graphblocks_budget.validate_exhaustion_policy(policy, production=True) == []
    assert graphblocks_budget.ExhaustionPolicy is ExhaustionPolicy
    assert "ExhaustionPolicy" in graphblocks_budget.__all__
    assert "ContinuationEnvelope" in graphblocks_budget.__all__
    assert "validate_exhaustion_policy" in graphblocks_budget.__all__


def test_budget_package_exposes_local_sqlite_ledger(monkeypatch) -> None:
    graphblocks_budget = importlib.import_module("graphblocks.budget")
    ledger = graphblocks_budget.SQLiteBudgetLedger.in_memory()

    ledger.allocate(
        "budget-1",
        graphblocks_budget.ResourceRef("tenant:acme"),
        [graphblocks_budget.UsageAmount("model_total_tokens", Decimal("100"), "tokens")],
        policy_ref="policy-1",
    )
    ledger.reserve(
        "budget-1",
        graphblocks_budget.ResourceRef("run:1"),
        [graphblocks_budget.UsageAmount("model_total_tokens", Decimal("40"), "tokens")],
        purpose="provider_call",
        expires_at="later",
    )

    assert ledger.balance("budget-1").available == [
        graphblocks_budget.UsageAmount("model_total_tokens", Decimal("60"), "tokens")
    ]
    assert "SQLiteBudgetLedger" in graphblocks_budget.__all__
    ledger.close()


def test_budget_package_lazy_native_exhaustion_helper_delegates_to_runtime(monkeypatch) -> None:
    calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def admit_exhaustion_work(policy: dict[str, object], request: dict[str, object]) -> dict[str, object]:
        calls.append((policy, request))
        return {"allowed": True, "policy": policy, "request": request}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(admit_exhaustion_work=admit_exhaustion_work),
    )
    graphblocks_budget = importlib.import_module("graphblocks.budget")

    result = graphblocks_budget.admit_native_exhaustion_work(
        {"preset": "finish_current_turn", "unit": "turn"},
        {"workKind": "declared_finalization", "workEpoch": 8},
    )

    assert result == {
        "allowed": True,
        "policy": {"preset": "finish_current_turn", "unit": "turn"},
        "request": {"workKind": "declared_finalization", "workEpoch": 8},
    }
    assert calls == [
        (
            {"preset": "finish_current_turn", "unit": "turn"},
            {"workKind": "declared_finalization", "workEpoch": 8},
        )
    ]
    assert "admit_native_exhaustion_work" in graphblocks_budget.__all__


def test_budget_package_lazy_native_budget_ledger_helper_delegates_to_runtime(monkeypatch) -> None:
    calls: list[object] = []

    def evaluate_budget_ledger(operations: object) -> dict[str, object]:
        calls.append(operations)
        return {"ok": True, "operations": operations}

    monkeypatch.setitem(
        sys.modules,
        "graphblocks_runtime",
        SimpleNamespace(evaluate_budget_ledger=evaluate_budget_ledger),
    )
    graphblocks_budget = importlib.import_module("graphblocks.budget")
    operations = [{"op": "allocate", "budgetId": "budget-1"}]

    result = graphblocks_budget.evaluate_native_budget_ledger(operations)

    assert result == {
        "ok": True,
        "operations": [{"op": "allocate", "budgetId": "budget-1"}],
    }
    assert calls == [operations]
    assert "evaluate_native_budget_ledger" in graphblocks_budget.__all__


def test_budget_package_exposes_canonical_literal_sets(monkeypatch) -> None:
    graphblocks_budget = importlib.import_module("graphblocks.budget")

    assert graphblocks_budget.VALID_BUDGET_STATUSES is VALID_BUDGET_STATUSES
    assert graphblocks_budget.VALID_RESERVATION_PURPOSES is VALID_RESERVATION_PURPOSES
    assert graphblocks_budget.VALID_RESERVATION_STATUSES is VALID_RESERVATION_STATUSES
    assert graphblocks_budget.VALID_COMPLETION_RESERVE_PURPOSES is VALID_COMPLETION_RESERVE_PURPOSES
    assert graphblocks_budget.VALID_COMPLETION_RESERVE_STATUSES is VALID_COMPLETION_RESERVE_STATUSES
    assert {"active", "exhausted", "paused", "closed"}.issubset(graphblocks_budget.VALID_BUDGET_STATUSES)
    assert {"provider_call", "tool", "cleanup"}.issubset(graphblocks_budget.VALID_RESERVATION_PURPOSES)
    assert "VALID_BUDGET_STATUSES" in graphblocks_budget.__all__
