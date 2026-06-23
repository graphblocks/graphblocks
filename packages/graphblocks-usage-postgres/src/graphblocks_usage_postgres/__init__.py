from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graphblocks_usage import UsageAmount, UsageRecord


class PostgresUsageAdapterError(ValueError):
    """Raised when a Postgres usage SQL contract is invalid."""


def _validate_identifier(identifier: str) -> None:
    if not identifier or not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise PostgresUsageAdapterError(f"invalid SQL identifier: {identifier!r}")


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
            raise PostgresUsageAdapterError("statement name must not be empty")
        if not self.sql.strip():
            raise PostgresUsageAdapterError("statement SQL must not be empty")
        object.__setattr__(self, "params", dict(sorted(self.params.items())))


@dataclass(frozen=True, slots=True)
class PostgresUsageSchema:
    schema: str = "graphblocks_usage"

    def __post_init__(self) -> None:
        _validate_identifier(self.schema)

    def migration_statements(self) -> tuple[str, ...]:
        return (
            f"CREATE SCHEMA IF NOT EXISTS {self.schema};",
            f"""
CREATE TABLE IF NOT EXISTS {self.schema}.usage_records (
  record_id text PRIMARY KEY,
  source text NOT NULL,
  confidence text NOT NULL,
  amounts_json jsonb NOT NULL,
  occurred_at timestamptz NOT NULL,
  run_id text NULL,
  attempt_id text NULL,
  provider_response_id text NULL,
  pricing_ref text NULL,
  reconciliation_of text NULL,
  metadata_json jsonb NOT NULL,
  inserted_at timestamptz NOT NULL DEFAULT now()
);
""".strip(),
            f"""
CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe
ON {self.schema}.usage_records(provider_response_id, attempt_id)
WHERE provider_response_id IS NOT NULL AND reconciliation_of IS NULL;
""".strip(),
        )


def encode_usage_record(record: UsageRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "source": record.source,
        "confidence": record.confidence,
        "amounts_json": [_amount_contract(amount) for amount in record.amounts],
        "occurred_at": record.occurred_at,
        "run_id": record.run_id,
        "attempt_id": record.attempt_id,
        "provider_response_id": record.provider_response_id,
        "pricing_ref": record.pricing_ref,
        "reconciliation_of": record.reconciliation_of,
        "metadata_json": dict(sorted(record.metadata.items())),
    }


def append_usage_record_statement(
    record: UsageRecord,
    *,
    schema: PostgresUsageSchema | None = None,
) -> PostgresStatement:
    schema = schema or PostgresUsageSchema()
    return PostgresStatement(
        name="usage_record_append",
        sql=f"""
INSERT INTO {schema.schema}.usage_records (
  record_id,
  source,
  confidence,
  amounts_json,
  occurred_at,
  run_id,
  attempt_id,
  provider_response_id,
  pricing_ref,
  reconciliation_of,
  metadata_json
) VALUES (
  %(record_id)s,
  %(source)s,
  %(confidence)s,
  %(amounts_json)s,
  %(occurred_at)s,
  %(run_id)s,
  %(attempt_id)s,
  %(provider_response_id)s,
  %(pricing_ref)s,
  %(reconciliation_of)s,
  %(metadata_json)s
)
ON CONFLICT (record_id) DO NOTHING;
""".strip(),
        params=encode_usage_record(record),
    )


__all__ = [
    "PostgresStatement",
    "PostgresUsageAdapterError",
    "PostgresUsageSchema",
    "append_usage_record_statement",
    "encode_usage_record",
]
