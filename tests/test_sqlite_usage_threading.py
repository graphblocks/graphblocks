from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from graphblocks.budget import UsageAmount
from graphblocks.usage import (
    SQLiteUsageLedger,
    UsageLedgerClosedError,
    UsageRecord,
)


def _record(index: int) -> UsageRecord:
    return UsageRecord(
        record_id=f"usage-{index:03d}",
        source="runtime_measured",
        confidence="exact",
        amounts=(
            UsageAmount(
                kind="model_total_tokens",
                amount=Decimal(1),
                unit="tokens",
            ),
        ),
        occurred_at="2026-07-29T00:00:00Z",
        run_id="run-1",
        attempt_id=f"attempt-{index:03d}",
    )


def test_sqlite_usage_ledger_serializes_one_connection_across_threads() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    records = tuple(_record(index) for index in range(48))

    with ThreadPoolExecutor(max_workers=8) as executor:
        appended = tuple(executor.map(ledger.append, records))

    assert appended == records
    assert {
        record.record_id
        for record in ledger.records_for_run("run-1")
    } == {record.record_id for record in records}
    assert ledger.totals_for_run("run-1") == [
        UsageAmount(
            kind="model_total_tokens",
            amount=Decimal(48),
            unit="tokens",
        )
    ]
    ledger.close()


def test_sqlite_usage_ledger_fails_closed_after_cross_thread_close() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    record = ledger.append(_record(1))
    ledger.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(ledger.get, record.record_id)
        with pytest.raises(
            UsageLedgerClosedError,
            match="SQLite usage ledger is closed",
        ):
            future.result()

    ledger.close()
