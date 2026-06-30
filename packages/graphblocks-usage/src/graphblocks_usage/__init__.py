from __future__ import annotations

from graphblocks.budget import UsageAmount
from graphblocks.usage import (
    InMemoryUsageLedger,
    SQLiteUsageLedger,
    UsageConfidence,
    UsageLedgerError,
    UsageRecord,
    UsageRecordConflictError,
    UsageRecordNotFoundError,
    UsageSource,
    VALID_USAGE_CONFIDENCES,
    VALID_USAGE_SOURCES,
)


def evaluate_native_usage_ledger(operations: object, *, run_id: str | None = None) -> dict[str, object]:
    from graphblocks_runtime import evaluate_usage_ledger

    return evaluate_usage_ledger(operations, run_id=run_id)


__all__ = [
    "evaluate_native_usage_ledger",
    "InMemoryUsageLedger",
    "SQLiteUsageLedger",
    "UsageAmount",
    "UsageConfidence",
    "UsageLedgerError",
    "UsageRecord",
    "UsageRecordConflictError",
    "UsageRecordNotFoundError",
    "UsageSource",
    "VALID_USAGE_CONFIDENCES",
    "VALID_USAGE_SOURCES",
]
