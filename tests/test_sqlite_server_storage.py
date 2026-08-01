from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from graphblocks.canonical import canonical_hash
from graphblocks.run_store import SQLiteRunStore
from graphblocks.server_storage import (
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaim,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryRetry,
    AcceptedRunEffectDeliveryStateConflictError,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import (
    SQLITE_ACCEPTED_RUN_APPLICATION_ID,
    SQLITE_ACCEPTED_RUN_SCHEMA_VERSION,
    _SCHEMA_V1_STATEMENTS,
    _SCHEMA_V2_MIGRATION_STATEMENTS,
    _SCHEMA_V3_MIGRATION_STATEMENTS,
    _SCHEMA_V4_MIGRATION_STATEMENTS,
    _SCHEMA_V5_MIGRATION_STATEMENTS,
    _SCHEMA_V6_MIGRATION_STATEMENTS,
    _SCHEMA_V7_MIGRATION_STATEMENTS,
    _SCHEMA_V8_MIGRATION_STATEMENTS,
    _MAX_SQLITE_INTEGER,
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
        "provider_effect_events",
        "provider_effects",
        "run_checkpoints",
        "run_controls",
        "run_events",
    }
)


def _initialize_version_one_database(path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V1_STATEMENTS:
            connection.execute(statement)
        connection.executemany(
            """
            INSERT INTO accepted_run_storage_metadata (key, value)
            VALUES (?, ?)
            """,
            (
                ("schema_name", "graphblocks.accepted-runs.sqlite"),
                ("schema_version", "1"),
            ),
        )
        connection.execute(
            f"PRAGMA application_id = {SQLITE_ACCEPTED_RUN_APPLICATION_ID}"
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()


def _insert_version_one_completion_effect(path) -> None:
    digest = canonical_hash({})
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
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
              lease_generation,
              fencing_token
            )
            VALUES (
              'internal-1', 'run-1', 'tenant-1', 'principal-1',
              'POST:/runs', 'admission-1', ?, '{}', '{}', ?, '{}',
              'graphblocks.ai/Graph@v1', 'graphblocks.runtime@v1',
              'graphblocks.runtime-checkpoint.v1', 1000, 1000,
              'ready_initial', 1, 1, 1, 0, 0
            )
            """,
            (digest, digest),
        )
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
              'effect-1', 'internal-1', NULL, 'completion',
              'completion-run-1', '{}', ?, 'pending', 0, NULL, 0, 0,
              NULL, 1250, NULL
            )
            """,
            (digest,),
        )
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_one_database_to_version_two(path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V2_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '2'
            WHERE key = 'schema_version' AND value = '1'
            """
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_two_database_to_version_three(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V3_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '3'
            WHERE key = 'schema_version' AND value = '2'
            """
        )
        connection.execute("PRAGMA user_version = 3")
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_three_database_to_version_four(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V4_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '4'
            WHERE key = 'schema_version' AND value = '3'
            """
        )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()


def _initialize_version_five_claimed_effect(
    path: Path,
    *,
    attempt_count: int = 1,
    claim_generation: int = 7,
    claim_fencing_token: int = 9,
) -> None:
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)
    _upgrade_version_one_database_to_version_two(path)
    _upgrade_version_two_database_to_version_three(path)
    _upgrade_version_three_database_to_version_four(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V5_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '5'
            WHERE key = 'schema_version' AND value = '4'
            """
        )
        connection.execute("PRAGMA user_version = 5")
        connection.execute(
            """
            UPDATE effect_outbox
            SET delivery_state = 'claimed',
                attempt_count = ?,
                claim_owner_id = 'legacy-dispatcher',
                claim_generation = ?,
                claim_fencing_token = ?,
                claim_expires_at_unix_ms = ?
            WHERE effect_id = 'effect-1'
            """,
            (
                attempt_count,
                claim_generation,
                claim_fencing_token,
                _MAX_SQLITE_INTEGER - 99,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_five_database_to_version_six(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V6_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '6'
            WHERE key = 'schema_version' AND value = '5'
            """
        )
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
    finally:
        connection.close()


def _initialize_version_six_claimed_effect(
    path: Path,
    *,
    attempt_count: int = 2,
    claim_generation: int = 9,
    claim_fencing_token: int = 11,
) -> None:
    _initialize_version_five_claimed_effect(path)
    _upgrade_version_five_database_to_version_six(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE effect_outbox
            SET delivery_state = 'claimed',
                attempt_count = ?,
                claim_owner_id = 'v6-dispatcher',
                claim_generation = ?,
                claim_fencing_token = ?,
                claim_expires_at_unix_ms = ?
            WHERE effect_id = 'effect-1'
            """,
            (
                attempt_count,
                claim_generation,
                claim_fencing_token,
                _MAX_SQLITE_INTEGER - 99,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_six_database_to_version_seven(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V7_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '7'
            WHERE key = 'schema_version' AND value = '6'
            """
        )
        connection.execute("PRAGMA user_version = 7")
        connection.commit()
    finally:
        connection.close()


def _upgrade_version_seven_database_to_version_eight(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in _SCHEMA_V8_MIGRATION_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            UPDATE accepted_run_storage_metadata
            SET value = '8'
            WHERE key = 'schema_version' AND value = '7'
            """
        )
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
    finally:
        connection.close()


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
    assert SQLITE_ACCEPTED_RUN_SCHEMA_VERSION == 9


def test_sqlite_accepted_run_database_migrates_v7_provider_effect_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v7.sqlite3"
    _initialize_version_six_claimed_effect(path)
    _upgrade_version_six_database_to_version_seven(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("provider_effects")'
            ).fetchall()
        )
    ) >= frozenset(
        {
            "run_internal_id",
            "effect_id",
            "idempotency_key",
            "provider_target",
            "provider_operation",
            "intent_json",
            "intent_digest",
            "capability_snapshot_json",
            "capability_snapshot_digest",
            "origin_transfer_json",
            "origin_transfer_digest",
            "state",
            "state_version",
            "event_high_watermark",
            "created_at_unix_ms",
            "updated_at_unix_ms",
        }
    )
    provider_effect_count = database._run_read(
        lambda connection: int(
            connection.execute("SELECT count(*) FROM provider_effects").fetchone()[0]
        )
    )
    assert provider_effect_count == 0


def test_sqlite_accepted_run_database_migrates_v8_provider_claim_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v8.sqlite3"
    _initialize_version_six_claimed_effect(path)
    _upgrade_version_six_database_to_version_seven(path)
    _upgrade_version_seven_database_to_version_eight(path)

    database = SQLiteAcceptedRunDatabase(path)
    provider_effect_columns = database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("provider_effects")'
            ).fetchall()
        )
    )

    assert database.schema_info().schema_version == 9
    assert {
        "claim_json",
        "claim_digest",
        "claim_authority_digest",
        "claim_owner_id",
        "claim_generation",
        "claim_fencing_token",
        "claim_started_at_unix_ms",
        "claim_expires_at_unix_ms",
        "admitted_at_unix_ms",
        "send_attempt_id",
        "previous_send_attempt_digest",
        "last_pre_send_release_json",
        "last_pre_send_release_digest",
    } <= provider_effect_columns
    assert "provider_effects_active_send_attempt" in database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA index_list("provider_effects")'
            ).fetchall()
        )
    )


def test_sqlite_accepted_run_database_rejects_unrecoverable_v8_provider_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v8-claimed-provider.sqlite3"
    _initialize_version_six_claimed_effect(path)
    _upgrade_version_six_database_to_version_seven(path)
    _upgrade_version_seven_database_to_version_eight(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO provider_effects (
              run_internal_id,
              effect_id,
              idempotency_key,
              provider_target,
              provider_operation,
              intent_json,
              intent_digest,
              capability_snapshot_json,
              capability_snapshot_digest,
              origin_transfer_json,
              origin_transfer_digest,
              state,
              state_version,
              event_high_watermark,
              created_at_unix_ms,
              updated_at_unix_ms
            )
            VALUES (
              'internal-1', 'provider-effect-1', 'provider-key-1',
              'payments.primary', 'capture', '{}', ?, '{}', ?, '{}', ?,
              'claimed', 1, 1, 2000, 2000
            )
            """,
            (
                "sha256:" + ("1" * 64),
                "sha256:" + ("2" * 64),
                "sha256:" + ("3" * 64),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="no recoverable pre-send claim metadata",
    ):
        SQLiteAcceptedRunDatabase(path)

    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 8
        columns = frozenset(
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("provider_effects")'
            ).fetchall()
        )
    finally:
        connection.close()
    assert "claim_json" not in columns


def test_sqlite_accepted_run_database_migrates_v1_to_current_schema(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs-v1.sqlite3"
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)

    database = SQLiteAcceptedRunDatabase(path)
    schema = database.schema_info()
    effect_columns = database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("effect_outbox")'
            ).fetchall()
        )
    )

    assert schema.user_version == 9
    assert schema.schema_version == 9
    assert "available_at_unix_ms" in effect_columns
    assert "cancelled_at_unix_ms" in effect_columns
    assert "claim_started_at_unix_ms" in effect_columns
    assert "last_delivery_command_json" in effect_columns
    assert "last_delivery_command_digest" in effect_columns
    assert database._run_read(
        lambda connection: int(
            connection.execute(
                """
                SELECT available_at_unix_ms
                FROM effect_outbox
                WHERE effect_id = 'effect-1'
                """
            ).fetchone()[0]
        )
    ) == 1_250
    assert database._run_read(
        lambda connection: str(
            connection.execute(
                """
                SELECT invocation_json
                FROM accepted_runs
                WHERE external_run_id = 'run-1'
                """
            ).fetchone()[0]
        )
    ) == "{}"


def test_sqlite_accepted_run_database_migrates_v2_invocation_metadata(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs-v2.sqlite3"
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)
    _upgrade_version_one_database_to_version_two(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: str(
            connection.execute(
                "SELECT invocation_json FROM accepted_runs"
            ).fetchone()[0]
        )
    ) == "{}"


def test_sqlite_accepted_run_database_migrates_v3_control_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v3.sqlite3"
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)
    _upgrade_version_one_database_to_version_two(path)
    _upgrade_version_two_database_to_version_three(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("run_controls")'
            ).fetchall()
        )
    ) == frozenset(
        {
            "run_internal_id",
            "idempotency_key",
            "action",
            "request_digest",
            "requested_by_principal_id",
            "expected_state_version",
            "accepted_state_version",
            "accepted_event_sequence",
            "requested_at_unix_ms",
            "resulting_phase",
        }
    )


def test_sqlite_accepted_run_database_migrates_v4_pause_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v4.sqlite3"
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)
    _upgrade_version_one_database_to_version_two(path)
    _upgrade_version_two_database_to_version_three(path)
    _upgrade_version_three_database_to_version_four(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("accepted_runs")'
            ).fetchall()
        )
    ).issuperset({"paused_from_phase", "paused_at_unix_ms"})
    assert database._run_read(
        lambda connection: frozenset(
            str(row["name"])
            for row in connection.execute(
                'PRAGMA table_info("run_checkpoints")'
            ).fetchall()
        )
    ).issuperset({"callback_expected_state_version"})


def test_sqlite_accepted_run_database_rejects_ambiguous_v4_state_control(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v4-pause.sqlite3"
    _initialize_version_one_database(path)
    _insert_version_one_completion_effect(path)
    _upgrade_version_one_database_to_version_two(path)
    _upgrade_version_two_database_to_version_three(path)
    _upgrade_version_three_database_to_version_four(path)
    digest = "sha256:" + ("b" * 64)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
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
            VALUES (
              'internal-1', 2, 'run_paused', '{}', ?, 2000
            )
            """,
            (digest,),
        )
        connection.execute(
            """
            INSERT INTO run_controls (
              run_internal_id,
              idempotency_key,
              action,
              request_digest,
              requested_by_principal_id,
              expected_state_version,
              accepted_state_version,
              accepted_event_sequence,
              requested_at_unix_ms
            )
            VALUES (
              'internal-1', 'pause-v4', 'pause', ?, 'principal-1',
              1, 2, 2, 2000
            )
            """,
            (digest,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="no recoverable resulting phase",
    ):
        SQLiteAcceptedRunDatabase(path)

    connection = sqlite3.connect(path)
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4
        checkpoint_columns = {
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("run_checkpoints")'
            ).fetchall()
        }
    finally:
        connection.close()
    assert "callback_expected_state_version" not in checkpoint_columns


def test_sqlite_accepted_run_database_invalidates_v5_effect_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v5-claimed-effect.sqlite3"
    _initialize_version_five_claimed_effect(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: tuple(
            connection.execute(
                """
                SELECT delivery_state,
                       attempt_count,
                       claim_owner_id,
                       claim_generation,
                       claim_fencing_token,
                       claim_expires_at_unix_ms,
                       available_at_unix_ms
                FROM effect_outbox
                WHERE effect_id = 'effect-1'
                """
            ).fetchone()
        )
    ) == ("pending", 1, None, 8, 10, None, 1_250)

    claimed = SQLiteOutboxDispatcherRepository(
        path,
        clock=lambda: 3_000,
    ).claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="current-dispatcher",
            now_unix_ms=2_500,
            lease_duration_ms=1_000,
        )
    )
    assert claimed is not None
    assert claimed.claim is not None
    assert claimed.attempt_count == 2
    assert claimed.claim.delivery_owner_id == "current-dispatcher"
    assert claimed.claim.claim_generation == 9
    assert claimed.claim.fencing_token == 11
    assert claimed.claim.lease_expires_at_unix_ms == 4_000


@pytest.mark.parametrize(
    ("attempt_count", "claim_generation", "claim_fencing_token"),
    (
        (1, _MAX_SQLITE_INTEGER - 1, 9),
        (1, 7, _MAX_SQLITE_INTEGER - 1),
        (_MAX_SQLITE_INTEGER, 7, 9),
    ),
)
def test_sqlite_accepted_run_database_rolls_back_unreclaimable_v5_claim(
    tmp_path: Path,
    attempt_count: int,
    claim_generation: int,
    claim_fencing_token: int,
) -> None:
    path = tmp_path / "accepted-runs-v5-exhausted-effect.sqlite3"
    _initialize_version_five_claimed_effect(
        path,
        attempt_count=attempt_count,
        claim_generation=claim_generation,
        claim_fencing_token=claim_fencing_token,
    )

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="effect claim counters lack reclaim headroom",
    ):
        SQLiteAcceptedRunDatabase(path)

    connection = sqlite3.connect(path)
    try:
        stored = tuple(
            connection.execute(
                """
                SELECT delivery_state,
                       attempt_count,
                       claim_owner_id,
                       claim_generation,
                       claim_fencing_token,
                       claim_expires_at_unix_ms
                FROM effect_outbox
                WHERE effect_id = 'effect-1'
                """
            ).fetchone()
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata_version = str(
            connection.execute(
                """
                SELECT value
                FROM accepted_run_storage_metadata
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert stored == (
        "claimed",
        attempt_count,
        "legacy-dispatcher",
        claim_generation,
        claim_fencing_token,
        _MAX_SQLITE_INTEGER - 99,
    )
    assert user_version == 5
    assert metadata_version == "5"


def test_sqlite_accepted_run_database_invalidates_v6_effect_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v6-claimed-effect.sqlite3"
    _initialize_version_six_claimed_effect(path)

    database = SQLiteAcceptedRunDatabase(path)

    assert database.schema_info().schema_version == 9
    assert database._run_read(
        lambda connection: tuple(
            connection.execute(
                """
                SELECT delivery_state,
                       attempt_count,
                       claim_owner_id,
                       claim_generation,
                       claim_fencing_token,
                       claim_started_at_unix_ms,
                       claim_expires_at_unix_ms,
                       last_delivery_command_json,
                       last_delivery_command_digest,
                       available_at_unix_ms
                FROM effect_outbox
                WHERE effect_id = 'effect-1'
                """
            ).fetchone()
        )
    ) == ("pending", 2, None, 10, 12, None, None, None, None, 1_250)

    claimed = SQLiteOutboxDispatcherRepository(
        path,
        clock=lambda: 3_000,
    ).claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="current-dispatcher",
            now_unix_ms=2_500,
            lease_duration_ms=1_000,
        )
    )
    assert claimed is not None
    assert claimed.claim is not None
    assert claimed.attempt_count == 3
    assert claimed.claim.delivery_owner_id == "current-dispatcher"
    assert claimed.claim.claim_generation == 11
    assert claimed.claim.fencing_token == 13
    assert claimed.claim.claim_started_at_unix_ms == 3_000
    assert claimed.claim.lease_expires_at_unix_ms == 4_000


@pytest.mark.parametrize(
    ("attempt_count", "claim_generation", "claim_fencing_token"),
    (
        (1, _MAX_SQLITE_INTEGER - 1, 9),
        (1, 7, _MAX_SQLITE_INTEGER - 1),
        (_MAX_SQLITE_INTEGER, 7, 9),
    ),
)
def test_sqlite_accepted_run_database_rolls_back_unreclaimable_v6_claim(
    tmp_path: Path,
    attempt_count: int,
    claim_generation: int,
    claim_fencing_token: int,
) -> None:
    path = tmp_path / "accepted-runs-v6-exhausted-effect.sqlite3"
    _initialize_version_six_claimed_effect(
        path,
        attempt_count=attempt_count,
        claim_generation=claim_generation,
        claim_fencing_token=claim_fencing_token,
    )

    with pytest.raises(
        SQLiteAcceptedRunSchemaMismatchError,
        match="v6 effect claim counters lack reclaim headroom",
    ):
        SQLiteAcceptedRunDatabase(path)

    connection = sqlite3.connect(path)
    try:
        stored = tuple(
            connection.execute(
                """
                SELECT delivery_state,
                       attempt_count,
                       claim_owner_id,
                       claim_generation,
                       claim_fencing_token,
                       claim_expires_at_unix_ms
                FROM effect_outbox
                WHERE effect_id = 'effect-1'
                """
            ).fetchone()
        )
        effect_columns = frozenset(
            str(row[1])
            for row in connection.execute(
                'PRAGMA table_info("effect_outbox")'
            ).fetchall()
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        metadata_version = str(
            connection.execute(
                """
                SELECT value
                FROM accepted_run_storage_metadata
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert stored == (
        "claimed",
        attempt_count,
        "v6-dispatcher",
        claim_generation,
        claim_fencing_token,
        _MAX_SQLITE_INTEGER - 99,
    )
    assert "claim_started_at_unix_ms" not in effect_columns
    assert user_version == 6
    assert metadata_version == "6"


def test_sqlite_accepted_run_database_rejects_unverifiable_v6_ack_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v6-delivered-effect.sqlite3"
    _initialize_version_six_claimed_effect(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE effect_outbox
            SET delivery_state = 'delivered',
                claim_owner_id = NULL,
                claim_expires_at_unix_ms = NULL,
                delivered_at_unix_ms = 3000
            WHERE effect_id = 'effect-1'
            """
        )
        connection.commit()
    finally:
        connection.close()

    dispatcher = SQLiteOutboxDispatcherRepository(path, clock=lambda: 4_000)
    legacy_claim = AcceptedRunEffectDeliveryClaim(
        effect_id="effect-1",
        delivery_owner_id="v6-dispatcher",
        claim_generation=9,
        fencing_token=11,
        claim_started_at_unix_ms=2_500,
        lease_expires_at_unix_ms=_MAX_SQLITE_INTEGER - 99,
    )

    with pytest.raises(AcceptedRunEffectDeliveryStateConflictError):
        dispatcher.mark_effect_delivered(
            AcceptedRunEffectDeliveryAck(
                claim=legacy_claim,
                delivered_at_unix_ms=3_000,
            )
        )


def test_sqlite_accepted_run_database_rejects_unverifiable_v6_retry_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v6-retried-effect.sqlite3"
    _initialize_version_six_claimed_effect(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE effect_outbox
            SET delivery_state = 'pending',
                available_at_unix_ms = 4000,
                claim_owner_id = NULL,
                claim_expires_at_unix_ms = NULL
            WHERE effect_id = 'effect-1'
            """
        )
        connection.commit()
    finally:
        connection.close()

    dispatcher = SQLiteOutboxDispatcherRepository(path, clock=lambda: 4_000)
    legacy_claim = AcceptedRunEffectDeliveryClaim(
        effect_id="effect-1",
        delivery_owner_id="v6-dispatcher",
        claim_generation=9,
        fencing_token=11,
        claim_started_at_unix_ms=2_500,
        lease_expires_at_unix_ms=_MAX_SQLITE_INTEGER - 99,
    )

    with pytest.raises(AcceptedRunEffectDeliveryStateConflictError):
        dispatcher.release_effect_for_retry(
            AcceptedRunEffectDeliveryRetry(
                claim=legacy_claim,
                released_at_unix_ms=3_000,
                available_at_unix_ms=4_000,
            )
        )


def test_sqlite_accepted_run_database_serializes_concurrent_v6_migration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "accepted-runs-v6.sqlite3"
    _initialize_version_five_claimed_effect(path)
    _upgrade_version_five_database_to_version_six(path)
    starting = Barrier(2)

    def migrate(_: int):
        starting.wait()
        return SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=250,
        ).schema_info()

    with ThreadPoolExecutor(max_workers=2) as executor:
        infos = tuple(executor.map(migrate, range(2)))

    assert infos == (infos[0], infos[0])
    assert infos[0].schema_version == 9


def test_sqlite_accepted_run_database_serializes_concurrent_v1_migration(
    tmp_path,
) -> None:
    path = tmp_path / "accepted-runs-v1.sqlite3"
    _initialize_version_one_database(path)
    starting = Barrier(2)

    def migrate(_: int):
        starting.wait()
        return SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=250,
        ).schema_info()

    with ThreadPoolExecutor(max_workers=2) as executor:
        infos = tuple(executor.map(migrate, range(2)))

    assert infos == (infos[0], infos[0])
    assert infos[0].schema_version == 9


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
        return SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=250,
        ).schema_info()

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
    provider_effect_references = database._run_read(
        lambda connection: referenced_tables(connection, "provider_effects")
    )
    provider_event_references = database._run_read(
        lambda connection: referenced_tables(connection, "provider_effect_events")
    )

    assert "run_checkpoints" in accepted_run_references
    assert "effect_outbox" in checkpoint_references
    assert "run_checkpoints" in effect_references
    assert provider_effect_references == {"accepted_runs"}
    assert provider_event_references == {"provider_effects"}


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
