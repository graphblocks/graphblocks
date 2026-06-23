from __future__ import annotations

from decimal import Decimal
import importlib
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _add_budget_postgres_paths(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-budget" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-budget-postgres" / "src"))


def _add_usage_postgres_paths(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-budget" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-usage" / "src"))
    monkeypatch.syspath_prepend(str(ROOT / "packages" / "graphblocks-usage-postgres" / "src"))


def test_budget_postgres_schema_and_account_codec(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    account = graphblocks_budget.BudgetAccount(
        budget_id="budget-1",
        scope=graphblocks_budget.ResourceRef("tenant-1", "tenant"),
        allocated=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("100"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        policy_ref="policy/budget-standard",
        revision=3,
    )

    assert schema.migration_statements()[0].startswith("CREATE SCHEMA IF NOT EXISTS gb_budget")
    assert "CREATE TABLE IF NOT EXISTS gb_budget.budget_accounts" in "\n".join(schema.migration_statements())
    assert graphblocks_budget_postgres.encode_budget_account(account) == {
        "budget_id": "budget-1",
        "scope_json": {
            "resource_id": "tenant-1",
            "resource_kind": "tenant",
            "tenant_id": None,
            "attributes": {},
        },
        "allocated_json": [
            {
                "kind": "model_total_tokens",
                "amount": "100",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "parent_budget_id": None,
        "status": "active",
        "policy_ref": "policy/budget-standard",
        "revision": 3,
    }

    statement = graphblocks_budget_postgres.upsert_budget_account_statement(account, schema=schema)
    assert statement.name == "budget_account_upsert"
    assert "ON CONFLICT (budget_id) DO UPDATE" in statement.sql
    assert statement.params["budget_id"] == "budget-1"


def test_usage_postgres_schema_and_record_codec(monkeypatch) -> None:
    _add_usage_postgres_paths(monkeypatch)
    graphblocks_usage = importlib.import_module("graphblocks_usage")
    graphblocks_usage_postgres = importlib.import_module("graphblocks_usage_postgres")
    schema = graphblocks_usage_postgres.PostgresUsageSchema(schema="gb_usage")
    record = graphblocks_usage.UsageRecord(
        record_id="usage-1",
        source="provider_reported",
        confidence="provider_exact",
        amounts=[
            graphblocks_usage.UsageAmount(
                "model_output_tokens",
                Decimal("21"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        occurred_at="2026-06-23T00:00:00Z",
        run_id="run-1",
        attempt_id="attempt-1",
        provider_response_id="response-1",
        metadata={"provider": "openai-compatible"},
    )

    assert schema.migration_statements()[0].startswith("CREATE SCHEMA IF NOT EXISTS gb_usage")
    assert "CREATE TABLE IF NOT EXISTS gb_usage.usage_records" in "\n".join(schema.migration_statements())
    assert graphblocks_usage_postgres.encode_usage_record(record) == {
        "record_id": "usage-1",
        "source": "provider_reported",
        "confidence": "provider_exact",
        "amounts_json": [
            {
                "kind": "model_output_tokens",
                "amount": "21",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "occurred_at": "2026-06-23T00:00:00Z",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "provider_response_id": "response-1",
        "pricing_ref": None,
        "reconciliation_of": None,
        "metadata_json": {"provider": "openai-compatible"},
    }

    statement = graphblocks_usage_postgres.append_usage_record_statement(record, schema=schema)
    assert statement.name == "usage_record_append"
    assert "ON CONFLICT (record_id) DO NOTHING" in statement.sql
    assert statement.params["provider_response_id"] == "response-1"
