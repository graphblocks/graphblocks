from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier

import pytest

from graphblocks.run_store import SQLiteRunStore
from graphblocks.sqlite_server_storage import (
    SQLITE_ACCEPTED_RUN_APPLICATION_ID,
    SQLITE_ACCEPTED_RUN_SCHEMA_VERSION,
    SQLiteAcceptedRunBusyError,
    SQLiteAcceptedRunCorruptionError,
    SQLiteAcceptedRunDatabase,
    SQLiteAcceptedRunSchemaMismatchError,
    SQLiteAcceptedRunSchemaVersionError,
    SQLiteAcceptedRunUnavailableError,
)


_EXPECTED_TABLES = frozenset(
    {
        "accepted_run_storage_metadata",
        "accepted_runs",
        "callback_inbox",
        "effect_outbox",
        "run_checkpoints",
        "run_events",
    }
)


def test_sqlite_accepted_run_database_initializes_dedicated_schema(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"

    database = SQLiteAcceptedRunDatabase(path, busy_timeout_ms=250)
    schema = database.schema_info()

    assert schema.application_id == SQLITE_ACCEPTED_RUN_APPLICATION_ID
    assert schema.user_version == SQLITE_ACCEPTED_RUN_SCHEMA_VERSION
    assert schema.schema_name == "graphblocks.accepted-runs.sqlite"
    assert schema.schema_version == SQLITE_ACCEPTED_RUN_SCHEMA_VERSION
    assert schema.journal_mode == "wal"
    assert schema.foreign_keys_enabled
    assert schema.synchronous == 2
    assert schema.busy_timeout_ms == 250
    assert schema.tables == _EXPECTED_TABLES


def test_sqlite_accepted_run_database_reopens_without_recreating_schema(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    first = SQLiteAcceptedRunDatabase(path)
    first_info = first.schema_info()

    second = SQLiteAcceptedRunDatabase(path)

    assert second.schema_info() == first_info


def test_sqlite_accepted_run_database_serializes_concurrent_initialization(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    starting = Barrier(2)

    def initialize(_: int):
        starting.wait()
        return SQLiteAcceptedRunDatabase(path).schema_info()

    with ThreadPoolExecutor(max_workers=2) as executor:
        infos = tuple(executor.map(initialize, range(2)))

    assert infos == (infos[0], infos[0])


def test_sqlite_accepted_run_schema_fences_checkpoint_effect_relationships(
    tmp_path,
) -> None:
    database = SQLiteAcceptedRunDatabase(tmp_path / "accepted-runs.sqlite3")

    def referenced_tables(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> frozenset[str]:
        return frozenset(
            str(row["table"])
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table_name}")'
            ).fetchall()
        )

    accepted_run_references = database._run_read(
        lambda connection: referenced_tables(connection, "accepted_runs")
    )
    checkpoint_references = database._run_read(
        lambda connection: referenced_tables(connection, "run_checkpoints")
    )
    effect_references = database._run_read(
        lambda connection: referenced_tables(connection, "effect_outbox")
    )

    assert "run_checkpoints" in accepted_run_references
    assert "effect_outbox" in checkpoint_references
    assert "run_checkpoints" in effect_references


def test_sqlite_accepted_run_database_rejects_legacy_run_store_file(
    tmp_path,
) -> None:
    path = tmp_path / "legacy-runs.sqlite3"
    legacy = SQLiteRunStore(path)
    legacy.create_run(
        "sha256:legacy",
        {"request": "preserved"},
        run_id="legacy-run-1",
    )
    legacy.close()

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="belongs to another application or has no accepted-run identity",
    ):
        SQLiteAcceptedRunDatabase(path)

    reopened = SQLiteRunStore(path)
    try:
        assert reopened.get_run("legacy-run-1").inputs == {
            "request": "preserved"
        }
    finally:
        reopened.close()


def test_sqlite_accepted_run_database_rejects_newer_schema_version(
    tmp_path,
) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        f"PRAGMA application_id = {SQLITE_ACCEPTED_RUN_APPLICATION_ID}"
    )
    connection.execute(
        f"PRAGMA user_version = {SQLITE_ACCEPTED_RUN_SCHEMA_VERSION + 1}"
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        SQLiteAcceptedRunSchemaVersionError,
        match="unsupported accepted-run SQLite schema version",
    ):
        SQLiteAcceptedRunDatabase(path)


def test_sqlite_accepted_run_database_rejects_forged_incomplete_schema(
    tmp_path,
) -> None:
    path = tmp_path / "forged.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        f"PRAGMA application_id = {SQLITE_ACCEPTED_RUN_APPLICATION_ID}"
    )
    connection.execute(
        f"PRAGMA user_version = {SQLITE_ACCEPTED_RUN_SCHEMA_VERSION}"
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="accepted-run SQLite schema tables do not match",
    ):
        SQLiteAcceptedRunDatabase(path)


def test_sqlite_accepted_run_database_maps_corruption_as_non_retryable(
    tmp_path,
) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(SQLiteAcceptedRunCorruptionError) as raised:
        SQLiteAcceptedRunDatabase(path)

    assert not raised.value.retryable


def test_sqlite_accepted_run_database_uses_connections_in_calling_thread(
    tmp_path,
) -> None:
    database = SQLiteAcceptedRunDatabase(tmp_path / "accepted-runs.sqlite3")

    with ThreadPoolExecutor(max_workers=4) as executor:
        infos = tuple(executor.map(lambda _: database.schema_info(), range(12)))

    assert infos == (infos[0],) * len(infos)


def test_sqlite_accepted_run_database_maps_busy_writer_as_retryable(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs.sqlite3"
    database = SQLiteAcceptedRunDatabase(path, busy_timeout_ms=20)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SQLiteAcceptedRunBusyError) as raised:
            database._run_immediate(lambda connection: None)
    finally:
        blocker.rollback()
        blocker.close()

    assert raised.value.retryable


def test_sqlite_accepted_run_transaction_rolls_back_and_releases_connection(
    tmp_path,
) -> None:
    database = SQLiteAcceptedRunDatabase(tmp_path / "accepted-runs.sqlite3")

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO accepted_run_storage_metadata (key, value)
            VALUES ('transient-test-key', 'must-roll-back')
            """
        )
        raise RuntimeError("injected transaction failure")

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        database._run_immediate(fail_after_write)

    assert (
        database._run_read(
            lambda connection: connection.execute(
                """
                SELECT value
                FROM accepted_run_storage_metadata
                WHERE key = 'transient-test-key'
                """
            ).fetchone()
        )
        is None
    )
    database._run_immediate(
        lambda connection: connection.execute(
            """
            INSERT INTO accepted_run_storage_metadata (key, value)
            VALUES ('after-rollback', 'committed')
            """
        )
    )


def test_sqlite_accepted_run_read_connection_is_query_only(tmp_path) -> None:
    database = SQLiteAcceptedRunDatabase(tmp_path / "accepted-runs.sqlite3")

    with pytest.raises(
        SQLiteAcceptedRunUnavailableError,
        match="accepted-run SQLite database operation failed",
    ):
        database._run_read(
            lambda connection: connection.execute(
                """
                INSERT INTO accepted_run_storage_metadata (key, value)
                VALUES ('read-write-attempt', 'rejected')
                """
            )
        )

    assert (
        database._run_read(
            lambda connection: connection.execute(
                """
                SELECT value
                FROM accepted_run_storage_metadata
                WHERE key = 'read-write-attempt'
                """
            ).fetchone()
        )
        is None
    )


def test_sqlite_accepted_run_database_rejects_process_local_memory_mode() -> None:
    with pytest.raises(
        ValueError,
        match="requires a filesystem path and does not support :memory:",
    ):
        SQLiteAcceptedRunDatabase(":memory:")
