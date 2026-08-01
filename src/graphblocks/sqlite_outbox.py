"""Fenced SQLite bookkeeping for at-least-once external effect delivery.

This module never performs the external send. A receiver must durably
deduplicate the stable effect id or idempotency key because a process can crash
after sending and before acknowledging delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3

from .server_storage import (
    AcceptedRunEffectDeliveryAck,
    AcceptedRunEffectDeliveryClaim,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryLeaseExpiredError,
    AcceptedRunEffectDeliveryRecord,
    AcceptedRunEffectDeliveryRetry,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEffectDeliveryStateConflictError,
    AcceptedRunEffectKind,
    AcceptedRunEffectNotFoundError,
    StaleAcceptedRunEffectDeliveryClaimError,
    accepted_run_system_clock,
    assert_current_effect_delivery_claim,
)
from .sqlite_server_storage import (
    _MAX_SQLITE_INTEGER,
    SQLiteAcceptedRunCorruptionError,
    SQLiteAcceptedRunDatabase,
    _decode_effect_delivery_command_row,
    _decode_sqlite_integer,
    _decode_sqlite_text,
    _effect_delivery_command_identity,
    _validate_lookup_text,
)


_DEFAULT_MAX_LEASE_DURATION_MS = 30_000


class SQLiteOutboxDispatcherRepository:
    """SQLite authority for the two transactions around an external send."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        failpoint: Callable[[str], None] | None = None,
        clock: Callable[[], int] = accepted_run_system_clock,
        max_lease_duration_ms: int = _DEFAULT_MAX_LEASE_DURATION_MS,
    ) -> None:
        if failpoint is not None and not callable(failpoint):
            raise ValueError("accepted-run SQLite outbox failpoint must be callable")
        if not callable(clock):
            raise ValueError("accepted-run SQLite outbox clock must be callable")
        if (
            isinstance(max_lease_duration_ms, bool)
            or not isinstance(max_lease_duration_ms, int)
            or max_lease_duration_ms <= 0
            or max_lease_duration_ms > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "accepted-run SQLite outbox max lease duration must be a "
                "positive SQLite integer"
            )
        self._database = SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._failpoint = failpoint
        self._clock = clock
        self._max_lease_duration_ms = max_lease_duration_ms

    @property
    def transaction_clock(self) -> Callable[[], int]:
        return self._clock

    def _hit_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    def _transaction_now_unix_ms(self) -> int:
        now_unix_ms = self._clock()
        if (
            isinstance(now_unix_ms, bool)
            or not isinstance(now_unix_ms, int)
            or now_unix_ms < 0
            or now_unix_ms > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "accepted-run SQLite outbox clock must return a non-negative "
                "SQLite integer"
            )
        return now_unix_ms

    @staticmethod
    def _effect_row(
        connection: sqlite3.Connection,
        effect_id: str,
    ) -> sqlite3.Row | None:
        row = connection.execute(
            """
            SELECT effect_outbox.*,
                   accepted_runs.external_run_id,
                   accepted_runs.tenant_id,
                   accepted_runs.owner_principal_id
            FROM effect_outbox
            JOIN accepted_runs
              ON accepted_runs.internal_id = effect_outbox.run_internal_id
            WHERE effect_outbox.effect_id = ?
            """,
            (effect_id,),
        ).fetchone()
        if row is None:
            return None
        if not isinstance(row, sqlite3.Row):
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite outbox query did not return a Row"
            )
        return row

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row,
    ) -> AcceptedRunEffectDeliveryRecord:
        try:
            effect_id = _decode_sqlite_text(
                "effect_id",
                row["effect_id"],
            )
            stored_delivery_state = AcceptedRunEffectDeliveryState(
                _decode_sqlite_text(
                    "delivery_state",
                    row["delivery_state"],
                )
            )
            cancelled_at = row["cancelled_at_unix_ms"]
            if cancelled_at is None:
                delivery_state = stored_delivery_state
                cancelled_at_unix_ms = None
            else:
                if stored_delivery_state is not AcceptedRunEffectDeliveryState.PENDING:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite cancelled outbox effect has an "
                        "invalid physical state"
                    )
                delivery_state = AcceptedRunEffectDeliveryState.CANCELLED
                cancelled_at_unix_ms = _decode_sqlite_integer(
                    "cancelled_at_unix_ms",
                    cancelled_at,
                )
            claim = None
            if delivery_state is AcceptedRunEffectDeliveryState.CLAIMED:
                claim = AcceptedRunEffectDeliveryClaim(
                    effect_id=effect_id,
                    delivery_owner_id=_decode_sqlite_text(
                        "claim_owner_id",
                        row["claim_owner_id"],
                    ),
                    claim_generation=_decode_sqlite_integer(
                        "claim_generation",
                        row["claim_generation"],
                    ),
                    fencing_token=_decode_sqlite_integer(
                        "claim_fencing_token",
                        row["claim_fencing_token"],
                    ),
                    claim_started_at_unix_ms=_decode_sqlite_integer(
                        "claim_started_at_unix_ms",
                        row["claim_started_at_unix_ms"],
                    ),
                    lease_expires_at_unix_ms=_decode_sqlite_integer(
                        "claim_expires_at_unix_ms",
                        row["claim_expires_at_unix_ms"],
                    ),
                )
            elif row["claim_started_at_unix_ms"] is not None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite inactive outbox effect retains a "
                    "claim start"
                )
            checkpoint_digest = row["checkpoint_digest"]
            delivered_at = row["delivered_at_unix_ms"]
            record = AcceptedRunEffectDeliveryRecord(
                effect_id=effect_id,
                tenant_id=_decode_sqlite_text(
                    "tenant_id",
                    row["tenant_id"],
                ),
                run_id=_decode_sqlite_text(
                    "external_run_id",
                    row["external_run_id"],
                ),
                owner_principal_id=_decode_sqlite_text(
                    "owner_principal_id",
                    row["owner_principal_id"],
                ),
                checkpoint_digest=(
                    None
                    if checkpoint_digest is None
                    else _decode_sqlite_text(
                        "checkpoint_digest",
                        checkpoint_digest,
                    )
                ),
                kind=AcceptedRunEffectKind(
                    _decode_sqlite_text(
                        "effect_kind",
                        row["effect_kind"],
                    )
                ),
                idempotency_key=_decode_sqlite_text(
                    "effect idempotency_key",
                    row["idempotency_key"],
                ),
                payload_json=_decode_sqlite_text(
                    "effect payload_json",
                    row["payload_json"],
                ),
                payload_digest=_decode_sqlite_text(
                    "effect payload_digest",
                    row["payload_digest"],
                ),
                delivery_state=delivery_state,
                attempt_count=_decode_sqlite_integer(
                    "attempt_count",
                    row["attempt_count"],
                ),
                available_at_unix_ms=_decode_sqlite_integer(
                    "available_at_unix_ms",
                    row["available_at_unix_ms"],
                ),
                claim=claim,
                created_at_unix_ms=_decode_sqlite_integer(
                    "effect created_at_unix_ms",
                    row["created_at_unix_ms"],
                ),
                delivered_at_unix_ms=(
                    None
                    if delivered_at is None
                    else _decode_sqlite_integer(
                        "delivered_at_unix_ms",
                        delivered_at,
                    )
                ),
                cancelled_at_unix_ms=cancelled_at_unix_ms,
            )
            if (
                claim is not None
                and claim.claim_started_at_unix_ms
                < record.available_at_unix_ms
            ):
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox claim starts before effect "
                    "availability"
                )
            _decode_effect_delivery_command_row(row)
            return record
        except (TypeError, ValueError) as error:
            raise SQLiteAcceptedRunCorruptionError(
                "accepted-run SQLite outbox effect is invalid"
            ) from error

    @staticmethod
    def _assert_replay_command(
        row: sqlite3.Row,
        command: AcceptedRunEffectDeliveryAck | AcceptedRunEffectDeliveryRetry,
    ) -> None:
        stored_command = _decode_effect_delivery_command_row(row)
        stored_state = AcceptedRunEffectDeliveryState(
            _decode_sqlite_text(
                "delivery_state",
                row["delivery_state"],
            )
        )
        if stored_command is None:
            raise AcceptedRunEffectDeliveryStateConflictError(
                command.claim.effect_id,
                stored_state,
            )
        assert_current_effect_delivery_claim(
            current=stored_command.claim,
            provided=command.claim,
        )
        if stored_command != command:
            raise AcceptedRunEffectDeliveryStateConflictError(
                command.claim.effect_id,
                stored_state,
            )

    def get_effect(
        self,
        *,
        effect_id: str,
    ) -> AcceptedRunEffectDeliveryRecord | None:
        effect_id = _validate_lookup_text(
            "accepted-run SQLite outbox lookup",
            "effect_id",
            effect_id,
        )

        def read(
            connection: sqlite3.Connection,
        ) -> AcceptedRunEffectDeliveryRecord | None:
            row = self._effect_row(connection, effect_id)
            return None if row is None else self._record_from_row(row)

        return self._database._run_read(read)

    def claim_next_effect(
        self,
        request: AcceptedRunEffectDeliveryClaimRequest,
    ) -> AcceptedRunEffectDeliveryRecord | None:
        if not isinstance(request, AcceptedRunEffectDeliveryClaimRequest):
            raise TypeError(
                "accepted-run SQLite outbox claim request must be an "
                "AcceptedRunEffectDeliveryClaimRequest"
            )
        if request.lease_duration_ms > self._max_lease_duration_ms:
            raise ValueError(
                "accepted-run SQLite outbox lease duration exceeds the "
                "repository maximum"
            )

        def transition(
            connection: sqlite3.Connection,
        ) -> AcceptedRunEffectDeliveryRecord | None:
            transaction_now = self._transaction_now_unix_ms()
            if request.now_unix_ms > transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox claim request timestamp must "
                    "not be later than the repository clock"
                )
            if transaction_now > _MAX_SQLITE_INTEGER - request.lease_duration_ms:
                raise ValueError(
                    "accepted-run SQLite outbox lease expiry exceeds SQLite "
                    "integer range"
                )
            lease_expires_at = transaction_now + request.lease_duration_ms
            row = connection.execute(
                """
                SELECT effect_outbox.*,
                       accepted_runs.external_run_id,
                       accepted_runs.tenant_id,
                       accepted_runs.owner_principal_id
                FROM effect_outbox
                JOIN accepted_runs
                  ON accepted_runs.internal_id =
                     effect_outbox.run_internal_id
                WHERE effect_outbox.cancelled_at_unix_ms IS NULL
                  AND (
                    (
                      effect_outbox.delivery_state = 'pending'
                      AND effect_outbox.available_at_unix_ms <= ?
                    )
                    OR
                    (
                      effect_outbox.delivery_state = 'claimed'
                      AND effect_outbox.claim_expires_at_unix_ms <= ?
                    )
                  )
                ORDER BY
                  CASE effect_outbox.delivery_state
                    WHEN 'pending'
                    THEN effect_outbox.available_at_unix_ms
                    ELSE effect_outbox.claim_expires_at_unix_ms
                  END,
                  effect_outbox.created_at_unix_ms,
                  effect_outbox.effect_id
                LIMIT 1
                """,
                (transaction_now, transaction_now),
            ).fetchone()
            if row is None:
                return None
            current = self._record_from_row(row)
            if (
                current.attempt_count >= _MAX_SQLITE_INTEGER
                or _decode_sqlite_integer(
                    "claim_generation",
                    row["claim_generation"],
                )
                >= _MAX_SQLITE_INTEGER
                or _decode_sqlite_integer(
                    "claim_fencing_token",
                    row["claim_fencing_token"],
                )
                >= _MAX_SQLITE_INTEGER
            ):
                raise OverflowError(
                    "accepted-run SQLite outbox claim counters are exhausted"
                )
            next_attempt = current.attempt_count + 1
            next_generation = (
                _decode_sqlite_integer(
                    "claim_generation",
                    row["claim_generation"],
                )
                + 1
            )
            next_fence = (
                _decode_sqlite_integer(
                    "claim_fencing_token",
                    row["claim_fencing_token"],
                )
                + 1
            )
            if current.delivery_state is AcceptedRunEffectDeliveryState.PENDING:
                updated = connection.execute(
                    """
                    UPDATE effect_outbox
                    SET delivery_state = 'claimed',
                        attempt_count = ?,
                        claim_owner_id = ?,
                        claim_generation = ?,
                        claim_fencing_token = ?,
                        claim_started_at_unix_ms = ?,
                        claim_expires_at_unix_ms = ?,
                        delivered_at_unix_ms = NULL,
                        last_delivery_command_json = NULL,
                        last_delivery_command_digest = NULL
                    WHERE effect_id = ?
                      AND delivery_state = 'pending'
                      AND available_at_unix_ms = ?
                      AND available_at_unix_ms <= ?
                      AND claim_generation = ?
                      AND claim_fencing_token = ?
                    """,
                    (
                        next_attempt,
                        request.delivery_owner_id,
                        next_generation,
                        next_fence,
                        transaction_now,
                        lease_expires_at,
                        current.effect_id,
                        current.available_at_unix_ms,
                        transaction_now,
                        next_generation - 1,
                        next_fence - 1,
                    ),
                )
            elif current.delivery_state is AcceptedRunEffectDeliveryState.CLAIMED:
                assert current.claim is not None
                updated = connection.execute(
                    """
                    UPDATE effect_outbox
                    SET attempt_count = ?,
                        claim_owner_id = ?,
                        claim_generation = ?,
                        claim_fencing_token = ?,
                        claim_started_at_unix_ms = ?,
                        claim_expires_at_unix_ms = ?,
                        delivered_at_unix_ms = NULL,
                        last_delivery_command_json = NULL,
                        last_delivery_command_digest = NULL
                    WHERE effect_id = ?
                      AND delivery_state = 'claimed'
                      AND claim_owner_id = ?
                      AND claim_generation = ?
                      AND claim_fencing_token = ?
                      AND claim_started_at_unix_ms = ?
                      AND claim_expires_at_unix_ms = ?
                      AND claim_expires_at_unix_ms <= ?
                    """,
                    (
                        next_attempt,
                        request.delivery_owner_id,
                        next_generation,
                        next_fence,
                        transaction_now,
                        lease_expires_at,
                        current.effect_id,
                        current.claim.delivery_owner_id,
                        current.claim.claim_generation,
                        current.claim.fencing_token,
                        current.claim.claim_started_at_unix_ms,
                        current.claim.lease_expires_at_unix_ms,
                        transaction_now,
                    ),
                )
            else:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox selected an ineligible effect"
                )
            if updated.rowcount != 1:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox lost its claim candidate"
                )
            self._hit_failpoint("claim_next_effect.after_state_update")
            claimed_row = self._effect_row(connection, current.effect_id)
            if claimed_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox claim lost its effect"
                )
            claimed = self._record_from_row(claimed_row)
            assert claimed.claim is not None
            commit_now = self._transaction_now_unix_ms()
            if commit_now < transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox clock moved backwards during "
                    "claim acquisition"
                )
            if commit_now >= claimed.claim.lease_expires_at_unix_ms:
                raise AcceptedRunEffectDeliveryLeaseExpiredError(
                    claimed.claim,
                    "claim acquisition",
                )
            return claimed

        claimed = self._database._run_immediate(transition)
        if claimed is not None:
            self._hit_failpoint("claim_next_effect.after_commit")
        return claimed

    def mark_effect_delivered(
        self,
        command: AcceptedRunEffectDeliveryAck,
    ) -> AcceptedRunEffectDeliveryRecord:
        if not isinstance(command, AcceptedRunEffectDeliveryAck):
            raise TypeError(
                "accepted-run SQLite outbox acknowledgement must be an "
                "AcceptedRunEffectDeliveryAck"
            )
        if command.delivered_at_unix_ms > _MAX_SQLITE_INTEGER:
            raise ValueError(
                "accepted-run SQLite outbox delivery timestamp exceeds "
                "SQLite integer range"
            )
        command_json, command_digest = _effect_delivery_command_identity(command)

        def transition(
            connection: sqlite3.Connection,
        ) -> AcceptedRunEffectDeliveryRecord:
            claim = command.claim
            row = self._effect_row(connection, claim.effect_id)
            if row is None:
                raise AcceptedRunEffectNotFoundError(claim.effect_id)
            current = self._record_from_row(row)
            if current.delivery_state is AcceptedRunEffectDeliveryState.DELIVERED:
                self._assert_replay_command(row, command)
                if current.delivered_at_unix_ms != command.delivered_at_unix_ms:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite delivered outbox projection does "
                        "not match its replay command"
                    )
                return current
            if (
                current.delivery_state
                is AcceptedRunEffectDeliveryState.SATISFIED_BY_CALLBACK
            ):
                self._assert_replay_command(row, command)
                return current
            if current.delivery_state is not AcceptedRunEffectDeliveryState.CLAIMED:
                raise AcceptedRunEffectDeliveryStateConflictError(
                    claim.effect_id,
                    current.delivery_state,
                )
            assert_current_effect_delivery_claim(
                current=current.claim,
                provided=claim,
            )
            transaction_now = self._transaction_now_unix_ms()
            if transaction_now >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunEffectDeliveryLeaseExpiredError(
                    claim,
                    "delivery acknowledgement",
                )
            if command.delivered_at_unix_ms > transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox delivery timestamp must not "
                    "be later than the repository clock"
                )
            if command.delivered_at_unix_ms < claim.claim_started_at_unix_ms:
                raise ValueError(
                    "accepted-run SQLite outbox delivery timestamp must not "
                    "precede claim start"
                )
            updated = connection.execute(
                """
                UPDATE effect_outbox
                SET delivery_state = 'delivered',
                    claim_owner_id = NULL,
                    claim_started_at_unix_ms = NULL,
                    claim_expires_at_unix_ms = NULL,
                    delivered_at_unix_ms = ?,
                    last_delivery_command_json = ?,
                    last_delivery_command_digest = ?
                WHERE effect_id = ?
                  AND delivery_state = 'claimed'
                  AND claim_owner_id = ?
                  AND claim_generation = ?
                  AND claim_fencing_token = ?
                  AND claim_started_at_unix_ms = ?
                  AND claim_expires_at_unix_ms = ?
                  AND claim_expires_at_unix_ms > ?
                """,
                (
                    command.delivered_at_unix_ms,
                    command_json,
                    command_digest,
                    claim.effect_id,
                    claim.delivery_owner_id,
                    claim.claim_generation,
                    claim.fencing_token,
                    claim.claim_started_at_unix_ms,
                    claim.lease_expires_at_unix_ms,
                    transaction_now,
                ),
            )
            if updated.rowcount != 1:
                raise StaleAcceptedRunEffectDeliveryClaimError(
                    current.claim,
                    claim,
                )
            self._hit_failpoint("mark_effect_delivered.after_state_update")
            delivered_row = self._effect_row(connection, claim.effect_id)
            if delivered_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox acknowledgement lost its effect"
                )
            delivered = self._record_from_row(delivered_row)
            commit_now = self._transaction_now_unix_ms()
            if commit_now < transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox clock moved backwards during "
                    "delivery acknowledgement"
                )
            if commit_now >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunEffectDeliveryLeaseExpiredError(
                    claim,
                    "delivery acknowledgement",
                )
            return delivered

        delivered = self._database._run_immediate(transition)
        self._hit_failpoint("mark_effect_delivered.after_commit")
        return delivered

    def release_effect_for_retry(
        self,
        command: AcceptedRunEffectDeliveryRetry,
    ) -> AcceptedRunEffectDeliveryRecord:
        if not isinstance(command, AcceptedRunEffectDeliveryRetry):
            raise TypeError(
                "accepted-run SQLite outbox retry must be an "
                "AcceptedRunEffectDeliveryRetry"
            )
        if (
            command.released_at_unix_ms > _MAX_SQLITE_INTEGER
            or command.available_at_unix_ms > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "accepted-run SQLite outbox retry timestamp exceeds SQLite "
                "integer range"
            )
        command_json, command_digest = _effect_delivery_command_identity(command)

        def transition(
            connection: sqlite3.Connection,
        ) -> AcceptedRunEffectDeliveryRecord:
            claim = command.claim
            row = self._effect_row(connection, claim.effect_id)
            if row is None:
                raise AcceptedRunEffectNotFoundError(claim.effect_id)
            current = self._record_from_row(row)
            if current.delivery_state is AcceptedRunEffectDeliveryState.PENDING:
                self._assert_replay_command(row, command)
                if current.available_at_unix_ms != command.available_at_unix_ms:
                    raise SQLiteAcceptedRunCorruptionError(
                        "accepted-run SQLite pending outbox projection does "
                        "not match its replay command"
                    )
                return current
            if current.delivery_state in {
                AcceptedRunEffectDeliveryState.DELIVERED,
                AcceptedRunEffectDeliveryState.SATISFIED_BY_CALLBACK,
            }:
                self._assert_replay_command(row, command)
                return current
            if current.delivery_state is not AcceptedRunEffectDeliveryState.CLAIMED:
                raise AcceptedRunEffectDeliveryStateConflictError(
                    claim.effect_id,
                    current.delivery_state,
                )
            assert_current_effect_delivery_claim(
                current=current.claim,
                provided=claim,
            )
            transaction_now = self._transaction_now_unix_ms()
            if transaction_now >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunEffectDeliveryLeaseExpiredError(
                    claim,
                    "retry release",
                )
            if command.released_at_unix_ms > transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox retry timestamp must not be "
                    "later than the repository clock"
                )
            if command.released_at_unix_ms < claim.claim_started_at_unix_ms:
                raise ValueError(
                    "accepted-run SQLite outbox retry timestamp must not "
                    "precede claim start"
                )
            if command.available_at_unix_ms < transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox retry availability must not "
                    "precede the repository clock"
                )
            updated = connection.execute(
                """
                UPDATE effect_outbox
                SET delivery_state = 'pending',
                    available_at_unix_ms = ?,
                    claim_owner_id = NULL,
                    claim_started_at_unix_ms = NULL,
                    claim_expires_at_unix_ms = NULL,
                    delivered_at_unix_ms = NULL,
                    last_delivery_command_json = ?,
                    last_delivery_command_digest = ?
                WHERE effect_id = ?
                  AND delivery_state = 'claimed'
                  AND claim_owner_id = ?
                  AND claim_generation = ?
                  AND claim_fencing_token = ?
                  AND claim_started_at_unix_ms = ?
                  AND claim_expires_at_unix_ms = ?
                  AND claim_expires_at_unix_ms > ?
                """,
                (
                    command.available_at_unix_ms,
                    command_json,
                    command_digest,
                    claim.effect_id,
                    claim.delivery_owner_id,
                    claim.claim_generation,
                    claim.fencing_token,
                    claim.claim_started_at_unix_ms,
                    claim.lease_expires_at_unix_ms,
                    transaction_now,
                ),
            )
            if updated.rowcount != 1:
                raise StaleAcceptedRunEffectDeliveryClaimError(
                    current.claim,
                    claim,
                )
            self._hit_failpoint("release_effect_for_retry.after_state_update")
            pending_row = self._effect_row(connection, claim.effect_id)
            if pending_row is None:
                raise SQLiteAcceptedRunCorruptionError(
                    "accepted-run SQLite outbox retry lost its effect"
                )
            pending = self._record_from_row(pending_row)
            commit_now = self._transaction_now_unix_ms()
            if commit_now < transaction_now:
                raise ValueError(
                    "accepted-run SQLite outbox clock moved backwards during "
                    "retry release"
                )
            if commit_now >= claim.lease_expires_at_unix_ms:
                raise AcceptedRunEffectDeliveryLeaseExpiredError(
                    claim,
                    "retry release",
                )
            return pending

        pending = self._database._run_immediate(transition)
        self._hit_failpoint("release_effect_for_retry.after_commit")
        return pending
