from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path

from graphblocks.usage import VALID_USAGE_CONFIDENCES, VALID_USAGE_SOURCES


ROOT = Path(__file__).parents[1]


def test_usage_package_exposes_immutable_usage_reconciliation_contract(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-usage" / "src"))
    graphblocks_usage = importlib.import_module("graphblocks_usage")

    provisional = graphblocks_usage.UsageRecord(
        record_id="usage-provisional",
        source="tokenizer_estimated",
        confidence="estimated",
        amounts=[
            graphblocks_usage.UsageAmount(
                kind="model_output_tokens",
                amount=Decimal("18"),
                unit="tokens",
            )
        ],
        occurred_at="2026-06-23T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="resp-1",
        quota_window_id="tenant-a:2026-06",
        execution_scope="turn:turn-1",
    )
    ledger = graphblocks_usage.InMemoryUsageLedger()

    appended = ledger.append(provisional)
    reconciled = ledger.reconcile(
        appended.record_id,
        amounts=[graphblocks_usage.UsageAmount("model_output_tokens", Decimal("21"), "tokens")],
        occurred_at="2026-06-23T00:01:00Z",
        record_id="usage-reconciled",
    )

    assert reconciled.source == "reconciled"
    assert reconciled.reconciliation_of == "usage-provisional"
    assert reconciled.quota_window_id == "tenant-a:2026-06"
    assert reconciled.execution_scope == "turn:turn-1"
    assert ledger.totals_for_run("run-1") == [
        graphblocks_usage.UsageAmount("model_output_tokens", Decimal("21"), "tokens")
    ]
    assert "SQLiteUsageLedger" in graphblocks_usage.__all__


def test_usage_package_exposes_canonical_literal_sets(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-usage" / "src"))
    graphblocks_usage = importlib.import_module("graphblocks_usage")

    assert graphblocks_usage.VALID_USAGE_SOURCES is VALID_USAGE_SOURCES
    assert graphblocks_usage.VALID_USAGE_CONFIDENCES is VALID_USAGE_CONFIDENCES
    assert "reconciled" in graphblocks_usage.VALID_USAGE_SOURCES
    assert "provider_exact" in graphblocks_usage.VALID_USAGE_CONFIDENCES
    assert "VALID_USAGE_SOURCES" in graphblocks_usage.__all__
