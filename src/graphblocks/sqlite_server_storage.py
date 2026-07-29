from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import TypeVar

from .server_storage import AcceptedRunStorageError


SQLITE_ACCEPTED_RUN_APPLICATION_ID = 0x47424152
SQLITE_ACCEPTED_RUN_SCHEMA_VERSION = 1
_SQLITE_ACCEPTED_RUN_SCHEMA_NAME = "graphblocks.accepted-runs.sqlite"
_MAX_BUSY_TIMEOUT_MS = 60_000
_T = TypeVar("_T")

_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE accepted_run_storage_metadata (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      CHECK (length(key) > 0)
    )
    """,
    """
    CREATE TABLE accepted_runs (
      internal_id TEXT PRIMARY KEY,
      external_run_id TEXT NOT NULL,
      tenant_id TEXT NOT NULL,
      owner_principal_id TEXT NOT NULL,
      admission_scope TEXT NOT NULL,
      admission_idempotency_key TEXT NOT NULL,
      request_digest TEXT NOT NULL,
      ticket_json TEXT NOT NULL,
      graph_json TEXT NOT NULL,
      graph_hash TEXT NOT NULL,
      inputs_json TEXT NOT NULL,
      graph_format_version TEXT NOT NULL,
      runtime_format_version TEXT NOT NULL,
      checkpoint_format_version TEXT NOT NULL,
      created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
      updated_at_unix_ms INTEGER NOT NULL CHECK (updated_at_unix_ms >= 0),
      phase TEXT NOT NULL CHECK (
        phase IN (
          'ready_initial',
          'running',
          'waiting_callback',
          'ready_resume',
          'terminal'
        )
      ),
      state_version INTEGER NOT NULL CHECK (state_version >= 0),
      event_low_watermark INTEGER NOT NULL CHECK (event_low_watermark >= 0),
      event_high_watermark INTEGER NOT NULL CHECK (
        event_high_watermark >= event_low_watermark
      ),
      current_checkpoint_digest TEXT,
      terminal_status TEXT,
      terminal_result_json TEXT,
      terminal_result_digest TEXT,
      lease_owner_id TEXT,
      lease_generation INTEGER NOT NULL CHECK (lease_generation >= 0),
      fencing_token INTEGER NOT NULL CHECK (fencing_token >= 0),
      lease_expires_at_unix_ms INTEGER CHECK (
        lease_expires_at_unix_ms IS NULL OR lease_expires_at_unix_ms >= 0
      ),
      UNIQUE (tenant_id, external_run_id),
      UNIQUE (
        tenant_id,
        owner_principal_id,
        admission_scope,
        admission_idempotency_key
      ),
      CHECK (
        (
          phase = 'running'
          AND lease_owner_id IS NOT NULL
          AND lease_expires_at_unix_ms IS NOT NULL
        )
        OR
        (
          phase <> 'running'
          AND lease_owner_id IS NULL
          AND lease_expires_at_unix_ms IS NULL
        )
      ),
      CHECK (
        (
          phase = 'terminal'
          AND terminal_status IS NOT NULL
          AND terminal_result_json IS NOT NULL
          AND terminal_result_digest IS NOT NULL
        )
        OR
        (
          phase <> 'terminal'
          AND terminal_status IS NULL
          AND terminal_result_json IS NULL
          AND terminal_result_digest IS NULL
        )
      ),
      FOREIGN KEY (internal_id, current_checkpoint_digest)
        REFERENCES run_checkpoints (run_internal_id, checkpoint_digest)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE run_events (
      run_internal_id TEXT NOT NULL,
      sequence INTEGER NOT NULL CHECK (sequence > 0),
      kind TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      payload_digest TEXT NOT NULL,
      created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
      PRIMARY KEY (run_internal_id, sequence),
      FOREIGN KEY (run_internal_id)
        REFERENCES accepted_runs (internal_id)
        ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE run_checkpoints (
      run_internal_id TEXT NOT NULL,
      checkpoint_digest TEXT NOT NULL,
      checkpoint_format_version TEXT NOT NULL,
      checkpoint_json TEXT NOT NULL,
      graph_hash TEXT NOT NULL,
      operation_id TEXT NOT NULL,
      operation_attempt_id TEXT NOT NULL,
      callback_idempotency_key TEXT NOT NULL,
      issuing_lease_generation INTEGER NOT NULL CHECK (
        issuing_lease_generation > 0
      ),
      issuing_fencing_token INTEGER NOT NULL CHECK (
        issuing_fencing_token > 0
      ),
      dispatch_effect_id TEXT NOT NULL,
      created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
      PRIMARY KEY (run_internal_id, checkpoint_digest),
      UNIQUE (run_internal_id, operation_id, operation_attempt_id),
      UNIQUE (run_internal_id, callback_idempotency_key),
      UNIQUE (dispatch_effect_id),
      FOREIGN KEY (run_internal_id)
        REFERENCES accepted_runs (internal_id)
        ON DELETE RESTRICT,
      FOREIGN KEY (dispatch_effect_id)
        REFERENCES effect_outbox (effect_id)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE callback_inbox (
      run_internal_id TEXT NOT NULL,
      checkpoint_digest TEXT NOT NULL,
      callback_idempotency_key TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      payload_digest TEXT NOT NULL,
      receipt_json TEXT NOT NULL,
      accepted_event_sequence INTEGER NOT NULL CHECK (
        accepted_event_sequence > 0
      ),
      accepted_state_version INTEGER NOT NULL CHECK (
        accepted_state_version >= 0
      ),
      received_at_unix_ms INTEGER NOT NULL CHECK (received_at_unix_ms >= 0),
      PRIMARY KEY (run_internal_id, checkpoint_digest),
      UNIQUE (run_internal_id, callback_idempotency_key),
      FOREIGN KEY (run_internal_id, checkpoint_digest)
        REFERENCES run_checkpoints (run_internal_id, checkpoint_digest)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
      FOREIGN KEY (run_internal_id, accepted_event_sequence)
        REFERENCES run_events (run_internal_id, sequence)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
    )
    """,
    """
    CREATE TABLE effect_outbox (
      effect_id TEXT PRIMARY KEY,
      run_internal_id TEXT NOT NULL,
      checkpoint_digest TEXT,
      effect_kind TEXT NOT NULL CHECK (
        effect_kind IN ('operation_dispatch', 'completion')
      ),
      idempotency_key TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      payload_digest TEXT NOT NULL,
      delivery_state TEXT NOT NULL CHECK (
        delivery_state IN (
          'pending',
          'claimed',
          'delivered',
          'satisfied_by_callback',
          'dead_letter'
        )
      ),
      attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
      claim_owner_id TEXT,
      claim_generation INTEGER NOT NULL CHECK (claim_generation >= 0),
      claim_fencing_token INTEGER NOT NULL CHECK (claim_fencing_token >= 0),
      claim_expires_at_unix_ms INTEGER CHECK (
        claim_expires_at_unix_ms IS NULL OR claim_expires_at_unix_ms >= 0
      ),
      created_at_unix_ms INTEGER NOT NULL CHECK (created_at_unix_ms >= 0),
      delivered_at_unix_ms INTEGER CHECK (
        delivered_at_unix_ms IS NULL OR delivered_at_unix_ms >= 0
      ),
      UNIQUE (run_internal_id, effect_kind, idempotency_key),
      FOREIGN KEY (run_internal_id)
        REFERENCES accepted_runs (internal_id)
        ON DELETE RESTRICT,
      FOREIGN KEY (run_internal_id, checkpoint_digest)
        REFERENCES run_checkpoints (run_internal_id, checkpoint_digest)
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
      CHECK (
        (
          effect_kind = 'operation_dispatch'
          AND checkpoint_digest IS NOT NULL
        )
        OR
        (
          effect_kind = 'completion'
          AND checkpoint_digest IS NULL
        )
      ),
      CHECK (
        (
          delivery_state = 'claimed'
          AND claim_owner_id IS NOT NULL
          AND claim_expires_at_unix_ms IS NOT NULL
        )
        OR
        (
          delivery_state <> 'claimed'
          AND claim_owner_id IS NULL
          AND claim_expires_at_unix_ms IS NULL
        )
      )
    )
    """,
    """
    CREATE INDEX accepted_runs_claimable
    ON accepted_runs (phase, lease_expires_at_unix_ms, created_at_unix_ms)
    """,
    """
    CREATE INDEX effect_outbox_claimable
    ON effect_outbox (
      delivery_state,
      claim_expires_at_unix_ms,
      created_at_unix_ms
    )
    """,
)

_REQUIRED_COLUMNS = {
    "accepted_run_storage_metadata": frozenset({"key", "value"}),
    "accepted_runs": frozenset(
        {
            "internal_id",
            "external_run_id",
            "tenant_id",
            "owner_principal_id",
            "admission_scope",
            "admission_idempotency_key",
            "request_digest",
            "ticket_json",
            "graph_json",
            "graph_hash",
            "inputs_json",
            "graph_format_version",
            "runtime_format_version",
            "checkpoint_format_version",
            "created_at_unix_ms",
            "updated_at_unix_ms",
            "phase",
            "state_version",
            "event_low_watermark",
            "event_high_watermark",
            "current_checkpoint_digest",
            "terminal_status",
            "terminal_result_json",
            "terminal_result_digest",
            "lease_owner_id",
            "lease_generation",
            "fencing_token",
            "lease_expires_at_unix_ms",
        }
    ),
    "run_events": frozenset(
        {
            "run_internal_id",
            "sequence",
            "kind",
            "payload_json",
            "payload_digest",
            "created_at_unix_ms",
        }
    ),
    "run_checkpoints": frozenset(
        {
            "run_internal_id",
            "checkpoint_digest",
            "checkpoint_format_version",
            "checkpoint_json",
            "graph_hash",
            "operation_id",
            "operation_attempt_id",
            "callback_idempotency_key",
            "issuing_lease_generation",
            "issuing_fencing_token",
            "dispatch_effect_id",
            "created_at_unix_ms",
        }
    ),
    "callback_inbox": frozenset(
        {
            "run_internal_id",
            "checkpoint_digest",
            "callback_idempotency_key",
            "payload_json",
            "payload_digest",
            "receipt_json",
            "accepted_event_sequence",
            "accepted_state_version",
            "received_at_unix_ms",
        }
    ),
    "effect_outbox": frozenset(
        {
            "effect_id",
            "run_internal_id",
            "checkpoint_digest",
            "effect_kind",
            "idempotency_key",
            "payload_json",
            "payload_digest",
            "delivery_state",
            "attempt_count",
            "claim_owner_id",
            "claim_generation",
            "claim_fencing_token",
            "claim_expires_at_unix_ms",
            "created_at_unix_ms",
            "delivered_at_unix_ms",
        }
    ),
}
_REQUIRED_TABLES = frozenset(_REQUIRED_COLUMNS)


class SQLiteAcceptedRunStorageError(AcceptedRunStorageError):
    retryable = False


class SQLiteAcceptedRunBusyError(SQLiteAcceptedRunStorageError):
    retryable = True


class SQLiteAcceptedRunCorruptionError(SQLiteAcceptedRunStorageError):
    pass


class SQLiteAcceptedRunUnavailableError(SQLiteAcceptedRunStorageError):
    pass


class SQLiteAcceptedRunSchemaMismatchError(SQLiteAcceptedRunStorageError):
    pass


class SQLiteAcceptedRunSchemaVersionError(
    SQLiteAcceptedRunSchemaMismatchError
):
    pass


@dataclass(frozen=True, slots=True)
class SQLiteAcceptedRunSchemaInfo:
    application_id: int
    user_version: int
    schema_name: str
    schema_version: int
    journal_mode: str
    foreign_keys_enabled: bool
    synchronous: int
    busy_timeout_ms: int
    tables: frozenset[str]


def _sqlite_primary_error_code(error: sqlite3.Error) -> int | None:
    code = getattr(error, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return None
    return code & 0xFF


def _translate_sqlite_error(
    error: sqlite3.Error,
) -> SQLiteAcceptedRunStorageError:
    code = _sqlite_primary_error_code(error)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return SQLiteAcceptedRunBusyError(
            "accepted-run SQLite database is busy"
        )
    if code in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
    }:
        return SQLiteAcceptedRunCorruptionError(
            "accepted-run SQLite database is corrupt or not a database"
        )
    return SQLiteAcceptedRunUnavailableError(
        f"accepted-run SQLite database operation failed: {error}"
    )


class SQLiteAcceptedRunDatabase:
    """Owns schema identity and creates one SQLite connection per operation."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError(
                "accepted-run SQLite database path must be a string or Path"
            )
        if str(path) == ":memory:":
            raise ValueError(
                "accepted-run SQLite database requires a filesystem path "
                "and does not support :memory:"
            )
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
            or busy_timeout_ms > _MAX_BUSY_TIMEOUT_MS
        ):
            raise ValueError(
                "accepted-run SQLite busy_timeout_ms must be an integer "
                f"between 0 and {_MAX_BUSY_TIMEOUT_MS}"
            )
        self.path = Path(path)
        if self.path.exists() and self.path.is_dir():
            raise ValueError(
                "accepted-run SQLite database path must not be a directory"
            )
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _open_connection(
        self,
        *,
        validate_identity: bool = True,
    ) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                str(self.path),
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                f"PRAGMA busy_timeout = {self.busy_timeout_ms}"
            )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            if validate_identity:
                self._assert_identity(connection)
        except sqlite3.Error as error:
            if "connection" in locals():
                connection.close()
            raise _translate_sqlite_error(error) from error
        except Exception:
            if "connection" in locals():
                connection.close()
            raise
        return connection

    @staticmethod
    def _application_id(connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA application_id").fetchone()[0])

    @staticmethod
    def _user_version(connection: sqlite3.Connection) -> int:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        )

    def _assert_identity(self, connection: sqlite3.Connection) -> None:
        application_id = self._application_id(connection)
        if application_id != SQLITE_ACCEPTED_RUN_APPLICATION_ID:
            raise SQLiteAcceptedRunSchemaMismatchError(
                "SQLite file belongs to another application or has no "
                "accepted-run identity"
            )
        user_version = self._user_version(connection)
        if user_version != SQLITE_ACCEPTED_RUN_SCHEMA_VERSION:
            raise SQLiteAcceptedRunSchemaVersionError(
                "unsupported accepted-run SQLite schema version "
                f"{user_version}; expected "
                f"{SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
            )

    def _initialize(self) -> None:
        connection = self._open_connection(validate_identity=False)
        try:
            application_id = self._application_id(connection)
            user_version = self._user_version(connection)
            tables = self._table_names(connection)
            if application_id == 0 and user_version == 0 and not tables:
                self._initialize_empty_database(connection)
            else:
                self._assert_identity(connection)
                self._validate_schema(connection)
                self._ensure_wal(connection)
        except sqlite3.Error as error:
            raise _translate_sqlite_error(error) from error
        finally:
            connection.close()

    @staticmethod
    def _ensure_wal(connection: sqlite3.Connection) -> None:
        journal_mode = str(
            connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        ).lower()
        if journal_mode != "wal":
            raise SQLiteAcceptedRunUnavailableError(
                "accepted-run SQLite database requires WAL journal mode"
            )

    def _initialize_empty_database(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        self._ensure_wal(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            application_id = self._application_id(connection)
            user_version = self._user_version(connection)
            tables = self._table_names(connection)
            if application_id != 0 or user_version != 0 or tables:
                self._assert_identity(connection)
                self._validate_schema(connection)
                connection.commit()
                return
            for statement in _SCHEMA_V1_STATEMENTS:
                connection.execute(statement)
            connection.executemany(
                """
                INSERT INTO accepted_run_storage_metadata (key, value)
                VALUES (?, ?)
                """,
                (
                    ("schema_name", _SQLITE_ACCEPTED_RUN_SCHEMA_NAME),
                    (
                        "schema_version",
                        str(SQLITE_ACCEPTED_RUN_SCHEMA_VERSION),
                    ),
                ),
            )
            connection.execute(
                "PRAGMA application_id = "
                f"{SQLITE_ACCEPTED_RUN_APPLICATION_ID}"
            )
            connection.execute(
                "PRAGMA user_version = "
                f"{SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        self._validate_schema(connection)

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        tables = self._table_names(connection)
        if tables != _REQUIRED_TABLES:
            raise SQLiteAcceptedRunSchemaMismatchError(
                "accepted-run SQLite schema tables do not match "
                f"version {SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
            )
        for table_name, expected_columns in _REQUIRED_COLUMNS.items():
            actual_columns = frozenset(
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            )
            if actual_columns != expected_columns:
                raise SQLiteAcceptedRunSchemaMismatchError(
                    "accepted-run SQLite schema columns do not match "
                    f"version {SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}: "
                    f"{table_name}"
                )
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM accepted_run_storage_metadata"
            ).fetchall()
        }
        if metadata.get("schema_name") != _SQLITE_ACCEPTED_RUN_SCHEMA_NAME:
            raise SQLiteAcceptedRunSchemaMismatchError(
                "accepted-run SQLite schema metadata name does not match"
            )
        if metadata.get("schema_version") != str(
            SQLITE_ACCEPTED_RUN_SCHEMA_VERSION
        ):
            raise SQLiteAcceptedRunSchemaMismatchError(
                "accepted-run SQLite schema metadata version does not match"
            )
        foreign_key_failures = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_failures:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite schema has foreign-key violations"
            )

    def _run_read(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        if not callable(operation):
            raise TypeError("accepted-run SQLite read operation must be callable")
        connection = self._open_connection()
        try:
            connection.execute("PRAGMA query_only = ON")
            return operation(connection)
        except sqlite3.Error as error:
            raise _translate_sqlite_error(error) from error
        finally:
            connection.close()

    def _run_immediate(
        self,
        operation: Callable[[sqlite3.Connection], _T],
    ) -> _T:
        if not callable(operation):
            raise TypeError(
                "accepted-run SQLite transaction operation must be callable"
            )
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            connection.commit()
            return result
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, sqlite3.Error):
                raise _translate_sqlite_error(error) from error
            raise
        finally:
            connection.close()

    def schema_info(self) -> SQLiteAcceptedRunSchemaInfo:
        def inspect(
            connection: sqlite3.Connection,
        ) -> SQLiteAcceptedRunSchemaInfo:
            self._validate_schema(connection)
            metadata = {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    "SELECT key, value FROM accepted_run_storage_metadata"
                ).fetchall()
            }
            return SQLiteAcceptedRunSchemaInfo(
                application_id=self._application_id(connection),
                user_version=self._user_version(connection),
                schema_name=metadata["schema_name"],
                schema_version=int(metadata["schema_version"]),
                journal_mode=str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower(),
                foreign_keys_enabled=bool(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                ),
                synchronous=int(
                    connection.execute("PRAGMA synchronous").fetchone()[0]
                ),
                busy_timeout_ms=int(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0]
                ),
                tables=self._table_names(connection),
            )

        return self._run_read(inspect)
