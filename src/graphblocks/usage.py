from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
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
VALID_USAGE_SOURCES = frozenset(
    {
        "provider_reported",
        "runtime_measured",
        "tokenizer_estimated",
        "pricing_estimated",
        "reconciled",
    }
)
VALID_USAGE_CONFIDENCES = frozenset({"exact", "provider_exact", "estimated", "unknown"})


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
    amounts: tuple[UsageAmount, ...]
    occurred_at: str
    run_id: str | None = None
    attempt_id: str | None = None
    provider_response_id: str | None = None
    pricing_ref: str | None = None
    quota_window_id: str | None = None
    execution_scope: str | None = None
    reconciliation_of: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str):
            raise ValueError("usage record_id must be a string")
        if not self.record_id.strip():
            raise ValueError("usage record_id must not be empty")
        if self.source not in VALID_USAGE_SOURCES:
            raise ValueError(f"invalid usage source {self.source}")
        if self.confidence not in VALID_USAGE_CONFIDENCES:
            raise ValueError(f"invalid usage confidence {self.confidence}")
        if not isinstance(self.occurred_at, str):
            raise ValueError("usage occurred_at must be a string")
        if not self.occurred_at.strip():
            raise ValueError("usage occurred_at must not be empty")
        for field_name in (
            "run_id",
            "attempt_id",
            "provider_response_id",
            "pricing_ref",
            "quota_window_id",
            "execution_scope",
            "reconciliation_of",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"usage {field_name} must be a string")
            if not value.strip():
                raise ValueError(f"usage {field_name} must not be empty")
        amounts = tuple(
            UsageAmount(
                kind=amount.kind,
                amount=amount.amount,
                unit=amount.unit,
                dimensions=MappingProxyType(dict(amount.dimensions)),
            )
            for amount in self.amounts
        )
        object.__setattr__(self, "amounts", amounts)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(slots=True)
class InMemoryUsageLedger:
    _records: dict[str, UsageRecord] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _provider_dedupe: dict[tuple[str, str | None], str] = field(default_factory=dict)

    def append(self, record: UsageRecord) -> UsageRecord:
        existing = self._records.get(record.record_id)
        if existing is not None:
            if existing == record:
                return existing
            raise UsageRecordConflictError(f"usage record {record.record_id!r} already exists")
        if record.provider_response_id is not None and record.reconciliation_of is None:
            dedupe_key = (record.provider_response_id, record.attempt_id)
            existing_id = self._provider_dedupe.get(dedupe_key)
            if existing_id is not None:
                return self._records[existing_id]
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

    def totals_for_run(self, run_id: str) -> list[UsageAmount]:
        records = self.records_for_run(run_id)
        superseded_record_ids = {
            record.reconciliation_of for record in records if record.reconciliation_of is not None
        }
        totals: dict[tuple[str, str, tuple[tuple[str, str], ...]], Decimal] = {}
        for record in records:
            if record.record_id in superseded_record_ids:
                continue
            for amount in record.amounts:
                key = (amount.kind, amount.unit, tuple(sorted(amount.dimensions.items())))
                totals[key] = totals.get(key, Decimal("0")) + amount.amount
        return [
            UsageAmount(kind=kind, amount=totals[(kind, unit, dimensions)], unit=unit, dimensions=dict(dimensions))
            for kind, unit, dimensions in sorted(totals)
            if totals[(kind, unit, dimensions)] != 0
        ]

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
            quota_window_id=original.quota_window_id,
            execution_scope=original.execution_scope,
            reconciliation_of=original.record_id,
            metadata=dict(original.metadata),
        )
        return self.append(reconciled)


@dataclass(slots=True)
class SQLiteUsageLedger:
    path: str | Path
    _connection: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_records (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              record_id TEXT NOT NULL UNIQUE,
              source TEXT NOT NULL,
              confidence TEXT NOT NULL,
              amounts_json TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              run_id TEXT,
              attempt_id TEXT,
              provider_response_id TEXT,
              pricing_ref TEXT,
              quota_window_id TEXT,
              execution_scope TEXT,
              reconciliation_of TEXT,
              metadata_json TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_with_attempt
            ON usage_records(provider_response_id, attempt_id)
            WHERE provider_response_id IS NOT NULL
              AND attempt_id IS NOT NULL
              AND reconciliation_of IS NULL
            """
        )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_without_attempt
            ON usage_records(provider_response_id)
            WHERE provider_response_id IS NOT NULL
              AND attempt_id IS NULL
              AND reconciliation_of IS NULL
            """
        )
        self._connection.commit()

    @classmethod
    def in_memory(cls) -> SQLiteUsageLedger:
        return cls(":memory:")

    def close(self) -> None:
        self._connection.close()

    def append(self, record: UsageRecord) -> UsageRecord:
        try:
            existing = self.get(record.record_id)
        except UsageRecordNotFoundError:
            existing = None
        if existing is not None:
            if existing == record:
                return existing
            raise UsageRecordConflictError(f"usage record {record.record_id!r} already exists")
        if record.provider_response_id is not None and record.reconciliation_of is None:
            existing = self._provider_dedupe_record(record.provider_response_id, record.attempt_id)
            if existing is not None:
                return existing
        try:
            self._connection.execute(
                """
                INSERT INTO usage_records (
                  record_id,
                  source,
                  confidence,
                  amounts_json,
                  occurred_at,
                  run_id,
                  attempt_id,
                  provider_response_id,
                  pricing_ref,
                  quota_window_id,
                  execution_scope,
                  reconciliation_of,
                  metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.source,
                    record.confidence,
                    json.dumps(
                        [
                            {
                                "kind": amount.kind,
                                "amount": str(amount.amount),
                                "unit": amount.unit,
                                "dimensions": dict(sorted(amount.dimensions.items())),
                            }
                            for amount in record.amounts
                        ],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    record.occurred_at,
                    record.run_id,
                    record.attempt_id,
                    record.provider_response_id,
                    record.pricing_ref,
                    record.quota_window_id,
                    record.execution_scope,
                    record.reconciliation_of,
                    json.dumps(dict(sorted(record.metadata.items())), sort_keys=True, separators=(",", ":")),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            if record.provider_response_id is not None and record.reconciliation_of is None:
                existing = self._provider_dedupe_record(record.provider_response_id, record.attempt_id)
                if existing is not None:
                    return existing
            raise UsageRecordConflictError(f"usage record {record.record_id!r} already exists") from error
        return record

    def get(self, record_id: str) -> UsageRecord:
        row = self._connection.execute(
            "SELECT * FROM usage_records WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise UsageRecordNotFoundError(f"usage record {record_id!r} does not exist")
        return self._record_from_row(row)

    def records_for_run(self, run_id: str) -> list[UsageRecord]:
        rows = self._connection.execute(
            "SELECT * FROM usage_records WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def totals_for_run(self, run_id: str) -> list[UsageAmount]:
        records = self.records_for_run(run_id)
        superseded_record_ids = {
            record.reconciliation_of for record in records if record.reconciliation_of is not None
        }
        totals: dict[tuple[str, str, tuple[tuple[str, str], ...]], Decimal] = {}
        for record in records:
            if record.record_id in superseded_record_ids:
                continue
            for amount in record.amounts:
                key = (amount.kind, amount.unit, tuple(sorted(amount.dimensions.items())))
                totals[key] = totals.get(key, Decimal("0")) + amount.amount
        return [
            UsageAmount(kind=kind, amount=totals[(kind, unit, dimensions)], unit=unit, dimensions=dict(dimensions))
            for kind, unit, dimensions in sorted(totals)
            if totals[(kind, unit, dimensions)] != 0
        ]

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
            quota_window_id=original.quota_window_id,
            execution_scope=original.execution_scope,
            reconciliation_of=original.record_id,
            metadata=dict(original.metadata),
        )
        return self.append(reconciled)

    def _provider_dedupe_record(self, provider_response_id: str, attempt_id: str | None) -> UsageRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM usage_records
            WHERE provider_response_id = ?
              AND ((attempt_id IS NULL AND ? IS NULL) OR attempt_id = ?)
              AND reconciliation_of IS NULL
            ORDER BY sequence
            LIMIT 1
            """,
            (provider_response_id, attempt_id, attempt_id),
        ).fetchone()
        return None if row is None else self._record_from_row(row)

    def _record_from_row(self, row: sqlite3.Row) -> UsageRecord:
        amounts = []
        for amount in json.loads(row["amounts_json"]):
            amounts.append(
                UsageAmount(
                    kind=str(amount["kind"]),
                    amount=Decimal(str(amount["amount"])),
                    unit=str(amount["unit"]),
                    dimensions={str(key): str(value) for key, value in dict(amount.get("dimensions", {})).items()},
                )
            )
        return UsageRecord(
            record_id=str(row["record_id"]),
            source=row["source"],
            confidence=row["confidence"],
            amounts=amounts,
            occurred_at=str(row["occurred_at"]),
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
            provider_response_id=row["provider_response_id"],
            pricing_ref=row["pricing_ref"],
            quota_window_id=row["quota_window_id"],
            execution_scope=row["execution_scope"],
            reconciliation_of=row["reconciliation_of"],
            metadata=dict(json.loads(row["metadata_json"])),
        )
