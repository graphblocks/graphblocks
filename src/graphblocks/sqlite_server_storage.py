from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import secrets
import sqlite3
import time
from typing import TypeVar
import uuid

from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .server_storage import (
    CHECKPOINT_FORMAT_VERSION,
    AcceptedRunAdmission,
    AcceptedRunCallbackCommit,
    AcceptedRunCallbackExpiredError,
    AcceptedRunClaim,
    AcceptedRunClaimRequest,
    AcceptedRunEvent,
    AcceptedRunEventPage,
    AcceptedRunIdConflictError,
    AcceptedRunLeaseExpiredError,
    AcceptedRunNotFoundError,
    AcceptedRunPhase,
    AcceptedRunSnapshot,
    AcceptedRunStateConflictError,
    AcceptedRunStorageError,
    AcceptedRunTerminalCommit,
    AcceptedRunTerminalConflictError,
    AcceptedRunWaitingCommit,
    AdmissionIdentity,
    AdmissionResult,
    CallbackAcceptance,
    CallbackIssuanceConflictError,
    CallbackIssuanceIdentity,
    CallbackSubmissionIdentity,
    CheckpointIntegrityError,
    StaleAcceptedRunClaimError,
    StoredRuntimeCheckpoint,
    assert_current_claim,
    assert_accepted_run_transition,
    decode_runtime_checkpoint,
    resolve_admission_replay,
    resolve_callback_replay,
)


SQLITE_ACCEPTED_RUN_APPLICATION_ID = 0x47424152
SQLITE_ACCEPTED_RUN_SCHEMA_VERSION = 2
_SQLITE_ACCEPTED_RUN_SCHEMA_NAME = "graphblocks.accepted-runs.sqlite"
_SQLITE_ACCEPTED_RUN_INITIAL_SCHEMA_VERSION = 1
_MAX_BUSY_TIMEOUT_MS = 60_000
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_UUID7_UNIX_MS = (1 << 48) - 1
MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE = 1_000
_TERMINAL_EVENT_KINDS = {
    "cancelled": "run_cancelled",
    "completed": "run_completed",
    "expired": "run_expired",
    "failed": "run_failed",
    "policy_stopped": "run_policy_stopped",
    "succeeded": "run_succeeded",
}
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

_SCHEMA_V2_MIGRATION_STATEMENTS = (
    """
    ALTER TABLE effect_outbox
    ADD COLUMN available_at_unix_ms INTEGER NOT NULL DEFAULT 0
      CHECK (available_at_unix_ms >= 0)
    """,
    """
    UPDATE effect_outbox
    SET available_at_unix_ms = created_at_unix_ms
    """,
    """
    DROP INDEX effect_outbox_claimable
    """,
    """
    CREATE INDEX effect_outbox_claimable
    ON effect_outbox (
      delivery_state,
      available_at_unix_ms,
      claim_expires_at_unix_ms,
      created_at_unix_ms
    )
    """,
)

_REQUIRED_COLUMNS_V1 = {
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
_REQUIRED_COLUMNS = {
    **_REQUIRED_COLUMNS_V1,
    "effect_outbox": (
        _REQUIRED_COLUMNS_V1["effect_outbox"]
        | frozenset({"available_at_unix_ms"})
    ),
}
_REQUIRED_TABLES = frozenset(_REQUIRED_COLUMNS)


def _validate_lookup_text(owner: str, field_name: str, value: object) -> str:
    if type(value) is not str:
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value or value != value.strip():
        raise ValueError(
            f"{owner} {field_name} must be an exact non-empty string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        ) from error
    return value


def _uuid7(unix_ms: int) -> str:
    if unix_ms < 0 or unix_ms > _MAX_UUID7_UNIX_MS:
        raise ValueError(
            "accepted run created_at_unix_ms exceeds UUIDv7 timestamp range"
        )
    random_bits = secrets.randbits(74)
    random_a = random_bits >> 62
    random_b = random_bits & ((1 << 62) - 1)
    value = (
        (unix_ms << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))


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


def _decode_sqlite_text(field_name: str, value: object) -> str:
    if type(value) is not str:
        raise SQLiteAcceptedRunCorruptionError(
            f"accepted-run SQLite {field_name} is not text"
        )
    return value


def _decode_sqlite_integer(field_name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SQLiteAcceptedRunCorruptionError(
            f"accepted-run SQLite {field_name} is not an integer"
        )
    return value


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

    def _assert_application_identity(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        application_id = self._application_id(connection)
        if application_id != SQLITE_ACCEPTED_RUN_APPLICATION_ID:
            raise SQLiteAcceptedRunSchemaMismatchError(
                "SQLite file belongs to another application or has no "
                "accepted-run identity"
            )

    def _assert_identity(self, connection: sqlite3.Connection) -> None:
        self._assert_application_identity(connection)
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
                self._assert_application_identity(connection)
                if (
                    user_version
                    == _SQLITE_ACCEPTED_RUN_INITIAL_SCHEMA_VERSION
                ):
                    self._migrate_v1_to_v2(connection)
                else:
                    self._assert_identity(connection)
                self._validate_schema(connection)
                self._ensure_wal(connection)
        except sqlite3.Error as error:
            raise _translate_sqlite_error(error) from error
        finally:
            connection.close()

    def _ensure_wal(self, connection: sqlite3.Connection) -> None:
        deadline = time.monotonic() + (self.busy_timeout_ms / 1_000)
        connection.execute("PRAGMA busy_timeout = 0")
        try:
            while True:
                try:
                    current_mode = str(
                        connection.execute(
                            "PRAGMA journal_mode"
                        ).fetchone()[0]
                    ).lower()
                    if current_mode == "wal":
                        return
                    journal_mode = str(
                        connection.execute(
                            "PRAGMA journal_mode = WAL"
                        ).fetchone()[0]
                    ).lower()
                    if journal_mode == "wal":
                        return
                    raise SQLiteAcceptedRunUnavailableError(
                        "accepted-run SQLite database requires WAL journal mode"
                    )
                except sqlite3.Error as error:
                    if (
                        _sqlite_primary_error_code(error)
                        not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(
                        0.005 + (secrets.randbelow(6) / 1_000)
                    )
        finally:
            connection.execute(
                f"PRAGMA busy_timeout = {self.busy_timeout_ms}"
            )

    def _initialize_empty_database(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            application_id = self._application_id(connection)
            user_version = self._user_version(connection)
            tables = self._table_names(connection)
            if application_id != 0 or user_version != 0 or tables:
                self._assert_identity(connection)
                self._validate_schema(connection)
                connection.commit()
            else:
                for statement in _SCHEMA_V1_STATEMENTS:
                    connection.execute(statement)
                for statement in _SCHEMA_V2_MIGRATION_STATEMENTS:
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
        self._ensure_wal(connection)

    def _migrate_v1_to_v2(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_application_identity(connection)
            user_version = self._user_version(connection)
            if user_version == SQLITE_ACCEPTED_RUN_SCHEMA_VERSION:
                self._validate_schema(connection)
                connection.commit()
                return
            if user_version != _SQLITE_ACCEPTED_RUN_INITIAL_SCHEMA_VERSION:
                raise SQLiteAcceptedRunSchemaVersionError(
                    "unsupported accepted-run SQLite schema version "
                    f"{user_version}; expected "
                    f"{SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
                )
            self._validate_schema_version(
                connection,
                schema_version=_SQLITE_ACCEPTED_RUN_INITIAL_SCHEMA_VERSION,
                required_columns=_REQUIRED_COLUMNS_V1,
            )
            for statement in _SCHEMA_V2_MIGRATION_STATEMENTS:
                connection.execute(statement)
            updated = connection.execute(
                """
                UPDATE accepted_run_storage_metadata
                SET value = ?
                WHERE key = 'schema_version' AND value = ?
                """,
                (
                    str(SQLITE_ACCEPTED_RUN_SCHEMA_VERSION),
                    str(_SQLITE_ACCEPTED_RUN_INITIAL_SCHEMA_VERSION),
                ),
            )
            if updated.rowcount != 1:
                raise SQLiteAcceptedRunSchemaMismatchError(
                    "accepted-run SQLite schema metadata version changed "
                    "during migration"
                )
            connection.execute(
                "PRAGMA user_version = "
                f"{SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
            )
            self._validate_schema(connection)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_version(
            connection,
            schema_version=SQLITE_ACCEPTED_RUN_SCHEMA_VERSION,
            required_columns=_REQUIRED_COLUMNS,
        )

    def _validate_schema_version(
        self,
        connection: sqlite3.Connection,
        *,
        schema_version: int,
        required_columns: dict[str, frozenset[str]],
    ) -> None:
        tables = self._table_names(connection)
        if tables != _REQUIRED_TABLES:
            raise SQLiteAcceptedRunSchemaMismatchError(
                "accepted-run SQLite schema tables do not match "
                f"version {schema_version}"
            )
        for table_name, expected_columns in required_columns.items():
            actual_columns = frozenset(
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            )
            if actual_columns != expected_columns:
                raise SQLiteAcceptedRunSchemaMismatchError(
                    "accepted-run SQLite schema columns do not match "
                    f"version {schema_version}: "
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
        if metadata.get("schema_version") != str(schema_version):
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
            connection.execute("BEGIN")
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


class SQLiteAcceptedRunRepository:
    """Preview repository for atomic restart-durable accepted-run transitions."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        if failpoint is not None and not callable(failpoint):
            raise ValueError(
                "accepted-run SQLite repository failpoint must be callable"
            )
        self._database = SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._failpoint = failpoint

    def _hit_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    @staticmethod
    def _stored_admission_identity(row: sqlite3.Row) -> AdmissionIdentity:
        return AdmissionIdentity(
            tenant_id=_decode_sqlite_text("tenant_id", row["tenant_id"]),
            owner_principal_id=_decode_sqlite_text(
                "owner_principal_id",
                row["owner_principal_id"],
            ),
            admission_scope=_decode_sqlite_text(
                "admission_scope",
                row["admission_scope"],
            ),
            idempotency_key=_decode_sqlite_text(
                "admission_idempotency_key",
                row["admission_idempotency_key"],
            ),
            request_digest=_decode_sqlite_text(
                "request_digest",
                row["request_digest"],
            ),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> AcceptedRunSnapshot:
        try:
            external_run_id = _decode_sqlite_text(
                "external_run_id",
                row["external_run_id"],
            )
            phase = AcceptedRunPhase(
                _decode_sqlite_text("phase", row["phase"])
            )
            claim = None
            if phase is AcceptedRunPhase.RUNNING:
                claim = AcceptedRunClaim(
                    tenant_id=_decode_sqlite_text(
                        "tenant_id",
                        row["tenant_id"],
                    ),
                    run_id=external_run_id,
                    lease_owner_id=_decode_sqlite_text(
                        "lease_owner_id",
                        row["lease_owner_id"],
                    ),
                    lease_generation=_decode_sqlite_integer(
                        "lease_generation",
                        row["lease_generation"],
                    ),
                    fencing_token=_decode_sqlite_integer(
                        "fencing_token",
                        row["fencing_token"],
                    ),
                    lease_expires_at_unix_ms=_decode_sqlite_integer(
                        "lease_expires_at_unix_ms",
                        row["lease_expires_at_unix_ms"]
                    ),
                )
            checkpoint_digest = row["current_checkpoint_digest"]
            terminal_status = row["terminal_status"]
            terminal_result_json = row["terminal_result_json"]
            return AcceptedRunSnapshot(
                run_id=external_run_id,
                tenant_id=_decode_sqlite_text(
                    "tenant_id",
                    row["tenant_id"],
                ),
                owner_principal_id=_decode_sqlite_text(
                    "owner_principal_id",
                    row["owner_principal_id"],
                ),
                phase=phase,
                state_version=_decode_sqlite_integer(
                    "state_version",
                    row["state_version"],
                ),
                event_low_watermark=_decode_sqlite_integer(
                    "event_low_watermark",
                    row["event_low_watermark"],
                ),
                event_high_watermark=_decode_sqlite_integer(
                    "event_high_watermark",
                    row["event_high_watermark"],
                ),
                checkpoint_digest=(
                    None
                    if checkpoint_digest is None
                    else _decode_sqlite_text(
                        "current_checkpoint_digest",
                        checkpoint_digest,
                    )
                ),
                claim=claim,
                terminal_status=(
                    None
                    if terminal_status is None
                    else _decode_sqlite_text(
                        "terminal_status",
                        terminal_status,
                    )
                ),
                terminal_result_json=(
                    None
                    if terminal_result_json is None
                    else _decode_sqlite_text(
                        "terminal_result_json",
                        terminal_result_json,
                    )
                ),
            )
        except (TypeError, ValueError) as error:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite snapshot is invalid"
            ) from error

    @staticmethod
    def _event_from_row(
        external_run_id: str,
        row: sqlite3.Row,
    ) -> AcceptedRunEvent:
        try:
            return AcceptedRunEvent(
                run_id=external_run_id,
                sequence=_decode_sqlite_integer(
                    "event sequence",
                    row["sequence"],
                ),
                kind=_decode_sqlite_text("event kind", row["kind"]),
                payload_json=_decode_sqlite_text(
                    "event payload_json",
                    row["payload_json"],
                ),
                payload_digest=_decode_sqlite_text(
                    "event payload_digest",
                    row["payload_digest"],
                ),
                created_at_unix_ms=_decode_sqlite_integer(
                    "event created_at_unix_ms",
                    row["created_at_unix_ms"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite event is invalid"
            ) from error

    def accept_run(
        self,
        admission: AcceptedRunAdmission,
    ) -> AdmissionResult:
        if not isinstance(admission, AcceptedRunAdmission):
            raise TypeError(
                "accepted-run SQLite admission must be an AcceptedRunAdmission"
            )
        if admission.checkpoint_format_version != CHECKPOINT_FORMAT_VERSION:
            raise ValueError(
                "accepted-run SQLite admission checkpoint format is unsupported"
            )
        if admission.accepted_event.kind != "run_accepted":
            raise ValueError(
                "accepted-run SQLite admission event kind must be run_accepted"
            )
        if admission.created_at_unix_ms > _MAX_UUID7_UNIX_MS:
            raise ValueError(
                "accepted run created_at_unix_ms exceeds UUIDv7 timestamp range"
            )
        if admission.accepted_event.created_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite admission event timestamp exceeds "
                "SQLite integer range"
            )
        if (
            canonical_hash(canonical_loads(admission.graph_json))
            != admission.graph_hash
        ):
            raise ValueError(
                "accepted-run SQLite admission graph_hash must match graph_json"
            )

        def transition(connection: sqlite3.Connection) -> AdmissionResult:
            identity = admission.identity
            existing = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ?
                  AND owner_principal_id = ?
                  AND admission_scope = ?
                  AND admission_idempotency_key = ?
                """,
                (
                    identity.tenant_id,
                    identity.owner_principal_id,
                    identity.admission_scope,
                    identity.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                replay = resolve_admission_replay(
                    existing_identity=self._stored_admission_identity(existing),
                    existing_result=AdmissionResult(
                        run_id=_decode_sqlite_text(
                            "external_run_id",
                            existing["external_run_id"],
                        ),
                        ticket_json=_decode_sqlite_text(
                            "ticket_json",
                            existing["ticket_json"],
                        ),
                    ),
                    requested_identity=identity,
                )
                if replay is None:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite admission index returned "
                        "a foreign identity"
                    )
                if replay.run_id != admission.run_id:
                    raise AcceptedRunIdConflictError(
                        identity.tenant_id,
                        admission.run_id,
                        "admission replay run_id does not match stored run_id",
                    )
                return replay

            run_collision = connection.execute(
                """
                SELECT 1
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (identity.tenant_id, admission.run_id),
            ).fetchone()
            if run_collision is not None:
                raise AcceptedRunIdConflictError(
                    identity.tenant_id,
                    admission.run_id,
                    "accepted run_id already exists in tenant",
                )

            internal_id = _uuid7(admission.created_at_unix_ms)
            connection.execute(
                """
                INSERT INTO accepted_runs (
                  internal_id,
                  external_run_id,
                  tenant_id,
                  owner_principal_id,
                  admission_scope,
                  admission_idempotency_key,
                  request_digest,
                  ticket_json,
                  graph_json,
                  graph_hash,
                  inputs_json,
                  graph_format_version,
                  runtime_format_version,
                  checkpoint_format_version,
                  created_at_unix_ms,
                  updated_at_unix_ms,
                  phase,
                  state_version,
                  event_low_watermark,
                  event_high_watermark,
                  current_checkpoint_digest,
                  terminal_status,
                  terminal_result_json,
                  terminal_result_digest,
                  lease_owner_id,
                  lease_generation,
                  fencing_token,
                  lease_expires_at_unix_ms
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  'ready_initial', 1, 1, 1, NULL, NULL, NULL, NULL,
                  NULL, 0, 0, NULL
                )
                """,
                (
                    internal_id,
                    admission.run_id,
                    identity.tenant_id,
                    identity.owner_principal_id,
                    identity.admission_scope,
                    identity.idempotency_key,
                    identity.request_digest,
                    admission.ticket_json,
                    admission.graph_json,
                    admission.graph_hash,
                    admission.inputs_json,
                    admission.graph_format_version,
                    admission.runtime_format_version,
                    admission.checkpoint_format_version,
                    admission.created_at_unix_ms,
                    admission.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("accept_run.after_run_insert")
            event = admission.accepted_event
            connection.execute(
                """
                INSERT INTO run_events (
                  run_internal_id,
                  sequence,
                  kind,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    event.kind,
                    event.payload_json,
                    event.payload_digest,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("accept_run.after_event_insert")
            return AdmissionResult(
                run_id=admission.run_id,
                ticket_json=admission.ticket_json,
            )

        result = self._database._run_immediate(transition)
        self._hit_failpoint("accept_run.after_commit")
        return result

    def claim_run(
        self,
        request: AcceptedRunClaimRequest,
    ) -> AcceptedRunClaim | None:
        if not isinstance(request, AcceptedRunClaimRequest):
            raise TypeError(
                "accepted-run SQLite claim must be an "
                "AcceptedRunClaimRequest"
            )
        lease_expires_at_unix_ms = (
            request.now_unix_ms + request.lease_duration_ms
        )
        if lease_expires_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite claim lease expiration exceeds SQLite "
                "integer range"
            )

        def transition(
            connection: sqlite3.Connection,
        ) -> AcceptedRunClaim | None:
            row = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (request.tenant_id, request.run_id),
            ).fetchone()
            if row is None:
                raise AcceptedRunNotFoundError(
                    request.tenant_id,
                    request.run_id,
                )
            try:
                phase = AcceptedRunPhase(
                    _decode_sqlite_text("phase", row["phase"])
                )
            except ValueError as error:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite phase is invalid"
                ) from error
            if phase is AcceptedRunPhase.RUNNING:
                current_expiry = _decode_sqlite_integer(
                    "lease_expires_at_unix_ms",
                    row["lease_expires_at_unix_ms"],
                )
                if current_expiry > request.now_unix_ms:
                    return None
                event_kind = "run_reclaimed"
            elif phase is AcceptedRunPhase.READY_INITIAL:
                assert_accepted_run_transition(
                    phase,
                    AcceptedRunPhase.RUNNING,
                )
                event_kind = "run_claimed"
            elif phase is AcceptedRunPhase.READY_RESUME:
                assert_accepted_run_transition(
                    phase,
                    AcceptedRunPhase.RUNNING,
                )
                event_kind = "run_resume_claimed"
            else:
                return None

            internal_id = _decode_sqlite_text(
                "internal_id",
                row["internal_id"],
            )
            state_version = _decode_sqlite_integer(
                "state_version",
                row["state_version"],
            )
            event_sequence = _decode_sqlite_integer(
                "event_high_watermark",
                row["event_high_watermark"],
            )
            lease_generation = _decode_sqlite_integer(
                "lease_generation",
                row["lease_generation"],
            )
            fencing_token = _decode_sqlite_integer(
                "fencing_token",
                row["fencing_token"],
            )
            if (
                state_version >= _MAX_SQLITE_INTEGER
                or event_sequence >= _MAX_SQLITE_INTEGER
                or lease_generation >= _MAX_SQLITE_INTEGER
                or fencing_token >= _MAX_SQLITE_INTEGER
            ):
                raise OverflowError(
                    "accepted-run SQLite claim counters are exhausted"
                )
            next_state_version = state_version + 1
            next_event_sequence = event_sequence + 1
            next_lease_generation = lease_generation + 1
            next_fencing_token = fencing_token + 1
            claim = AcceptedRunClaim(
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                lease_owner_id=request.lease_owner_id,
                lease_generation=next_lease_generation,
                fencing_token=next_fencing_token,
                lease_expires_at_unix_ms=lease_expires_at_unix_ms,
            )
            updated = connection.execute(
                """
                UPDATE accepted_runs
                SET phase = 'running',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    lease_owner_id = ?,
                    lease_generation = ?,
                    fencing_token = ?,
                    lease_expires_at_unix_ms = ?
                WHERE internal_id = ?
                  AND phase = ?
                  AND state_version = ?
                  AND lease_generation = ?
                  AND fencing_token = ?
                """,
                (
                    next_state_version,
                    next_event_sequence,
                    request.now_unix_ms,
                    request.lease_owner_id,
                    next_lease_generation,
                    next_fencing_token,
                    lease_expires_at_unix_ms,
                    internal_id,
                    phase.value,
                    state_version,
                    lease_generation,
                    fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite claim lost its locked state"
                )
            self._hit_failpoint("claim_run.after_state_update")
            event_payload = {
                "leaseGeneration": next_lease_generation,
                "runId": request.run_id,
                "state": "running",
            }
            event_json = canonical_dumps(event_payload)
            connection.execute(
                """
                INSERT INTO run_events (
                  run_internal_id,
                  sequence,
                  kind,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    next_event_sequence,
                    event_kind,
                    event_json,
                    canonical_hash(event_payload),
                    request.now_unix_ms,
                ),
            )
            self._hit_failpoint("claim_run.after_event_insert")
            return claim

        claim = self._database._run_immediate(transition)
        if claim is not None:
            self._hit_failpoint("claim_run.after_commit")
        return claim

    def commit_waiting(
        self,
        command: AcceptedRunWaitingCommit,
    ) -> AcceptedRunSnapshot:
        if not isinstance(command, AcceptedRunWaitingCommit):
            raise TypeError(
                "accepted-run SQLite waiting command must be an "
                "AcceptedRunWaitingCommit"
            )
        checkpoint = decode_runtime_checkpoint(command.checkpoint)
        claim = command.claim
        event = command.waiting_event
        effect = command.dispatch_effect
        if event.created_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite waiting timestamp exceeds SQLite "
                "integer range"
            )

        def transition(connection: sqlite3.Connection) -> AcceptedRunSnapshot:
            row = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (claim.tenant_id, claim.run_id),
            ).fetchone()
            if row is None:
                raise AcceptedRunNotFoundError(
                    claim.tenant_id,
                    claim.run_id,
                )
            snapshot = self._snapshot_from_row(row)
            if snapshot.phase is AcceptedRunPhase.WAITING_CALLBACK:
                return self._replay_waiting_commit(
                    connection,
                    row,
                    snapshot,
                    command,
                )
            if snapshot.phase is not AcceptedRunPhase.RUNNING:
                raise StaleAcceptedRunClaimError(snapshot.claim, claim)
            assert_current_claim(current=snapshot.claim, provided=claim)
            if snapshot.state_version != command.expected_state_version:
                raise AcceptedRunStateConflictError(
                    claim.run_id,
                    command.expected_state_version,
                    snapshot.state_version,
                )
            if event.created_at_unix_ms >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunLeaseExpiredError(
                    claim,
                    "waiting commit",
                )
            stored_graph_hash = _decode_sqlite_text(
                "graph_hash",
                row["graph_hash"],
            )
            if checkpoint.graph_hash != stored_graph_hash:
                raise CheckpointIntegrityError(
                    "checkpoint graph hash does not match accepted run"
                )
            stored_inputs_json = _decode_sqlite_text(
                "inputs_json",
                row["inputs_json"],
            )
            if canonical_dumps(checkpoint.inputs) != stored_inputs_json:
                raise CheckpointIntegrityError(
                    "checkpoint inputs do not match accepted run"
                )

            internal_id = _decode_sqlite_text(
                "internal_id",
                row["internal_id"],
            )
            event_sequence = snapshot.event_high_watermark
            if (
                snapshot.state_version >= _MAX_SQLITE_INTEGER
                or event_sequence >= _MAX_SQLITE_INTEGER
            ):
                raise OverflowError(
                    "accepted-run SQLite waiting counters are exhausted"
                )
            next_state_version = snapshot.state_version + 1
            next_event_sequence = event_sequence + 1
            issuance = command.callback_issuance
            connection.execute(
                """
                INSERT INTO run_checkpoints (
                  run_internal_id,
                  checkpoint_digest,
                  checkpoint_format_version,
                  checkpoint_json,
                  graph_hash,
                  operation_id,
                  operation_attempt_id,
                  callback_idempotency_key,
                  issuing_lease_generation,
                  issuing_fencing_token,
                  dispatch_effect_id,
                  created_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    command.checkpoint.checkpoint_digest,
                    command.checkpoint.format_version,
                    command.checkpoint.checkpoint_json,
                    checkpoint.graph_hash,
                    issuance.operation_id,
                    issuance.operation_attempt_id,
                    issuance.callback_idempotency_key,
                    issuance.lease_generation,
                    issuance.fencing_token,
                    effect.effect_id,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("commit_waiting.after_checkpoint_insert")
            connection.execute(
                """
                INSERT INTO effect_outbox (
                  effect_id,
                  run_internal_id,
                  checkpoint_digest,
                  effect_kind,
                  idempotency_key,
                  payload_json,
                  payload_digest,
                  available_at_unix_ms,
                  delivery_state,
                  attempt_count,
                  claim_owner_id,
                  claim_generation,
                  claim_fencing_token,
                  claim_expires_at_unix_ms,
                  created_at_unix_ms,
                  delivered_at_unix_ms
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, 0, 0, NULL, ?,
                  NULL
                )
                """,
                (
                    effect.effect_id,
                    internal_id,
                    command.checkpoint.checkpoint_digest,
                    effect.kind.value,
                    effect.idempotency_key,
                    effect.payload_json,
                    effect.payload_digest,
                    event.created_at_unix_ms,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("commit_waiting.after_outbox_insert")
            connection.execute(
                """
                INSERT INTO run_events (
                  run_internal_id,
                  sequence,
                  kind,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    next_event_sequence,
                    event.kind,
                    event.payload_json,
                    event.payload_digest,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("commit_waiting.after_event_insert")
            updated = connection.execute(
                """
                UPDATE accepted_runs
                SET phase = 'waiting_callback',
                    state_version = ?,
                    event_high_watermark = ?,
                    current_checkpoint_digest = ?,
                    updated_at_unix_ms = ?,
                    lease_owner_id = NULL,
                    lease_expires_at_unix_ms = NULL
                WHERE internal_id = ?
                  AND phase = 'running'
                  AND state_version = ?
                  AND lease_owner_id = ?
                  AND lease_generation = ?
                  AND fencing_token = ?
                  AND lease_expires_at_unix_ms = ?
                """,
                (
                    next_state_version,
                    next_event_sequence,
                    command.checkpoint.checkpoint_digest,
                    event.created_at_unix_ms,
                    internal_id,
                    command.expected_state_version,
                    claim.lease_owner_id,
                    claim.lease_generation,
                    claim.fencing_token,
                    claim.lease_expires_at_unix_ms,
                ),
            )
            if updated.rowcount != 1:
                raise StaleAcceptedRunClaimError(snapshot.claim, claim)
            self._hit_failpoint("commit_waiting.after_state_update")
            updated_row = connection.execute(
                "SELECT * FROM accepted_runs WHERE internal_id = ?",
                (internal_id,),
            ).fetchone()
            if updated_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite waiting transition lost its run"
                )
            return self._snapshot_from_row(updated_row)

        snapshot = self._database._run_immediate(transition)
        self._hit_failpoint("commit_waiting.after_commit")
        return snapshot

    def _replay_waiting_commit(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        snapshot: AcceptedRunSnapshot,
        command: AcceptedRunWaitingCommit,
    ) -> AcceptedRunSnapshot:
        claim = command.claim
        current_generation = _decode_sqlite_integer(
            "lease_generation",
            row["lease_generation"],
        )
        current_fence = _decode_sqlite_integer(
            "fencing_token",
            row["fencing_token"],
        )
        if (
            current_generation != claim.lease_generation
            or current_fence != claim.fencing_token
        ):
            raise StaleAcceptedRunClaimError(None, claim)
        expected_committed_version = command.expected_state_version + 1
        if snapshot.state_version != expected_committed_version:
            raise AcceptedRunStateConflictError(
                claim.run_id,
                expected_committed_version,
                snapshot.state_version,
            )
        if snapshot.checkpoint_digest != command.checkpoint.checkpoint_digest:
            raise CheckpointIntegrityError(
                "stored waiting checkpoint conflicts with retry"
            )
        internal_id = _decode_sqlite_text(
            "internal_id",
            row["internal_id"],
        )
        checkpoint_row = connection.execute(
            """
            SELECT *
            FROM run_checkpoints
            WHERE run_internal_id = ? AND checkpoint_digest = ?
            """,
            (internal_id, command.checkpoint.checkpoint_digest),
        ).fetchone()
        if checkpoint_row is None:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite waiting transition is incomplete"
            )
        stored_effect_id = _decode_sqlite_text(
            "dispatch_effect_id",
            checkpoint_row["dispatch_effect_id"],
        )
        effect_row = connection.execute(
            "SELECT * FROM effect_outbox WHERE effect_id = ?",
            (stored_effect_id,),
        ).fetchone()
        event_row = connection.execute(
            """
            SELECT *
            FROM run_events
            WHERE run_internal_id = ? AND sequence = ?
            """,
            (internal_id, snapshot.event_high_watermark),
        ).fetchone()
        if effect_row is None or event_row is None:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite waiting transition is incomplete"
            )
        issuance = command.callback_issuance
        checkpoint_matches = (
            _decode_sqlite_text(
                "checkpoint_format_version",
                checkpoint_row["checkpoint_format_version"],
            )
            == command.checkpoint.format_version
            and _decode_sqlite_text(
                "checkpoint_json",
                checkpoint_row["checkpoint_json"],
            )
            == command.checkpoint.checkpoint_json
            and _decode_sqlite_text(
                "operation_id",
                checkpoint_row["operation_id"],
            )
            == issuance.operation_id
            and _decode_sqlite_text(
                "operation_attempt_id",
                checkpoint_row["operation_attempt_id"],
            )
            == issuance.operation_attempt_id
            and _decode_sqlite_text(
                "callback_idempotency_key",
                checkpoint_row["callback_idempotency_key"],
            )
            == issuance.callback_idempotency_key
            and _decode_sqlite_integer(
                "issuing_lease_generation",
                checkpoint_row["issuing_lease_generation"],
            )
            == issuance.lease_generation
            and _decode_sqlite_integer(
                "issuing_fencing_token",
                checkpoint_row["issuing_fencing_token"],
            )
            == issuance.fencing_token
            and _decode_sqlite_text(
                "dispatch_effect_id",
                checkpoint_row["dispatch_effect_id"],
            )
            == command.dispatch_effect.effect_id
        )
        effect_matches = (
            _decode_sqlite_text(
                "effect_kind",
                effect_row["effect_kind"],
            )
            == command.dispatch_effect.kind.value
            and _decode_sqlite_text(
                "effect idempotency_key",
                effect_row["idempotency_key"],
            )
            == command.dispatch_effect.idempotency_key
            and _decode_sqlite_text(
                "effect payload_json",
                effect_row["payload_json"],
            )
            == command.dispatch_effect.payload_json
            and _decode_sqlite_text(
                "effect payload_digest",
                effect_row["payload_digest"],
            )
            == command.dispatch_effect.payload_digest
        )
        event_matches = (
            _decode_sqlite_text("event kind", event_row["kind"])
            == command.waiting_event.kind
            and _decode_sqlite_text(
                "event payload_json",
                event_row["payload_json"],
            )
            == command.waiting_event.payload_json
            and _decode_sqlite_text(
                "event payload_digest",
                event_row["payload_digest"],
            )
            == command.waiting_event.payload_digest
            and _decode_sqlite_integer(
                "event created_at_unix_ms",
                event_row["created_at_unix_ms"],
            )
            == command.waiting_event.created_at_unix_ms
        )
        if not checkpoint_matches or not effect_matches or not event_matches:
            raise CheckpointIntegrityError(
                "stored waiting transition conflicts with retry"
            )
        return snapshot

    def accept_callback_and_queue_resume(
        self,
        command: AcceptedRunCallbackCommit,
    ) -> CallbackAcceptance:
        if not isinstance(command, AcceptedRunCallbackCommit):
            raise TypeError(
                "accepted-run SQLite callback command must be an "
                "AcceptedRunCallbackCommit"
            )
        if command.accepted_event.kind != "external_callback_received":
            raise ValueError(
                "accepted-run SQLite callback event kind must be "
                "external_callback_received"
            )
        if (
            command.accepted_event.created_at_unix_ms
            != command.received_at_unix_ms
        ):
            raise ValueError(
                "accepted-run SQLite callback event time must match "
                "received_at_unix_ms"
            )
        if command.received_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite callback timestamp exceeds SQLite "
                "integer range"
            )

        def transition(connection: sqlite3.Connection) -> CallbackAcceptance:
            requested = command.submission
            requested_issuance = requested.issuance
            run = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (command.tenant_id, requested_issuance.run_id),
            ).fetchone()
            if run is None:
                raise AcceptedRunNotFoundError(
                    command.tenant_id,
                    requested_issuance.run_id,
                )
            stored_owner = _decode_sqlite_text(
                "owner_principal_id",
                run["owner_principal_id"],
            )
            if stored_owner != command.owner_principal_id:
                raise AcceptedRunNotFoundError(
                    command.tenant_id,
                    requested_issuance.run_id,
                )
            internal_id = _decode_sqlite_text(
                "internal_id",
                run["internal_id"],
            )

            def checkpoint_issuance(
                checkpoint_row: sqlite3.Row,
            ) -> CallbackIssuanceIdentity:
                return CallbackIssuanceIdentity(
                    run_id=requested_issuance.run_id,
                    checkpoint_digest=_decode_sqlite_text(
                        "checkpoint_digest",
                        checkpoint_row["checkpoint_digest"],
                    ),
                    operation_id=_decode_sqlite_text(
                        "operation_id",
                        checkpoint_row["operation_id"],
                    ),
                    operation_attempt_id=_decode_sqlite_text(
                        "operation_attempt_id",
                        checkpoint_row["operation_attempt_id"],
                    ),
                    callback_idempotency_key=_decode_sqlite_text(
                        "callback_idempotency_key",
                        checkpoint_row["callback_idempotency_key"],
                    ),
                    lease_generation=_decode_sqlite_integer(
                        "issuing_lease_generation",
                        checkpoint_row["issuing_lease_generation"],
                    ),
                    fencing_token=_decode_sqlite_integer(
                        "issuing_fencing_token",
                        checkpoint_row["issuing_fencing_token"],
                    ),
                )

            def replay_existing(
                inbox_row: sqlite3.Row,
            ) -> CallbackAcceptance:
                checkpoint_row = connection.execute(
                    """
                    SELECT *
                    FROM run_checkpoints
                    WHERE run_internal_id = ? AND checkpoint_digest = ?
                    """,
                    (
                        internal_id,
                        _decode_sqlite_text(
                            "callback checkpoint_digest",
                            inbox_row["checkpoint_digest"],
                        ),
                    ),
                ).fetchone()
                if checkpoint_row is None:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite callback inbox has no checkpoint"
                    )
                expected = checkpoint_issuance(checkpoint_row)
                acceptance = CallbackAcceptance(
                    submission=CallbackSubmissionIdentity(
                        issuance=expected,
                        payload_digest=_decode_sqlite_text(
                            "callback payload_digest",
                            inbox_row["payload_digest"],
                        ),
                    ),
                    receipt_json=_decode_sqlite_text(
                        "callback receipt_json",
                        inbox_row["receipt_json"],
                    ),
                    accepted_event_sequence=_decode_sqlite_integer(
                        "callback accepted_event_sequence",
                        inbox_row["accepted_event_sequence"],
                    ),
                    state_version=_decode_sqlite_integer(
                        "callback accepted_state_version",
                        inbox_row["accepted_state_version"],
                    ),
                )
                replay = resolve_callback_replay(
                    expected_issuance=expected,
                    existing_acceptance=acceptance,
                    requested_submission=requested,
                )
                if replay is None:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite callback replay disappeared"
                    )
                return replay

            existing = connection.execute(
                """
                SELECT *
                FROM callback_inbox
                WHERE run_internal_id = ? AND callback_idempotency_key = ?
                """,
                (
                    internal_id,
                    requested_issuance.callback_idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                return replay_existing(existing)
            existing = connection.execute(
                """
                SELECT *
                FROM callback_inbox
                WHERE run_internal_id = ? AND checkpoint_digest = ?
                """,
                (internal_id, requested_issuance.checkpoint_digest),
            ).fetchone()
            if existing is not None:
                return replay_existing(existing)

            current_checkpoint_digest = run["current_checkpoint_digest"]
            if current_checkpoint_digest is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite callback target has no checkpoint"
                )
            checkpoint_row = connection.execute(
                """
                SELECT *
                FROM run_checkpoints
                WHERE run_internal_id = ? AND checkpoint_digest = ?
                """,
                (
                    internal_id,
                    _decode_sqlite_text(
                        "current_checkpoint_digest",
                        current_checkpoint_digest,
                    ),
                ),
            ).fetchone()
            if checkpoint_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite callback target checkpoint is missing"
                )
            expected_issuance = checkpoint_issuance(checkpoint_row)
            resolve_callback_replay(
                expected_issuance=expected_issuance,
                existing_acceptance=None,
                requested_submission=requested,
            )
            snapshot = self._snapshot_from_row(run)
            if snapshot.phase is not AcceptedRunPhase.WAITING_CALLBACK:
                raise CallbackIssuanceConflictError(
                    expected_issuance,
                    requested_issuance,
                )
            if snapshot.state_version != command.expected_state_version:
                raise AcceptedRunStateConflictError(
                    requested_issuance.run_id,
                    command.expected_state_version,
                    snapshot.state_version,
                )
            try:
                stored_checkpoint = StoredRuntimeCheckpoint(
                    format_version=_decode_sqlite_text(
                        "checkpoint_format_version",
                        checkpoint_row["checkpoint_format_version"],
                    ),
                    checkpoint_digest=expected_issuance.checkpoint_digest,
                    checkpoint_json=_decode_sqlite_text(
                        "checkpoint_json",
                        checkpoint_row["checkpoint_json"],
                    ),
                )
                checkpoint = decode_runtime_checkpoint(stored_checkpoint)
            except (CheckpointIntegrityError, ValueError) as error:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite callback checkpoint is invalid"
                ) from error
            submitted_at = checkpoint.operation["submitted_at_unix_ms"]
            if (
                not isinstance(submitted_at, int)
                or isinstance(submitted_at, bool)
                or command.received_at_unix_ms < submitted_at
            ):
                raise CallbackIssuanceConflictError(
                    expected_issuance,
                    requested_issuance,
                )
            expires_at = checkpoint.operation.get("expires_at_unix_ms")
            if (
                isinstance(expires_at, int)
                and not isinstance(expires_at, bool)
                and command.received_at_unix_ms >= expires_at
            ):
                raise AcceptedRunCallbackExpiredError(
                    expected_issuance,
                    command.received_at_unix_ms,
                )
            if snapshot.event_high_watermark >= _MAX_SQLITE_INTEGER:
                raise OverflowError(
                    "accepted-run SQLite callback event sequence is exhausted"
                )
            if snapshot.state_version >= _MAX_SQLITE_INTEGER:
                raise OverflowError(
                    "accepted-run SQLite callback state version is exhausted"
                )
            next_event_sequence = snapshot.event_high_watermark + 1
            next_state_version = snapshot.state_version + 1
            event = command.accepted_event
            connection.execute(
                """
                INSERT INTO run_events (
                  run_internal_id,
                  sequence,
                  kind,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    next_event_sequence,
                    event.kind,
                    event.payload_json,
                    event.payload_digest,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("accept_callback.after_event_insert")
            connection.execute(
                """
                INSERT INTO callback_inbox (
                  run_internal_id,
                  checkpoint_digest,
                  callback_idempotency_key,
                  payload_json,
                  payload_digest,
                  receipt_json,
                  accepted_event_sequence,
                  accepted_state_version,
                  received_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    expected_issuance.checkpoint_digest,
                    expected_issuance.callback_idempotency_key,
                    command.payload_json,
                    command.submission.payload_digest,
                    command.receipt_json,
                    next_event_sequence,
                    next_state_version,
                    command.received_at_unix_ms,
                ),
            )
            self._hit_failpoint("accept_callback.after_inbox_insert")
            dispatch_effect_id = _decode_sqlite_text(
                "dispatch_effect_id",
                checkpoint_row["dispatch_effect_id"],
            )
            dispatch = connection.execute(
                """
                SELECT delivery_state
                FROM effect_outbox
                WHERE effect_id = ? AND run_internal_id = ?
                """,
                (dispatch_effect_id, internal_id),
            ).fetchone()
            if dispatch is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite callback dispatch effect is missing"
                )
            delivery_state = _decode_sqlite_text(
                "delivery_state",
                dispatch["delivery_state"],
            )
            if delivery_state in {"pending", "claimed", "dead_letter"}:
                updated_dispatch = connection.execute(
                    """
                    UPDATE effect_outbox
                    SET delivery_state = 'satisfied_by_callback',
                        claim_owner_id = NULL,
                        claim_expires_at_unix_ms = NULL
                    WHERE effect_id = ?
                      AND run_internal_id = ?
                      AND delivery_state = ?
                    """,
                    (
                        dispatch_effect_id,
                        internal_id,
                        delivery_state,
                    ),
                )
                if updated_dispatch.rowcount != 1:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite callback lost its dispatch effect"
                    )
            elif delivery_state not in {"delivered"}:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite callback dispatch state is invalid"
                )
            self._hit_failpoint("accept_callback.after_dispatch_satisfied")
            updated = connection.execute(
                """
                UPDATE accepted_runs
                SET phase = 'ready_resume',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?
                WHERE internal_id = ?
                  AND phase = 'waiting_callback'
                  AND state_version = ?
                  AND current_checkpoint_digest = ?
                """,
                (
                    next_state_version,
                    next_event_sequence,
                    command.received_at_unix_ms,
                    internal_id,
                    command.expected_state_version,
                    expected_issuance.checkpoint_digest,
                ),
            )
            if updated.rowcount != 1:
                raise AcceptedRunStateConflictError(
                    requested_issuance.run_id,
                    command.expected_state_version,
                    snapshot.state_version,
                )
            self._hit_failpoint("accept_callback.after_state_update")
            return CallbackAcceptance(
                submission=command.submission,
                receipt_json=command.receipt_json,
                accepted_event_sequence=next_event_sequence,
                state_version=next_state_version,
            )

        acceptance = self._database._run_immediate(transition)
        self._hit_failpoint("accept_callback.after_commit")
        return acceptance

    def commit_terminal(
        self,
        command: AcceptedRunTerminalCommit,
    ) -> AcceptedRunSnapshot:
        if not isinstance(command, AcceptedRunTerminalCommit):
            raise TypeError(
                "accepted-run SQLite terminal command must be an "
                "AcceptedRunTerminalCommit"
            )
        expected_event_kind = _TERMINAL_EVENT_KINDS.get(
            command.terminal_status
        )
        if expected_event_kind is None:
            raise ValueError(
                "accepted-run SQLite terminal_status is unsupported"
            )
        if command.terminal_event.kind != expected_event_kind:
            raise ValueError(
                "accepted-run SQLite terminal event kind must match "
                "terminal_status"
            )
        if command.terminal_event.created_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite terminal timestamp exceeds SQLite "
                "integer range"
            )

        def transition(connection: sqlite3.Connection) -> AcceptedRunSnapshot:
            claim = command.claim
            row = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (claim.tenant_id, claim.run_id),
            ).fetchone()
            if row is None:
                raise AcceptedRunNotFoundError(
                    claim.tenant_id,
                    claim.run_id,
                )
            snapshot = self._snapshot_from_row(row)
            if snapshot.phase is AcceptedRunPhase.TERMINAL:
                return self._replay_terminal_commit(
                    connection,
                    row,
                    snapshot,
                    command,
                )
            if snapshot.phase is not AcceptedRunPhase.RUNNING:
                raise StaleAcceptedRunClaimError(snapshot.claim, claim)
            assert_current_claim(current=snapshot.claim, provided=claim)
            if snapshot.state_version != command.expected_state_version:
                raise AcceptedRunStateConflictError(
                    claim.run_id,
                    command.expected_state_version,
                    snapshot.state_version,
                )
            event = command.terminal_event
            if event.created_at_unix_ms >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunLeaseExpiredError(
                    claim,
                    "terminal commit",
                )
            if (
                snapshot.state_version >= _MAX_SQLITE_INTEGER
                or snapshot.event_high_watermark >= _MAX_SQLITE_INTEGER
            ):
                raise OverflowError(
                    "accepted-run SQLite terminal counters are exhausted"
                )
            internal_id = _decode_sqlite_text(
                "internal_id",
                row["internal_id"],
            )
            next_state_version = snapshot.state_version + 1
            next_event_sequence = snapshot.event_high_watermark + 1
            effect = command.completion_effect
            connection.execute(
                """
                INSERT INTO effect_outbox (
                  effect_id,
                  run_internal_id,
                  checkpoint_digest,
                  effect_kind,
                  idempotency_key,
                  payload_json,
                  payload_digest,
                  available_at_unix_ms,
                  delivery_state,
                  attempt_count,
                  claim_owner_id,
                  claim_generation,
                  claim_fencing_token,
                  claim_expires_at_unix_ms,
                  created_at_unix_ms,
                  delivered_at_unix_ms
                )
                VALUES (
                  ?, ?, NULL, ?, ?, ?, ?, ?, 'pending', 0, NULL, 0, 0, NULL,
                  ?, NULL
                )
                """,
                (
                    effect.effect_id,
                    internal_id,
                    effect.kind.value,
                    effect.idempotency_key,
                    effect.payload_json,
                    effect.payload_digest,
                    event.created_at_unix_ms,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("commit_terminal.after_outbox_insert")
            connection.execute(
                """
                INSERT INTO run_events (
                  run_internal_id,
                  sequence,
                  kind,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    internal_id,
                    next_event_sequence,
                    event.kind,
                    event.payload_json,
                    event.payload_digest,
                    event.created_at_unix_ms,
                ),
            )
            self._hit_failpoint("commit_terminal.after_event_insert")
            updated = connection.execute(
                """
                UPDATE accepted_runs
                SET phase = 'terminal',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    terminal_status = ?,
                    terminal_result_json = ?,
                    terminal_result_digest = ?,
                    lease_owner_id = NULL,
                    lease_expires_at_unix_ms = NULL
                WHERE internal_id = ?
                  AND phase = 'running'
                  AND state_version = ?
                  AND lease_owner_id = ?
                  AND lease_generation = ?
                  AND fencing_token = ?
                  AND lease_expires_at_unix_ms = ?
                """,
                (
                    next_state_version,
                    next_event_sequence,
                    event.created_at_unix_ms,
                    command.terminal_status,
                    command.result_json,
                    command.result_digest,
                    internal_id,
                    command.expected_state_version,
                    claim.lease_owner_id,
                    claim.lease_generation,
                    claim.fencing_token,
                    claim.lease_expires_at_unix_ms,
                ),
            )
            if updated.rowcount != 1:
                raise StaleAcceptedRunClaimError(snapshot.claim, claim)
            self._hit_failpoint("commit_terminal.after_state_update")
            updated_row = connection.execute(
                "SELECT * FROM accepted_runs WHERE internal_id = ?",
                (internal_id,),
            ).fetchone()
            if updated_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite terminal transition lost its run"
                )
            return self._snapshot_from_row(updated_row)

        snapshot = self._database._run_immediate(transition)
        self._hit_failpoint("commit_terminal.after_commit")
        return snapshot

    def _replay_terminal_commit(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        snapshot: AcceptedRunSnapshot,
        command: AcceptedRunTerminalCommit,
    ) -> AcceptedRunSnapshot:
        claim = command.claim
        if (
            _decode_sqlite_integer(
                "lease_generation",
                row["lease_generation"],
            )
            != claim.lease_generation
            or _decode_sqlite_integer(
                "fencing_token",
                row["fencing_token"],
            )
            != claim.fencing_token
        ):
            raise StaleAcceptedRunClaimError(None, claim)
        if snapshot.state_version != command.expected_state_version + 1:
            raise AcceptedRunTerminalConflictError(claim.run_id)
        stored_result_digest = _decode_sqlite_text(
            "terminal_result_digest",
            row["terminal_result_digest"],
        )
        if (
            snapshot.terminal_status != command.terminal_status
            or snapshot.terminal_result_json != command.result_json
            or stored_result_digest != command.result_digest
        ):
            raise AcceptedRunTerminalConflictError(claim.run_id)
        internal_id = _decode_sqlite_text(
            "internal_id",
            row["internal_id"],
        )
        effects = connection.execute(
            """
            SELECT *
            FROM effect_outbox
            WHERE run_internal_id = ? AND effect_kind = 'completion'
            """,
            (internal_id,),
        ).fetchall()
        event_row = connection.execute(
            """
            SELECT *
            FROM run_events
            WHERE run_internal_id = ? AND sequence = ?
            """,
            (internal_id, snapshot.event_high_watermark),
        ).fetchone()
        if len(effects) != 1 or event_row is None:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite terminal transition is incomplete"
            )
        effect_row = effects[0]
        effect = command.completion_effect
        effect_matches = (
            _decode_sqlite_text("effect_id", effect_row["effect_id"])
            == effect.effect_id
            and _decode_sqlite_text(
                "effect idempotency_key",
                effect_row["idempotency_key"],
            )
            == effect.idempotency_key
            and _decode_sqlite_text(
                "effect payload_json",
                effect_row["payload_json"],
            )
            == effect.payload_json
            and _decode_sqlite_text(
                "effect payload_digest",
                effect_row["payload_digest"],
            )
            == effect.payload_digest
        )
        event = command.terminal_event
        event_matches = (
            _decode_sqlite_text("event kind", event_row["kind"]) == event.kind
            and _decode_sqlite_text(
                "event payload_json",
                event_row["payload_json"],
            )
            == event.payload_json
            and _decode_sqlite_text(
                "event payload_digest",
                event_row["payload_digest"],
            )
            == event.payload_digest
            and _decode_sqlite_integer(
                "event created_at_unix_ms",
                event_row["created_at_unix_ms"],
            )
            == event.created_at_unix_ms
        )
        if not effect_matches or not event_matches:
            raise AcceptedRunTerminalConflictError(claim.run_id)
        return snapshot

    def get_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> AcceptedRunSnapshot | None:
        tenant_id = _validate_lookup_text(
            "accepted-run SQLite lookup",
            "tenant_id",
            tenant_id,
        )
        run_id = _validate_lookup_text(
            "accepted-run SQLite lookup",
            "run_id",
            run_id,
        )

        def read(
            connection: sqlite3.Connection,
        ) -> AcceptedRunSnapshot | None:
            row = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (tenant_id, run_id),
            ).fetchone()
            return None if row is None else self._snapshot_from_row(row)

        return self._database._run_read(read)

    def get_checkpoint(
        self,
        *,
        tenant_id: str,
        run_id: str,
        checkpoint_digest: str,
    ) -> StoredRuntimeCheckpoint | None:
        tenant_id = _validate_lookup_text(
            "accepted-run SQLite checkpoint read",
            "tenant_id",
            tenant_id,
        )
        run_id = _validate_lookup_text(
            "accepted-run SQLite checkpoint read",
            "run_id",
            run_id,
        )
        checkpoint_digest = _validate_lookup_text(
            "accepted-run SQLite checkpoint read",
            "checkpoint_digest",
            checkpoint_digest,
        )

        def read(
            connection: sqlite3.Connection,
        ) -> StoredRuntimeCheckpoint | None:
            row = connection.execute(
                """
                SELECT run_checkpoints.checkpoint_format_version,
                       run_checkpoints.checkpoint_digest,
                       run_checkpoints.checkpoint_json
                FROM run_checkpoints
                JOIN accepted_runs
                  ON accepted_runs.internal_id =
                     run_checkpoints.run_internal_id
                WHERE accepted_runs.tenant_id = ?
                  AND accepted_runs.external_run_id = ?
                  AND run_checkpoints.checkpoint_digest = ?
                """,
                (tenant_id, run_id, checkpoint_digest),
            ).fetchone()
            if row is None:
                return None
            try:
                return StoredRuntimeCheckpoint(
                    format_version=_decode_sqlite_text(
                        "checkpoint_format_version",
                        row["checkpoint_format_version"],
                    ),
                    checkpoint_digest=_decode_sqlite_text(
                        "checkpoint_digest",
                        row["checkpoint_digest"],
                    ),
                    checkpoint_json=_decode_sqlite_text(
                        "checkpoint_json",
                        row["checkpoint_json"],
                    ),
                )
            except ValueError as error:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite checkpoint is invalid"
                ) from error

        return self._database._run_read(read)

    def read_events(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int,
        limit: int,
    ) -> AcceptedRunEventPage:
        tenant_id = _validate_lookup_text(
            "accepted-run SQLite event read",
            "tenant_id",
            tenant_id,
        )
        run_id = _validate_lookup_text(
            "accepted-run SQLite event read",
            "run_id",
            run_id,
        )
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
            or after_sequence > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "accepted-run SQLite event read after_sequence must be "
                "a non-negative SQLite integer"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE
        ):
            raise ValueError(
                "accepted-run SQLite event read limit must be between 1 and "
                f"{MAX_ACCEPTED_RUN_EVENT_PAGE_SIZE}"
            )

        def read(connection: sqlite3.Connection) -> AcceptedRunEventPage:
            run = connection.execute(
                """
                SELECT internal_id, event_low_watermark, event_high_watermark
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (tenant_id, run_id),
            ).fetchone()
            if run is None:
                raise AcceptedRunNotFoundError(tenant_id, run_id)
            rows = connection.execute(
                """
                SELECT sequence, kind, payload_json, payload_digest,
                       created_at_unix_ms
                FROM run_events
                WHERE run_internal_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (
                    _decode_sqlite_text(
                        "internal_id",
                        run["internal_id"],
                    ),
                    after_sequence,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            events = tuple(
                self._event_from_row(run_id, row)
                for row in page_rows
            )
            next_after_sequence = (
                events[-1].sequence if has_more and events else None
            )
            try:
                return AcceptedRunEventPage(
                    events=events,
                    low_watermark=_decode_sqlite_integer(
                        "event_low_watermark",
                        run["event_low_watermark"],
                    ),
                    high_watermark=_decode_sqlite_integer(
                        "event_high_watermark",
                        run["event_high_watermark"],
                    ),
                    next_after_sequence=next_after_sequence,
                )
            except (TypeError, ValueError) as error:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite event page is invalid"
                ) from error

        return self._database._run_read(read)
