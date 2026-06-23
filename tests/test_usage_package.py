from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path


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
    assert ledger.totals_for_run("run-1") == [
        graphblocks_usage.UsageAmount("model_output_tokens", Decimal("21"), "tokens")
    ]
