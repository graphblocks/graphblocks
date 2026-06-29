from __future__ import annotations

import json
import queue
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from graphblocks.budget import (
    BudgetCompletionReserveStateError,
    BudgetExceededError,
    SQLiteBudgetLedger,
    UsageAmount,
)
from graphblocks.policy import ResourceRef


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "tck" / "budget-race" / "cases.json").read_text(encoding="utf-8"))


def _usage_amounts(raw_amounts: list[dict[str, Any]]) -> list[UsageAmount]:
    return [
        UsageAmount(
            kind=str(amount["kind"]),
            amount=Decimal(str(amount["amount"])),
            unit=str(amount["unit"]),
        )
        for amount in raw_amounts
    ]


def _amount_contract(amounts: list[UsageAmount]) -> list[dict[str, Any]]:
    return [
        {
            "kind": amount.kind,
            "amount": int(amount.amount) if amount.amount == amount.amount.to_integral_value() else str(amount.amount),
            "unit": amount.unit,
        }
        for amount in amounts
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_sqlite_budget_ledger_matches_shared_budget_race_tck(case: dict[str, Any], tmp_path: Path) -> None:
    db_path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(db_path)
    ledger.allocate(
        str(case["budgetId"]),
        ResourceRef(str(case["scope"])),
        _usage_amounts(case["allocated"]),
        policy_ref=str(case["policyRef"]),
    )
    if case["kind"] == "completion_reserve_race":
        ledger.create_completion_reserve(
            str(case["reserveId"]),
            str(case["budgetId"]),
            purpose=str(case["reservePurpose"]),
            amounts=_usage_amounts(case["reserveAmounts"]),
            spendable_by=tuple(str(spender) for spender in case["spendableBy"]),
            expires_at=case.get("reserveExpiresAt"),
        )
    ledger.close()

    worker_inputs = case["owners"] if case["kind"] == "reservation_race" else case["spenders"]
    barrier = threading.Barrier(len(worker_inputs) + 1)
    outcomes: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()

    def run_worker(worker_input: str) -> None:
        worker_ledger = SQLiteBudgetLedger(db_path)
        try:
            barrier.wait(timeout=10)
            if case["kind"] == "reservation_race":
                try:
                    worker_ledger.reserve(
                        str(case["budgetId"]),
                        ResourceRef(str(worker_input)),
                        _usage_amounts(case["reservationAmounts"]),
                        purpose=str(case["reservationPurpose"]),
                        expires_at=str(case["expiresAt"]),
                    )
                    outcomes.put(("allowed", None))
                except BudgetExceededError:
                    outcomes.put(("denied", "BudgetExceeded"))
            else:
                try:
                    worker_ledger.spend_completion_reserve(
                        str(case["reserveId"]),
                        str(worker_input),
                        expires_at=str(case["expiresAt"]),
                    )
                    outcomes.put(("allowed", None))
                except BudgetCompletionReserveStateError:
                    outcomes.put(("denied", "CompletionReserveState"))
        except Exception as error:  # pragma: no cover - surfaced in the main test thread.
            outcomes.put(("error", repr(error)))
        finally:
            worker_ledger.close()

    threads = [threading.Thread(target=run_worker, args=(str(worker_input),)) for worker_input in worker_inputs]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join()

    observed = [outcomes.get_nowait() for _ in threads]
    errors = [detail for status, detail in observed if status == "error"]
    assert errors == []
    assert sum(1 for status, _detail in observed if status == "allowed") == case["expectedAllowed"]
    assert sum(1 for status, _detail in observed if status == "denied") == case["expectedDenied"]
    expected_denied_error = case.get("expectedDeniedError")
    if expected_denied_error is not None:
        assert [detail for status, detail in observed if status == "denied"] == [
            expected_denied_error
        ] * case["expectedDenied"]

    final_ledger = SQLiteBudgetLedger(db_path)
    balance = final_ledger.balance(str(case["budgetId"]))
    assert _amount_contract(balance.reserved) == case["expectedReserved"]
    if case["kind"] == "reservation_race":
        assert _amount_contract(balance.available) == case["expectedAvailable"]
    else:
        reserve = final_ledger.completion_reserve(str(case["reserveId"]))
        assert reserve.status == case["expectedReserveStatus"]
    final_ledger.close()
