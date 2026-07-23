from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from graphblocks.usage import UsageAmount, UsageRecord


class PostgresUsageAdapterError(ValueError):
    """Raised when a Postgres usage SQL contract is invalid."""


def _validate_identifier(identifier: object) -> None:
    if (
        not isinstance(identifier, str)
        or not identifier
        or not identifier.replace("_", "").isalnum()
        or identifier[0].isdigit()
    ):
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
        if not isinstance(self.name, str) or not self.name.strip():
            raise PostgresUsageAdapterError("statement name must not be empty")
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise PostgresUsageAdapterError("statement SQL must not be empty")
        if not isinstance(self.params, Mapping) or any(
            not isinstance(name, str) for name in self.params
        ):
            raise PostgresUsageAdapterError("statement params must be a string-keyed mapping")
        object.__setattr__(
            self,
            "params",
            deepcopy(dict(sorted(self.params.items()))),
        )


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
  quota_window_id text NULL,
  execution_scope text NULL,
  reconciliation_of text NULL,
  metadata_json jsonb NOT NULL,
  inserted_at timestamptz NOT NULL DEFAULT now()
);
""".strip(),
            f"""
CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_with_attempt
ON {self.schema}.usage_records(provider_response_id, attempt_id)
WHERE provider_response_id IS NOT NULL
  AND attempt_id IS NOT NULL
  AND reconciliation_of IS NULL;
""".strip(),
            f"""
CREATE UNIQUE INDEX IF NOT EXISTS usage_records_provider_dedupe_without_attempt
ON {self.schema}.usage_records(provider_response_id)
WHERE provider_response_id IS NOT NULL
  AND attempt_id IS NULL
  AND reconciliation_of IS NULL;
""".strip(),
            f"""
CREATE UNIQUE INDEX IF NOT EXISTS usage_records_single_reconciliation
ON {self.schema}.usage_records(reconciliation_of)
WHERE reconciliation_of IS NOT NULL;
""".strip(),
            f"""
CREATE OR REPLACE FUNCTION {self.schema}.append_usage_record(
  p_record_id text,
  p_source text,
  p_confidence text,
  p_amounts_json jsonb,
  p_occurred_at timestamptz,
  p_run_id text,
  p_attempt_id text,
  p_provider_response_id text,
  p_pricing_ref text,
  p_quota_window_id text,
  p_execution_scope text,
  p_reconciliation_of text,
  p_metadata_json jsonb
)
RETURNS TABLE (append_status text, record_id text)
LANGUAGE plpgsql
VOLATILE
SET search_path = pg_catalog
AS $graphblocks_usage_append$
DECLARE
  existing {self.schema}.usage_records%ROWTYPE;
  lock_key bigint;
  append_attempt integer;
BEGIN
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION USING
      ERRCODE = '0A000',
      MESSAGE = 'GraphBlocks usage append requires READ COMMITTED transaction isolation; ' ||
        'roll back and retry the whole transaction at READ COMMITTED';
  END IF;

  FOR lock_key IN
    SELECT DISTINCT lock_values.value
    FROM pg_catalog.unnest(
      ARRAY[
        pg_catalog.hashtextextended('{self.schema}:record:' || p_record_id, 0),
        CASE
          WHEN p_provider_response_id IS NOT NULL AND p_reconciliation_of IS NULL
          THEN pg_catalog.hashtextextended(
            '{self.schema}:provider:' || p_provider_response_id || ':attempt:' ||
              COALESCE(p_attempt_id, '<null>'),
            0
          )
          ELSE NULL
        END,
        CASE
          WHEN p_reconciliation_of IS NOT NULL
          THEN pg_catalog.hashtextextended(
            '{self.schema}:reconciliation:' || p_reconciliation_of,
            0
          )
          ELSE NULL
        END
      ]::bigint[]
    ) AS lock_values(value)
    WHERE lock_values.value IS NOT NULL
    ORDER BY lock_values.value
  LOOP
    PERFORM pg_catalog.pg_advisory_xact_lock(lock_key);
  END LOOP;

  FOR append_attempt IN 1..2
  LOOP
    SELECT stored.*
    INTO existing
    FROM {self.schema}.usage_records AS stored
    WHERE stored.record_id = p_record_id;
    IF FOUND THEN
      IF ROW(
        existing.source,
        existing.confidence,
        existing.amounts_json,
        existing.occurred_at,
        existing.run_id,
        existing.attempt_id,
        existing.provider_response_id,
        existing.pricing_ref,
        existing.quota_window_id,
        existing.execution_scope,
        existing.reconciliation_of,
        existing.metadata_json
      ) IS NOT DISTINCT FROM ROW(
        p_source,
        p_confidence,
        p_amounts_json,
        p_occurred_at,
        p_run_id,
        p_attempt_id,
        p_provider_response_id,
        p_pricing_ref,
        p_quota_window_id,
        p_execution_scope,
        p_reconciliation_of,
        p_metadata_json
      ) THEN
        RETURN QUERY SELECT 'deduplicated'::text, existing.record_id;
        RETURN;
      END IF;
      RAISE EXCEPTION USING
        ERRCODE = '23505',
        MESSAGE = 'usage record ' || p_record_id || ' already exists with a different payload',
        SCHEMA = '{self.schema}',
        TABLE = 'usage_records',
        CONSTRAINT = 'usage_records_pkey';
    END IF;

    IF p_reconciliation_of IS NOT NULL THEN
      IF p_source IS DISTINCT FROM 'reconciled'
        OR p_confidence IS DISTINCT FROM 'exact'
      THEN
        RAISE EXCEPTION USING
          ERRCODE = '23514',
          MESSAGE = 'usage reconciliation must have reconciled source and exact confidence',
          SCHEMA = '{self.schema}',
          TABLE = 'usage_records',
          CONSTRAINT = 'usage_records_reconciliation_contract';
      END IF;

      SELECT stored.*
      INTO existing
      FROM {self.schema}.usage_records AS stored
      WHERE stored.record_id = p_reconciliation_of;
      IF NOT FOUND THEN
        RAISE EXCEPTION USING
          ERRCODE = '23503',
          MESSAGE = 'usage record ' || p_reconciliation_of || ' does not exist',
          SCHEMA = '{self.schema}',
          TABLE = 'usage_records',
          CONSTRAINT = 'usage_records_reconciliation_source';
      END IF;
      IF ROW(
        existing.run_id,
        existing.attempt_id,
        existing.provider_response_id,
        existing.pricing_ref,
        existing.quota_window_id,
        existing.execution_scope,
        existing.metadata_json
      ) IS DISTINCT FROM ROW(
        p_run_id,
        p_attempt_id,
        p_provider_response_id,
        p_pricing_ref,
        p_quota_window_id,
        p_execution_scope,
        p_metadata_json
      ) OR p_occurred_at < existing.occurred_at THEN
        RAISE EXCEPTION USING
          ERRCODE = '23514',
          MESSAGE = 'usage reconciliation must preserve source record ' ||
            p_reconciliation_of || ' identity and ordering',
          SCHEMA = '{self.schema}',
          TABLE = 'usage_records',
          CONSTRAINT = 'usage_records_reconciliation_contract';
      END IF;

      SELECT stored.*
      INTO existing
      FROM {self.schema}.usage_records AS stored
      WHERE stored.reconciliation_of = p_reconciliation_of
      LIMIT 1;
      IF FOUND THEN
        RAISE EXCEPTION USING
          ERRCODE = '23505',
          MESSAGE = 'usage record ' || p_reconciliation_of || ' already has a reconciliation',
          SCHEMA = '{self.schema}',
          TABLE = 'usage_records',
          CONSTRAINT = 'usage_records_single_reconciliation';
      END IF;
    ELSIF p_source IS NOT DISTINCT FROM 'reconciled' THEN
      RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'reconciled usage requires a source record',
        SCHEMA = '{self.schema}',
        TABLE = 'usage_records',
        CONSTRAINT = 'usage_records_reconciliation_contract';
    END IF;

    IF p_provider_response_id IS NOT NULL AND p_reconciliation_of IS NULL THEN
      SELECT stored.*
      INTO existing
      FROM {self.schema}.usage_records AS stored
      WHERE stored.provider_response_id = p_provider_response_id
        AND stored.attempt_id IS NOT DISTINCT FROM p_attempt_id
        AND stored.reconciliation_of IS NULL
      LIMIT 1;
      IF FOUND THEN
        IF ROW(
          existing.source,
          existing.confidence,
          existing.amounts_json,
          existing.occurred_at,
          existing.run_id,
          existing.attempt_id,
          existing.provider_response_id,
          existing.pricing_ref,
          existing.quota_window_id,
          existing.execution_scope,
          existing.reconciliation_of,
          existing.metadata_json
        ) IS DISTINCT FROM ROW(
          p_source,
          p_confidence,
          p_amounts_json,
          p_occurred_at,
          p_run_id,
          p_attempt_id,
          p_provider_response_id,
          p_pricing_ref,
          p_quota_window_id,
          p_execution_scope,
          p_reconciliation_of,
          p_metadata_json
        ) THEN
          RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = 'provider response ' || p_provider_response_id ||
              ' conflicts with existing usage',
            SCHEMA = '{self.schema}',
            TABLE = 'usage_records';
        END IF;
        RETURN QUERY SELECT 'deduplicated'::text, existing.record_id;
        RETURN;
      END IF;
    END IF;

    BEGIN
      INSERT INTO {self.schema}.usage_records (
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
      ) VALUES (
        p_record_id,
        p_source,
        p_confidence,
        p_amounts_json,
        p_occurred_at,
        p_run_id,
        p_attempt_id,
        p_provider_response_id,
        p_pricing_ref,
        p_quota_window_id,
        p_execution_scope,
        p_reconciliation_of,
        p_metadata_json
      )
      RETURNING * INTO existing;
      RETURN QUERY SELECT 'inserted'::text, existing.record_id;
      RETURN;
    EXCEPTION
      WHEN unique_violation THEN
        IF append_attempt = 2 THEN
          RAISE;
        END IF;
    END;
  END LOOP;

  RAISE EXCEPTION 'usage record append did not resolve';
END;
$graphblocks_usage_append$;
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
        "quota_window_id": record.quota_window_id,
        "execution_scope": record.execution_scope,
        "reconciliation_of": record.reconciliation_of,
        "metadata_json": dict(sorted(record.metadata.items())),
    }


def append_usage_record_statement(
    record: UsageRecord,
    *,
    schema: PostgresUsageSchema | None = None,
) -> PostgresStatement:
    """Build an append that inserts or deduplicates and raises on conflicts.

    The database function supports READ COMMITTED transactions only. Callers
    encountering an isolation rejection or database error must roll back and
    retry the whole transaction at READ COMMITTED, not retry this statement in
    the failed transaction.
    """
    schema = schema or PostgresUsageSchema()
    return PostgresStatement(
        name="usage_record_append",
        sql=f"""
SELECT append_status, record_id
FROM {schema.schema}.append_usage_record(
  %(record_id)s::text,
  %(source)s::text,
  %(confidence)s::text,
  %(amounts_json)s::jsonb,
  %(occurred_at)s::timestamptz,
  %(run_id)s::text,
  %(attempt_id)s::text,
  %(provider_response_id)s::text,
  %(pricing_ref)s::text,
  %(quota_window_id)s::text,
  %(execution_scope)s::text,
  %(reconciliation_of)s::text,
  %(metadata_json)s::jsonb
);
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
