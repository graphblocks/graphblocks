from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .provider_effects import (
    ProviderCapabilitySnapshot,
    ProviderEffectContractError,
    ProviderEffectIdentityConflictError,
    ProviderEffectIntent,
    ProviderEffectOriginTransfer,
    ProviderEffectState,
    ProviderRunAuthoritySnapshot,
    _matches_exact_closed_value,
    _revalidate_provider_capability_snapshot,
    _revalidate_provider_effect_intent,
    _validate_intent_capability_binding,
    _validate_intent_origin_transfer_binding,
)
from .server_storage import (
    AcceptedRunClaim,
    AcceptedRunLeaseExpiredError,
    AcceptedRunNotFoundError,
    StaleAcceptedRunClaimError,
    accepted_run_system_clock,
)
from .sqlite_server_storage import (
    _MAX_SQLITE_INTEGER,
    SQLiteAcceptedRunCorruptionError,
    SQLiteAcceptedRunDatabase,
)


PROVIDER_EFFECT_EVENT_FORMAT_VERSION = "graphblocks.provider-effect-event.v1"
MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE = 1_000


class SQLiteProviderEffectCorruptionError(SQLiteAcceptedRunCorruptionError):
    """Raised when persisted provider-effect identity is not exact or coherent."""


@dataclass(frozen=True, slots=True)
class StoredProviderEffect:
    tenant_id: str
    run_id: str
    owner_principal_id: str
    intent: ProviderEffectIntent
    capability: ProviderCapabilitySnapshot
    origin_transfer: ProviderEffectOriginTransfer
    state: ProviderEffectState
    state_version: int
    event_high_watermark: int
    created_at_unix_ms: int
    updated_at_unix_ms: int

    def __post_init__(self) -> None:
        owner = "stored provider effect"
        for field_name in ("tenant_id", "run_id", "owner_principal_id"):
            value = getattr(self, field_name)
            if type(value) is not str or not value or value != value.strip():
                raise ProviderEffectContractError(
                    f"{owner} {field_name} must be an exact non-empty string"
                )
        if type(self.intent) is not ProviderEffectIntent:
            raise ProviderEffectContractError(
                f"{owner} intent must be ProviderEffectIntent"
            )
        if type(self.capability) is not ProviderCapabilitySnapshot:
            raise ProviderEffectContractError(
                f"{owner} capability must be ProviderCapabilitySnapshot"
            )
        if type(self.origin_transfer) is not ProviderEffectOriginTransfer:
            raise ProviderEffectContractError(
                f"{owner} origin_transfer must be ProviderEffectOriginTransfer"
            )
        if type(self.state) is not ProviderEffectState:
            raise ProviderEffectContractError(
                f"{owner} state must be ProviderEffectState"
            )
        for field_name in (
            "state_version",
            "event_high_watermark",
            "created_at_unix_ms",
            "updated_at_unix_ms",
        ):
            value = getattr(self, field_name)
            if (
                type(value) is not int
                or value < (1 if field_name.endswith("watermark") else 0)
                or value > _MAX_SQLITE_INTEGER
            ):
                raise ProviderEffectContractError(
                    f"{owner} {field_name} must fit a non-negative SQLite integer"
                )
        if self.state_version < 1:
            raise ProviderEffectContractError(f"{owner} state_version must be positive")
        if self.state_version != self.event_high_watermark:
            raise ProviderEffectContractError(
                f"{owner} state version must match its event high watermark"
            )
        if self.updated_at_unix_ms < self.created_at_unix_ms:
            raise ProviderEffectContractError(
                f"{owner} update time must not predate creation"
            )
        if (
            self.intent.tenant_id != self.tenant_id
            or self.intent.run_id != self.run_id
            or self.intent.owner_principal_id != self.owner_principal_id
            or self.origin_transfer.tenant_id != self.tenant_id
            or self.origin_transfer.run_id != self.run_id
            or self.origin_transfer.owner_principal_id != self.owner_principal_id
            or self.origin_transfer.effect_id != self.intent.effect_id
            or self.origin_transfer.intent_digest != self.intent.digest
            or self.intent.capability_snapshot_digest != self.capability.digest
            or self.created_at_unix_ms != self.intent.created_at_unix_ms
        ):
            raise ProviderEffectContractError(
                f"{owner} immutable identities do not match"
            )
        _validate_intent_capability_binding(self.intent, self.capability)
        _validate_intent_origin_transfer_binding(
            self.intent,
            self.origin_transfer,
        )


@dataclass(frozen=True, slots=True)
class StoredProviderEffectEvent:
    effect_id: str
    sequence: int
    kind: str
    from_state: ProviderEffectState | None
    to_state: ProviderEffectState
    payload_json: str
    payload_digest: str
    created_at_unix_ms: int

    def __post_init__(self) -> None:
        owner = "stored provider effect event"
        for field_name in ("effect_id", "kind"):
            value = getattr(self, field_name)
            if type(value) is not str or not value or value != value.strip():
                raise ProviderEffectContractError(
                    f"{owner} {field_name} must be an exact non-empty string"
                )
        if (
            type(self.sequence) is not int
            or self.sequence < 1
            or self.sequence > _MAX_SQLITE_INTEGER
        ):
            raise ProviderEffectContractError(
                f"{owner} sequence must be a positive SQLite integer"
            )
        if (
            self.from_state is not None
            and type(self.from_state) is not ProviderEffectState
        ):
            raise ProviderEffectContractError(
                f"{owner} from_state must be ProviderEffectState or None"
            )
        if type(self.to_state) is not ProviderEffectState:
            raise ProviderEffectContractError(
                f"{owner} to_state must be ProviderEffectState"
            )
        if type(self.payload_json) is not str or type(self.payload_digest) is not str:
            raise ProviderEffectContractError(
                f"{owner} payload identity must be exact text"
            )
        try:
            payload = canonical_loads(self.payload_json)
        except (TypeError, ValueError) as error:
            raise ProviderEffectContractError(
                f"{owner} payload must be canonical JSON"
            ) from error
        if (
            type(payload) is not dict
            or canonical_dumps(payload) != self.payload_json
            or canonical_hash(payload) != self.payload_digest
        ):
            raise ProviderEffectContractError(
                f"{owner} payload identity must be canonical and exact"
            )
        if (
            self.kind != "origin_transferred"
            or self.from_state is not None
            or self.to_state is not ProviderEffectState.PENDING
            or set(payload)
            != {
                "capabilitySnapshotDigest",
                "effectId",
                "formatVersion",
                "intentDigest",
                "originTransferDigest",
                "state",
            }
            or payload["effectId"] != self.effect_id
            or payload["formatVersion"] != PROVIDER_EFFECT_EVENT_FORMAT_VERSION
            or payload["state"] != ProviderEffectState.PENDING.value
        ):
            raise ProviderEffectContractError(
                f"{owner} origin-transfer event is not closed and exact"
            )
        if (
            type(self.created_at_unix_ms) is not int
            or self.created_at_unix_ms < 0
            or self.created_at_unix_ms > _MAX_SQLITE_INTEGER
        ):
            raise ProviderEffectContractError(
                f"{owner} creation time must fit a non-negative SQLite integer"
            )


@dataclass(frozen=True, slots=True)
class StoredProviderEffectEventPage:
    events: tuple[StoredProviderEffectEvent, ...]
    next_after_sequence: int | None

    def __post_init__(self) -> None:
        if type(self.events) is not tuple or any(
            type(event) is not StoredProviderEffectEvent for event in self.events
        ):
            raise ProviderEffectContractError(
                "stored provider effect event page must contain exact events"
            )
        if self.next_after_sequence is not None and (
            type(self.next_after_sequence) is not int
            or self.next_after_sequence < 1
            or not self.events
            or self.next_after_sequence != self.events[-1].sequence
        ):
            raise ProviderEffectContractError(
                "stored provider effect next sequence must match the page tail"
            )


class SQLiteProviderEffectRepository:
    """Durably transfers live accepted-run authority to provider effects."""

    def __init__(
        self,
        path: str | Path,
        *,
        origin_authority_digest: str,
        busy_timeout_ms: int = 5_000,
        failpoint: Callable[[str], None] | None = None,
        clock: Callable[[], int] = accepted_run_system_clock,
    ) -> None:
        if (
            type(origin_authority_digest) is not str
            or len(origin_authority_digest) != 71
            or not origin_authority_digest.startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in origin_authority_digest[7:]
            )
        ):
            raise ValueError(
                "provider-effect SQLite origin_authority_digest must be a canonical "
                "sha256 digest"
            )
        if failpoint is not None and not callable(failpoint):
            raise TypeError("provider-effect SQLite failpoint must be callable")
        if not callable(clock):
            raise TypeError("provider-effect SQLite clock must be callable")
        self.authority_digest = origin_authority_digest
        self._database = SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._failpoint = failpoint
        self._clock = clock

    def _hit_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    def _record_from_row(self, row: sqlite3.Row) -> StoredProviderEffect:
        try:
            intent_json = row["intent_json"]
            intent_digest = row["intent_digest"]
            capability_json = row["capability_snapshot_json"]
            capability_digest = row["capability_snapshot_digest"]
            transfer_json = row["origin_transfer_json"]
            transfer_digest = row["origin_transfer_digest"]
            if any(
                type(value) is not str
                for value in (
                    intent_json,
                    intent_digest,
                    capability_json,
                    capability_digest,
                    transfer_json,
                    transfer_digest,
                )
            ):
                raise ValueError("provider-effect record identity is not text")
            intent_wire = canonical_loads(intent_json)
            capability_wire = canonical_loads(capability_json)
            transfer_wire = canonical_loads(transfer_json)
            if (
                canonical_dumps(intent_wire) != intent_json
                or canonical_dumps(capability_wire) != capability_json
                or canonical_dumps(transfer_wire) != transfer_json
            ):
                raise ValueError("provider-effect record JSON is not canonical")
            intent = ProviderEffectIntent.from_wire(intent_wire)
            capability = ProviderCapabilitySnapshot.from_wire(capability_wire)
            origin_transfer = ProviderEffectOriginTransfer.from_wire(transfer_wire)
            if (
                intent.digest != intent_digest
                or capability.digest != capability_digest
                or origin_transfer.digest != transfer_digest
            ):
                raise ValueError("provider-effect record digest does not match JSON")
            for field_name in (
                "tenant_id",
                "external_run_id",
                "owner_principal_id",
                "effect_id",
                "idempotency_key",
                "provider_target",
                "provider_operation",
                "state",
            ):
                if type(row[field_name]) is not str:
                    raise ValueError(f"provider-effect {field_name} is not text")
            for field_name in (
                "state_version",
                "event_high_watermark",
                "created_at_unix_ms",
                "updated_at_unix_ms",
            ):
                if type(row[field_name]) is not int:
                    raise ValueError(f"provider-effect {field_name} is not an integer")
            if (
                row["effect_id"] != intent.effect_id
                or row["idempotency_key"] != intent.idempotency_key
                or row["provider_target"] != intent.provider_target
                or row["provider_operation"] != intent.provider_operation
            ):
                raise ValueError(
                    "provider-effect indexed identity does not match intent"
                )
            return StoredProviderEffect(
                tenant_id=row["tenant_id"],
                run_id=row["external_run_id"],
                owner_principal_id=row["owner_principal_id"],
                intent=intent,
                capability=capability,
                origin_transfer=origin_transfer,
                state=ProviderEffectState(row["state"]),
                state_version=row["state_version"],
                event_high_watermark=row["event_high_watermark"],
                created_at_unix_ms=row["created_at_unix_ms"],
                updated_at_unix_ms=row["updated_at_unix_ms"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite record is invalid"
            ) from error

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> StoredProviderEffectEvent:
        try:
            for field_name in (
                "effect_id",
                "kind",
                "to_state",
                "payload_json",
                "payload_digest",
            ):
                if type(row[field_name]) is not str:
                    raise ValueError(f"provider-effect event {field_name} is not text")
            for field_name in ("sequence", "created_at_unix_ms"):
                if type(row[field_name]) is not int:
                    raise ValueError(
                        f"provider-effect event {field_name} is not an integer"
                    )
            if row["from_state"] is not None and type(row["from_state"]) is not str:
                raise ValueError("provider-effect from_state is not text")
            return StoredProviderEffectEvent(
                effect_id=row["effect_id"],
                sequence=row["sequence"],
                kind=row["kind"],
                from_state=(
                    None
                    if row["from_state"] is None
                    else ProviderEffectState(row["from_state"])
                ),
                to_state=ProviderEffectState(row["to_state"]),
                payload_json=row["payload_json"],
                payload_digest=row["payload_digest"],
                created_at_unix_ms=row["created_at_unix_ms"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event is invalid"
            ) from error

    def _assert_event_chain(
        self,
        connection: sqlite3.Connection,
        *,
        run_internal_id: str,
        record: StoredProviderEffect,
    ) -> None:
        aggregate = connection.execute(
            """
            SELECT count(*) AS event_count,
                   min(sequence) AS minimum_sequence,
                   max(sequence) AS maximum_sequence
            FROM provider_effect_events
            WHERE run_internal_id = ? AND effect_id = ?
            """,
            (run_internal_id, record.intent.effect_id),
        ).fetchone()
        if (
            aggregate is None
            or type(aggregate["event_count"]) is not int
            or aggregate["event_count"] != record.event_high_watermark
            or aggregate["minimum_sequence"] != 1
            or aggregate["maximum_sequence"] != record.event_high_watermark
        ):
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event chain is not contiguous"
            )
        last_row = connection.execute(
            """
            SELECT *
            FROM provider_effect_events
            WHERE run_internal_id = ?
              AND effect_id = ?
              AND sequence = ?
            """,
            (
                run_internal_id,
                record.intent.effect_id,
                record.event_high_watermark,
            ),
        ).fetchone()
        if last_row is None:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event chain has no authoritative tail"
            )
        last_event = self._event_from_row(last_row)
        payload = canonical_loads(last_event.payload_json)
        if (
            last_event.to_state is not record.state
            or last_event.created_at_unix_ms > record.updated_at_unix_ms
            or type(payload) is not dict
            or payload["intentDigest"] != record.intent.digest
            or payload["capabilitySnapshotDigest"] != record.capability.digest
            or payload["originTransferDigest"] != record.origin_transfer.digest
        ):
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event tail does not match its projection"
            )

    def persist_transferred_effect(
        self,
        *,
        claim: AcceptedRunClaim,
        intent: ProviderEffectIntent,
        capability: ProviderCapabilitySnapshot,
    ) -> StoredProviderEffect:
        """Persist one exact intent and authority transfer under the live run lease."""

        if type(claim) is not AcceptedRunClaim:
            raise TypeError("provider-effect origin claim must be AcceptedRunClaim")
        try:
            claim = AcceptedRunClaim(
                tenant_id=claim.tenant_id,
                run_id=claim.run_id,
                lease_owner_id=claim.lease_owner_id,
                lease_generation=claim.lease_generation,
                fencing_token=claim.fencing_token,
                lease_expires_at_unix_ms=claim.lease_expires_at_unix_ms,
            )
            intent = _revalidate_provider_effect_intent(intent)
            capability = _revalidate_provider_capability_snapshot(capability)
            _validate_intent_capability_binding(intent, capability)
        except (TypeError, ValueError) as error:
            raise ProviderEffectContractError(
                "provider-effect SQLite origin input is invalid"
            ) from error
        if intent.tenant_id != claim.tenant_id or intent.run_id != claim.run_id:
            raise ProviderEffectContractError(
                "provider-effect intent does not target the claimed run"
            )

        def transition(connection: sqlite3.Connection) -> StoredProviderEffect:
            run = connection.execute(
                """
                SELECT *
                FROM accepted_runs
                WHERE tenant_id = ? AND external_run_id = ?
                """,
                (intent.tenant_id, intent.run_id),
            ).fetchone()
            if run is None:
                raise AcceptedRunNotFoundError(intent.tenant_id, intent.run_id)
            existing = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effects
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE provider_effects.run_internal_id = ?
                  AND provider_effects.effect_id = ?
                """,
                (run["internal_id"], intent.effect_id),
            ).fetchone()
            if existing is not None:
                stored = self._record_from_row(existing)
                if type(existing["run_internal_id"]) is not str:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite run identity is not text"
                    )
                self._assert_event_chain(
                    connection,
                    run_internal_id=existing["run_internal_id"],
                    record=stored,
                )
                if stored.intent != intent or stored.capability != capability:
                    raise ProviderEffectIdentityConflictError(
                        "provider-effect replay changed immutable intent or capability"
                    )
                if (
                    stored.origin_transfer.repository_authority_digest
                    != self.authority_digest
                ):
                    raise ProviderEffectContractError(
                        "provider-effect replay uses another origin authority"
                    )
                return stored
            conflicting_identity = connection.execute(
                """
                SELECT effect_id
                FROM provider_effects
                WHERE run_internal_id = ?
                  AND provider_target = ?
                  AND provider_operation = ?
                  AND idempotency_key = ?
                """,
                (
                    run["internal_id"],
                    intent.provider_target,
                    intent.provider_operation,
                    intent.idempotency_key,
                ),
            ).fetchone()
            if conflicting_identity is not None:
                raise ProviderEffectIdentityConflictError(
                    "provider-effect idempotency identity belongs to another effect"
                )
            transaction_now = self._clock()
            if (
                type(transaction_now) is not int
                or transaction_now < 0
                or transaction_now > _MAX_SQLITE_INTEGER
            ):
                raise ValueError(
                    "provider-effect SQLite clock must return a non-negative SQLite "
                    "integer"
                )
            if intent.created_at_unix_ms > transaction_now:
                raise ProviderEffectContractError(
                    "provider-effect intent creation time is in the future"
                )
            try:
                if type(run["phase"]) is not str or run["phase"] != "running":
                    current_claim = None
                else:
                    current_claim = AcceptedRunClaim(
                        tenant_id=run["tenant_id"],
                        run_id=run["external_run_id"],
                        lease_owner_id=run["lease_owner_id"],
                        lease_generation=run["lease_generation"],
                        fencing_token=run["fencing_token"],
                        lease_expires_at_unix_ms=run["lease_expires_at_unix_ms"],
                    )
            except (TypeError, ValueError) as error:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run claim is invalid"
                ) from error
            if current_claim != claim:
                raise StaleAcceptedRunClaimError(current_claim, claim)
            if transaction_now >= current_claim.lease_expires_at_unix_ms:
                raise AcceptedRunLeaseExpiredError(
                    current_claim,
                    "provider effect origin transfer",
                )
            try:
                run_state_version = run["state_version"]
                run_updated_at = run["updated_at_unix_ms"]
                if (
                    type(run_state_version) is not int
                    or run_state_version < 0
                    or type(run_updated_at) is not int
                    or run_updated_at < 0
                    or type(run["owner_principal_id"]) is not str
                    or type(run["internal_id"]) is not str
                    or (
                        run["current_checkpoint_digest"] is not None
                        and type(run["current_checkpoint_digest"]) is not str
                    )
                ):
                    raise ValueError("run authority fields have invalid SQLite types")
                if not (
                    run_updated_at
                    <= intent.created_at_unix_ms
                    < current_claim.lease_expires_at_unix_ms
                ):
                    raise ProviderEffectContractError(
                        "provider-effect intent was not created during the active run lease"
                    )
                run_authority = ProviderRunAuthoritySnapshot(
                    tenant_id=current_claim.tenant_id,
                    run_id=current_claim.run_id,
                    owner_principal_id=run["owner_principal_id"],
                    run_state_version=run_state_version,
                    lease_generation=current_claim.lease_generation,
                    fencing_token=current_claim.fencing_token,
                    checkpoint_digest=run["current_checkpoint_digest"],
                )
                origin_transfer = (
                    ProviderEffectOriginTransfer.from_intent_and_run_authority(
                        intent=intent,
                        run_authority=run_authority,
                        repository_authority_digest=self.authority_digest,
                    )
                )
            except (TypeError, ValueError) as error:
                if isinstance(error, ProviderEffectContractError):
                    raise
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run authority is invalid"
                ) from error
            intent_json = canonical_dumps(intent.to_wire())
            capability_json = canonical_dumps(capability.to_wire())
            transfer_json = canonical_dumps(origin_transfer.to_wire())
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, 1, ?, ?)
                """,
                (
                    run["internal_id"],
                    intent.effect_id,
                    intent.idempotency_key,
                    intent.provider_target,
                    intent.provider_operation,
                    intent_json,
                    intent.digest,
                    capability_json,
                    capability.digest,
                    transfer_json,
                    origin_transfer.digest,
                    intent.created_at_unix_ms,
                    transaction_now,
                ),
            )
            self._hit_failpoint("persist_transferred_effect.after_effect_insert")
            event_payload = {
                "capabilitySnapshotDigest": capability.digest,
                "effectId": intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": intent.digest,
                "originTransferDigest": origin_transfer.digest,
                "state": ProviderEffectState.PENDING.value,
            }
            event_json = canonical_dumps(event_payload)
            connection.execute(
                """
                INSERT INTO provider_effect_events (
                  run_internal_id,
                  effect_id,
                  sequence,
                  kind,
                  from_state,
                  to_state,
                  payload_json,
                  payload_digest,
                  created_at_unix_ms
                )
                VALUES (?, ?, 1, 'origin_transferred', NULL, 'pending', ?, ?, ?)
                """,
                (
                    run["internal_id"],
                    intent.effect_id,
                    event_json,
                    canonical_hash(event_payload),
                    transaction_now,
                ),
            )
            self._hit_failpoint("persist_transferred_effect.after_event_insert")
            commit_now = self._clock()
            if (
                type(commit_now) is not int
                or commit_now < transaction_now
                or commit_now > _MAX_SQLITE_INTEGER
            ):
                raise ValueError(
                    "provider-effect SQLite clock must remain monotonic within the "
                    "transaction"
                )
            if commit_now >= current_claim.lease_expires_at_unix_ms:
                raise AcceptedRunLeaseExpiredError(
                    current_claim,
                    "provider effect origin transfer commit",
                )
            return StoredProviderEffect(
                tenant_id=intent.tenant_id,
                run_id=intent.run_id,
                owner_principal_id=intent.owner_principal_id,
                intent=intent,
                capability=capability,
                origin_transfer=origin_transfer,
                state=ProviderEffectState.PENDING,
                state_version=1,
                event_high_watermark=1,
                created_at_unix_ms=intent.created_at_unix_ms,
                updated_at_unix_ms=transaction_now,
            )

        stored = self._database._run_immediate(transition)
        self._hit_failpoint("persist_transferred_effect.after_commit")
        return stored

    def get_effect(
        self,
        *,
        tenant_id: str,
        run_id: str,
        owner_principal_id: str,
        effect_id: str,
    ) -> StoredProviderEffect | None:
        for field_name, value in (
            ("tenant_id", tenant_id),
            ("run_id", run_id),
            ("owner_principal_id", owner_principal_id),
            ("effect_id", effect_id),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(
                    f"provider-effect SQLite lookup {field_name} must be exact text"
                )

        def read(connection: sqlite3.Connection) -> StoredProviderEffect | None:
            row = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effects
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE accepted_runs.tenant_id = ?
                  AND accepted_runs.external_run_id = ?
                  AND accepted_runs.owner_principal_id = ?
                  AND provider_effects.effect_id = ?
                """,
                (tenant_id, run_id, owner_principal_id, effect_id),
            ).fetchone()
            if row is None:
                return None
            record = self._record_from_row(row)
            if type(row["run_internal_id"]) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_event_chain(
                connection,
                run_internal_id=row["run_internal_id"],
                record=record,
            )
            return record

        return self._database._run_read(read)

    def verify_transferred_origin(
        self,
        *,
        intent: ProviderEffectIntent,
        origin_transfer: ProviderEffectOriginTransfer,
        admitted_at_unix_ms: int,
    ) -> bool:
        if (
            type(intent) is not ProviderEffectIntent
            or type(origin_transfer) is not ProviderEffectOriginTransfer
            or type(admitted_at_unix_ms) is not int
            or admitted_at_unix_ms < 0
            or admitted_at_unix_ms > _MAX_SQLITE_INTEGER
        ):
            return False
        try:
            intent = _revalidate_provider_effect_intent(intent)
            decoded_transfer = ProviderEffectOriginTransfer.from_wire(
                origin_transfer.to_wire()
            )
            if not _matches_exact_closed_value(origin_transfer, decoded_transfer):
                return False
            origin_transfer = decoded_transfer
        except (TypeError, ValueError):
            return False
        stored = self.get_effect(
            tenant_id=intent.tenant_id,
            run_id=intent.run_id,
            owner_principal_id=intent.owner_principal_id,
            effect_id=intent.effect_id,
        )
        return bool(
            stored is not None
            and stored.intent == intent
            and stored.intent.digest == intent.digest
            and stored.origin_transfer == origin_transfer
            and stored.origin_transfer.digest == origin_transfer.digest
            and origin_transfer.repository_authority_digest == self.authority_digest
            and admitted_at_unix_ms >= origin_transfer.transferred_at_unix_ms
        )

    def read_events(
        self,
        *,
        tenant_id: str,
        run_id: str,
        owner_principal_id: str,
        effect_id: str,
        after_sequence: int,
        limit: int,
    ) -> StoredProviderEffectEventPage:
        for field_name, value in (
            ("tenant_id", tenant_id),
            ("run_id", run_id),
            ("owner_principal_id", owner_principal_id),
            ("effect_id", effect_id),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(
                    f"provider-effect SQLite event {field_name} must be exact text"
                )
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or after_sequence > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "provider-effect SQLite after_sequence must be non-negative"
            )
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE
        ):
            raise ValueError(
                "provider-effect SQLite event limit must be between 1 and "
                f"{MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE}"
            )

        def read(connection: sqlite3.Connection) -> StoredProviderEffectEventPage:
            effect_row = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effects
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE accepted_runs.tenant_id = ?
                  AND accepted_runs.external_run_id = ?
                  AND accepted_runs.owner_principal_id = ?
                  AND provider_effects.effect_id = ?
                """,
                (tenant_id, run_id, owner_principal_id, effect_id),
            ).fetchone()
            if effect_row is None:
                return StoredProviderEffectEventPage(
                    events=(),
                    next_after_sequence=None,
                )
            record = self._record_from_row(effect_row)
            if type(effect_row["run_internal_id"]) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_event_chain(
                connection,
                run_internal_id=effect_row["run_internal_id"],
                record=record,
            )
            rows = connection.execute(
                """
                SELECT provider_effect_events.*
                FROM provider_effect_events
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effect_events.run_internal_id
                WHERE accepted_runs.tenant_id = ?
                  AND accepted_runs.external_run_id = ?
                  AND accepted_runs.owner_principal_id = ?
                  AND provider_effect_events.effect_id = ?
                  AND provider_effect_events.sequence > ?
                ORDER BY provider_effect_events.sequence
                LIMIT ?
                """,
                (
                    tenant_id,
                    run_id,
                    owner_principal_id,
                    effect_id,
                    after_sequence,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            event_tuple = tuple(self._event_from_row(row) for row in rows[:limit])
            return StoredProviderEffectEventPage(
                events=event_tuple,
                next_after_sequence=(
                    event_tuple[-1].sequence if has_more and event_tuple else None
                ),
            )

        return self._database._run_read(read)


__all__ = [
    "MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE",
    "PROVIDER_EFFECT_EVENT_FORMAT_VERSION",
    "SQLiteProviderEffectCorruptionError",
    "SQLiteProviderEffectRepository",
    "StoredProviderEffect",
    "StoredProviderEffectEvent",
    "StoredProviderEffectEventPage",
]
