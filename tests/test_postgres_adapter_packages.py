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


def test_budget_postgres_reservation_statement(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    reservation = graphblocks_budget.BudgetReservation(
        reservation_id="reservation-1",
        budget_id="budget-1",
        owner=graphblocks_budget.ResourceRef("run-1", "run"),
        amounts=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("40"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        purpose="provider_call",
        expires_at="2026-06-23T00:00:00Z",
        fencing_token=7,
    )

    assert graphblocks_budget_postgres.encode_budget_reservation(reservation) == {
        "reservation_id": "reservation-1",
        "budget_id": "budget-1",
        "owner_json": {
            "resource_id": "run-1",
            "resource_kind": "run",
            "tenant_id": None,
            "attributes": {},
        },
        "amounts_json": [
            {
                "kind": "model_total_tokens",
                "amount": "40",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "purpose": "provider_call",
        "expires_at": "2026-06-23T00:00:00Z",
        "fencing_token": 7,
        "status": "reserved",
    }

    statement = graphblocks_budget_postgres.upsert_budget_reservation_statement(reservation, schema=schema)

    assert statement.name == "budget_reservation_upsert"
    assert "INSERT INTO gb_budget.budget_reservations" in statement.sql
    assert "ON CONFLICT (reservation_id) DO UPDATE" in statement.sql
    assert statement.params["reservation_id"] == "reservation-1"
    assert statement.params["fencing_token"] == 7


def test_budget_postgres_settlement_statement(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    settlement = graphblocks_budget.BudgetSettlement(
        reservation_id="reservation-1",
        budget_id="budget-1",
        committed=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("25"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        released=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("15"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        status="committed",
        revision=9,
    )

    migrations = "\n".join(schema.migration_statements())
    assert "CREATE TABLE IF NOT EXISTS gb_budget.budget_settlements" in migrations
    assert graphblocks_budget_postgres.encode_budget_settlement(settlement) == {
        "reservation_id": "reservation-1",
        "budget_id": "budget-1",
        "committed_json": [
            {
                "kind": "model_total_tokens",
                "amount": "25",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "released_json": [
            {
                "kind": "model_total_tokens",
                "amount": "15",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "overdraft_json": [],
        "status": "committed",
        "revision": 9,
    }

    statement = graphblocks_budget_postgres.append_budget_settlement_statement(settlement, schema=schema)

    assert statement.name == "budget_settlement_append"
    assert "INSERT INTO gb_budget.budget_settlements" in statement.sql
    assert "ON CONFLICT (reservation_id) DO NOTHING" in statement.sql
    assert statement.params["committed_json"][0]["amount"] == "25"
    assert statement.params["revision"] == 9


def test_budget_postgres_settlement_statement_can_record_permit_link(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    settlement = graphblocks_budget.BudgetSettlement(
        reservation_id="reservation-1",
        budget_id="budget-1",
        committed=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("25"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        status="committed",
        revision=9,
    )

    migrations = "\n".join(schema.migration_statements())
    assert "permit_id text NULL REFERENCES gb_budget.budget_permits(permit_id)" in migrations

    statement = graphblocks_budget_postgres.append_budget_settlement_statement(
        settlement,
        schema=schema,
        permit_id="permit-1",
    )

    assert statement.name == "budget_settlement_append"
    assert "permit_id" in statement.sql
    assert "%(permit_id)s" in statement.sql
    assert statement.params["permit_id"] == "permit-1"


def test_budget_postgres_permit_statement(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    permit = graphblocks_budget.BudgetPermit(
        permit_id="permit-1",
        reservation_refs=("reservation-1", "reservation-2"),
        owner=graphblocks_budget.ResourceRef("worker-1", "worker"),
        atomic_unit=graphblocks_budget.ResourceRef("turn-1", "turn"),
        admission_epoch=3,
        authorized_amounts=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("40"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        continuation_profile="finish_current_turn",
        policy_snapshot_digest="sha256:policy",
        expires_at="2026-06-23T00:00:00Z",
        low_watermark=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("10"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        fencing_tokens={"budget-2": 5, "budget-1": 4},
    )

    migrations = "\n".join(schema.migration_statements())
    assert "CREATE TABLE IF NOT EXISTS gb_budget.budget_permits" in migrations
    assert graphblocks_budget_postgres.encode_budget_permit(permit) == {
        "permit_id": "permit-1",
        "reservation_refs_json": ["reservation-1", "reservation-2"],
        "owner_json": {
            "resource_id": "worker-1",
            "resource_kind": "worker",
            "tenant_id": None,
            "attributes": {},
        },
        "atomic_unit_json": {
            "resource_id": "turn-1",
            "resource_kind": "turn",
            "tenant_id": None,
            "attributes": {},
        },
        "admission_epoch": 3,
        "authorized_amounts_json": [
            {
                "kind": "model_total_tokens",
                "amount": "40",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "continuation_profile": "finish_current_turn",
        "policy_snapshot_digest": "sha256:policy",
        "expires_at": "2026-06-23T00:00:00Z",
        "low_watermark_json": [
            {
                "kind": "model_total_tokens",
                "amount": "10",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "fencing_tokens_json": {"budget-1": 4, "budget-2": 5},
    }

    statement = graphblocks_budget_postgres.append_budget_permit_statement(permit, schema=schema)

    assert statement.name == "budget_permit_append"
    assert "INSERT INTO gb_budget.budget_permits" in statement.sql
    assert "ON CONFLICT (permit_id) DO NOTHING" in statement.sql
    assert statement.params["fencing_tokens_json"] == {"budget-1": 4, "budget-2": 5}


def test_budget_postgres_completion_reserve_statement(monkeypatch) -> None:
    _add_budget_postgres_paths(monkeypatch)
    graphblocks_budget = importlib.import_module("graphblocks_budget")
    graphblocks_budget_postgres = importlib.import_module("graphblocks_budget_postgres")
    schema = graphblocks_budget_postgres.PostgresBudgetSchema(schema="gb_budget")
    reserve = graphblocks_budget.CompletionReserve(
        reserve_id="reserve-1",
        budget_id="budget-1",
        purpose="checkpoint",
        amounts=[
            graphblocks_budget.UsageAmount(
                "model_total_tokens",
                Decimal("20"),
                "tokens",
                {"model": "gpt-test"},
            )
        ],
        spendable_by=frozenset({"checkpoint.worker", "agent.finalize"}),
        expires_at="2026-06-23T00:00:00Z",
        status="spent",
        reservation_id="reservation-1",
        fencing_token=11,
    )

    migrations = "\n".join(schema.migration_statements())
    assert "CREATE TABLE IF NOT EXISTS gb_budget.completion_reserves" in migrations
    assert graphblocks_budget_postgres.encode_completion_reserve(reserve) == {
        "reserve_id": "reserve-1",
        "budget_id": "budget-1",
        "purpose": "checkpoint",
        "amounts_json": [
            {
                "kind": "model_total_tokens",
                "amount": "20",
                "unit": "tokens",
                "dimensions": {"model": "gpt-test"},
            }
        ],
        "spendable_by_json": ["agent.finalize", "checkpoint.worker"],
        "expires_at": "2026-06-23T00:00:00Z",
        "status": "spent",
        "reservation_id": "reservation-1",
        "fencing_token": 11,
    }

    statement = graphblocks_budget_postgres.upsert_completion_reserve_statement(reserve, schema=schema)

    assert statement.name == "completion_reserve_upsert"
    assert "INSERT INTO gb_budget.completion_reserves" in statement.sql
    assert "ON CONFLICT (reserve_id) DO UPDATE" in statement.sql
    assert "completion_reserves.fencing_token <= EXCLUDED.fencing_token" in statement.sql
    assert statement.params["spendable_by_json"] == ["agent.finalize", "checkpoint.worker"]


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
        quota_window_id="tenant-a:2026-06",
        execution_scope="turn:turn-1/model:generate",
        metadata={"provider": "openai-compatible"},
    )

    assert schema.migration_statements()[0].startswith("CREATE SCHEMA IF NOT EXISTS gb_usage")
    assert "CREATE TABLE IF NOT EXISTS gb_usage.usage_records" in "\n".join(schema.migration_statements())
    assert "quota_window_id text NULL" in "\n".join(schema.migration_statements())
    assert "execution_scope text NULL" in "\n".join(schema.migration_statements())
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
        "quota_window_id": "tenant-a:2026-06",
        "execution_scope": "turn:turn-1/model:generate",
        "metadata_json": {"provider": "openai-compatible"},
    }

    statement = graphblocks_usage_postgres.append_usage_record_statement(record, schema=schema)
    assert statement.name == "usage_record_append"
    assert "ON CONFLICT (record_id) DO NOTHING" in statement.sql
    assert statement.params["provider_response_id"] == "response-1"
    assert statement.params["quota_window_id"] == "tenant-a:2026-06"
