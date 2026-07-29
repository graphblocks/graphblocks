import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from graphblocks.budget import (
    InMemoryBudgetLedger,
    SQLiteBudgetLedger,
    UsageAmount,
    _budget_ledger_to_snapshot,
)
from graphblocks.canonical import canonical_dumps
from graphblocks.policy import ResourceRef


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(
        kind="model_total_tokens",
        amount=Decimal(value),
        unit="tokens",
    )


def test_sqlite_budget_mutation_writes_only_touched_account_rows(
    tmp_path: Path,
) -> None:
    ledger = SQLiteBudgetLedger(tmp_path / "budget.sqlite3")
    for index in range(64):
        ledger.allocate(
            f"budget-{index:03d}",
            ResourceRef(f"tenant:{index:03d}"),
            [_tokens("100")],
            policy_ref="policy-1",
        )

    ledger._connection.execute(
        "CREATE TEMP TABLE touched_budget_accounts (budget_id TEXT NOT NULL)"
    )
    ledger._connection.execute(
        """
        CREATE TEMP TRIGGER record_touched_budget_account
        AFTER UPDATE ON budget_ledger_accounts
        BEGIN
          INSERT INTO touched_budget_accounts (budget_id)
          VALUES (NEW.budget_id);
        END
        """
    )
    statements: list[str] = []
    ledger._connection.set_trace_callback(statements.append)

    ledger.reserve(
        "budget-063",
        ResourceRef("run:1"),
        [_tokens("10")],
        purpose="provider_call",
        expires_at="2026-07-29T02:00:00Z",
    )

    ledger._connection.set_trace_callback(None)
    touched = ledger._connection.execute(
        "SELECT budget_id FROM touched_budget_accounts"
    ).fetchall()
    snapshot_object = ledger._connection.execute(
        """
        SELECT type
        FROM sqlite_master
        WHERE name = 'budget_ledger_snapshots'
        """
    ).fetchone()
    mutation_count = ledger._connection.execute(
        "SELECT COUNT(*) AS count FROM budget_ledger_mutations"
    ).fetchone()

    assert [row["budget_id"] for row in touched] == ["budget-063"]
    assert snapshot_object["type"] == "view"
    assert mutation_count["count"] == 65
    assert not any(
        "FROM BUDGET_LEDGER_ACCOUNTS" in statement.upper()
        for statement in statements
    )
    assert not any(
        "UPDATE BUDGET_LEDGER_SNAPSHOTS" in statement.upper()
        for statement in statements
    )
    ledger.close()


def test_sqlite_budget_handles_refresh_by_generation_without_lost_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    first = SQLiteBudgetLedger(path)
    second = SQLiteBudgetLedger(path)

    first.allocate(
        "budget-a",
        ResourceRef("tenant:a"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    assert second.balance("budget-a").available == [_tokens("100")]

    second.reserve(
        "budget-a",
        ResourceRef("run:1"),
        [_tokens("25")],
        purpose="task",
        expires_at="2026-07-29T02:00:00Z",
    )
    assert first.balance("budget-a").reserved == [_tokens("25")]

    first.close()
    second.close()


def test_sqlite_budget_concurrent_accounts_commit_independently(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    setup = SQLiteBudgetLedger(path)
    for suffix in ("a", "b"):
        setup.allocate(
            f"budget-{suffix}",
            ResourceRef(f"tenant:{suffix}"),
            [_tokens("100")],
            policy_ref="policy-1",
        )
    setup.close()

    barrier = Barrier(2)

    def reserve(suffix: str) -> str:
        ledger = SQLiteBudgetLedger(path)
        try:
            barrier.wait()
            reservation = ledger.reserve(
                f"budget-{suffix}",
                ResourceRef(f"run:{suffix}"),
                [_tokens("10")],
                purpose="task",
                expires_at="2026-07-29T02:00:00Z",
            )
            return reservation.reservation_id
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservation_ids = tuple(
            executor.map(reserve, ("a", "b"))
        )

    reopened = SQLiteBudgetLedger(path)
    assert len(set(reservation_ids)) == 2
    assert reopened.balance("budget-a").available == [_tokens("90")]
    assert reopened.balance("budget-b").available == [_tokens("90")]
    reopened.close()


def test_sqlite_budget_mutation_is_atomic_when_generation_commit_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    ledger = SQLiteBudgetLedger(path)
    ledger.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    ledger._connection.execute(
        """
        CREATE TRIGGER reject_budget_generation_update
        BEFORE UPDATE OF generation ON budget_ledger_metadata
        BEGIN
          SELECT RAISE(ABORT, 'injected generation failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="generation failure"):
        ledger.reserve(
            "budget-1",
            ResourceRef("run:1"),
            [_tokens("10")],
            purpose="task",
            expires_at="2026-07-29T02:00:00Z",
        )

    reservation_count = ledger._connection.execute(
        "SELECT COUNT(*) AS count FROM budget_ledger_reservations"
    ).fetchone()
    mutation_count = ledger._connection.execute(
        "SELECT COUNT(*) AS count FROM budget_ledger_mutations"
    ).fetchone()
    assert reservation_count["count"] == 0
    assert mutation_count["count"] == 1
    assert ledger.balance("budget-1").available == [_tokens("100")]
    ledger.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.balance("budget-1").available == [_tokens("100")]
    reopened.close()


def test_sqlite_budget_migrates_legacy_snapshot_once(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.sqlite3"
    legacy = InMemoryBudgetLedger()
    legacy.allocate(
        "budget-1",
        ResourceRef("tenant:acme"),
        [_tokens("100")],
        policy_ref="policy-1",
    )
    legacy.reserve(
        "budget-1",
        ResourceRef("run:1"),
        [_tokens("10")],
        purpose="task",
        expires_at="2026-07-29T02:00:00Z",
    )
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE budget_ledger_snapshots (
          snapshot_id TEXT PRIMARY KEY,
          state_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO budget_ledger_snapshots (snapshot_id, state_json)
        VALUES (?, ?)
        """,
        (
            "default",
            canonical_dumps(_budget_ledger_to_snapshot(legacy)),
        ),
    )
    connection.commit()
    connection.close()

    migrated = SQLiteBudgetLedger(path)
    object_rows = migrated._connection.execute(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE name IN (
          'budget_ledger_snapshots',
          'budget_ledger_legacy_snapshots'
        )
        ORDER BY name
        """
    ).fetchall()
    mutation = migrated._connection.execute(
        """
        SELECT mutation_kind, touched_json
        FROM budget_ledger_mutations
        WHERE generation = 1
        """
    ).fetchone()

    assert migrated.balance("budget-1").reserved == [_tokens("10")]
    assert [(row["name"], row["type"]) for row in object_rows] == [
        ("budget_ledger_legacy_snapshots", "table"),
        ("budget_ledger_snapshots", "view"),
    ]
    assert mutation["mutation_kind"] == "legacy_snapshot_migrated"
    assert json.loads(mutation["touched_json"]) == {
        "snapshot_ids": ["default"]
    }
    migrated.close()

    reopened = SQLiteBudgetLedger(path)
    assert reopened.balance("budget-1").reserved == [_tokens("10")]
    migration_count = reopened._connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM budget_ledger_mutations
        WHERE mutation_kind = 'legacy_snapshot_migrated'
        """
    ).fetchone()
    assert migration_count["count"] == 1
    reopened.close()
