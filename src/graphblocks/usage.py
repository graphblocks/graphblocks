from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .budget import UsageAmount


UsageSource = Literal[
    "provider_reported",
    "runtime_measured",
    "tokenizer_estimated",
    "pricing_estimated",
    "reconciled",
]
UsageConfidence = Literal["exact", "provider_exact", "estimated", "unknown"]


class UsageLedgerError(RuntimeError):
    pass


class UsageRecordNotFoundError(UsageLedgerError):
    pass


class UsageRecordConflictError(UsageLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class UsageRecord:
    record_id: str
    source: UsageSource
    confidence: UsageConfidence
    amounts: list[UsageAmount]
    occurred_at: str
    run_id: str | None = None
    attempt_id: str | None = None
    provider_response_id: str | None = None
    pricing_ref: str | None = None
    reconciliation_of: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class InMemoryUsageLedger:
    _records: dict[str, UsageRecord] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _provider_dedupe: dict[tuple[str, str | None], str] = field(default_factory=dict)

    def append(self, record: UsageRecord) -> UsageRecord:
        if record.provider_response_id is not None and record.reconciliation_of is None:
            dedupe_key = (record.provider_response_id, record.attempt_id)
            existing_id = self._provider_dedupe.get(dedupe_key)
            if existing_id is not None:
                return self._records[existing_id]
        if record.record_id in self._records:
            raise UsageRecordConflictError(f"usage record {record.record_id!r} already exists")
        self._records[record.record_id] = record
        self._order.append(record.record_id)
        if record.provider_response_id is not None and record.reconciliation_of is None:
            self._provider_dedupe[(record.provider_response_id, record.attempt_id)] = record.record_id
        return record

    def get(self, record_id: str) -> UsageRecord:
        record = self._records.get(record_id)
        if record is None:
            raise UsageRecordNotFoundError(f"usage record {record_id!r} does not exist")
        return record

    def records_for_run(self, run_id: str) -> list[UsageRecord]:
        return [self._records[record_id] for record_id in self._order if self._records[record_id].run_id == run_id]

    def reconcile(
        self,
        source_record_id: str,
        *,
        amounts: list[UsageAmount],
        occurred_at: str,
        record_id: str | None = None,
    ) -> UsageRecord:
        original = self.get(source_record_id)
        reconciled = UsageRecord(
            record_id=record_id or f"{source_record_id}:reconciled",
            source="reconciled",
            confidence="exact",
            amounts=amounts,
            occurred_at=occurred_at,
            run_id=original.run_id,
            attempt_id=original.attempt_id,
            provider_response_id=original.provider_response_id,
            pricing_ref=original.pricing_ref,
            reconciliation_of=original.record_id,
            metadata=dict(original.metadata),
        )
        return self.append(reconciled)
