from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from graphblocks.budget import (
    BudgetLedgerClosedError,
    SQLiteBudgetLedger,
    UsageAmount,
)
from graphblocks.policy import ResourceRef


def _tokens(value: int) -> UsageAmount:
    return UsageAmount(
        kind="model_total_tokens",
        amount=Decimal(value),
        unit="tokens",
    )


def test_sqlite_budget_ledger_serializes_one_connection_across_threads() -> None:
    ledger = SQLiteBudgetLedger.in_memory()

    def allocate(index: int) -> str:
        return ledger.allocate(
            f"budget-{index:03d}",
            ResourceRef(f"tenant:tenant-{index:03d}"),
            [_tokens(100)],
            policy_ref="policy-1",
        ).budget_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        budget_ids = tuple(executor.map(allocate, range(32)))
        balances = tuple(executor.map(ledger.balance, budget_ids))

    assert budget_ids == tuple(
        f"budget-{index:03d}" for index in range(32)
    )
    assert all(balance.available == [_tokens(100)] for balance in balances)
    ledger.close()


def test_sqlite_budget_ledger_fails_closed_after_cross_thread_close() -> None:
    ledger = SQLiteBudgetLedger.in_memory()
    account = ledger.allocate(
        "budget-1",
        ResourceRef("tenant:tenant-1"),
        [_tokens(100)],
        policy_ref="policy-1",
    )
    ledger.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ledger.balance, account.budget_id)
        with pytest.raises(
            BudgetLedgerClosedError,
            match="SQLite budget ledger is closed",
        ):
            future.result()

    ledger.close()
