from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks_budget import BudgetAccount, BudgetReservation, BudgetSettlement, ResourceRef, UsageAmount


class PostgresBudgetAdapterError(ValueError):
    """Raised when a Postgres budget SQL contract is invalid."""


def _validate_identifier(identifier: str) -> None:
    if not identifier or not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise PostgresBudgetAdapterError(f"invalid SQL identifier: {identifier!r}")


def _resource_contract(resource: ResourceRef) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "resource_kind": resource.resource_kind,
        "tenant_id": resource.tenant_id,
        "attributes": dict(sorted(resource.attributes.items())),
    }


def _amount_contract(amount: UsageAmount) -> dict[str, object]:
    return {
        "kind": amount.kind,
        "amount": str(amount.amount),
        "unit": amount.unit,
        "dimensions": dict(sorted(amount.dimensions.items())),
    }


@dataclass(frozen=True, slots=True)
class PostgresStatement:
    name: str
    sql: str
    params: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PostgresBudgetAdapterError("statement name must not be empty")
        if not self.sql.strip():
            raise PostgresBudgetAdapterError("statement SQL must not be empty")
        object.__setattr__(self, "params", dict(sorted(self.params.items())))


@dataclass(frozen=True, slots=True)
class PostgresBudgetSchema:
    schema: str = "graphblocks_budget"

    def __post_init__(self) -> None:
        _validate_identifier(self.schema)

    def migration_statements(self) -> tuple[str, ...]:
        return (
            f"CREATE SCHEMA IF NOT EXISTS {self.schema};",
            f"""
CREATE TABLE IF NOT EXISTS {self.schema}.budget_accounts (
  budget_id text PRIMARY KEY,
  scope_json jsonb NOT NULL,
  allocated_json jsonb NOT NULL,
  parent_budget_id text NULL,
  status text NOT NULL,
  policy_ref text NOT NULL,
  revision bigint NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
""".strip(),
            f"""
CREATE TABLE IF NOT EXISTS {self.schema}.budget_reservations (
  reservation_id text PRIMARY KEY,
  budget_id text NOT NULL REFERENCES {self.schema}.budget_accounts(budget_id),
  owner_json jsonb NOT NULL,
  amounts_json jsonb NOT NULL,
  purpose text NOT NULL,
  expires_at timestamptz NOT NULL,
  fencing_token bigint NOT NULL,
  status text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
""".strip(),
            f"""
CREATE TABLE IF NOT EXISTS {self.schema}.budget_settlements (
  reservation_id text PRIMARY KEY REFERENCES {self.schema}.budget_reservations(reservation_id),
  budget_id text NOT NULL REFERENCES {self.schema}.budget_accounts(budget_id),
  committed_json jsonb NOT NULL,
  released_json jsonb NOT NULL,
  overdraft_json jsonb NOT NULL,
  status text NOT NULL,
  revision bigint NOT NULL,
  settled_at timestamptz NOT NULL DEFAULT now()
);
""".strip(),
        )


def encode_budget_account(account: BudgetAccount) -> dict[str, object]:
    return {
        "budget_id": account.budget_id,
        "scope_json": _resource_contract(account.scope),
        "allocated_json": [_amount_contract(amount) for amount in account.allocated],
        "parent_budget_id": account.parent_budget_id,
        "status": account.status,
        "policy_ref": account.policy_ref,
        "revision": account.revision,
    }


def encode_budget_reservation(reservation: BudgetReservation) -> dict[str, object]:
    return {
        "reservation_id": reservation.reservation_id,
        "budget_id": reservation.budget_id,
        "owner_json": _resource_contract(reservation.owner),
        "amounts_json": [_amount_contract(amount) for amount in reservation.amounts],
        "purpose": reservation.purpose,
        "expires_at": reservation.expires_at,
        "fencing_token": reservation.fencing_token,
        "status": reservation.status,
    }


def encode_budget_settlement(settlement: BudgetSettlement) -> dict[str, object]:
    return {
        "reservation_id": settlement.reservation_id,
        "budget_id": settlement.budget_id,
        "committed_json": [_amount_contract(amount) for amount in settlement.committed],
        "released_json": [_amount_contract(amount) for amount in settlement.released],
        "overdraft_json": [_amount_contract(amount) for amount in settlement.overdraft],
        "status": settlement.status,
        "revision": settlement.revision,
    }


def upsert_budget_account_statement(
    account: BudgetAccount,
    *,
    schema: PostgresBudgetSchema | None = None,
) -> PostgresStatement:
    schema = schema or PostgresBudgetSchema()
    return PostgresStatement(
        name="budget_account_upsert",
        sql=f"""
INSERT INTO {schema.schema}.budget_accounts (
  budget_id,
  scope_json,
  allocated_json,
  parent_budget_id,
  status,
  policy_ref,
  revision
) VALUES (
  %(budget_id)s,
  %(scope_json)s,
  %(allocated_json)s,
  %(parent_budget_id)s,
  %(status)s,
  %(policy_ref)s,
  %(revision)s
)
ON CONFLICT (budget_id) DO UPDATE SET
  scope_json = EXCLUDED.scope_json,
  allocated_json = EXCLUDED.allocated_json,
  parent_budget_id = EXCLUDED.parent_budget_id,
  status = EXCLUDED.status,
  policy_ref = EXCLUDED.policy_ref,
  revision = EXCLUDED.revision,
  updated_at = now()
WHERE {schema.schema}.budget_accounts.revision <= EXCLUDED.revision;
""".strip(),
        params=encode_budget_account(account),
    )


def upsert_budget_reservation_statement(
    reservation: BudgetReservation,
    *,
    schema: PostgresBudgetSchema | None = None,
) -> PostgresStatement:
    schema = schema or PostgresBudgetSchema()
    return PostgresStatement(
        name="budget_reservation_upsert",
        sql=f"""
INSERT INTO {schema.schema}.budget_reservations (
  reservation_id,
  budget_id,
  owner_json,
  amounts_json,
  purpose,
  expires_at,
  fencing_token,
  status
) VALUES (
  %(reservation_id)s,
  %(budget_id)s,
  %(owner_json)s,
  %(amounts_json)s,
  %(purpose)s,
  %(expires_at)s,
  %(fencing_token)s,
  %(status)s
)
ON CONFLICT (reservation_id) DO UPDATE SET
  budget_id = EXCLUDED.budget_id,
  owner_json = EXCLUDED.owner_json,
  amounts_json = EXCLUDED.amounts_json,
  purpose = EXCLUDED.purpose,
  expires_at = EXCLUDED.expires_at,
  fencing_token = EXCLUDED.fencing_token,
  status = EXCLUDED.status,
  updated_at = now();
""".strip(),
        params=encode_budget_reservation(reservation),
    )


def append_budget_settlement_statement(
    settlement: BudgetSettlement,
    *,
    schema: PostgresBudgetSchema | None = None,
) -> PostgresStatement:
    schema = schema or PostgresBudgetSchema()
    return PostgresStatement(
        name="budget_settlement_append",
        sql=f"""
INSERT INTO {schema.schema}.budget_settlements (
  reservation_id,
  budget_id,
  committed_json,
  released_json,
  overdraft_json,
  status,
  revision
) VALUES (
  %(reservation_id)s,
  %(budget_id)s,
  %(committed_json)s,
  %(released_json)s,
  %(overdraft_json)s,
  %(status)s,
  %(revision)s
)
ON CONFLICT (reservation_id) DO NOTHING;
""".strip(),
        params=encode_budget_settlement(settlement),
    )


__all__ = [
    "append_budget_settlement_statement",
    "PostgresBudgetAdapterError",
    "PostgresBudgetSchema",
    "PostgresStatement",
    "encode_budget_account",
    "encode_budget_reservation",
    "encode_budget_settlement",
    "upsert_budget_account_statement",
    "upsert_budget_reservation_statement",
]
