from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import math
import sqlite3
from threading import Barrier

import pytest

from graphblocks.budget import UsageAmount
from graphblocks.usage import (
    InMemoryUsageLedger,
    SQLiteUsageLedger,
    UsageRecord,
    UsageRecordConflictError,
    UsageRecordNotFoundError,
)


def _tokens(value: str) -> UsageAmount:
    return UsageAmount(kind="model_output_tokens", amount=Decimal(value), unit="tokens")


def test_usage_ledger_appends_immutable_records_and_queries_by_run() -> None:
    ledger = InMemoryUsageLedger()
    record = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("12")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )

    appended = ledger.append(record)

    assert appended == record
    assert ledger.records_for_run("run-1") == [record]
    assert ledger.records_for_run("missing") == []


def test_usage_record_deep_copies_mutable_amounts_and_metadata() -> None:
    amounts = [_tokens("12")]
    context = {"tags": ["initial"]}
    metadata = {"phase": "generation", "context": context}
    record = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=amounts,
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        metadata=metadata,
    )
    amounts.append(_tokens("99"))
    metadata["phase"] = "mutated"
    context["tags"].append("mutated")

    assert record.amounts == (_tokens("12"),)
    assert record.metadata == {"phase": "generation", "context": {"tags": ["initial"]}}
    with pytest.raises(AttributeError):
        record.amounts.append(_tokens("13"))  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        record.amounts[0].dimensions["scope"] = "direct"
    with pytest.raises(TypeError):
        record.metadata["phase"] = "direct"
    with pytest.raises(TypeError):
        record.metadata["context"]["tags"] += ("direct",)  # type: ignore[index,operator]
    with pytest.raises(TypeError):
        dict.__setitem__(record.metadata, "phase", "bypassed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage metadata must be a mapping"):
        UsageRecord(
            record_id="usage-2",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
            metadata=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="usage metadata keys must be non-empty strings"):
        UsageRecord(
            record_id="usage-2",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
            metadata={" ": "generation"},
        )
    with pytest.raises(ValueError, match="usage metadata must be valid strict JSON"):
        UsageRecord(
            record_id="usage-2",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
            metadata={"invalid": object()},
        )
    with pytest.raises(ValueError, match="usage metadata must be valid strict JSON"):
        UsageRecord(
            record_id="usage-2",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
            metadata={"invalid": math.nan},
        )


def test_usage_record_rejects_invalid_identity_source_and_confidence() -> None:
    with pytest.raises(ValueError, match="usage record_id must be a string"):
        UsageRecord(
            record_id=object(),  # type: ignore[arg-type]
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
        )

    with pytest.raises(ValueError, match="usage record_id must not be empty"):
        UsageRecord(
            record_id=" ",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
        )

    with pytest.raises(ValueError, match="invalid usage source manual"):
        UsageRecord(
            record_id="usage-1",
            source="manual",  # type: ignore[arg-type]
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
        )

    with pytest.raises(ValueError, match="invalid usage confidence guessed"):
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="guessed",  # type: ignore[arg-type]
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
        )

    with pytest.raises(ValueError, match="usage occurred_at must not be empty"):
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="",
        )

    with pytest.raises(ValueError, match="usage occurred_at must be a string"):
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="usage occurred_at must be an ISO datetime"):
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="later",
        )

    optional_identity_cases = (
        ({"run_id": " "}, "usage run_id must not be empty"),
        ({"attempt_id": ""}, "usage attempt_id must not be empty"),
        ({"provider_response_id": "\t"}, "usage provider_response_id must not be empty"),
        ({"reconciliation_of": " "}, "usage reconciliation_of must not be empty"),
    )
    for overrides, message in optional_identity_cases:
        with pytest.raises(ValueError, match=message):
            UsageRecord(
                record_id="usage-1",
                source="runtime_measured",
                confidence="estimated",
                amounts=[_tokens("12")],
                occurred_at="2026-06-22T00:00:00Z",
                **overrides,
            )

    with pytest.raises(ValueError, match="usage amounts must not be empty"):
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="estimated",
            amounts=[],
            occurred_at="2026-06-22T00:00:00Z",
        )

    for amounts in (object(), [object()], "tokens"):
        with pytest.raises(ValueError, match="usage amounts must be UsageAmount"):
            UsageRecord(
                record_id="usage-1",
                source="runtime_measured",
                confidence="estimated",
                amounts=amounts,  # type: ignore[arg-type]
                occurred_at="2026-06-22T00:00:00Z",
            )


def test_usage_timestamps_reject_non_rfc3339_forms() -> None:
    for occurred_at in (
        "2026-06-22 00:00:00Z",
        "2026-06-22T00:00:00",
        "2026-06-22T00:00:00+0000",
        "2026-06-22T00:00:00z",
        " 2026-06-22T00:00:00Z",
    ):
        with pytest.raises(ValueError, match="usage occurred_at must be an ISO datetime"):
            UsageRecord(
                record_id="usage-1",
                source="runtime_measured",
                confidence="estimated",
                amounts=[_tokens("12")],
                occurred_at=occurred_at,
            )

    ledger = InMemoryUsageLedger()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
        )
    )

    with pytest.raises(ValueError, match="usage occurred_at must be an ISO datetime"):
        ledger.reconcile(
            provisional.record_id,
            amounts=[_tokens("21")],
            occurred_at="2026-06-22T00:05:00+0000",
            record_id="usage-reconciled",
        )


def test_usage_ledger_replays_identical_records_without_double_counting() -> None:
    ledger = InMemoryUsageLedger()
    record = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("12")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    changed = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("13")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert ledger.append(record) == record
    assert ledger.append(record) == record
    with pytest.raises(UsageRecordConflictError):
        ledger.append(changed)
    assert ledger.records_for_run("run-1") == [record]
    assert ledger.totals_for_run("run-1") == [_tokens("12")]


def test_usage_ledger_deduplicates_provider_response_for_same_attempt() -> None:
    ledger = InMemoryUsageLedger()
    first = UsageRecord(
        record_id="usage-1",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[_tokens("20")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="resp-1",
    )
    duplicate = UsageRecord(
        record_id="usage-duplicate",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[_tokens("20")],
        occurred_at="2026-06-22T00:00:01Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="resp-1",
    )

    assert ledger.append(first) == first
    assert ledger.append(duplicate) == first
    assert ledger.records_for_run("run-1") == [first]


def test_usage_ledger_reconcile_writes_new_record_for_late_final_usage() -> None:
    ledger = InMemoryUsageLedger()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
            provider_response_id="resp-1",
            pricing_ref="pricing-2026-06",
            quota_window_id="tenant-a:2026-06",
            execution_scope="turn:turn-1/tool:call-1",
            metadata={"tool_call_id": "call-1", "tool_name": "knowledge.search"},
        )
    )

    reconciled = ledger.reconcile(
        provisional.record_id,
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        record_id="usage-reconciled",
    )

    assert reconciled.source == "reconciled"
    assert reconciled.confidence == "exact"
    assert reconciled.reconciliation_of == "usage-provisional"
    assert reconciled.provider_response_id == "resp-1"
    assert reconciled.pricing_ref == "pricing-2026-06"
    assert reconciled.quota_window_id == "tenant-a:2026-06"
    assert reconciled.execution_scope == "turn:turn-1/tool:call-1"
    assert reconciled.metadata == {"tool_call_id": "call-1", "tool_name": "knowledge.search"}
    assert ledger.records_for_run("run-1") == [provisional, reconciled]


def test_usage_ledger_totals_replace_provisional_with_reconciled_usage() -> None:
    ledger = InMemoryUsageLedger()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
            provider_response_id="resp-1",
        )
    )
    ledger.append(
        UsageRecord(
            record_id="usage-runtime",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("2")],
            occurred_at="2026-06-22T00:00:01Z",
            run_id="run-1",
            attempt_id="attempt-2",
        )
    )
    ledger.reconcile(
        provisional.record_id,
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        record_id="usage-reconciled",
    )

    assert ledger.totals_for_run("run-1") == [_tokens("23")]


def test_usage_ledger_rejects_multiple_reconciliations_for_same_source_record() -> None:
    ledger = InMemoryUsageLedger()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
            provider_response_id="resp-1",
        )
    )
    first = ledger.reconcile(
        provisional.record_id,
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        record_id="usage-reconciled-1",
    )

    with pytest.raises(UsageRecordConflictError, match="already has a reconciliation"):
        ledger.reconcile(
            provisional.record_id,
            amounts=[_tokens("22")],
            occurred_at="2026-06-22T00:06:00Z",
            record_id="usage-reconciled-2",
        )

    assert ledger.records_for_run("run-1") == [provisional, first]
    assert ledger.totals_for_run("run-1") == [_tokens("21")]


def test_usage_ledger_rejects_reconciliation_before_source_record() -> None:
    ledger = InMemoryUsageLedger()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:05:00Z",
            run_id="run-1",
        )
    )

    with pytest.raises(ValueError, match="reconciled usage occurred_at must not precede source usage"):
        ledger.reconcile(
            provisional.record_id,
            amounts=[_tokens("21")],
            occurred_at="2026-06-22T00:04:59Z",
            record_id="usage-reconciled",
        )

    assert ledger.records_for_run("run-1") == [provisional]
    assert ledger.totals_for_run("run-1") == [_tokens("18")]


@pytest.mark.parametrize("ledger_factory", (InMemoryUsageLedger, SQLiteUsageLedger.in_memory))
def test_usage_ledger_rejects_orphaned_and_cross_identity_reconciliations(ledger_factory) -> None:
    ledger = ledger_factory()
    orphaned = UsageRecord(
        record_id="usage-orphaned",
        source="reconciled",
        confidence="exact",
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        run_id="run-1",
        reconciliation_of="usage-missing",
    )
    with pytest.raises(UsageRecordNotFoundError, match="usage-missing"):
        ledger.append(orphaned)

    source = ledger.append(
        UsageRecord(
            record_id="usage-source",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
        )
    )
    cross_run = UsageRecord(
        record_id="usage-cross-run",
        source="reconciled",
        confidence="exact",
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        run_id="run-2",
        attempt_id="attempt-1",
        reconciliation_of=source.record_id,
    )
    with pytest.raises(UsageRecordConflictError, match="must preserve source record"):
        ledger.append(cross_run)

    assert ledger.records_for_run("run-1") == [source]
    assert ledger.records_for_run("run-2") == []
    if isinstance(ledger, SQLiteUsageLedger):
        ledger.close()


def test_sqlite_usage_ledger_persists_records_across_reopen(tmp_path) -> None:
    path = tmp_path / "usage.sqlite3"
    record = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("12")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        quota_window_id="tenant-a:2026-06",
        execution_scope="turn:turn-1/model:generate",
        metadata={"phase": "generation"},
    )

    ledger = SQLiteUsageLedger(path)
    assert ledger.append(record) == record
    ledger.close()

    reopened = SQLiteUsageLedger(path)
    assert reopened.records_for_run("run-1") == [record]
    assert reopened.get("usage-1") == record
    reopened.close()


def test_sqlite_usage_ledger_replays_identical_records_without_double_counting() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    record = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("12")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    changed = UsageRecord(
        record_id="usage-1",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("13")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )

    assert ledger.append(record) == record
    assert ledger.append(record) == record
    with pytest.raises(UsageRecordConflictError):
        ledger.append(changed)
    assert ledger.records_for_run("run-1") == [record]
    assert ledger.totals_for_run("run-1") == [_tokens("12")]
    ledger.close()


def test_sqlite_usage_ledger_deduplicates_racing_identical_record_ids(
    tmp_path,
) -> None:
    database = tmp_path / "concurrent-usage.sqlite3"
    barrier = Barrier(2)
    record = UsageRecord(
        record_id="usage-racing",
        source="runtime_measured",
        confidence="estimated",
        amounts=[_tokens("12")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
    )

    class CoordinatedUsageLedger(SQLiteUsageLedger):
        def get(self, record_id: str) -> UsageRecord:
            try:
                return super().get(record_id)
            except UsageRecordNotFoundError:
                barrier.wait()
                raise

    def append() -> tuple[UsageRecord, bool]:
        ledger = CoordinatedUsageLedger(database)
        try:
            appended = ledger.append(record)
            return appended, ledger._connection.in_transaction
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: append(), range(2)))

    assert results == [(record, False), (record, False)]
    ledger = SQLiteUsageLedger(database)
    assert ledger.records_for_run("run-1") == [record]
    ledger.close()


def test_sqlite_usage_ledger_deduplicates_and_reconciles_late_usage() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    first = UsageRecord(
        record_id="usage-1",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[_tokens("20")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="resp-1",
        quota_window_id="tenant-a:2026-06",
        execution_scope="turn:turn-1/tool:call-1",
        metadata={"tool_call_id": "call-1", "tool_name": "ticket.create"},
    )
    duplicate = UsageRecord(
        record_id="usage-duplicate",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[_tokens("20")],
        occurred_at="2026-06-22T00:00:01Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="resp-1",
    )

    assert ledger.append(first) == first
    assert ledger.append(duplicate) == first
    reconciled = ledger.reconcile(
        "usage-1",
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        record_id="usage-reconciled",
    )

    assert reconciled.source == "reconciled"
    assert reconciled.reconciliation_of == "usage-1"
    assert reconciled.quota_window_id == "tenant-a:2026-06"
    assert reconciled.execution_scope == "turn:turn-1/tool:call-1"
    assert reconciled.metadata == {"tool_call_id": "call-1", "tool_name": "ticket.create"}
    assert ledger.records_for_run("run-1") == [first, reconciled]
    assert ledger.totals_for_run("run-1") == [_tokens("21")]
    ledger.close()


def test_sqlite_usage_ledger_rejects_multiple_reconciliations_for_same_source_record() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
            attempt_id="attempt-1",
            provider_response_id="resp-1",
        )
    )
    first = ledger.reconcile(
        provisional.record_id,
        amounts=[_tokens("21")],
        occurred_at="2026-06-22T00:05:00Z",
        record_id="usage-reconciled-1",
    )

    with pytest.raises(UsageRecordConflictError, match="already has a reconciliation"):
        ledger.reconcile(
            provisional.record_id,
            amounts=[_tokens("22")],
            occurred_at="2026-06-22T00:06:00Z",
            record_id="usage-reconciled-2",
        )

    assert ledger.records_for_run("run-1") == [provisional, first]
    assert ledger.totals_for_run("run-1") == [_tokens("21")]
    ledger.close()


def test_sqlite_usage_ledger_rejects_reconciliation_before_source_record() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    provisional = ledger.append(
        UsageRecord(
            record_id="usage-provisional",
            source="tokenizer_estimated",
            confidence="estimated",
            amounts=[_tokens("18")],
            occurred_at="2026-06-22T00:05:00Z",
            run_id="run-1",
        )
    )

    with pytest.raises(ValueError, match="reconciled usage occurred_at must not precede source usage"):
        ledger.reconcile(
            provisional.record_id,
            amounts=[_tokens("21")],
            occurred_at="2026-06-22T00:04:59Z",
            record_id="usage-reconciled",
        )

    assert ledger.records_for_run("run-1") == [provisional]
    assert ledger.totals_for_run("run-1") == [_tokens("18")]
    ledger.close()


@pytest.mark.parametrize(
    ("column", "payload", "field_name"),
    (
        (
            "amounts_json",
            '[{"kind":"model_output_tokens","amount":NaN,"unit":"tokens","dimensions":{}}]',
            "amounts_json",
        ),
        ("metadata_json", '{"value": NaN}', "metadata_json"),
    ),
)
def test_sqlite_usage_ledger_rejects_non_standard_json_constants_on_replay(
    column: str,
    payload: str,
    field_name: str,
) -> None:
    ledger = SQLiteUsageLedger.in_memory()
    ledger.append(
        UsageRecord(
            record_id="usage-1",
            source="runtime_measured",
            confidence="estimated",
            amounts=[_tokens("12")],
            occurred_at="2026-06-22T00:00:00Z",
            run_id="run-1",
        )
    )
    ledger._connection.execute(
        f"UPDATE usage_records SET {column} = ? WHERE record_id = ?",
        (payload, "usage-1"),
    )
    ledger._connection.commit()

    with pytest.raises(ValueError, match=f"usage ledger {field_name} must be valid strict JSON"):
        ledger.records_for_run("run-1")
    ledger.close()


def test_sqlite_usage_ledger_enforces_provider_dedupe_for_null_attempt_at_storage_boundary() -> None:
    ledger = SQLiteUsageLedger.in_memory()
    first = UsageRecord(
        record_id="usage-1",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[_tokens("20")],
        occurred_at="2026-06-22T00:00:00Z",
        run_id="run-1",
        provider_response_id="resp-1",
    )

    ledger.append(first)

    with pytest.raises(sqlite3.IntegrityError):
        ledger._connection.execute(
            """
            INSERT INTO usage_records (
              record_id,
              source,
              confidence,
              amounts_json,
              occurred_at,
              run_id,
              provider_response_id,
              metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "usage-duplicate",
                "provider_reported",
                "provider_exact",
                json.dumps(
                    [
                        {
                            "kind": "model_output_tokens",
                            "amount": "20",
                            "unit": "tokens",
                            "dimensions": {},
                        }
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "2026-06-22T00:00:01Z",
                "run-1",
                "resp-1",
                "{}",
            ),
        )
    ledger.close()
