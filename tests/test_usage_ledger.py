from __future__ import annotations

from decimal import Decimal

from graphblocks.budget import UsageAmount
from graphblocks.usage import InMemoryUsageLedger, UsageRecord


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
