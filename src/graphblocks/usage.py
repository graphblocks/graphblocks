from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from types import MappingProxyType
from typing import Literal

from .budget import UsageAmount
from .canonical import MAX_CANONICAL_JSON_DEPTH, canonical_dumps


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


class _FrozenUsageMetadataList(tuple[object, ...]):
    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return super().__eq__(other)


def _freeze_usage_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_usage_metadata(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return _FrozenUsageMetadataList(_freeze_usage_metadata(item) for item in value)
    return value


def _mutable_usage_metadata(
    value: object,
    *,
    _active_containers: set[int] | None = None,
    _depth: int = 0,
) -> object:
    if _depth > MAX_CANONICAL_JSON_DEPTH:
        raise ValueError(
            "usage metadata nesting must not exceed "
            f"{MAX_CANONICAL_JSON_DEPTH} levels"
        )
    active_containers = set() if _active_containers is None else _active_containers
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("usage metadata must not be recursive")
        active_containers.add(identity)
        try:
            return {
                key: _mutable_usage_metadata(
                    item,
                    _active_containers=active_containers,
                    _depth=_depth + 1,
                )
                for key, item in value.items()
            }
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise ValueError("usage metadata must not be recursive")
        active_containers.add(identity)
        try:
            return [
                _mutable_usage_metadata(
                    item,
                    _active_containers=active_containers,
                    _depth=_depth + 1,
                )
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    return value


def _parse_datetime(owner: str, field_name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{owner} {field_name} must not be empty")
    normalized = value
    if normalized != normalized.strip() or len(normalized) <= 19 or normalized[10] != "T":
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    timezone_start = 19
    if normalized[timezone_start] == ".":
        timezone_start += 1
        while timezone_start < len(normalized) and normalized[timezone_start].isdigit():
            timezone_start += 1
        if timezone_start == 20:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    suffix = normalized[timezone_start:]
    if suffix == "Z":
        normalized = f"{normalized[:timezone_start]}+00:00"
    elif (
        len(suffix) == 6
        and suffix[0] in {"+", "-"}
        and suffix[1:3].isdigit()
        and suffix[3] == ":"
        and suffix[4:6].isdigit()
    ):
        offset_hours = int(suffix[1:3])
        offset_minutes = int(suffix[4:6])
        if offset_hours > 23 or offset_minutes > 59:
            raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    else:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{owner} {field_name} must be an ISO datetime") from error
    return parsed.astimezone(timezone.utc)


def _validate_reconciliation_order(source: UsageRecord, occurred_at: str) -> None:
    if _parse_datetime("usage", "occurred_at", occurred_at) < _parse_datetime(
        "usage",
        "occurred_at",
        source.occurred_at,
    ):
        raise ValueError("reconciled usage occurred_at must not precede source usage")


def _validate_reconciliation_record(record: UsageRecord, source: UsageRecord) -> None:
    if source.reconciliation_of is not None:
        raise UsageRecordConflictError("reconciled usage cannot itself be reconciled")
    if record.source != "reconciled" or record.confidence != "exact":
        raise UsageRecordConflictError(
            "usage reconciliation must have reconciled source and exact confidence"
        )
    identity_fields = (
        "run_id",
        "attempt_id",
        "provider_response_id",
        "pricing_ref",
        "quota_window_id",
        "execution_scope",
        "metadata",
    )
    if any(getattr(record, field_name) != getattr(source, field_name) for field_name in identity_fields):
        raise UsageRecordConflictError(
            f"usage reconciliation must preserve source record {source.record_id!r} identity"
        )
    _validate_reconciliation_order(source, record.occurred_at)


def _usage_provider_duplicate_conflict(
    existing: UsageRecord,
    incoming: UsageRecord,
) -> bool:
    fields = (
        "source",
        "confidence",
        "amounts",
        "occurred_at",
        "run_id",
        "attempt_id",
        "provider_response_id",
        "pricing_ref",
        "quota_window_id",
        "execution_scope",
        "reconciliation_of",
        "metadata",
    )
    return any(
        getattr(existing, field_name) != getattr(incoming, field_name)
        for field_name in fields
    )


def _loads_strict_json(field_name: str, value: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise ValueError(f"duplicate JSON object key {key!r}")
            decoded[key] = item
        return decoded

    try:
        return json.loads(
            value,
            parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)),
            object_pairs_hook=reject_duplicate_keys,
        )
    except ValueError as error:
        raise ValueError(f"usage ledger {field_name} must be valid strict JSON") from error


def _dumps_strict_json(field_name: str, value: object) -> str:
    try:
        return canonical_dumps(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"usage ledger {field_name} must be valid strict JSON") from error


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
        if self.record_id != self.record_id.strip():
            raise ValueError("usage record_id must not contain surrounding whitespace")
        if self.source not in VALID_USAGE_SOURCES:
            raise ValueError(f"invalid usage source {self.source}")
        if self.confidence not in VALID_USAGE_CONFIDENCES:
            raise ValueError(f"invalid usage confidence {self.confidence}")
        _parse_datetime("usage", "occurred_at", self.occurred_at)
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
            if value != value.strip():
                raise ValueError(
                    f"usage {field_name} must not contain surrounding whitespace"
                )
        if self.source == "reconciled":
            if self.reconciliation_of is None:
                raise ValueError(
                    "reconciled usage records must identify reconciliation_of"
                )
            if self.confidence != "exact":
                raise ValueError("reconciled usage records must have exact confidence")
        elif self.reconciliation_of is not None:
            raise ValueError("usage reconciliation_of requires reconciled source")
        try:
            raw_amounts = tuple(self.amounts)
        except TypeError as error:
            raise ValueError("usage amounts must be UsageAmount") from error
        if not raw_amounts:
            raise ValueError("usage amounts must not be empty")
        if any(not isinstance(amount, UsageAmount) for amount in raw_amounts):
            raise ValueError("usage amounts must be UsageAmount")
        amounts = tuple(
            UsageAmount(
                kind=amount.kind,
                amount=amount.amount,
                unit=amount.unit,
                dimensions=MappingProxyType(dict(amount.dimensions)),
            )
            for amount in raw_amounts
        )
        object.__setattr__(self, "amounts", amounts)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("usage metadata must be a mapping")
        if any(not isinstance(key, str) or not key.strip() for key in self.metadata):
            raise ValueError("usage metadata keys must be non-empty strings")
        try:
            metadata = _mutable_usage_metadata(self.metadata)
            canonical_dumps(metadata)
        except (TypeError, ValueError) as error:
            raise ValueError("usage metadata must be valid strict JSON") from error
        assert isinstance(metadata, dict)
        object.__setattr__(self, "metadata", _freeze_usage_metadata(metadata))


@dataclass(slots=True)
class InMemoryUsageLedger:
    _records: dict[str, UsageRecord] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _provider_dedupe: dict[tuple[str, str | None], str] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def append(self, record: UsageRecord) -> UsageRecord:
        with self._lock:
            existing = self._records.get(record.record_id)
            if existing is not None:
                if existing == record:
                    return existing
                raise UsageRecordConflictError(f"usage record {record.record_id!r} already exists")
            if record.reconciliation_of is not None:
                original = self._records.get(record.reconciliation_of)
                if original is None:
                    raise UsageRecordNotFoundError(
                        f"usage record {record.reconciliation_of!r} does not exist"
                    )
                _validate_reconciliation_record(record, original)
                existing_reconciliation = self._reconciliation_for(record.reconciliation_of)
                if existing_reconciliation is not None:
                    raise UsageRecordConflictError(
                        f"usage record {record.reconciliation_of!r} already has a reconciliation"
                    )
            if record.provider_response_id is not None and record.reconciliation_of is None:
                dedupe_key = (record.provider_response_id, record.attempt_id)
                existing_id = self._provider_dedupe.get(dedupe_key)
                if existing_id is not None:
                    existing = self._records[existing_id]
                    if _usage_provider_duplicate_conflict(existing, record):
                        raise UsageRecordConflictError(
                            f"provider response {record.provider_response_id!r} conflicts with existing usage"
                        )
                    return existing
            self._records[record.record_id] = record
            self._order.append(record.record_id)
            if record.provider_response_id is not None and record.reconciliation_of is None:
                self._provider_dedupe[(record.provider_response_id, record.attempt_id)] = record.record_id
            return record

    def _reconciliation_for(self, source_record_id: str) -> UsageRecord | None:
        for record_id in self._order:
            record = self._records[record_id]
            if record.reconciliation_of == source_record_id:
                return record
        return None

    def get(self, record_id: str) -> UsageRecord:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                raise UsageRecordNotFoundError(f"usage record {record_id!r} does not exist")
            return record

    def records_for_run(self, run_id: str) -> list[UsageRecord]:
        with self._lock:
            return [
                self._records[record_id]
                for record_id in self._order
                if self._records[record_id].run_id == run_id
            ]

    def totals_for_run(self, run_id: str) -> list[UsageAmount]:
        with self._lock:
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
                UsageAmount(
                    kind=kind,
                    amount=totals[(kind, unit, dimensions)],
                    unit=unit,
                    dimensions=dict(dimensions),
                )
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
        with self._lock:
            original = self.get(source_record_id)
            _validate_reconciliation_order(original, occurred_at)
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
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS usage_records_single_reconciliation
            ON usage_records(reconciliation_of)
            WHERE reconciliation_of IS NOT NULL
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
        if record.reconciliation_of is not None:
            original = self.get(record.reconciliation_of)
            _validate_reconciliation_record(record, original)
            existing_reconciliation = self._reconciliation_for(record.reconciliation_of)
            if existing_reconciliation is not None:
                raise UsageRecordConflictError(
                    f"usage record {record.reconciliation_of!r} already has a reconciliation"
                )
        if record.provider_response_id is not None and record.reconciliation_of is None:
            existing = self._provider_dedupe_record(record.provider_response_id, record.attempt_id)
            if existing is not None:
                if _usage_provider_duplicate_conflict(existing, record):
                    raise UsageRecordConflictError(
                        f"provider response {record.provider_response_id!r} conflicts with existing usage"
                    )
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
                    _dumps_strict_json(
                        "amounts_json",
                        [
                            {
                                "kind": amount.kind,
                                "amount": str(amount.amount),
                                "unit": amount.unit,
                                "dimensions": dict(sorted(amount.dimensions.items())),
                            }
                            for amount in record.amounts
                        ],
                    ),
                    record.occurred_at,
                    record.run_id,
                    record.attempt_id,
                    record.provider_response_id,
                    record.pricing_ref,
                    record.quota_window_id,
                    record.execution_scope,
                    record.reconciliation_of,
                    _dumps_strict_json(
                        "metadata_json",
                        _mutable_usage_metadata(record.metadata),
                    ),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            try:
                existing = self.get(record.record_id)
            except UsageRecordNotFoundError:
                existing = None
            if existing is not None:
                if existing == record:
                    return existing
                raise UsageRecordConflictError(
                    f"usage record {record.record_id!r} already exists"
                ) from error
            if record.provider_response_id is not None and record.reconciliation_of is None:
                existing = self._provider_dedupe_record(record.provider_response_id, record.attempt_id)
                if existing is not None:
                    if _usage_provider_duplicate_conflict(existing, record):
                        raise UsageRecordConflictError(
                            f"provider response {record.provider_response_id!r} conflicts with existing usage"
                        ) from error
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
        _validate_reconciliation_order(original, occurred_at)
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

    def _reconciliation_for(self, source_record_id: str) -> UsageRecord | None:
        row = self._connection.execute(
            "SELECT * FROM usage_records WHERE reconciliation_of = ? ORDER BY sequence LIMIT 1",
            (source_record_id,),
        ).fetchone()
        return None if row is None else self._record_from_row(row)

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
        raw_amounts = _loads_strict_json("amounts_json", row["amounts_json"])
        if not isinstance(raw_amounts, list):
            raise ValueError("usage ledger amounts_json must be an array")
        amounts = []
        for amount in raw_amounts:
            if not isinstance(amount, Mapping):
                raise ValueError("usage ledger amounts_json items must be objects")
            dimensions = amount.get("dimensions", {})
            if not isinstance(dimensions, Mapping):
                raise ValueError(
                    "usage ledger amounts_json dimensions must be an object"
                )
            amounts.append(
                UsageAmount(
                    kind=amount.get("kind"),  # type: ignore[arg-type]
                    amount=amount.get("amount"),  # type: ignore[arg-type]
                    unit=amount.get("unit"),  # type: ignore[arg-type]
                    dimensions=dict(dimensions),
                )
            )
        metadata = _loads_strict_json("metadata_json", row["metadata_json"])
        if not isinstance(metadata, Mapping):
            raise ValueError("usage ledger metadata_json must be an object")
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
            metadata=dict(metadata),
        )


def evaluate_native_usage_ledger(
    operations: object,
    *,
    run_id: str | None = None,
) -> dict[str, object]:
    from graphblocks_runtime import evaluate_usage_ledger

    return evaluate_usage_ledger(operations, run_id=run_id)


__all__ = [
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
    "evaluate_native_usage_ledger",
]
