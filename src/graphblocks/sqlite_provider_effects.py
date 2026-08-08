from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import secrets
import sqlite3

from ._canonical_reference import canonical_dumps, canonical_hash, canonical_loads
from .provider_effects import (
    ProviderCancellation,
    ProviderCapabilitySnapshot,
    ProviderEffectAdmission,
    ProviderEffectAdmissionReceipt,
    ProviderEffectContractError,
    ProviderEffectIdentityConflictError,
    ProviderEffectIntent,
    ProviderEffectOriginTransfer,
    ProviderEffectSendAttempt,
    ProviderEffectState,
    ProviderEffectStateConflictError,
    ProviderEffectTransition,
    ProviderReconciliationEvidence,
    ProviderReconciliationMethod,
    ProviderReconciliationOutcome,
    ProviderRunAuthoritySnapshot,
    ProviderStatusLookup,
    _applicable_reconciliation_methods,
    _matches_exact_closed_value,
    _revalidate_provider_capability_snapshot,
    _revalidate_provider_effect_admission_receipt,
    _revalidate_provider_effect_intent,
    _revalidate_provider_effect_send_attempt,
    _validate_intent_capability_binding,
    _validate_intent_origin_transfer_binding,
    retry_same_provider_effect_intent,
    transition_provider_effect_state,
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
PROVIDER_EFFECT_CLAIM_FORMAT_VERSION = "graphblocks.provider-effect-claim.v1"
PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION = (
    "graphblocks.provider-effect-claim-release.v1"
)
PROVIDER_EFFECT_RECONCILIATION_CONTROL_FORMAT_VERSION = (
    "graphblocks.provider-effect-reconciliation-control.v1"
)
PROVIDER_EFFECT_RETRY_COMMAND_FORMAT_VERSION = (
    "graphblocks.provider-effect-retry-command.v1"
)
MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE = 1_000
MAX_PROVIDER_EFFECT_CLAIM_LEASE_DURATION_MS = 60_000
_ACTIVE_PROVIDER_EFFECT_SEND_STATES = frozenset(
    {
        ProviderEffectState.SEND_STARTED,
        ProviderEffectState.QUARANTINED_UNKNOWN,
        ProviderEffectState.RECONCILING,
        ProviderEffectState.MANUAL_REVIEW_UNKNOWN,
    }
)


def _reconciliation_settlement_state(
    current: ProviderEffectState,
    outcome: ProviderReconciliationOutcome,
) -> ProviderEffectState | None:
    if current not in {
        ProviderEffectState.SEND_STARTED,
        ProviderEffectState.RECONCILING,
    }:
        return None
    return {
        ProviderReconciliationOutcome.COMMITTED: (
            ProviderEffectState.CONFIRMED_COMMITTED
        ),
        ProviderReconciliationOutcome.NOT_COMMITTED: (
            ProviderEffectState.CONFIRMED_NOT_COMMITTED
        ),
        ProviderReconciliationOutcome.CANCELLED_CONFIRMED: (
            ProviderEffectState.CONFIRMED_CANCELLED
        ),
        ProviderReconciliationOutcome.UNKNOWN: (
            ProviderEffectState.QUARANTINED_UNKNOWN
        ),
    }[outcome]


_PROVIDER_EFFECT_CLAIM_FIELDS = frozenset(
    {
        "admittedAtUnixMs",
        "claimAuthorityDigest",
        "claimExpiresAtUnixMs",
        "claimFencingToken",
        "claimGeneration",
        "claimOwnerId",
        "claimStartedAtUnixMs",
        "effectId",
        "formatVersion",
        "intentDigest",
        "ownerPrincipalId",
        "previousSendAttemptDigest",
        "runId",
        "sendAttemptId",
        "tenantId",
    }
)
_PROVIDER_EFFECT_CLAIM_RELEASE_FIELDS = frozenset(
    {
        "claimDigest",
        "claimFencingToken",
        "claimGeneration",
        "effectId",
        "formatVersion",
        "ownerPrincipalId",
        "releasedAtUnixMs",
        "resultingEventSequence",
        "resultingStateVersion",
        "runId",
        "tenantId",
    }
)
_PROVIDER_EFFECT_RECONCILIATION_CONTROL_FIELDS = frozenset(
    {
        "controlId",
        "effectId",
        "expectedStateVersion",
        "formatVersion",
        "ownerPrincipalId",
        "runId",
        "tenantId",
        "transition",
    }
)
_PROVIDER_EFFECT_RETRY_COMMAND_FIELDS = frozenset(
    {
        "effectId",
        "expectedStateVersion",
        "formatVersion",
        "intentDigest",
        "ownerPrincipalId",
        "retryId",
        "runId",
        "tenantId",
    }
)


class SQLiteProviderEffectCorruptionError(SQLiteAcceptedRunCorruptionError):
    """Raised when persisted provider-effect identity is not exact or coherent."""


class StaleProviderEffectClaimError(ProviderEffectStateConflictError):
    """Raised when a pre-send claim is no longer repository-authoritative."""


def _require_exact_string(owner: str, field_name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be an exact non-empty string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProviderEffectContractError(
            f"{owner} {field_name} must contain Unicode scalar values"
        ) from error
    return value


def _require_digest(owner: str, field_name: str, value: object) -> str:
    digest = _require_exact_string(owner, field_name, value)
    if (
        len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be a canonical sha256 digest"
        )
    return digest


def _require_optional_digest(
    owner: str,
    field_name: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    return _require_digest(owner, field_name, value)


def _require_sqlite_integer(
    owner: str,
    field_name: str,
    value: object,
    *,
    positive: bool = False,
) -> int:
    if (
        type(value) is not int
        or value < (1 if positive else 0)
        or value > _MAX_SQLITE_INTEGER
    ):
        qualifier = "positive " if positive else "non-negative "
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be a {qualifier}SQLite integer"
        )
    return value


def _new_provider_send_attempt_id() -> str:
    return f"provider-send-{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class ProviderEffectClaimRequest:
    tenant_id: str
    owner_principal_id: str
    claim_owner_id: str
    lease_duration_ms: int

    def __post_init__(self) -> None:
        owner = "provider effect claim request"
        for field_name in ("tenant_id", "owner_principal_id", "claim_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _require_exact_string(owner, field_name, getattr(self, field_name)),
            )
        lease_duration_ms = _require_sqlite_integer(
            owner,
            "lease_duration_ms",
            self.lease_duration_ms,
            positive=True,
        )
        if lease_duration_ms > MAX_PROVIDER_EFFECT_CLAIM_LEASE_DURATION_MS:
            raise ProviderEffectContractError(
                "provider effect claim lease exceeds the repository policy maximum"
            )
        object.__setattr__(self, "lease_duration_ms", lease_duration_ms)


@dataclass(frozen=True, slots=True)
class ProviderEffectReconciliationControl:
    tenant_id: str
    run_id: str
    owner_principal_id: str
    effect_id: str
    control_id: str
    transition: ProviderEffectTransition
    expected_state_version: int
    format_version: str = PROVIDER_EFFECT_RECONCILIATION_CONTROL_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect reconciliation control"
        for field_name in (
            "tenant_id",
            "run_id",
            "owner_principal_id",
            "effect_id",
            "control_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_exact_string(owner, field_name, getattr(self, field_name)),
            )
        if type(
            self.transition
        ) is not ProviderEffectTransition or self.transition not in {
            ProviderEffectTransition.BEGIN_RECONCILIATION,
            ProviderEffectTransition.ESCALATE_MANUAL_REVIEW,
            ProviderEffectTransition.RESUME_RECONCILIATION,
        }:
            raise ProviderEffectContractError(
                f"{owner} transition is not an operator control transition"
            )
        object.__setattr__(
            self,
            "expected_state_version",
            _require_sqlite_integer(
                owner,
                "expected_state_version",
                self.expected_state_version,
                positive=True,
            ),
        )
        format_version = _require_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        if format_version != PROVIDER_EFFECT_RECONCILIATION_CONTROL_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )
        object.__setattr__(self, "format_version", format_version)

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "controlId": self.control_id,
            "effectId": self.effect_id,
            "expectedStateVersion": self.expected_state_version,
            "formatVersion": self.format_version,
            "ownerPrincipalId": self.owner_principal_id,
            "runId": self.run_id,
            "tenantId": self.tenant_id,
            "transition": self.transition.value,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectReconciliationControl:
        owner = "provider effect reconciliation control"
        if (
            type(value) is not dict
            or set(value) != _PROVIDER_EFFECT_RECONCILIATION_CONTROL_FIELDS
        ):
            raise ProviderEffectContractError(f"{owner} must contain the closed fields")
        try:
            transition = ProviderEffectTransition(
                _require_exact_string(owner, "transition", value["transition"])
            )
        except ValueError as error:
            raise ProviderEffectContractError(
                f"{owner} transition is not supported"
            ) from error
        return cls(
            tenant_id=_require_exact_string(owner, "tenantId", value["tenantId"]),
            run_id=_require_exact_string(owner, "runId", value["runId"]),
            owner_principal_id=_require_exact_string(
                owner,
                "ownerPrincipalId",
                value["ownerPrincipalId"],
            ),
            effect_id=_require_exact_string(owner, "effectId", value["effectId"]),
            control_id=_require_exact_string(
                owner,
                "controlId",
                value["controlId"],
            ),
            transition=transition,
            expected_state_version=_require_sqlite_integer(
                owner,
                "expectedStateVersion",
                value["expectedStateVersion"],
                positive=True,
            ),
            format_version=_require_exact_string(
                owner,
                "formatVersion",
                value["formatVersion"],
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderEffectRetryCommand:
    tenant_id: str
    run_id: str
    owner_principal_id: str
    effect_id: str
    retry_id: str
    intent_digest: str
    expected_state_version: int
    format_version: str = PROVIDER_EFFECT_RETRY_COMMAND_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect retry command"
        for field_name in (
            "tenant_id",
            "run_id",
            "owner_principal_id",
            "effect_id",
            "retry_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "intent_digest",
            _require_digest(owner, "intent_digest", self.intent_digest),
        )
        object.__setattr__(
            self,
            "expected_state_version",
            _require_sqlite_integer(
                owner,
                "expected_state_version",
                self.expected_state_version,
                positive=True,
            ),
        )
        format_version = _require_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        if format_version != PROVIDER_EFFECT_RETRY_COMMAND_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )
        object.__setattr__(self, "format_version", format_version)

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "effectId": self.effect_id,
            "expectedStateVersion": self.expected_state_version,
            "formatVersion": self.format_version,
            "intentDigest": self.intent_digest,
            "ownerPrincipalId": self.owner_principal_id,
            "retryId": self.retry_id,
            "runId": self.run_id,
            "tenantId": self.tenant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectRetryCommand:
        owner = "provider effect retry command"
        if (
            type(value) is not dict
            or set(value) != _PROVIDER_EFFECT_RETRY_COMMAND_FIELDS
        ):
            raise ProviderEffectContractError(f"{owner} must contain the closed fields")
        return cls(
            tenant_id=_require_exact_string(owner, "tenantId", value["tenantId"]),
            run_id=_require_exact_string(owner, "runId", value["runId"]),
            owner_principal_id=_require_exact_string(
                owner,
                "ownerPrincipalId",
                value["ownerPrincipalId"],
            ),
            effect_id=_require_exact_string(owner, "effectId", value["effectId"]),
            retry_id=_require_exact_string(owner, "retryId", value["retryId"]),
            intent_digest=_require_digest(
                owner,
                "intentDigest",
                value["intentDigest"],
            ),
            expected_state_version=_require_sqlite_integer(
                owner,
                "expectedStateVersion",
                value["expectedStateVersion"],
                positive=True,
            ),
            format_version=_require_exact_string(
                owner,
                "formatVersion",
                value["formatVersion"],
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderEffectClaim:
    effect_id: str
    intent_digest: str
    tenant_id: str
    run_id: str
    owner_principal_id: str
    claim_authority_digest: str
    claim_owner_id: str
    claim_generation: int
    claim_fencing_token: int
    claim_started_at_unix_ms: int
    claim_expires_at_unix_ms: int
    admitted_at_unix_ms: int
    send_attempt_id: str
    previous_send_attempt_digest: str | None = None
    format_version: str = PROVIDER_EFFECT_CLAIM_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect claim"
        for field_name in (
            "effect_id",
            "tenant_id",
            "run_id",
            "owner_principal_id",
            "claim_owner_id",
            "send_attempt_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_exact_string(owner, field_name, getattr(self, field_name)),
            )
        for field_name in ("intent_digest", "claim_authority_digest"):
            object.__setattr__(
                self,
                field_name,
                _require_digest(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "previous_send_attempt_digest",
            _require_optional_digest(
                owner,
                "previous_send_attempt_digest",
                self.previous_send_attempt_digest,
            ),
        )
        for field_name in ("claim_generation", "claim_fencing_token"):
            object.__setattr__(
                self,
                field_name,
                _require_sqlite_integer(
                    owner,
                    field_name,
                    getattr(self, field_name),
                    positive=True,
                ),
            )
        for field_name in (
            "claim_started_at_unix_ms",
            "claim_expires_at_unix_ms",
            "admitted_at_unix_ms",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_sqlite_integer(owner, field_name, getattr(self, field_name)),
            )
        if not (
            self.claim_started_at_unix_ms
            <= self.admitted_at_unix_ms
            < self.claim_expires_at_unix_ms
        ):
            raise ProviderEffectContractError(
                "provider effect admission must occur in the half-open claim interval"
            )
        format_version = _require_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_EFFECT_CLAIM_FORMAT_VERSION:
            raise ProviderEffectContractError(
                "provider effect claim format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "admittedAtUnixMs": self.admitted_at_unix_ms,
            "claimAuthorityDigest": self.claim_authority_digest,
            "claimExpiresAtUnixMs": self.claim_expires_at_unix_ms,
            "claimFencingToken": self.claim_fencing_token,
            "claimGeneration": self.claim_generation,
            "claimOwnerId": self.claim_owner_id,
            "claimStartedAtUnixMs": self.claim_started_at_unix_ms,
            "effectId": self.effect_id,
            "formatVersion": self.format_version,
            "intentDigest": self.intent_digest,
            "ownerPrincipalId": self.owner_principal_id,
            "previousSendAttemptDigest": self.previous_send_attempt_digest,
            "runId": self.run_id,
            "sendAttemptId": self.send_attempt_id,
            "tenantId": self.tenant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectClaim:
        owner = "provider effect claim"
        if type(value) is not dict or set(value) != _PROVIDER_EFFECT_CLAIM_FIELDS:
            raise ProviderEffectContractError(
                "provider effect claim must contain the closed fields"
            )
        return cls(
            effect_id=_require_exact_string(owner, "effectId", value["effectId"]),
            intent_digest=_require_digest(
                owner,
                "intentDigest",
                value["intentDigest"],
            ),
            tenant_id=_require_exact_string(owner, "tenantId", value["tenantId"]),
            run_id=_require_exact_string(owner, "runId", value["runId"]),
            owner_principal_id=_require_exact_string(
                owner,
                "ownerPrincipalId",
                value["ownerPrincipalId"],
            ),
            claim_authority_digest=_require_digest(
                owner,
                "claimAuthorityDigest",
                value["claimAuthorityDigest"],
            ),
            claim_owner_id=_require_exact_string(
                owner,
                "claimOwnerId",
                value["claimOwnerId"],
            ),
            claim_generation=_require_sqlite_integer(
                owner,
                "claimGeneration",
                value["claimGeneration"],
                positive=True,
            ),
            claim_fencing_token=_require_sqlite_integer(
                owner,
                "claimFencingToken",
                value["claimFencingToken"],
                positive=True,
            ),
            claim_started_at_unix_ms=_require_sqlite_integer(
                owner,
                "claimStartedAtUnixMs",
                value["claimStartedAtUnixMs"],
            ),
            claim_expires_at_unix_ms=_require_sqlite_integer(
                owner,
                "claimExpiresAtUnixMs",
                value["claimExpiresAtUnixMs"],
            ),
            admitted_at_unix_ms=_require_sqlite_integer(
                owner,
                "admittedAtUnixMs",
                value["admittedAtUnixMs"],
            ),
            send_attempt_id=_require_exact_string(
                owner,
                "sendAttemptId",
                value["sendAttemptId"],
            ),
            previous_send_attempt_digest=_require_optional_digest(
                owner,
                "previousSendAttemptDigest",
                value["previousSendAttemptDigest"],
            ),
            format_version=_require_exact_string(
                owner,
                "formatVersion",
                value["formatVersion"],
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderEffectClaimRelease:
    effect_id: str
    tenant_id: str
    run_id: str
    owner_principal_id: str
    claim_digest: str
    claim_generation: int
    claim_fencing_token: int
    released_at_unix_ms: int
    resulting_state_version: int
    resulting_event_sequence: int
    format_version: str = PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect claim release"
        for field_name in (
            "effect_id",
            "tenant_id",
            "run_id",
            "owner_principal_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "claim_digest",
            _require_digest(owner, "claim_digest", self.claim_digest),
        )
        for field_name in (
            "claim_generation",
            "claim_fencing_token",
            "resulting_state_version",
            "resulting_event_sequence",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_sqlite_integer(
                    owner,
                    field_name,
                    getattr(self, field_name),
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "released_at_unix_ms",
            _require_sqlite_integer(
                owner,
                "released_at_unix_ms",
                self.released_at_unix_ms,
            ),
        )
        if self.resulting_state_version != self.resulting_event_sequence:
            raise ProviderEffectContractError(
                "provider effect release version must match its event sequence"
            )
        format_version = _require_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION:
            raise ProviderEffectContractError(
                "provider effect claim release format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "claimDigest": self.claim_digest,
            "claimFencingToken": self.claim_fencing_token,
            "claimGeneration": self.claim_generation,
            "effectId": self.effect_id,
            "formatVersion": self.format_version,
            "ownerPrincipalId": self.owner_principal_id,
            "releasedAtUnixMs": self.released_at_unix_ms,
            "resultingEventSequence": self.resulting_event_sequence,
            "resultingStateVersion": self.resulting_state_version,
            "runId": self.run_id,
            "tenantId": self.tenant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectClaimRelease:
        owner = "provider effect claim release"
        if (
            type(value) is not dict
            or set(value) != _PROVIDER_EFFECT_CLAIM_RELEASE_FIELDS
        ):
            raise ProviderEffectContractError(
                "provider effect claim release must contain the closed fields"
            )
        return cls(
            effect_id=_require_exact_string(owner, "effectId", value["effectId"]),
            tenant_id=_require_exact_string(owner, "tenantId", value["tenantId"]),
            run_id=_require_exact_string(owner, "runId", value["runId"]),
            owner_principal_id=_require_exact_string(
                owner,
                "ownerPrincipalId",
                value["ownerPrincipalId"],
            ),
            claim_digest=_require_digest(
                owner,
                "claimDigest",
                value["claimDigest"],
            ),
            claim_generation=_require_sqlite_integer(
                owner,
                "claimGeneration",
                value["claimGeneration"],
                positive=True,
            ),
            claim_fencing_token=_require_sqlite_integer(
                owner,
                "claimFencingToken",
                value["claimFencingToken"],
                positive=True,
            ),
            released_at_unix_ms=_require_sqlite_integer(
                owner,
                "releasedAtUnixMs",
                value["releasedAtUnixMs"],
            ),
            resulting_state_version=_require_sqlite_integer(
                owner,
                "resultingStateVersion",
                value["resultingStateVersion"],
                positive=True,
            ),
            resulting_event_sequence=_require_sqlite_integer(
                owner,
                "resultingEventSequence",
                value["resultingEventSequence"],
                positive=True,
            ),
            format_version=_require_exact_string(
                owner,
                "formatVersion",
                value["formatVersion"],
            ),
        )


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
    claim_generation: int = 0
    claim_fencing_token: int = 0
    claim: ProviderEffectClaim | None = None
    last_pre_send_release: ProviderEffectClaimRelease | None = None
    latest_send_attempt: ProviderEffectSendAttempt | None = None
    latest_admission_receipt: ProviderEffectAdmissionReceipt | None = None
    active_send_attempt: ProviderEffectSendAttempt | None = None
    active_admission_receipt: ProviderEffectAdmissionReceipt | None = None

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
        for field_name in ("claim_generation", "claim_fencing_token"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0 or value > _MAX_SQLITE_INTEGER:
                raise ProviderEffectContractError(
                    f"{owner} {field_name} must be a non-negative SQLite integer"
                )
        if (self.state is ProviderEffectState.CLAIMED) != (self.claim is not None):
            raise ProviderEffectContractError(
                f"{owner} active claim presence must match claimed state"
            )
        if self.claim is not None:
            if type(self.claim) is not ProviderEffectClaim:
                raise ProviderEffectContractError(
                    f"{owner} claim must be ProviderEffectClaim or None"
                )
            if (
                self.claim.effect_id != self.intent.effect_id
                or self.claim.intent_digest != self.intent.digest
                or self.claim.tenant_id != self.tenant_id
                or self.claim.run_id != self.run_id
                or self.claim.owner_principal_id != self.owner_principal_id
                or self.claim.claim_generation != self.claim_generation
                or self.claim.claim_fencing_token != self.claim_fencing_token
            ):
                raise ProviderEffectContractError(
                    f"{owner} active claim does not match its projection"
                )
        if self.last_pre_send_release is not None:
            if type(self.last_pre_send_release) is not ProviderEffectClaimRelease:
                raise ProviderEffectContractError(
                    f"{owner} last_pre_send_release must be an exact release"
                )
            if (
                self.state is not ProviderEffectState.PENDING
                or self.last_pre_send_release.effect_id != self.intent.effect_id
                or self.last_pre_send_release.tenant_id != self.tenant_id
                or self.last_pre_send_release.run_id != self.run_id
                or self.last_pre_send_release.owner_principal_id
                != self.owner_principal_id
                or self.last_pre_send_release.claim_generation != self.claim_generation
                or self.last_pre_send_release.claim_fencing_token
                != self.claim_fencing_token
                or self.last_pre_send_release.resulting_state_version
                != self.state_version
                or self.last_pre_send_release.resulting_event_sequence
                != self.event_high_watermark
            ):
                raise ProviderEffectContractError(
                    f"{owner} last pre-send release does not match its projection"
                )
        if (self.latest_send_attempt is None) != (
            self.latest_admission_receipt is None
        ):
            raise ProviderEffectContractError(
                f"{owner} latest attempt and receipt must be installed together"
            )
        if (self.active_send_attempt is None) != (
            self.active_admission_receipt is None
        ):
            raise ProviderEffectContractError(
                f"{owner} active attempt and receipt must be installed together"
            )
        if (self.state in _ACTIVE_PROVIDER_EFFECT_SEND_STATES) != (
            self.active_send_attempt is not None
        ):
            raise ProviderEffectContractError(
                f"{owner} active send presence must match its state"
            )
        if self.latest_send_attempt is not None:
            if (
                type(self.latest_send_attempt) is not ProviderEffectSendAttempt
                or type(self.latest_admission_receipt)
                is not ProviderEffectAdmissionReceipt
            ):
                raise ProviderEffectContractError(
                    f"{owner} latest send records must be exact closed records"
                )
            if (
                self.latest_send_attempt.effect_id != self.intent.effect_id
                or self.latest_send_attempt.intent_digest != self.intent.digest
                or self.latest_send_attempt.capability_snapshot_digest
                != self.capability.digest
                or self.latest_admission_receipt.effect_id != self.intent.effect_id
                or self.latest_admission_receipt.intent_digest != self.intent.digest
                or self.latest_admission_receipt.capability_snapshot_digest
                != self.capability.digest
                or self.latest_admission_receipt.send_attempt_digest
                != self.latest_send_attempt.digest
                or self.latest_admission_receipt.admission_digest
                != self.latest_send_attempt.admission_digest
                or self.latest_admission_receipt.send_attempt_id
                != self.latest_send_attempt.attempt_id
                or self.latest_admission_receipt.claim_owner_id
                != self.latest_send_attempt.claim_owner_id
                or self.latest_admission_receipt.claim_generation
                != self.latest_send_attempt.claim_generation
                or self.latest_admission_receipt.claim_fencing_token
                != self.latest_send_attempt.claim_fencing_token
                or self.latest_admission_receipt.send_started_at_unix_ms
                != self.latest_send_attempt.started_at_unix_ms
            ):
                raise ProviderEffectContractError(
                    f"{owner} latest send records do not match the effect"
                )
        if self.active_send_attempt is not None and (
            self.active_send_attempt != self.latest_send_attempt
            or self.active_admission_receipt != self.latest_admission_receipt
            or self.active_send_attempt.claim_generation != self.claim_generation
            or self.active_send_attempt.claim_fencing_token != self.claim_fencing_token
        ):
            raise ProviderEffectContractError(
                f"{owner} active send records do not match the latest fence"
            )
        if self.claim is not None:
            previous_digest = (
                None
                if self.latest_send_attempt is None
                else self.latest_send_attempt.digest
            )
            if self.claim.previous_send_attempt_digest != previous_digest:
                raise ProviderEffectContractError(
                    f"{owner} claim does not bind the latest send attempt"
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
class ProviderEffectWorkItem:
    effect: StoredProviderEffect
    claim: ProviderEffectClaim

    def __post_init__(self) -> None:
        if type(self.effect) is not StoredProviderEffect:
            raise ProviderEffectContractError(
                "provider effect work item effect must be StoredProviderEffect"
            )
        if type(self.claim) is not ProviderEffectClaim:
            raise ProviderEffectContractError(
                "provider effect work item claim must be ProviderEffectClaim"
            )
        if self.effect.state is not ProviderEffectState.CLAIMED:
            raise ProviderEffectContractError(
                "provider effect work item must contain a claimed effect"
            )
        if self.effect.claim != self.claim:
            raise ProviderEffectContractError(
                "provider effect work item claim must match its projection"
            )


@dataclass(frozen=True, slots=True)
class StoredProviderEffectActiveSend:
    effect: StoredProviderEffect
    consumed_claim_digest: str
    send_attempt: ProviderEffectSendAttempt
    admission_receipt: ProviderEffectAdmissionReceipt
    installed_state_version: int
    installed_event_sequence: int

    def __post_init__(self) -> None:
        if type(self.effect) is not StoredProviderEffect:
            raise ProviderEffectContractError(
                "stored provider effect active send requires an exact effect"
            )
        if type(self.send_attempt) is not ProviderEffectSendAttempt:
            raise ProviderEffectContractError(
                "stored provider effect active send requires an exact attempt"
            )
        if type(self.admission_receipt) is not ProviderEffectAdmissionReceipt:
            raise ProviderEffectContractError(
                "stored provider effect active send requires an exact receipt"
            )
        _require_digest(
            "stored provider effect active send",
            "consumed_claim_digest",
            self.consumed_claim_digest,
        )
        for field_name in (
            "installed_state_version",
            "installed_event_sequence",
        ):
            _require_sqlite_integer(
                "stored provider effect active send",
                field_name,
                getattr(self, field_name),
                positive=True,
            )
        if (
            self.effect.state not in _ACTIVE_PROVIDER_EFFECT_SEND_STATES
            or self.effect.active_send_attempt != self.send_attempt
            or self.effect.active_admission_receipt != self.admission_receipt
            or self.installed_state_version != self.installed_event_sequence
            or self.installed_state_version > self.effect.state_version
        ):
            raise ProviderEffectContractError(
                "stored provider effect active send does not match its projection"
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
            payload.get("effectId") != self.effect_id
            or payload.get("formatVersion") != PROVIDER_EFFECT_EVENT_FORMAT_VERSION
        ):
            raise ProviderEffectContractError(
                f"{owner} common identity is not closed and exact"
            )
        if self.kind == "origin_transferred":
            if (
                self.from_state is not None
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
                or payload["state"] != ProviderEffectState.PENDING.value
            ):
                raise ProviderEffectContractError(
                    f"{owner} origin-transfer event is not closed and exact"
                )
            _require_digest(owner, "intentDigest", payload["intentDigest"])
            _require_digest(
                owner,
                "capabilitySnapshotDigest",
                payload["capabilitySnapshotDigest"],
            )
            _require_digest(
                owner,
                "originTransferDigest",
                payload["originTransferDigest"],
            )
        elif self.kind in {"send_claimed", "send_claim_reclaimed"}:
            expected_from_state = (
                ProviderEffectState.PENDING
                if self.kind == "send_claimed"
                else ProviderEffectState.CLAIMED
            )
            if (
                self.from_state is not expected_from_state
                or self.to_state is not ProviderEffectState.CLAIMED
                or set(payload)
                != {
                    "claim",
                    "claimDigest",
                    "effectId",
                    "formatVersion",
                    "intentDigest",
                    "state",
                }
                or payload["state"] != ProviderEffectState.CLAIMED.value
            ):
                raise ProviderEffectContractError(
                    f"{owner} claim event is not closed and exact"
                )
            claim = ProviderEffectClaim.from_wire(payload["claim"])
            if (
                claim.effect_id != self.effect_id
                or claim.digest
                != _require_digest(owner, "claimDigest", payload["claimDigest"])
                or claim.intent_digest
                != _require_digest(owner, "intentDigest", payload["intentDigest"])
                or claim.admitted_at_unix_ms != self.created_at_unix_ms
            ):
                raise ProviderEffectContractError(
                    f"{owner} claim event identity is not exact"
                )
        elif self.kind == "send_claim_released":
            if (
                self.from_state is not ProviderEffectState.CLAIMED
                or self.to_state is not ProviderEffectState.PENDING
                or set(payload)
                != {
                    "effectId",
                    "formatVersion",
                    "intentDigest",
                    "release",
                    "releaseDigest",
                    "state",
                }
                or payload["state"] != ProviderEffectState.PENDING.value
            ):
                raise ProviderEffectContractError(
                    f"{owner} claim-release event is not closed and exact"
                )
            release = ProviderEffectClaimRelease.from_wire(payload["release"])
            if (
                release.effect_id != self.effect_id
                or release.digest
                != _require_digest(
                    owner,
                    "releaseDigest",
                    payload["releaseDigest"],
                )
                or release.released_at_unix_ms != self.created_at_unix_ms
                or release.resulting_event_sequence != self.sequence
            ):
                raise ProviderEffectContractError(
                    f"{owner} claim-release event identity is not exact"
                )
            _require_digest(owner, "intentDigest", payload["intentDigest"])
        elif self.kind == "send_started":
            if (
                self.from_state is not ProviderEffectState.CLAIMED
                or self.to_state is not ProviderEffectState.SEND_STARTED
                or set(payload)
                != {
                    "admissionReceipt",
                    "admissionReceiptDigest",
                    "consumedClaimDigest",
                    "effectId",
                    "formatVersion",
                    "intentDigest",
                    "sendAttempt",
                    "sendAttemptDigest",
                    "state",
                }
                or payload["state"] != ProviderEffectState.SEND_STARTED.value
            ):
                raise ProviderEffectContractError(
                    f"{owner} send-started event is not closed and exact"
                )
            attempt = ProviderEffectSendAttempt.from_wire(payload["sendAttempt"])
            receipt = ProviderEffectAdmissionReceipt.from_wire(
                payload["admissionReceipt"]
            )
            if (
                attempt.effect_id != self.effect_id
                or attempt.intent_digest
                != _require_digest(owner, "intentDigest", payload["intentDigest"])
                or attempt.digest
                != _require_digest(
                    owner,
                    "sendAttemptDigest",
                    payload["sendAttemptDigest"],
                )
                or receipt.digest
                != _require_digest(
                    owner,
                    "admissionReceiptDigest",
                    payload["admissionReceiptDigest"],
                )
                or receipt.send_attempt_digest != attempt.digest
                or receipt.consumed_at_unix_ms != self.created_at_unix_ms
            ):
                raise ProviderEffectContractError(
                    f"{owner} send-started event identity is not exact"
                )
            _require_digest(
                owner,
                "consumedClaimDigest",
                payload["consumedClaimDigest"],
            )
        elif self.kind == "reconciliation_evidence_applied":
            if set(payload) != {
                "admissionReceiptDigest",
                "effectId",
                "evidence",
                "evidenceDigest",
                "formatVersion",
                "intentDigest",
                "sendAttemptDigest",
                "state",
            }:
                raise ProviderEffectContractError(
                    f"{owner} reconciliation event is not closed and exact"
                )
            evidence = ProviderReconciliationEvidence.from_wire(payload["evidence"])
            expected_state = (
                None
                if self.from_state is None
                else _reconciliation_settlement_state(
                    self.from_state,
                    evidence.outcome,
                )
            )
            if (
                expected_state is None
                or self.to_state is not expected_state
                or payload["state"] != expected_state.value
                or evidence.effect_id != self.effect_id
                or evidence.digest
                != _require_digest(owner, "evidenceDigest", payload["evidenceDigest"])
                or evidence.send_attempt_digest
                != _require_digest(
                    owner,
                    "sendAttemptDigest",
                    payload["sendAttemptDigest"],
                )
            ):
                raise ProviderEffectContractError(
                    f"{owner} reconciliation event identity is not exact"
                )
            _require_digest(
                owner,
                "admissionReceiptDigest",
                payload["admissionReceiptDigest"],
            )
        elif self.kind == "reconciliation_control_applied":
            if set(payload) != {
                "admissionReceiptDigest",
                "control",
                "controlDigest",
                "effectId",
                "formatVersion",
                "intentDigest",
                "sendAttemptDigest",
                "state",
            }:
                raise ProviderEffectContractError(
                    f"{owner} reconciliation control event is not closed and exact"
                )
            control = ProviderEffectReconciliationControl.from_wire(payload["control"])
            expected_state = (
                None
                if self.from_state is None
                else transition_provider_effect_state(
                    self.from_state,
                    control.transition,
                )
            )
            if (
                expected_state is None
                or self.to_state is not expected_state
                or payload["state"] != expected_state.value
                or control.effect_id != self.effect_id
                or control.expected_state_version + 1 != self.sequence
                or control.digest
                != _require_digest(owner, "controlDigest", payload["controlDigest"])
            ):
                raise ProviderEffectContractError(
                    f"{owner} reconciliation control identity is not exact"
                )
            for field_name in (
                "admissionReceiptDigest",
                "intentDigest",
                "sendAttemptDigest",
            ):
                _require_digest(owner, field_name, payload[field_name])
        elif self.kind == "retry_same_intent_applied":
            if set(payload) != {
                "effectId",
                "formatVersion",
                "intentDigest",
                "previousSendAttemptDigest",
                "retryCommand",
                "retryCommandDigest",
                "state",
            }:
                raise ProviderEffectContractError(
                    f"{owner} retry event is not closed and exact"
                )
            command = ProviderEffectRetryCommand.from_wire(payload["retryCommand"])
            if (
                self.from_state
                not in {
                    ProviderEffectState.CONFIRMED_NOT_COMMITTED,
                    ProviderEffectState.CONFIRMED_CANCELLED,
                }
                or self.to_state is not ProviderEffectState.PENDING
                or payload["state"] != ProviderEffectState.PENDING.value
                or command.effect_id != self.effect_id
                or command.expected_state_version + 1 != self.sequence
                or command.intent_digest
                != _require_digest(owner, "intentDigest", payload["intentDigest"])
                or command.digest
                != _require_digest(
                    owner,
                    "retryCommandDigest",
                    payload["retryCommandDigest"],
                )
            ):
                raise ProviderEffectContractError(
                    f"{owner} retry event identity is not exact"
                )
            _require_digest(
                owner,
                "previousSendAttemptDigest",
                payload["previousSendAttemptDigest"],
            )
        else:
            raise ProviderEffectContractError(
                f"{owner} kind is not supported by this repository version"
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
        claim_authority_digest: str,
        busy_timeout_ms: int = 5_000,
        failpoint: Callable[[str], None] | None = None,
        clock: Callable[[], int] = accepted_run_system_clock,
        attempt_id_factory: Callable[[], str] = _new_provider_send_attempt_id,
    ) -> None:
        for field_name, value in (
            ("origin_authority_digest", origin_authority_digest),
            ("claim_authority_digest", claim_authority_digest),
        ):
            try:
                _require_digest("provider-effect SQLite repository", field_name, value)
            except ProviderEffectContractError as error:
                raise ValueError(
                    f"provider-effect SQLite {field_name} must be a canonical "
                    "sha256 digest"
                ) from error
        if failpoint is not None and not callable(failpoint):
            raise TypeError("provider-effect SQLite failpoint must be callable")
        if not callable(clock):
            raise TypeError("provider-effect SQLite clock must be callable")
        if not callable(attempt_id_factory):
            raise TypeError(
                "provider-effect SQLite attempt_id_factory must be callable"
            )
        self.authority_digest = origin_authority_digest
        self.origin_authority_digest = origin_authority_digest
        self.claim_authority_digest = claim_authority_digest
        self._database = SQLiteAcceptedRunDatabase(
            path,
            busy_timeout_ms=busy_timeout_ms,
        )
        self._failpoint = failpoint
        self._clock = clock
        self._attempt_id_factory = attempt_id_factory

    @property
    def send_claim_authority(self) -> SQLiteProviderEffectClaimAuthority:
        """Return the structurally distinct one-shot send-claim authority."""

        return SQLiteProviderEffectClaimAuthority(self)

    def _hit_failpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)

    def _transaction_now_unix_ms(self) -> int:
        now_unix_ms = self._clock()
        if (
            type(now_unix_ms) is not int
            or now_unix_ms < 0
            or now_unix_ms > _MAX_SQLITE_INTEGER
        ):
            raise ValueError(
                "provider-effect SQLite clock must return a non-negative SQLite integer"
            )
        return now_unix_ms

    def _record_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> StoredProviderEffect:
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

            run_internal_id = row["run_internal_id"]
            latest_attempt_digest = row["latest_send_attempt_digest"]
            latest_receipt_digest = row["latest_admission_receipt_digest"]
            active_attempt_digest = row["active_send_attempt_digest"]
            active_receipt_digest = row["active_admission_receipt_digest"]
            if type(run_internal_id) is not str:
                raise ValueError("provider-effect run identity is not text")
            if (latest_attempt_digest is None) != (latest_receipt_digest is None):
                raise ValueError("provider-effect latest send identity is incomplete")
            if (active_attempt_digest is None) != (active_receipt_digest is None):
                raise ValueError("provider-effect active send identity is incomplete")
            latest_send_attempt: ProviderEffectSendAttempt | None = None
            latest_admission_receipt: ProviderEffectAdmissionReceipt | None = None
            if latest_attempt_digest is None:
                historical_attempt = connection.execute(
                    """
                    SELECT 1
                    FROM provider_effect_send_attempts
                    WHERE run_internal_id = ? AND effect_id = ?
                    LIMIT 1
                    """,
                    (run_internal_id, intent.effect_id),
                ).fetchone()
                if historical_attempt is not None:
                    raise ValueError(
                        "provider-effect attempt history has no latest projection"
                    )
            else:
                if (
                    type(latest_attempt_digest) is not str
                    or type(latest_receipt_digest) is not str
                ):
                    raise ValueError("provider-effect latest send identity is not text")
                attempt_tails = connection.execute(
                    """
                    SELECT (
                      SELECT send_attempt_digest
                      FROM provider_effect_send_attempts
                      WHERE run_internal_id = ? AND effect_id = ?
                      ORDER BY installed_state_version DESC
                      LIMIT 1
                    ) AS state_version_tail_digest,
                    (
                      SELECT send_attempt_digest
                      FROM provider_effect_send_attempts
                      WHERE run_internal_id = ? AND effect_id = ?
                      ORDER BY claim_generation DESC
                      LIMIT 1
                    ) AS claim_generation_tail_digest,
                    (
                      SELECT send_attempt_digest
                      FROM provider_effect_send_attempts
                      WHERE run_internal_id = ? AND effect_id = ?
                      ORDER BY claim_fencing_token DESC
                      LIMIT 1
                    ) AS claim_fencing_tail_digest
                    """,
                    (
                        run_internal_id,
                        intent.effect_id,
                        run_internal_id,
                        intent.effect_id,
                        run_internal_id,
                        intent.effect_id,
                    ),
                ).fetchone()
                if attempt_tails is None or any(
                    attempt_tails[field_name] != latest_attempt_digest
                    for field_name in (
                        "state_version_tail_digest",
                        "claim_generation_tail_digest",
                        "claim_fencing_tail_digest",
                    )
                ):
                    raise ValueError(
                        "provider-effect latest send projection is not the authority "
                        "tail"
                    )
                attempt_rows = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_send_attempts
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND send_attempt_digest = ?
                      AND admission_receipt_digest = ?
                    LIMIT 2
                    """,
                    (
                        run_internal_id,
                        intent.effect_id,
                        latest_attempt_digest,
                        latest_receipt_digest,
                    ),
                ).fetchall()
                if len(attempt_rows) != 1:
                    raise ValueError(
                        "provider-effect latest send records are not unique"
                    )
                attempt_row = attempt_rows[0]
                attempt_json = attempt_row["send_attempt_json"]
                receipt_json = attempt_row["admission_receipt_json"]
                if type(attempt_json) is not str or type(receipt_json) is not str:
                    raise ValueError("provider-effect send records are not text")
                attempt_wire = canonical_loads(attempt_json)
                receipt_wire = canonical_loads(receipt_json)
                if (
                    canonical_dumps(attempt_wire) != attempt_json
                    or canonical_dumps(receipt_wire) != receipt_json
                ):
                    raise ValueError("provider-effect send records are not canonical")
                latest_send_attempt = _revalidate_provider_effect_send_attempt(
                    ProviderEffectSendAttempt.from_wire(attempt_wire)
                )
                latest_admission_receipt = (
                    _revalidate_provider_effect_admission_receipt(
                        ProviderEffectAdmissionReceipt.from_wire(receipt_wire)
                    )
                )
                for field_name in (
                    "attempt_id",
                    "admission_digest",
                    "consumed_claim_digest",
                    "send_attempt_digest",
                    "admission_receipt_digest",
                    "claim_owner_id",
                ):
                    if type(attempt_row[field_name]) is not str:
                        raise ValueError(
                            f"provider-effect attempt {field_name} is not text"
                        )
                for field_name in (
                    "claim_generation",
                    "claim_fencing_token",
                    "started_at_unix_ms",
                    "consumed_at_unix_ms",
                    "installed_state_version",
                    "installed_event_sequence",
                ):
                    if type(attempt_row[field_name]) is not int:
                        raise ValueError(
                            f"provider-effect attempt {field_name} is not an integer"
                        )
                if (
                    attempt_row["attempt_id"] != latest_send_attempt.attempt_id
                    or attempt_row["admission_digest"]
                    != latest_send_attempt.admission_digest
                    or attempt_row["admission_digest"]
                    != latest_admission_receipt.admission_digest
                    or attempt_row["send_attempt_digest"] != latest_send_attempt.digest
                    or attempt_row["admission_receipt_digest"]
                    != latest_admission_receipt.digest
                    or attempt_row["previous_send_attempt_digest"]
                    != latest_admission_receipt.previous_send_attempt_digest
                    or attempt_row["claim_owner_id"]
                    != latest_send_attempt.claim_owner_id
                    or attempt_row["claim_generation"]
                    != latest_send_attempt.claim_generation
                    or attempt_row["claim_fencing_token"]
                    != latest_send_attempt.claim_fencing_token
                    or attempt_row["started_at_unix_ms"]
                    != latest_send_attempt.started_at_unix_ms
                    or attempt_row["consumed_at_unix_ms"]
                    != latest_admission_receipt.consumed_at_unix_ms
                    or attempt_row["installed_state_version"]
                    != attempt_row["installed_event_sequence"]
                    or attempt_row["installed_state_version"] > row["state_version"]
                    or _require_digest(
                        "provider-effect consumed attempt",
                        "consumed_claim_digest",
                        attempt_row["consumed_claim_digest"],
                    )
                    != attempt_row["consumed_claim_digest"]
                ):
                    raise ValueError(
                        "provider-effect attempt projection does not match its records"
                    )
            if active_attempt_digest is not None and (
                active_attempt_digest != latest_attempt_digest
                or active_receipt_digest != latest_receipt_digest
            ):
                raise ValueError("provider-effect active send is not the latest")
            active_send_attempt = (
                None if active_attempt_digest is None else latest_send_attempt
            )
            active_admission_receipt = (
                None if active_receipt_digest is None else latest_admission_receipt
            )

            claim_json = row["claim_json"]
            claim_digest = row["claim_digest"]
            if (claim_json is None) != (claim_digest is None):
                raise ValueError("provider-effect claim identity is incomplete")
            claim: ProviderEffectClaim | None = None
            if claim_json is not None:
                if type(claim_json) is not str or type(claim_digest) is not str:
                    raise ValueError("provider-effect claim identity is not text")
                claim_wire = canonical_loads(claim_json)
                if canonical_dumps(claim_wire) != claim_json:
                    raise ValueError("provider-effect claim JSON is not canonical")
                claim = ProviderEffectClaim.from_wire(claim_wire)
                if claim.digest != claim_digest:
                    raise ValueError("provider-effect claim digest does not match JSON")
                if (
                    row["claim_authority_digest"] != claim.claim_authority_digest
                    or row["claim_owner_id"] != claim.claim_owner_id
                    or row["claim_generation"] != claim.claim_generation
                    or row["claim_fencing_token"] != claim.claim_fencing_token
                    or row["claim_started_at_unix_ms"] != claim.claim_started_at_unix_ms
                    or row["claim_expires_at_unix_ms"] != claim.claim_expires_at_unix_ms
                    or row["admitted_at_unix_ms"] != claim.admitted_at_unix_ms
                    or row["send_attempt_id"] != claim.send_attempt_id
                    or row["previous_send_attempt_digest"]
                    != claim.previous_send_attempt_digest
                ):
                    raise ValueError(
                        "provider-effect claim projection does not match its wire value"
                    )
                issuance_rows = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_send_claim_issuances
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND claim_digest = ?
                      AND attempt_id = ?
                    LIMIT 2
                    """,
                    (
                        run_internal_id,
                        intent.effect_id,
                        claim.digest,
                        claim.send_attempt_id,
                    ),
                ).fetchall()
                if len(issuance_rows) != 1:
                    raise ValueError(
                        "provider-effect active claim has no unique issuance"
                    )
                issuance = issuance_rows[0]
                if (
                    issuance["claim_json"] != claim_json
                    or issuance["claim_owner_id"] != claim.claim_owner_id
                    or issuance["claim_generation"] != claim.claim_generation
                    or issuance["claim_fencing_token"] != claim.claim_fencing_token
                    or issuance["issued_at_unix_ms"] != claim.claim_started_at_unix_ms
                    or issuance["claim_expires_at_unix_ms"]
                    != claim.claim_expires_at_unix_ms
                    or issuance["installed_state_version"] != row["state_version"]
                    or issuance["installed_event_sequence"]
                    != row["event_high_watermark"]
                ):
                    raise ValueError(
                        "provider-effect active claim does not match its issuance"
                    )
            elif any(
                row[field_name] is not None
                for field_name in (
                    "claim_authority_digest",
                    "claim_owner_id",
                    "claim_started_at_unix_ms",
                    "claim_expires_at_unix_ms",
                    "admitted_at_unix_ms",
                    "send_attempt_id",
                    "previous_send_attempt_digest",
                )
            ):
                raise ValueError("provider-effect inactive claim fields are not empty")

            release_json = row["last_pre_send_release_json"]
            release_digest = row["last_pre_send_release_digest"]
            if (release_json is None) != (release_digest is None):
                raise ValueError("provider-effect release identity is incomplete")
            release: ProviderEffectClaimRelease | None = None
            if release_json is not None:
                if type(release_json) is not str or type(release_digest) is not str:
                    raise ValueError("provider-effect release identity is not text")
                release_wire = canonical_loads(release_json)
                if canonical_dumps(release_wire) != release_json:
                    raise ValueError("provider-effect release JSON is not canonical")
                release = ProviderEffectClaimRelease.from_wire(release_wire)
                if release.digest != release_digest:
                    raise ValueError(
                        "provider-effect release digest does not match JSON"
                    )
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
                "claim_generation",
                "claim_fencing_token",
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
                claim_generation=row["claim_generation"],
                claim_fencing_token=row["claim_fencing_token"],
                claim=claim,
                last_pre_send_release=release,
                latest_send_attempt=latest_send_attempt,
                latest_admission_receipt=latest_admission_receipt,
                active_send_attempt=active_send_attempt,
                active_admission_receipt=active_admission_receipt,
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

    @staticmethod
    def _event_payload_for_record(
        event: StoredProviderEffectEvent,
        record: StoredProviderEffect,
    ) -> dict[str, object]:
        try:
            payload = canonical_loads(event.payload_json)
            if (
                type(payload) is not dict
                or payload["intentDigest"] != record.intent.digest
            ):
                raise ValueError("provider-effect event intent identity is invalid")
            if event.kind == "origin_transferred":
                if (
                    event.sequence != 1
                    or payload["capabilitySnapshotDigest"] != record.capability.digest
                    or payload["originTransferDigest"] != record.origin_transfer.digest
                ):
                    raise ValueError("provider-effect origin event identity is invalid")
            elif event.kind in {"send_claimed", "send_claim_reclaimed"}:
                claim = ProviderEffectClaim.from_wire(payload["claim"])
                if (
                    claim.tenant_id != record.tenant_id
                    or claim.run_id != record.run_id
                    or claim.owner_principal_id != record.owner_principal_id
                    or claim.effect_id != record.intent.effect_id
                    or claim.intent_digest != record.intent.digest
                ):
                    raise ValueError("provider-effect claim event scope is invalid")
            elif event.kind == "send_claim_released":
                release = ProviderEffectClaimRelease.from_wire(payload["release"])
                if (
                    release.tenant_id != record.tenant_id
                    or release.run_id != record.run_id
                    or release.owner_principal_id != record.owner_principal_id
                    or release.effect_id != record.intent.effect_id
                ):
                    raise ValueError("provider-effect release event scope is invalid")
            elif event.kind == "send_started":
                attempt = ProviderEffectSendAttempt.from_wire(payload["sendAttempt"])
                receipt = ProviderEffectAdmissionReceipt.from_wire(
                    payload["admissionReceipt"]
                )
                if (
                    attempt.effect_id != record.intent.effect_id
                    or attempt.intent_digest != record.intent.digest
                    or attempt.capability_snapshot_digest != record.capability.digest
                    or receipt.effect_id != record.intent.effect_id
                    or receipt.intent_digest != record.intent.digest
                    or receipt.capability_snapshot_digest != record.capability.digest
                    or receipt.send_attempt_digest != attempt.digest
                ):
                    raise ValueError(
                        "provider-effect send-started event scope is invalid"
                    )
            elif event.kind == "reconciliation_evidence_applied":
                evidence = ProviderReconciliationEvidence.from_wire(payload["evidence"])
                if (
                    evidence.effect_id != record.intent.effect_id
                    or evidence.intent_digest != record.intent.digest
                    or evidence.capability_snapshot_digest != record.capability.digest
                ):
                    raise ValueError(
                        "provider-effect reconciliation event scope is invalid"
                    )
                _require_digest(
                    "provider-effect reconciliation event",
                    "admissionReceiptDigest",
                    payload["admissionReceiptDigest"],
                )
            elif event.kind == "reconciliation_control_applied":
                control = ProviderEffectReconciliationControl.from_wire(
                    payload["control"]
                )
                if (
                    control.tenant_id != record.tenant_id
                    or control.run_id != record.run_id
                    or control.owner_principal_id != record.owner_principal_id
                    or control.effect_id != record.intent.effect_id
                ):
                    raise ValueError(
                        "provider-effect reconciliation control scope is invalid"
                    )
                for field_name in (
                    "sendAttemptDigest",
                    "admissionReceiptDigest",
                ):
                    _require_digest(
                        "provider-effect reconciliation control event",
                        field_name,
                        payload[field_name],
                    )
            elif event.kind == "retry_same_intent_applied":
                command = ProviderEffectRetryCommand.from_wire(payload["retryCommand"])
                if (
                    command.tenant_id != record.tenant_id
                    or command.run_id != record.run_id
                    or command.owner_principal_id != record.owner_principal_id
                    or command.effect_id != record.intent.effect_id
                    or command.intent_digest != record.intent.digest
                ):
                    raise ValueError("provider-effect retry event scope is invalid")
                _require_digest(
                    "provider-effect retry event",
                    "previousSendAttemptDigest",
                    payload["previousSendAttemptDigest"],
                )
            return payload
        except (KeyError, TypeError, ValueError) as error:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event does not match its effect identity"
            ) from error

    def _assert_projection_tail(
        self,
        connection: sqlite3.Connection,
        *,
        run_internal_id: str,
        record: StoredProviderEffect,
    ) -> None:
        row = connection.execute(
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
        if row is None:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite projection has no authoritative event tail"
            )
        future_event = connection.execute(
            """
            SELECT 1
            FROM provider_effect_events
            WHERE run_internal_id = ?
              AND effect_id = ?
              AND sequence > ?
            LIMIT 1
            """,
            (
                run_internal_id,
                record.intent.effect_id,
                record.event_high_watermark,
            ),
        ).fetchone()
        if future_event is not None:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite journal exceeds its projection watermark"
            )
        event = self._event_from_row(row)
        payload = self._event_payload_for_record(event, record)
        try:
            if (
                event.sequence != record.event_high_watermark
                or event.to_state is not record.state
                or event.created_at_unix_ms != record.updated_at_unix_ms
            ):
                raise ValueError("provider-effect event tail identity is invalid")
            if event.kind == "origin_transferred":
                if (
                    record.state is not ProviderEffectState.PENDING
                    or record.event_high_watermark != 1
                    or record.claim_generation != 0
                    or record.claim_fencing_token != 0
                    or record.claim is not None
                    or record.last_pre_send_release is not None
                ):
                    raise ValueError("provider-effect origin tail is invalid")
            elif event.kind in {"send_claimed", "send_claim_reclaimed"}:
                claim = ProviderEffectClaim.from_wire(payload["claim"])
                if (
                    record.state is not ProviderEffectState.CLAIMED
                    or record.claim != claim
                    or record.last_pre_send_release is not None
                    or claim.tenant_id != record.tenant_id
                    or claim.run_id != record.run_id
                    or claim.owner_principal_id != record.owner_principal_id
                    or claim.effect_id != record.intent.effect_id
                    or claim.intent_digest != record.intent.digest
                    or claim.claim_generation != record.claim_generation
                    or claim.claim_fencing_token != record.claim_fencing_token
                ):
                    raise ValueError("provider-effect claim tail is invalid")
            elif event.kind == "send_claim_released":
                release = ProviderEffectClaimRelease.from_wire(payload["release"])
                if (
                    record.state is not ProviderEffectState.PENDING
                    or record.claim is not None
                    or record.last_pre_send_release != release
                    or release.tenant_id != record.tenant_id
                    or release.run_id != record.run_id
                    or release.owner_principal_id != record.owner_principal_id
                    or release.effect_id != record.intent.effect_id
                    or release.claim_generation != record.claim_generation
                    or release.claim_fencing_token != record.claim_fencing_token
                    or release.resulting_state_version != record.state_version
                ):
                    raise ValueError("provider-effect release tail is invalid")
            elif event.kind == "send_started":
                attempt = ProviderEffectSendAttempt.from_wire(payload["sendAttempt"])
                receipt = ProviderEffectAdmissionReceipt.from_wire(
                    payload["admissionReceipt"]
                )
                attempt_row = connection.execute(
                    """
                    SELECT consumed_claim_digest,
                           installed_state_version,
                           installed_event_sequence
                    FROM provider_effect_send_attempts
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND send_attempt_digest = ?
                    """,
                    (run_internal_id, record.intent.effect_id, attempt.digest),
                ).fetchone()
                predecessor_row = connection.execute(
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
                        event.sequence - 1,
                    ),
                ).fetchone()
                predecessor_event = (
                    None
                    if predecessor_row is None
                    else self._event_from_row(predecessor_row)
                )
                predecessor_payload = (
                    None
                    if predecessor_event is None
                    else self._event_payload_for_record(
                        predecessor_event,
                        record,
                    )
                )
                consumed_claim = (
                    None
                    if predecessor_event is None
                    or predecessor_payload is None
                    or predecessor_event.kind
                    not in {"send_claimed", "send_claim_reclaimed"}
                    else ProviderEffectClaim.from_wire(predecessor_payload["claim"])
                )
                if (
                    record.state is not ProviderEffectState.SEND_STARTED
                    or record.claim is not None
                    or record.last_pre_send_release is not None
                    or record.active_send_attempt != attempt
                    or record.active_admission_receipt != receipt
                    or payload["sendAttemptDigest"] != attempt.digest
                    or payload["admissionReceiptDigest"] != receipt.digest
                    or attempt.claim_generation != record.claim_generation
                    or attempt.claim_fencing_token != record.claim_fencing_token
                    or attempt_row is None
                    or attempt_row["consumed_claim_digest"]
                    != payload["consumedClaimDigest"]
                    or attempt_row["installed_state_version"] != record.state_version
                    or attempt_row["installed_event_sequence"] != event.sequence
                    or predecessor_event is None
                    or predecessor_event.sequence != event.sequence - 1
                    or predecessor_event.to_state is not ProviderEffectState.CLAIMED
                    or consumed_claim is None
                    or consumed_claim.digest != payload["consumedClaimDigest"]
                    or consumed_claim.effect_id != attempt.effect_id
                    or consumed_claim.intent_digest != attempt.intent_digest
                    or consumed_claim.claim_authority_digest
                    != attempt.claim_authority_digest
                    or consumed_claim.send_attempt_id != attempt.attempt_id
                    or consumed_claim.claim_owner_id != attempt.claim_owner_id
                    or consumed_claim.claim_generation != attempt.claim_generation
                    or consumed_claim.claim_fencing_token != attempt.claim_fencing_token
                    or consumed_claim.admitted_at_unix_ms != receipt.admitted_at_unix_ms
                    or consumed_claim.claim_expires_at_unix_ms
                    != receipt.claim_expires_at_unix_ms
                    or consumed_claim.previous_send_attempt_digest
                    != receipt.previous_send_attempt_digest
                    or attempt.started_at_unix_ms < consumed_claim.admitted_at_unix_ms
                    or attempt.started_at_unix_ms
                    >= consumed_claim.claim_expires_at_unix_ms
                ):
                    raise ValueError("provider-effect send-started tail is invalid")
            elif event.kind == "reconciliation_evidence_applied":
                evidence = ProviderReconciliationEvidence.from_wire(payload["evidence"])
                evidence_row = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_reconciliation_evidence
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND evidence_digest = ?
                    """,
                    (run_internal_id, record.intent.effect_id, evidence.digest),
                ).fetchone()
                reconciliation_from_state = event.from_state
                expected_state = (
                    None
                    if reconciliation_from_state is None
                    else _reconciliation_settlement_state(
                        reconciliation_from_state,
                        evidence.outcome,
                    )
                )
                terminal = expected_state in {
                    ProviderEffectState.CONFIRMED_COMMITTED,
                    ProviderEffectState.CONFIRMED_NOT_COMMITTED,
                    ProviderEffectState.CONFIRMED_CANCELLED,
                }
                if (
                    expected_state is None
                    or record.state is not expected_state
                    or record.claim is not None
                    or record.last_pre_send_release is not None
                    or record.latest_send_attempt is None
                    or record.latest_admission_receipt is None
                    or evidence.send_attempt_digest != record.latest_send_attempt.digest
                    or payload["admissionReceiptDigest"]
                    != record.latest_admission_receipt.digest
                    or (
                        terminal
                        and (
                            record.active_send_attempt is not None
                            or record.active_admission_receipt is not None
                        )
                    )
                    or (
                        not terminal
                        and (
                            record.active_send_attempt != record.latest_send_attempt
                            or record.active_admission_receipt
                            != record.latest_admission_receipt
                        )
                    )
                    or evidence_row is None
                    or evidence_row["evidence_json"]
                    != canonical_dumps(evidence.to_wire())
                    or evidence_row["send_attempt_digest"]
                    != record.latest_send_attempt.digest
                    or evidence_row["admission_receipt_digest"]
                    != record.latest_admission_receipt.digest
                    or reconciliation_from_state is None
                    or evidence_row["from_state"] != reconciliation_from_state.value
                    or evidence_row["to_state"] != expected_state.value
                    or evidence_row["observed_at_unix_ms"]
                    != evidence.observed_at_unix_ms
                    or evidence_row["settled_at_unix_ms"] != record.updated_at_unix_ms
                    or evidence_row["installed_state_version"] != record.state_version
                    or evidence_row["installed_event_sequence"] != event.sequence
                ):
                    raise ValueError("provider-effect reconciliation tail is invalid")
            elif event.kind == "reconciliation_control_applied":
                control = ProviderEffectReconciliationControl.from_wire(
                    payload["control"]
                )
                control_row = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_reconciliation_controls
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND control_id = ?
                    """,
                    (run_internal_id, record.intent.effect_id, control.control_id),
                ).fetchone()
                control_from_state = event.from_state
                expected_state = (
                    None
                    if control_from_state is None
                    else transition_provider_effect_state(
                        control_from_state,
                        control.transition,
                    )
                )
                if (
                    expected_state is None
                    or record.state is not expected_state
                    or record.active_send_attempt is None
                    or record.active_admission_receipt is None
                    or payload["sendAttemptDigest"] != record.active_send_attempt.digest
                    or payload["admissionReceiptDigest"]
                    != record.active_admission_receipt.digest
                    or control_row is None
                    or control_row["request_digest"] != control.digest
                    or control_row["transition"] != control.transition.value
                    or control_from_state is None
                    or control_row["from_state"] != control_from_state.value
                    or control_row["to_state"] != expected_state.value
                    or control_row["requested_state_version"]
                    != control.expected_state_version
                    or control_row["applied_at_unix_ms"] != record.updated_at_unix_ms
                    or control_row["installed_state_version"] != record.state_version
                    or control_row["installed_event_sequence"] != event.sequence
                ):
                    raise ValueError(
                        "provider-effect reconciliation control tail is invalid"
                    )
            elif event.kind == "retry_same_intent_applied":
                command = ProviderEffectRetryCommand.from_wire(payload["retryCommand"])
                retry_row = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_retry_commands
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND retry_id = ?
                    """,
                    (run_internal_id, record.intent.effect_id, command.retry_id),
                ).fetchone()
                if (
                    record.state is not ProviderEffectState.PENDING
                    or record.claim is not None
                    or record.last_pre_send_release is not None
                    or record.active_send_attempt is not None
                    or record.active_admission_receipt is not None
                    or record.latest_send_attempt is None
                    or payload["previousSendAttemptDigest"]
                    != record.latest_send_attempt.digest
                    or retry_row is None
                    or retry_row["command_digest"] != command.digest
                    or retry_row["command_json"] != canonical_dumps(command.to_wire())
                    or retry_row["intent_digest"] != record.intent.digest
                    or retry_row["previous_send_attempt_digest"]
                    != record.latest_send_attempt.digest
                    or event.from_state is None
                    or retry_row["from_state"] != event.from_state.value
                    or retry_row["to_state"] != ProviderEffectState.PENDING.value
                    or retry_row["requested_state_version"]
                    != command.expected_state_version
                    or retry_row["applied_at_unix_ms"] != record.updated_at_unix_ms
                    or retry_row["installed_state_version"] != record.state_version
                    or retry_row["installed_event_sequence"] != event.sequence
                ):
                    raise ValueError("provider-effect retry tail is invalid")
        except (KeyError, TypeError, ValueError) as error:
            raise SQLiteProviderEffectCorruptionError(
                "provider-effect SQLite event tail does not match its projection"
            ) from error

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
                stored = self._record_from_row(connection, existing)
                if type(existing["run_internal_id"]) is not str:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite run identity is not text"
                    )
                self._assert_projection_tail(
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

    def claim_next_effect(
        self,
        request: ProviderEffectClaimRequest,
    ) -> ProviderEffectWorkItem | None:
        """Claim one pending or expired pre-send effect for an exact owner scope."""

        if type(request) is not ProviderEffectClaimRequest:
            raise TypeError(
                "provider-effect SQLite claim request must be "
                "ProviderEffectClaimRequest"
            )
        request = ProviderEffectClaimRequest(
            tenant_id=request.tenant_id,
            owner_principal_id=request.owner_principal_id,
            claim_owner_id=request.claim_owner_id,
            lease_duration_ms=request.lease_duration_ms,
        )

        def transition(
            connection: sqlite3.Connection,
        ) -> ProviderEffectWorkItem | None:
            transaction_now = self._transaction_now_unix_ms()
            replay_rows = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effects
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE accepted_runs.tenant_id = ?
                  AND accepted_runs.owner_principal_id = ?
                  AND provider_effects.state = 'claimed'
                  AND provider_effects.claim_owner_id = ?
                  AND provider_effects.claim_expires_at_unix_ms > ?
                ORDER BY provider_effects.updated_at_unix_ms,
                         provider_effects.created_at_unix_ms,
                         provider_effects.run_internal_id,
                         provider_effects.effect_id
                LIMIT 2
                """,
                (
                    request.tenant_id,
                    request.owner_principal_id,
                    request.claim_owner_id,
                    transaction_now,
                ),
            ).fetchall()
            if len(replay_rows) > 1:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect claim owner has multiple active claims in one "
                    "scope"
                )
            replay_row = replay_rows[0] if replay_rows else None
            if replay_row is not None:
                replayed = self._record_from_row(connection, replay_row)
                if type(replay_row["run_internal_id"]) is not str:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite run identity is not text"
                    )
                self._assert_projection_tail(
                    connection,
                    run_internal_id=replay_row["run_internal_id"],
                    record=replayed,
                )
                if (
                    replayed.origin_transfer.repository_authority_digest
                    != self.origin_authority_digest
                    or replayed.claim is None
                    or replayed.claim.claim_authority_digest
                    != self.claim_authority_digest
                ):
                    raise ProviderEffectContractError(
                        "provider-effect active claim uses another repository authority"
                    )
                if (
                    transaction_now < replayed.updated_at_unix_ms
                    or transaction_now < replayed.claim.claim_started_at_unix_ms
                    or transaction_now < replayed.claim.admitted_at_unix_ms
                ):
                    raise ValueError(
                        "provider-effect SQLite clock moved behind the active claim"
                    )
                replay_commit_now = self._transaction_now_unix_ms()
                if replay_commit_now < transaction_now:
                    raise ValueError(
                        "provider-effect SQLite clock must remain monotonic within "
                        "the claim replay transaction"
                    )
                if replay_commit_now < replayed.claim.claim_expires_at_unix_ms:
                    return ProviderEffectWorkItem(
                        effect=replayed,
                        claim=replayed.claim,
                    )
                transaction_now = replay_commit_now

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
                  AND accepted_runs.owner_principal_id = ?
                  AND (
                    provider_effects.state = 'pending'
                    OR (
                      provider_effects.state = 'claimed'
                      AND provider_effects.claim_expires_at_unix_ms <= ?
                    )
                  )
                ORDER BY provider_effects.updated_at_unix_ms,
                         provider_effects.created_at_unix_ms,
                         provider_effects.run_internal_id,
                         provider_effects.effect_id
                LIMIT 1
                """,
                (
                    request.tenant_id,
                    request.owner_principal_id,
                    transaction_now,
                ),
            ).fetchone()
            if row is None:
                return None
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            if (
                record.origin_transfer.repository_authority_digest
                != self.origin_authority_digest
            ):
                raise ProviderEffectContractError(
                    "provider-effect pending work uses another origin authority"
                )
            if record.updated_at_unix_ms > transaction_now:
                raise ValueError(
                    "provider-effect SQLite clock moved behind the projection"
                )
            old_claim_digest: str | None = None
            event_kind = "send_claimed"
            if record.state is ProviderEffectState.CLAIMED:
                if record.claim is None:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect claimed projection has no active claim"
                    )
                if record.claim.claim_authority_digest != self.claim_authority_digest:
                    raise ProviderEffectContractError(
                        "provider-effect expired claim uses another claim authority"
                    )
                if record.claim.claim_expires_at_unix_ms > transaction_now:
                    raise ProviderEffectStateConflictError(
                        "provider-effect claim is not expired"
                    )
                old_claim_digest = record.claim.digest
                event_kind = "send_claim_reclaimed"
            elif record.state is not ProviderEffectState.PENDING:
                raise ProviderEffectStateConflictError(
                    "provider-effect state is not pre-send claimable"
                )
            if (
                record.claim_generation >= _MAX_SQLITE_INTEGER
                or record.claim_fencing_token >= _MAX_SQLITE_INTEGER
                or record.state_version >= _MAX_SQLITE_INTEGER
            ):
                raise ProviderEffectStateConflictError(
                    "provider-effect claim authority counter is exhausted"
                )
            claim_expires_at = transaction_now + request.lease_duration_ms
            if claim_expires_at > _MAX_SQLITE_INTEGER:
                raise ValueError(
                    "provider-effect SQLite claim expiry exceeds its integer range"
                )
            send_attempt_id = _require_exact_string(
                "provider-effect SQLite repository",
                "send_attempt_id",
                self._attempt_id_factory(),
            )
            conflicting_attempt = connection.execute(
                """
                SELECT effect_id
                FROM provider_effects
                WHERE send_attempt_id = ?
                LIMIT 1
                """,
                (send_attempt_id,),
            ).fetchone()
            historical_attempt = connection.execute(
                """
                SELECT effect_id
                FROM provider_effect_send_attempts
                WHERE attempt_id = ?
                LIMIT 1
                """,
                (send_attempt_id,),
            ).fetchone()
            issued_attempt = connection.execute(
                """
                SELECT effect_id
                FROM provider_effect_send_claim_issuances
                WHERE attempt_id = ?
                LIMIT 1
                """,
                (send_attempt_id,),
            ).fetchone()
            if (
                conflicting_attempt is not None
                or historical_attempt is not None
                or issued_attempt is not None
            ):
                raise ProviderEffectIdentityConflictError(
                    "provider-effect send attempt identity is already issued"
                )
            claim = ProviderEffectClaim(
                effect_id=record.intent.effect_id,
                intent_digest=record.intent.digest,
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                claim_authority_digest=self.claim_authority_digest,
                claim_owner_id=request.claim_owner_id,
                claim_generation=record.claim_generation + 1,
                claim_fencing_token=record.claim_fencing_token + 1,
                claim_started_at_unix_ms=transaction_now,
                claim_expires_at_unix_ms=claim_expires_at,
                admitted_at_unix_ms=transaction_now,
                send_attempt_id=send_attempt_id,
                previous_send_attempt_digest=(
                    None
                    if record.latest_send_attempt is None
                    else record.latest_send_attempt.digest
                ),
            )
            next_version = record.state_version + 1
            claim_json = canonical_dumps(claim.to_wire())
            connection.execute(
                """
                INSERT INTO provider_effect_send_claim_issuances (
                  run_internal_id,
                  effect_id,
                  claim_digest,
                  claim_json,
                  attempt_id,
                  claim_owner_id,
                  claim_generation,
                  claim_fencing_token,
                  issued_at_unix_ms,
                  claim_expires_at_unix_ms,
                  installed_state_version,
                  installed_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    claim.digest,
                    claim_json,
                    claim.send_attempt_id,
                    claim.claim_owner_id,
                    claim.claim_generation,
                    claim.claim_fencing_token,
                    claim.claim_started_at_unix_ms,
                    claim.claim_expires_at_unix_ms,
                    next_version,
                    next_version,
                ),
            )
            self._hit_failpoint("claim_next_effect.after_issuance_insert")
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = 'claimed',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    claim_json = ?,
                    claim_digest = ?,
                    claim_authority_digest = ?,
                    claim_owner_id = ?,
                    claim_generation = ?,
                    claim_fencing_token = ?,
                    claim_started_at_unix_ms = ?,
                    claim_expires_at_unix_ms = ?,
                    admitted_at_unix_ms = ?,
                    send_attempt_id = ?,
                    previous_send_attempt_digest = ?,
                    last_pre_send_release_json = NULL,
                    last_pre_send_release_digest = NULL
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = ?
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND claim_generation = ?
                  AND claim_fencing_token = ?
                  AND claim_digest IS ?
                """,
                (
                    next_version,
                    next_version,
                    transaction_now,
                    claim_json,
                    claim.digest,
                    claim.claim_authority_digest,
                    claim.claim_owner_id,
                    claim.claim_generation,
                    claim.claim_fencing_token,
                    claim.claim_started_at_unix_ms,
                    claim.claim_expires_at_unix_ms,
                    claim.admitted_at_unix_ms,
                    claim.send_attempt_id,
                    claim.previous_send_attempt_digest,
                    run_internal_id,
                    record.intent.effect_id,
                    record.state.value,
                    record.state_version,
                    record.event_high_watermark,
                    record.claim_generation,
                    record.claim_fencing_token,
                    old_claim_digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProviderEffectStateConflictError(
                    "provider-effect claim projection changed concurrently"
                )
            self._hit_failpoint("claim_next_effect.after_effect_update")
            event_payload = {
                "claim": claim.to_wire(),
                "claimDigest": claim.digest,
                "effectId": record.intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": record.intent.digest,
                "state": ProviderEffectState.CLAIMED.value,
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
                VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    next_version,
                    event_kind,
                    record.state.value,
                    event_json,
                    canonical_hash(event_payload),
                    transaction_now,
                ),
            )
            self._hit_failpoint("claim_next_effect.after_event_insert")
            claimed = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=ProviderEffectState.CLAIMED,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=transaction_now,
                claim_generation=claim.claim_generation,
                claim_fencing_token=claim.claim_fencing_token,
                claim=claim,
                latest_send_attempt=record.latest_send_attempt,
                latest_admission_receipt=record.latest_admission_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=claimed,
            )
            commit_now = self._transaction_now_unix_ms()
            if commit_now < transaction_now:
                raise ValueError(
                    "provider-effect SQLite clock must remain monotonic within the "
                    "claim transaction"
                )
            if commit_now >= claim.claim_expires_at_unix_ms:
                raise ProviderEffectStateConflictError(
                    "provider-effect claim expired before commit"
                )
            return ProviderEffectWorkItem(effect=claimed, claim=claim)

        work_item = self._database._run_immediate(transition)
        self._hit_failpoint("claim_next_effect.after_commit")
        return work_item

    def release_claim_before_send(
        self,
        claim: ProviderEffectClaim,
    ) -> StoredProviderEffect:
        """Release one exact claim before provider I/O can begin."""

        if type(claim) is not ProviderEffectClaim:
            raise TypeError(
                "provider-effect SQLite release claim must be ProviderEffectClaim"
            )
        decoded_claim = ProviderEffectClaim.from_wire(claim.to_wire())
        if not _matches_exact_closed_value(claim, decoded_claim):
            raise ProviderEffectContractError(
                "provider-effect SQLite release claim is not exact"
            )
        claim = decoded_claim
        if claim.claim_authority_digest != self.claim_authority_digest:
            raise StaleProviderEffectClaimError(
                "provider-effect claim belongs to another claim authority"
            )

        def transition(connection: sqlite3.Connection) -> StoredProviderEffect:
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
                (
                    claim.tenant_id,
                    claim.run_id,
                    claim.owner_principal_id,
                    claim.effect_id,
                ),
            ).fetchone()
            if row is None:
                raise StaleProviderEffectClaimError(
                    "provider-effect claim no longer resolves to its scoped effect"
                )
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            if (
                record.origin_transfer.repository_authority_digest
                != self.origin_authority_digest
            ):
                raise ProviderEffectContractError(
                    "provider-effect release uses another origin authority"
                )
            if (
                record.state is ProviderEffectState.PENDING
                and record.last_pre_send_release is not None
                and record.last_pre_send_release.claim_digest == claim.digest
            ):
                return record
            if (
                record.state is not ProviderEffectState.CLAIMED
                or record.claim is None
                or record.claim != claim
                or record.claim.digest != claim.digest
            ):
                raise StaleProviderEffectClaimError(
                    "provider-effect claim is stale or no longer pre-send"
                )
            if record.state_version >= _MAX_SQLITE_INTEGER:
                raise ProviderEffectStateConflictError(
                    "provider-effect state version is exhausted"
                )
            transaction_now = self._transaction_now_unix_ms()
            if (
                transaction_now < record.updated_at_unix_ms
                or transaction_now < claim.claim_started_at_unix_ms
            ):
                raise ValueError(
                    "provider-effect SQLite clock moved behind the active claim"
                )
            next_version = record.state_version + 1
            release = ProviderEffectClaimRelease(
                effect_id=record.intent.effect_id,
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                claim_digest=claim.digest,
                claim_generation=claim.claim_generation,
                claim_fencing_token=claim.claim_fencing_token,
                released_at_unix_ms=transaction_now,
                resulting_state_version=next_version,
                resulting_event_sequence=next_version,
            )
            release_json = canonical_dumps(release.to_wire())
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = 'pending',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    claim_json = NULL,
                    claim_digest = NULL,
                    claim_authority_digest = NULL,
                    claim_owner_id = NULL,
                    claim_started_at_unix_ms = NULL,
                    claim_expires_at_unix_ms = NULL,
                    admitted_at_unix_ms = NULL,
                    send_attempt_id = NULL,
                    previous_send_attempt_digest = NULL,
                    last_pre_send_release_json = ?,
                    last_pre_send_release_digest = ?
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = 'claimed'
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND claim_generation = ?
                  AND claim_fencing_token = ?
                  AND claim_digest = ?
                """,
                (
                    next_version,
                    next_version,
                    transaction_now,
                    release_json,
                    release.digest,
                    run_internal_id,
                    record.intent.effect_id,
                    record.state_version,
                    record.event_high_watermark,
                    claim.claim_generation,
                    claim.claim_fencing_token,
                    claim.digest,
                ),
            )
            if updated.rowcount != 1:
                raise StaleProviderEffectClaimError(
                    "provider-effect claim changed before release"
                )
            self._hit_failpoint("release_claim_before_send.after_effect_update")
            event_payload = {
                "effectId": record.intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": record.intent.digest,
                "release": release.to_wire(),
                "releaseDigest": release.digest,
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
                VALUES (?, ?, ?, 'send_claim_released', 'claimed', 'pending', ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    next_version,
                    event_json,
                    canonical_hash(event_payload),
                    transaction_now,
                ),
            )
            self._hit_failpoint("release_claim_before_send.after_event_insert")
            released = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=ProviderEffectState.PENDING,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=transaction_now,
                claim_generation=record.claim_generation,
                claim_fencing_token=record.claim_fencing_token,
                last_pre_send_release=release,
                latest_send_attempt=record.latest_send_attempt,
                latest_admission_receipt=record.latest_admission_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=released,
            )
            commit_now = self._transaction_now_unix_ms()
            if commit_now < transaction_now:
                raise ValueError(
                    "provider-effect SQLite clock must remain monotonic within the "
                    "release transaction"
                )
            return released

        released = self._database._run_immediate(transition)
        self._hit_failpoint("release_claim_before_send.after_commit")
        return released

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
            record = self._record_from_row(connection, row)
            if type(row["run_internal_id"]) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=row["run_internal_id"],
                record=record,
            )
            return record

        return self._database._run_read(read)

    def get_active_send(
        self,
        *,
        tenant_id: str,
        run_id: str,
        owner_principal_id: str,
        effect_id: str,
    ) -> StoredProviderEffectActiveSend | None:
        """Rehydrate the active attempt and receipt without replaying send authority."""

        for field_name, value in (
            ("tenant_id", tenant_id),
            ("run_id", run_id),
            ("owner_principal_id", owner_principal_id),
            ("effect_id", effect_id),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(
                    "provider-effect SQLite active-send lookup "
                    f"{field_name} must be exact text"
                )

        def read(
            connection: sqlite3.Connection,
        ) -> StoredProviderEffectActiveSend | None:
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
            stored = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=stored,
            )
            if stored.active_send_attempt is None:
                return None
            if stored.active_admission_receipt is None:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite active receipt is missing"
                )
            attempt_row = connection.execute(
                """
                SELECT consumed_claim_digest,
                       installed_state_version,
                       installed_event_sequence
                FROM provider_effect_send_attempts
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND send_attempt_digest = ?
                  AND admission_receipt_digest = ?
                """,
                (
                    run_internal_id,
                    stored.intent.effect_id,
                    stored.active_send_attempt.digest,
                    stored.active_admission_receipt.digest,
                ),
            ).fetchone()
            if attempt_row is None:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite active send record is missing"
                )
            return StoredProviderEffectActiveSend(
                effect=stored,
                consumed_claim_digest=attempt_row["consumed_claim_digest"],
                send_attempt=stored.active_send_attempt,
                admission_receipt=stored.active_admission_receipt,
                installed_state_version=attempt_row["installed_state_version"],
                installed_event_sequence=attempt_row["installed_event_sequence"],
            )

        return self._database._run_read(read)

    def _verify_claim(
        self,
        *,
        intent: ProviderEffectIntent,
        previous_send_attempt: ProviderEffectSendAttempt | None,
        send_attempt_id: str,
        claim_owner_id: str,
        claim_generation: int,
        claim_fencing_token: int,
        claim_expires_at_unix_ms: int,
        admitted_at_unix_ms: int,
    ) -> bool:
        try:
            intent = _revalidate_provider_effect_intent(intent)
            previous_send_attempt = (
                None
                if previous_send_attempt is None
                else _revalidate_provider_effect_send_attempt(previous_send_attempt)
            )
            send_attempt_id = _require_exact_string(
                "provider-effect SQLite claim verification",
                "send_attempt_id",
                send_attempt_id,
            )
            claim_owner_id = _require_exact_string(
                "provider-effect SQLite claim verification",
                "claim_owner_id",
                claim_owner_id,
            )
            claim_generation = _require_sqlite_integer(
                "provider-effect SQLite claim verification",
                "claim_generation",
                claim_generation,
                positive=True,
            )
            claim_fencing_token = _require_sqlite_integer(
                "provider-effect SQLite claim verification",
                "claim_fencing_token",
                claim_fencing_token,
                positive=True,
            )
            claim_expires_at_unix_ms = _require_sqlite_integer(
                "provider-effect SQLite claim verification",
                "claim_expires_at_unix_ms",
                claim_expires_at_unix_ms,
                positive=True,
            )
            admitted_at_unix_ms = _require_sqlite_integer(
                "provider-effect SQLite claim verification",
                "admitted_at_unix_ms",
                admitted_at_unix_ms,
            )
        except (TypeError, ValueError):
            return False

        def read(connection: sqlite3.Connection) -> bool:
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
                (
                    intent.tenant_id,
                    intent.run_id,
                    intent.owner_principal_id,
                    intent.effect_id,
                ),
            ).fetchone()
            if row is None:
                return False
            record = self._record_from_row(connection, row)
            if type(row["run_internal_id"]) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=row["run_internal_id"],
                record=record,
            )
            claim = record.claim
            previous_digest = (
                None if previous_send_attempt is None else previous_send_attempt.digest
            )
            if (
                record.state is not ProviderEffectState.CLAIMED
                or claim is None
                or record.intent != intent
                or record.origin_transfer.repository_authority_digest
                != self.origin_authority_digest
                or claim.claim_authority_digest != self.claim_authority_digest
                or claim.send_attempt_id != send_attempt_id
                or claim.claim_owner_id != claim_owner_id
                or claim.claim_generation != claim_generation
                or claim.claim_fencing_token != claim_fencing_token
                or claim.claim_expires_at_unix_ms != claim_expires_at_unix_ms
                or claim.admitted_at_unix_ms != admitted_at_unix_ms
                or claim.previous_send_attempt_digest != previous_digest
                or record.latest_send_attempt != previous_send_attempt
                or record.active_send_attempt is not None
            ):
                return False
            transaction_now = self._transaction_now_unix_ms()
            if (
                transaction_now < record.updated_at_unix_ms
                or transaction_now < claim.claim_started_at_unix_ms
                or transaction_now < claim.admitted_at_unix_ms
            ):
                raise ValueError(
                    "provider-effect SQLite clock moved behind the active claim"
                )
            if transaction_now >= claim.claim_expires_at_unix_ms:
                return False
            verification_now = self._transaction_now_unix_ms()
            if verification_now < transaction_now:
                raise ValueError(
                    "provider-effect SQLite clock must remain monotonic within "
                    "claim verification"
                )
            return verification_now < claim.claim_expires_at_unix_ms

        return self._database._run_read(read)

    def _claim_send(
        self,
        *,
        admission: ProviderEffectAdmission,
        intent: ProviderEffectIntent,
    ) -> tuple[ProviderEffectSendAttempt, ProviderEffectAdmissionReceipt] | None:
        if (
            type(admission) is not ProviderEffectAdmission
            or type(intent) is not ProviderEffectIntent
        ):
            return None
        try:
            intent = _revalidate_provider_effect_intent(intent)
        except (TypeError, ValueError):
            return None
        if admission.claim_authority_digest != self.claim_authority_digest:
            return None

        def transition(
            connection: sqlite3.Connection,
        ) -> tuple[ProviderEffectSendAttempt, ProviderEffectAdmissionReceipt] | None:
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
                (
                    intent.tenant_id,
                    intent.run_id,
                    intent.owner_principal_id,
                    intent.effect_id,
                ),
            ).fetchone()
            if row is None:
                return None
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            claim = record.claim
            latest_digest = (
                None
                if record.latest_send_attempt is None
                else record.latest_send_attempt.digest
            )
            latest_receipt_digest = (
                None
                if record.latest_admission_receipt is None
                else record.latest_admission_receipt.digest
            )
            if (
                record.state is not ProviderEffectState.CLAIMED
                or claim is None
                or record.intent != intent
                or record.origin_transfer.repository_authority_digest
                != self.origin_authority_digest
                or admission.intent_digest != intent.digest
                or admission.capability_snapshot_digest != record.capability.digest
                or admission.capability_authority_digest
                != record.capability.authority_digest
                or admission.applicable_methods
                != _applicable_reconciliation_methods(intent, record.capability)
                or admission.origin_transfer_digest != record.origin_transfer.digest
                or admission.origin_authority_verifier_digest
                != self.origin_authority_digest
                or admission.send_attempt_id != claim.send_attempt_id
                or admission.claim_owner_id != claim.claim_owner_id
                or admission.claim_generation != claim.claim_generation
                or admission.claim_fencing_token != claim.claim_fencing_token
                or admission.claim_expires_at_unix_ms != claim.claim_expires_at_unix_ms
                or admission.admitted_at_unix_ms != claim.admitted_at_unix_ms
                or admission.previous_send_attempt_digest != latest_digest
                or claim.previous_send_attempt_digest != latest_digest
                or record.active_send_attempt is not None
            ):
                return None
            send_started_at = self._transaction_now_unix_ms()
            if (
                send_started_at < record.updated_at_unix_ms
                or send_started_at < claim.claim_started_at_unix_ms
                or send_started_at < claim.admitted_at_unix_ms
            ):
                raise ValueError(
                    "provider-effect SQLite clock moved behind the active claim"
                )
            if send_started_at >= claim.claim_expires_at_unix_ms:
                return None
            send_attempt = ProviderEffectSendAttempt(
                effect_id=intent.effect_id,
                intent_digest=intent.digest,
                capability_snapshot_digest=record.capability.digest,
                admission_digest=admission.digest,
                claim_authority_digest=self.claim_authority_digest,
                attempt_id=claim.send_attempt_id,
                claim_owner_id=claim.claim_owner_id,
                claim_generation=claim.claim_generation,
                claim_fencing_token=claim.claim_fencing_token,
                started_at_unix_ms=send_started_at,
            )
            consumed_at = self._transaction_now_unix_ms()
            if consumed_at < send_started_at:
                raise ValueError("provider-effect SQLite clock moved behind send start")
            if consumed_at >= claim.claim_expires_at_unix_ms:
                return None
            admission_receipt = ProviderEffectAdmissionReceipt.from_consumed(
                admission,
                send_attempt,
                consumed_at_unix_ms=consumed_at,
            )
            if record.state_version >= _MAX_SQLITE_INTEGER:
                raise ProviderEffectStateConflictError(
                    "provider-effect state version is exhausted"
                )
            next_version = record.state_version + 1
            connection.execute(
                """
                INSERT INTO provider_effect_send_attempts (
                  run_internal_id,
                  effect_id,
                  attempt_id,
                  admission_digest,
                  consumed_claim_digest,
                  send_attempt_json,
                  send_attempt_digest,
                  admission_receipt_json,
                  admission_receipt_digest,
                  previous_send_attempt_digest,
                  claim_owner_id,
                  claim_generation,
                  claim_fencing_token,
                  started_at_unix_ms,
                  consumed_at_unix_ms,
                  installed_state_version,
                  installed_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    intent.effect_id,
                    send_attempt.attempt_id,
                    admission.digest,
                    claim.digest,
                    canonical_dumps(send_attempt.to_wire()),
                    send_attempt.digest,
                    canonical_dumps(admission_receipt.to_wire()),
                    admission_receipt.digest,
                    admission.previous_send_attempt_digest,
                    claim.claim_owner_id,
                    claim.claim_generation,
                    claim.claim_fencing_token,
                    send_started_at,
                    consumed_at,
                    next_version,
                    next_version,
                ),
            )
            self._hit_failpoint("claim_send.after_attempt_insert")
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = 'send_started',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    claim_json = NULL,
                    claim_digest = NULL,
                    claim_authority_digest = NULL,
                    claim_owner_id = NULL,
                    claim_started_at_unix_ms = NULL,
                    claim_expires_at_unix_ms = NULL,
                    admitted_at_unix_ms = NULL,
                    send_attempt_id = NULL,
                    previous_send_attempt_digest = NULL,
                    last_pre_send_release_json = NULL,
                    last_pre_send_release_digest = NULL,
                    latest_send_attempt_digest = ?,
                    latest_admission_receipt_digest = ?,
                    active_send_attempt_digest = ?,
                    active_admission_receipt_digest = ?
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = 'claimed'
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND claim_digest = ?
                  AND claim_authority_digest = ?
                  AND claim_owner_id = ?
                  AND claim_generation = ?
                  AND claim_fencing_token = ?
                  AND claim_expires_at_unix_ms = ?
                  AND admitted_at_unix_ms = ?
                  AND send_attempt_id = ?
                  AND previous_send_attempt_digest IS ?
                  AND latest_send_attempt_digest IS ?
                  AND latest_admission_receipt_digest IS ?
                  AND active_send_attempt_digest IS NULL
                  AND active_admission_receipt_digest IS NULL
                """,
                (
                    next_version,
                    next_version,
                    consumed_at,
                    send_attempt.digest,
                    admission_receipt.digest,
                    send_attempt.digest,
                    admission_receipt.digest,
                    run_internal_id,
                    intent.effect_id,
                    record.state_version,
                    record.event_high_watermark,
                    claim.digest,
                    self.claim_authority_digest,
                    claim.claim_owner_id,
                    claim.claim_generation,
                    claim.claim_fencing_token,
                    claim.claim_expires_at_unix_ms,
                    claim.admitted_at_unix_ms,
                    claim.send_attempt_id,
                    claim.previous_send_attempt_digest,
                    latest_digest,
                    latest_receipt_digest,
                ),
            )
            if updated.rowcount != 1:
                raise StaleProviderEffectClaimError(
                    "provider-effect claim changed before send entry"
                )
            self._hit_failpoint("claim_send.after_effect_update")
            event_payload = {
                "admissionReceipt": admission_receipt.to_wire(),
                "admissionReceiptDigest": admission_receipt.digest,
                "consumedClaimDigest": claim.digest,
                "effectId": intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": intent.digest,
                "sendAttempt": send_attempt.to_wire(),
                "sendAttemptDigest": send_attempt.digest,
                "state": ProviderEffectState.SEND_STARTED.value,
            }
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
                VALUES (
                  ?, ?, ?, 'send_started', 'claimed', 'send_started', ?, ?, ?
                )
                """,
                (
                    run_internal_id,
                    intent.effect_id,
                    next_version,
                    canonical_dumps(event_payload),
                    canonical_hash(event_payload),
                    consumed_at,
                ),
            )
            self._hit_failpoint("claim_send.after_event_insert")
            started = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=ProviderEffectState.SEND_STARTED,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=consumed_at,
                claim_generation=claim.claim_generation,
                claim_fencing_token=claim.claim_fencing_token,
                latest_send_attempt=send_attempt,
                latest_admission_receipt=admission_receipt,
                active_send_attempt=send_attempt,
                active_admission_receipt=admission_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=started,
            )
            commit_now = self._transaction_now_unix_ms()
            if commit_now < consumed_at:
                raise ValueError(
                    "provider-effect SQLite clock must remain monotonic within "
                    "send entry"
                )
            if commit_now >= claim.claim_expires_at_unix_ms:
                raise ProviderEffectStateConflictError(
                    "provider-effect claim expired before send-entry commit"
                )
            return send_attempt, admission_receipt

        claimed_send = self._database._run_immediate(transition)
        self._hit_failpoint("claim_send.after_commit")
        return claimed_send

    def _verify_active_send(
        self,
        *,
        current: ProviderEffectState,
        admission_receipt: ProviderEffectAdmissionReceipt,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        if (
            type(current) is not ProviderEffectState
            or current
            not in {
                ProviderEffectState.SEND_STARTED,
                ProviderEffectState.RECONCILING,
            }
            or type(admission_receipt) is not ProviderEffectAdmissionReceipt
            or type(send_attempt) is not ProviderEffectSendAttempt
        ):
            return False
        try:
            decoded_receipt = _revalidate_provider_effect_admission_receipt(
                admission_receipt
            )
            decoded_attempt = _revalidate_provider_effect_send_attempt(send_attempt)
        except (TypeError, ValueError):
            return False
        if (
            not _matches_exact_closed_value(admission_receipt, decoded_receipt)
            or not _matches_exact_closed_value(send_attempt, decoded_attempt)
            or decoded_receipt.claim_authority_digest != self.claim_authority_digest
            or decoded_attempt.claim_authority_digest != self.claim_authority_digest
            or decoded_receipt.send_attempt_digest != decoded_attempt.digest
        ):
            return False

        def read(connection: sqlite3.Connection) -> bool:
            rows = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effect_send_attempts
                JOIN provider_effects
                  ON provider_effects.run_internal_id =
                       provider_effect_send_attempts.run_internal_id
                 AND provider_effects.effect_id =
                       provider_effect_send_attempts.effect_id
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE provider_effect_send_attempts.send_attempt_digest = ?
                  AND provider_effect_send_attempts.admission_receipt_digest = ?
                  AND provider_effects.active_send_attempt_digest =
                      provider_effect_send_attempts.send_attempt_digest
                  AND provider_effects.active_admission_receipt_digest =
                      provider_effect_send_attempts.admission_receipt_digest
                LIMIT 2
                """,
                (decoded_attempt.digest, decoded_receipt.digest),
            ).fetchall()
            if not rows:
                return False
            if len(rows) != 1:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect active send identity is not unique"
                )
            row = rows[0]
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            return bool(
                record.state is current
                and record.active_send_attempt == decoded_attempt
                and record.active_admission_receipt == decoded_receipt
                and record.latest_send_attempt == decoded_attempt
                and record.latest_admission_receipt == decoded_receipt
                and record.origin_transfer.repository_authority_digest
                == self.origin_authority_digest
            )

        return self._database._run_read(read)

    def _settle_active_send(
        self,
        *,
        current: ProviderEffectState,
        next_state: ProviderEffectState,
        admission_receipt: ProviderEffectAdmissionReceipt,
        send_attempt: ProviderEffectSendAttempt,
        evidence: ProviderReconciliationEvidence,
    ) -> bool:
        if (
            type(current) is not ProviderEffectState
            or type(next_state) is not ProviderEffectState
            or type(admission_receipt) is not ProviderEffectAdmissionReceipt
            or type(send_attempt) is not ProviderEffectSendAttempt
            or type(evidence) is not ProviderReconciliationEvidence
        ):
            return False
        try:
            decoded_receipt = _revalidate_provider_effect_admission_receipt(
                admission_receipt
            )
            decoded_attempt = _revalidate_provider_effect_send_attempt(send_attempt)
            decoded_evidence = ProviderReconciliationEvidence.from_wire(
                evidence.to_wire()
            )
        except (TypeError, ValueError):
            return False
        expected_state = _reconciliation_settlement_state(
            current,
            decoded_evidence.outcome,
        )
        if (
            not _matches_exact_closed_value(admission_receipt, decoded_receipt)
            or not _matches_exact_closed_value(send_attempt, decoded_attempt)
            or not _matches_exact_closed_value(evidence, decoded_evidence)
            or expected_state is None
            or next_state is not expected_state
            or decoded_receipt.claim_authority_digest != self.claim_authority_digest
            or decoded_attempt.claim_authority_digest != self.claim_authority_digest
            or decoded_receipt.send_attempt_digest != decoded_attempt.digest
            or decoded_evidence.effect_id != decoded_attempt.effect_id
            or decoded_evidence.intent_digest != decoded_attempt.intent_digest
            or decoded_evidence.capability_snapshot_digest
            != decoded_attempt.capability_snapshot_digest
            or decoded_evidence.send_attempt_digest != decoded_attempt.digest
            or decoded_evidence.method not in decoded_receipt.applicable_methods
            or decoded_evidence.observed_at_unix_ms < decoded_attempt.started_at_unix_ms
            or decoded_evidence.observed_at_unix_ms > _MAX_SQLITE_INTEGER
        ):
            return False

        evidence_json = canonical_dumps(decoded_evidence.to_wire())
        terminal = expected_state in {
            ProviderEffectState.CONFIRMED_COMMITTED,
            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
            ProviderEffectState.CONFIRMED_CANCELLED,
        }

        def transition(connection: sqlite3.Connection) -> bool:
            replay = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id,
                       provider_effect_reconciliation_evidence.evidence_json
                         AS reconciliation_evidence_json,
                       provider_effect_reconciliation_evidence.send_attempt_digest
                         AS reconciliation_send_attempt_digest,
                       provider_effect_reconciliation_evidence.
                         admission_receipt_digest
                         AS reconciliation_admission_receipt_digest,
                       provider_effect_reconciliation_evidence.from_state
                         AS reconciliation_from_state,
                       provider_effect_reconciliation_evidence.to_state
                         AS reconciliation_to_state,
                       provider_effect_reconciliation_evidence.observed_at_unix_ms
                         AS reconciliation_observed_at_unix_ms,
                       provider_effect_reconciliation_evidence.
                         installed_state_version
                         AS reconciliation_state_version,
                       provider_effect_reconciliation_evidence.
                         installed_event_sequence
                         AS reconciliation_event_sequence
                FROM provider_effect_reconciliation_evidence
                JOIN provider_effects
                  ON provider_effects.run_internal_id =
                       provider_effect_reconciliation_evidence.run_internal_id
                 AND provider_effects.effect_id =
                       provider_effect_reconciliation_evidence.effect_id
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE provider_effect_reconciliation_evidence.evidence_digest = ?
                LIMIT 2
                """,
                (decoded_evidence.digest,),
            ).fetchall()
            if replay:
                if len(replay) != 1:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect reconciliation evidence is not unique"
                    )
                row = replay[0]
                record = self._record_from_row(connection, row)
                run_internal_id = row["run_internal_id"]
                if type(run_internal_id) is not str:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite run identity is not text"
                    )
                self._assert_projection_tail(
                    connection,
                    run_internal_id=run_internal_id,
                    record=record,
                )
                active_attempt = None if terminal else decoded_attempt.digest
                active_receipt = None if terminal else decoded_receipt.digest
                if (
                    record.intent.effect_id != decoded_evidence.effect_id
                    or row["reconciliation_evidence_json"] != evidence_json
                    or row["reconciliation_send_attempt_digest"]
                    != decoded_attempt.digest
                    or row["reconciliation_admission_receipt_digest"]
                    != decoded_receipt.digest
                    or row["reconciliation_from_state"] != current.value
                    or row["reconciliation_to_state"] != expected_state.value
                    or row["reconciliation_observed_at_unix_ms"]
                    != decoded_evidence.observed_at_unix_ms
                ):
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect reconciliation replay changed identity"
                    )
                return bool(
                    record.state is expected_state
                    and record.state_version == row["reconciliation_state_version"]
                    and record.event_high_watermark
                    == row["reconciliation_event_sequence"]
                    and record.latest_send_attempt == decoded_attempt
                    and record.latest_admission_receipt == decoded_receipt
                    and row["active_send_attempt_digest"] == active_attempt
                    and row["active_admission_receipt_digest"] == active_receipt
                )

            rows = connection.execute(
                """
                SELECT provider_effects.*,
                       accepted_runs.tenant_id,
                       accepted_runs.external_run_id,
                       accepted_runs.owner_principal_id
                FROM provider_effect_send_attempts
                JOIN provider_effects
                  ON provider_effects.run_internal_id =
                       provider_effect_send_attempts.run_internal_id
                 AND provider_effects.effect_id =
                       provider_effect_send_attempts.effect_id
                JOIN accepted_runs
                  ON accepted_runs.internal_id = provider_effects.run_internal_id
                WHERE provider_effect_send_attempts.send_attempt_digest = ?
                  AND provider_effect_send_attempts.admission_receipt_digest = ?
                  AND provider_effects.active_send_attempt_digest =
                      provider_effect_send_attempts.send_attempt_digest
                  AND provider_effects.active_admission_receipt_digest =
                      provider_effect_send_attempts.admission_receipt_digest
                LIMIT 2
                """,
                (decoded_attempt.digest, decoded_receipt.digest),
            ).fetchall()
            if not rows:
                return False
            if len(rows) != 1:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect active send identity is not unique"
                )
            row = rows[0]
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            correlation_required = (
                decoded_evidence.method is ProviderReconciliationMethod.STATUS_LOOKUP
                and record.capability.status_lookup
                is ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID
            ) or (
                decoded_evidence.method
                is ProviderReconciliationMethod.CONFIRMED_CANCELLATION
                and record.capability.cancellation
                is ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID
            )
            if (
                record.state is not current
                or record.active_send_attempt != decoded_attempt
                or record.active_admission_receipt != decoded_receipt
                or record.latest_send_attempt != decoded_attempt
                or record.latest_admission_receipt != decoded_receipt
                or record.origin_transfer.repository_authority_digest
                != self.origin_authority_digest
                or decoded_evidence.effect_id != record.intent.effect_id
                or decoded_evidence.intent_digest != record.intent.digest
                or decoded_evidence.capability_snapshot_digest
                != record.capability.digest
                or decoded_evidence.verifier_id
                != record.capability.reconciliation_verifier_id
                or decoded_evidence.verifier_release_digest
                != record.capability.reconciliation_verifier_release_digest
                or decoded_evidence.verification_authority_digest
                != record.capability.reconciliation_verification_authority_digest
                or (
                    record.intent.provider_correlation_id is not None
                    and decoded_evidence.provider_correlation_id is not None
                    and decoded_evidence.provider_correlation_id
                    != record.intent.provider_correlation_id
                )
                or (
                    correlation_required
                    and decoded_evidence.provider_correlation_id
                    != record.intent.provider_correlation_id
                )
            ):
                return False
            settled_at = self._transaction_now_unix_ms()
            if settled_at < max(
                record.updated_at_unix_ms,
                decoded_evidence.observed_at_unix_ms,
            ):
                raise ValueError(
                    "provider-effect SQLite clock moved behind reconciliation evidence"
                )
            if record.state_version >= _MAX_SQLITE_INTEGER:
                raise ProviderEffectStateConflictError(
                    "provider-effect state version is exhausted"
                )
            next_version = record.state_version + 1
            connection.execute(
                """
                INSERT INTO provider_effect_reconciliation_evidence (
                  run_internal_id,
                  effect_id,
                  evidence_digest,
                  evidence_json,
                  send_attempt_digest,
                  admission_receipt_digest,
                  from_state,
                  to_state,
                  observed_at_unix_ms,
                  settled_at_unix_ms,
                  installed_state_version,
                  installed_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    decoded_evidence.digest,
                    evidence_json,
                    decoded_attempt.digest,
                    decoded_receipt.digest,
                    current.value,
                    expected_state.value,
                    decoded_evidence.observed_at_unix_ms,
                    settled_at,
                    next_version,
                    next_version,
                ),
            )
            self._hit_failpoint("settle_active_send.after_evidence_insert")
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = ?,
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?,
                    active_send_attempt_digest = ?,
                    active_admission_receipt_digest = ?
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = ?
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND latest_send_attempt_digest = ?
                  AND latest_admission_receipt_digest = ?
                  AND active_send_attempt_digest = ?
                  AND active_admission_receipt_digest = ?
                """,
                (
                    expected_state.value,
                    next_version,
                    next_version,
                    settled_at,
                    None if terminal else decoded_attempt.digest,
                    None if terminal else decoded_receipt.digest,
                    run_internal_id,
                    record.intent.effect_id,
                    current.value,
                    record.state_version,
                    record.event_high_watermark,
                    decoded_attempt.digest,
                    decoded_receipt.digest,
                    decoded_attempt.digest,
                    decoded_receipt.digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProviderEffectStateConflictError(
                    "provider-effect active send changed during settlement"
                )
            self._hit_failpoint("settle_active_send.after_effect_update")
            event_payload = {
                "admissionReceiptDigest": decoded_receipt.digest,
                "effectId": record.intent.effect_id,
                "evidence": decoded_evidence.to_wire(),
                "evidenceDigest": decoded_evidence.digest,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": record.intent.digest,
                "sendAttemptDigest": decoded_attempt.digest,
                "state": expected_state.value,
            }
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
                VALUES (?, ?, ?, 'reconciliation_evidence_applied', ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    next_version,
                    current.value,
                    expected_state.value,
                    canonical_dumps(event_payload),
                    canonical_hash(event_payload),
                    settled_at,
                ),
            )
            self._hit_failpoint("settle_active_send.after_event_insert")
            settled = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=expected_state,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=settled_at,
                claim_generation=record.claim_generation,
                claim_fencing_token=record.claim_fencing_token,
                latest_send_attempt=decoded_attempt,
                latest_admission_receipt=decoded_receipt,
                active_send_attempt=None if terminal else decoded_attempt,
                active_admission_receipt=None if terminal else decoded_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=settled,
            )
            return True

        settled = self._database._run_immediate(transition)
        self._hit_failpoint("settle_active_send.after_commit")
        return settled

    def apply_reconciliation_control(
        self,
        control: ProviderEffectReconciliationControl,
    ) -> StoredProviderEffect:
        """Atomically apply one idempotent operator transition to an active send."""

        if type(control) is not ProviderEffectReconciliationControl:
            raise TypeError("provider-effect reconciliation control must be exact")
        control = ProviderEffectReconciliationControl.from_wire(control.to_wire())

        def transition(connection: sqlite3.Connection) -> StoredProviderEffect:
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
                (
                    control.tenant_id,
                    control.run_id,
                    control.owner_principal_id,
                    control.effect_id,
                ),
            ).fetchone()
            if row is None:
                raise ProviderEffectStateConflictError(
                    "provider-effect reconciliation target is not available"
                )
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            replay = connection.execute(
                """
                SELECT *
                FROM provider_effect_reconciliation_controls
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND control_id = ?
                """,
                (run_internal_id, control.effect_id, control.control_id),
            ).fetchone()
            if replay is not None:
                if (
                    replay["request_digest"] != control.digest
                    or replay["transition"] != control.transition.value
                    or replay["requested_state_version"]
                    != control.expected_state_version
                ):
                    raise ProviderEffectIdentityConflictError(
                        "provider-effect reconciliation control identity was reused"
                    )
                if (
                    record.state_version != replay["installed_state_version"]
                    or record.event_high_watermark != replay["installed_event_sequence"]
                    or record.state.value != replay["to_state"]
                ):
                    raise ProviderEffectStateConflictError(
                        "provider-effect reconciliation control replay was superseded"
                    )
                return record
            if record.state_version != control.expected_state_version:
                raise ProviderEffectStateConflictError(
                    "provider-effect reconciliation control state version is stale"
                )
            if (
                record.active_send_attempt is None
                or record.active_admission_receipt is None
            ):
                raise ProviderEffectStateConflictError(
                    "provider-effect reconciliation control requires an active send"
                )
            next_state = transition_provider_effect_state(
                record.state,
                control.transition,
            )
            if record.state_version >= _MAX_SQLITE_INTEGER:
                raise ProviderEffectStateConflictError(
                    "provider-effect state version is exhausted"
                )
            applied_at = self._transaction_now_unix_ms()
            if applied_at < record.updated_at_unix_ms:
                raise ValueError(
                    "provider-effect SQLite clock moved behind the projection"
                )
            next_version = record.state_version + 1
            connection.execute(
                """
                INSERT INTO provider_effect_reconciliation_controls (
                  run_internal_id,
                  effect_id,
                  control_id,
                  request_digest,
                  transition,
                  from_state,
                  to_state,
                  requested_state_version,
                  applied_at_unix_ms,
                  installed_state_version,
                  installed_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    control.control_id,
                    control.digest,
                    control.transition.value,
                    record.state.value,
                    next_state.value,
                    control.expected_state_version,
                    applied_at,
                    next_version,
                    next_version,
                ),
            )
            self._hit_failpoint("apply_reconciliation_control.after_control_insert")
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = ?,
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = ?
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND active_send_attempt_digest = ?
                  AND active_admission_receipt_digest = ?
                """,
                (
                    next_state.value,
                    next_version,
                    next_version,
                    applied_at,
                    run_internal_id,
                    record.intent.effect_id,
                    record.state.value,
                    record.state_version,
                    record.event_high_watermark,
                    record.active_send_attempt.digest,
                    record.active_admission_receipt.digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProviderEffectStateConflictError(
                    "provider-effect reconciliation projection changed concurrently"
                )
            self._hit_failpoint("apply_reconciliation_control.after_effect_update")
            event_payload = {
                "admissionReceiptDigest": record.active_admission_receipt.digest,
                "control": control.to_wire(),
                "controlDigest": control.digest,
                "effectId": record.intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": record.intent.digest,
                "sendAttemptDigest": record.active_send_attempt.digest,
                "state": next_state.value,
            }
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
                VALUES (?, ?, ?, 'reconciliation_control_applied', ?, ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    next_version,
                    record.state.value,
                    next_state.value,
                    canonical_dumps(event_payload),
                    canonical_hash(event_payload),
                    applied_at,
                ),
            )
            self._hit_failpoint("apply_reconciliation_control.after_event_insert")
            controlled = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=next_state,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=applied_at,
                claim_generation=record.claim_generation,
                claim_fencing_token=record.claim_fencing_token,
                latest_send_attempt=record.latest_send_attempt,
                latest_admission_receipt=record.latest_admission_receipt,
                active_send_attempt=record.active_send_attempt,
                active_admission_receipt=record.active_admission_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=controlled,
            )
            return controlled

        controlled = self._database._run_immediate(transition)
        self._hit_failpoint("apply_reconciliation_control.after_commit")
        return controlled

    def retry_confirmed_effect(
        self,
        command: ProviderEffectRetryCommand,
        requested_intent: ProviderEffectIntent,
    ) -> StoredProviderEffect:
        """Return a confirmed-safe effect to pending without changing its intent."""

        if type(command) is not ProviderEffectRetryCommand:
            raise TypeError("provider-effect retry command must be exact")
        if type(requested_intent) is not ProviderEffectIntent:
            raise TypeError("provider-effect retry intent must be exact")
        command = ProviderEffectRetryCommand.from_wire(command.to_wire())
        requested_intent = _revalidate_provider_effect_intent(requested_intent)
        if (
            command.tenant_id != requested_intent.tenant_id
            or command.run_id != requested_intent.run_id
            or command.owner_principal_id != requested_intent.owner_principal_id
            or command.effect_id != requested_intent.effect_id
            or command.intent_digest != requested_intent.digest
        ):
            raise ProviderEffectIdentityConflictError(
                "provider-effect retry command does not match its intent"
            )

        def transition(connection: sqlite3.Connection) -> StoredProviderEffect:
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
                (
                    command.tenant_id,
                    command.run_id,
                    command.owner_principal_id,
                    command.effect_id,
                ),
            ).fetchone()
            if row is None:
                raise ProviderEffectStateConflictError(
                    "provider-effect retry target is not available"
                )
            record = self._record_from_row(connection, row)
            run_internal_id = row["run_internal_id"]
            if type(run_internal_id) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=record,
            )
            replay = connection.execute(
                """
                SELECT *
                FROM provider_effect_retry_commands
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND retry_id = ?
                """,
                (run_internal_id, command.effect_id, command.retry_id),
            ).fetchone()
            if replay is not None:
                if (
                    replay["command_digest"] != command.digest
                    or replay["command_json"] != canonical_dumps(command.to_wire())
                    or replay["intent_digest"] != requested_intent.digest
                    or replay["requested_state_version"]
                    != command.expected_state_version
                ):
                    raise ProviderEffectIdentityConflictError(
                        "provider-effect retry identity was reused"
                    )
                if (
                    record.state is not ProviderEffectState.PENDING
                    or record.state_version != replay["installed_state_version"]
                    or record.event_high_watermark != replay["installed_event_sequence"]
                ):
                    raise ProviderEffectStateConflictError(
                        "provider-effect retry replay was superseded"
                    )
                return record
            if record.state_version != command.expected_state_version:
                raise ProviderEffectStateConflictError(
                    "provider-effect retry state version is stale"
                )
            if (
                record.latest_send_attempt is None
                or record.latest_admission_receipt is None
                or record.active_send_attempt is not None
                or record.active_admission_receipt is not None
            ):
                raise ProviderEffectStateConflictError(
                    "provider-effect retry requires a settled send attempt"
                )
            next_state = retry_same_provider_effect_intent(
                record.state,
                record.intent,
                requested_intent,
            )
            if record.state_version >= _MAX_SQLITE_INTEGER:
                raise ProviderEffectStateConflictError(
                    "provider-effect state version is exhausted"
                )
            applied_at = self._transaction_now_unix_ms()
            if applied_at < record.updated_at_unix_ms:
                raise ValueError(
                    "provider-effect SQLite clock moved behind the projection"
                )
            next_version = record.state_version + 1
            command_json = canonical_dumps(command.to_wire())
            connection.execute(
                """
                INSERT INTO provider_effect_retry_commands (
                  run_internal_id,
                  effect_id,
                  retry_id,
                  command_digest,
                  command_json,
                  intent_digest,
                  previous_send_attempt_digest,
                  from_state,
                  to_state,
                  requested_state_version,
                  applied_at_unix_ms,
                  installed_state_version,
                  installed_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    command.retry_id,
                    command.digest,
                    command_json,
                    record.intent.digest,
                    record.latest_send_attempt.digest,
                    record.state.value,
                    command.expected_state_version,
                    applied_at,
                    next_version,
                    next_version,
                ),
            )
            self._hit_failpoint("retry_confirmed_effect.after_command_insert")
            updated = connection.execute(
                """
                UPDATE provider_effects
                SET state = 'pending',
                    state_version = ?,
                    event_high_watermark = ?,
                    updated_at_unix_ms = ?
                WHERE run_internal_id = ?
                  AND effect_id = ?
                  AND state = ?
                  AND state_version = ?
                  AND event_high_watermark = ?
                  AND latest_send_attempt_digest = ?
                  AND latest_admission_receipt_digest = ?
                  AND active_send_attempt_digest IS NULL
                  AND active_admission_receipt_digest IS NULL
                """,
                (
                    next_version,
                    next_version,
                    applied_at,
                    run_internal_id,
                    record.intent.effect_id,
                    record.state.value,
                    record.state_version,
                    record.event_high_watermark,
                    record.latest_send_attempt.digest,
                    record.latest_admission_receipt.digest,
                ),
            )
            if updated.rowcount != 1:
                raise ProviderEffectStateConflictError(
                    "provider-effect retry projection changed concurrently"
                )
            self._hit_failpoint("retry_confirmed_effect.after_effect_update")
            event_payload = {
                "effectId": record.intent.effect_id,
                "formatVersion": PROVIDER_EFFECT_EVENT_FORMAT_VERSION,
                "intentDigest": record.intent.digest,
                "previousSendAttemptDigest": record.latest_send_attempt.digest,
                "retryCommand": command.to_wire(),
                "retryCommandDigest": command.digest,
                "state": next_state.value,
            }
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
                VALUES (?, ?, ?, 'retry_same_intent_applied', ?, 'pending', ?, ?, ?)
                """,
                (
                    run_internal_id,
                    record.intent.effect_id,
                    next_version,
                    record.state.value,
                    canonical_dumps(event_payload),
                    canonical_hash(event_payload),
                    applied_at,
                ),
            )
            self._hit_failpoint("retry_confirmed_effect.after_event_insert")
            retried = StoredProviderEffect(
                tenant_id=record.tenant_id,
                run_id=record.run_id,
                owner_principal_id=record.owner_principal_id,
                intent=record.intent,
                capability=record.capability,
                origin_transfer=record.origin_transfer,
                state=next_state,
                state_version=next_version,
                event_high_watermark=next_version,
                created_at_unix_ms=record.created_at_unix_ms,
                updated_at_unix_ms=applied_at,
                claim_generation=record.claim_generation,
                claim_fencing_token=record.claim_fencing_token,
                latest_send_attempt=record.latest_send_attempt,
                latest_admission_receipt=record.latest_admission_receipt,
            )
            self._assert_projection_tail(
                connection,
                run_internal_id=run_internal_id,
                record=retried,
            )
            return retried

        retried = self._database._run_immediate(transition)
        self._hit_failpoint("retry_confirmed_effect.after_commit")
        return retried

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
            record = self._record_from_row(connection, effect_row)
            if type(effect_row["run_internal_id"]) is not str:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite run identity is not text"
                )
            self._assert_projection_tail(
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
                  AND provider_effect_events.sequence <= ?
                ORDER BY provider_effect_events.sequence
                LIMIT ?
                """,
                (
                    tenant_id,
                    run_id,
                    owner_principal_id,
                    effect_id,
                    after_sequence,
                    record.event_high_watermark,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            decoded_events = tuple(self._event_from_row(row) for row in rows)
            if not decoded_events and after_sequence < record.event_high_watermark:
                raise SQLiteProviderEffectCorruptionError(
                    "provider-effect SQLite event page has a missing suffix"
                )
            previous_event: StoredProviderEffectEvent | None = None
            previous_claim: ProviderEffectClaim | None = None
            previous_generation = 0
            previous_fencing_token = 0
            if decoded_events and after_sequence > 0:
                previous_row = connection.execute(
                    """
                    SELECT *
                    FROM provider_effect_events
                    WHERE run_internal_id = ?
                      AND effect_id = ?
                      AND sequence = ?
                    """,
                    (
                        effect_row["run_internal_id"],
                        record.intent.effect_id,
                        after_sequence,
                    ),
                ).fetchone()
                if previous_row is None:
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite event page cursor has no predecessor"
                    )
                previous_event = self._event_from_row(previous_row)
                previous_payload = self._event_payload_for_record(
                    previous_event,
                    record,
                )
                if previous_event.kind in {"send_claimed", "send_claim_reclaimed"}:
                    previous_claim = ProviderEffectClaim.from_wire(
                        previous_payload["claim"]
                    )
                    previous_generation = previous_claim.claim_generation
                    previous_fencing_token = previous_claim.claim_fencing_token
                elif previous_event.kind == "send_claim_released":
                    previous_release = ProviderEffectClaimRelease.from_wire(
                        previous_payload["release"]
                    )
                    previous_generation = previous_release.claim_generation
                    previous_fencing_token = previous_release.claim_fencing_token
                elif previous_event.kind == "send_started":
                    previous_attempt = ProviderEffectSendAttempt.from_wire(
                        previous_payload["sendAttempt"]
                    )
                    previous_generation = previous_attempt.claim_generation
                    previous_fencing_token = previous_attempt.claim_fencing_token
            for offset, event in enumerate(decoded_events, start=1):
                payload = self._event_payload_for_record(event, record)
                if (
                    event.sequence != after_sequence + offset
                    or event.from_state
                    is not (None if previous_event is None else previous_event.to_state)
                    or (
                        previous_event is not None
                        and event.created_at_unix_ms < previous_event.created_at_unix_ms
                    )
                ):
                    raise SQLiteProviderEffectCorruptionError(
                        "provider-effect SQLite event page is not contiguous"
                    )
                if event.kind == "origin_transferred":
                    if previous_event is not None:
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite origin event is not first"
                        )
                elif event.kind in {"send_claimed", "send_claim_reclaimed"}:
                    claim = ProviderEffectClaim.from_wire(payload["claim"])
                    if (
                        claim.claim_generation != previous_generation + 1
                        or claim.claim_fencing_token != previous_fencing_token + 1
                        or (
                            event.kind == "send_claim_reclaimed"
                            and (
                                previous_claim is None
                                or previous_claim.claim_expires_at_unix_ms
                                > claim.claim_started_at_unix_ms
                            )
                        )
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite event claim authority is not "
                            "contiguous"
                        )
                    previous_claim = claim
                    previous_generation = claim.claim_generation
                    previous_fencing_token = claim.claim_fencing_token
                elif event.kind == "send_claim_released":
                    release = ProviderEffectClaimRelease.from_wire(payload["release"])
                    if (
                        previous_claim is None
                        or release.claim_digest != previous_claim.digest
                        or release.claim_generation != previous_generation
                        or release.claim_fencing_token != previous_fencing_token
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite release authority is not contiguous"
                        )
                    previous_claim = None
                elif event.kind == "send_started":
                    attempt = ProviderEffectSendAttempt.from_wire(
                        payload["sendAttempt"]
                    )
                    receipt = ProviderEffectAdmissionReceipt.from_wire(
                        payload["admissionReceipt"]
                    )
                    if (
                        previous_claim is None
                        or payload["consumedClaimDigest"] != previous_claim.digest
                        or attempt.claim_generation != previous_generation
                        or attempt.claim_fencing_token != previous_fencing_token
                        or attempt.attempt_id != previous_claim.send_attempt_id
                        or attempt.claim_owner_id != previous_claim.claim_owner_id
                        or attempt.started_at_unix_ms
                        < previous_claim.admitted_at_unix_ms
                        or attempt.started_at_unix_ms
                        >= previous_claim.claim_expires_at_unix_ms
                        or receipt.send_attempt_digest != attempt.digest
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite send-start authority is not "
                            "contiguous"
                        )
                    previous_claim = None
                elif event.kind == "reconciliation_evidence_applied":
                    evidence = ProviderReconciliationEvidence.from_wire(
                        payload["evidence"]
                    )
                    expected_state = (
                        None
                        if event.from_state is None
                        else _reconciliation_settlement_state(
                            event.from_state,
                            evidence.outcome,
                        )
                    )
                    if (
                        expected_state is None
                        or event.to_state is not expected_state
                        or evidence.send_attempt_digest != payload["sendAttemptDigest"]
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite reconciliation authority is "
                            "not contiguous"
                        )
                elif event.kind == "reconciliation_control_applied":
                    control = ProviderEffectReconciliationControl.from_wire(
                        payload["control"]
                    )
                    expected_state = (
                        None
                        if event.from_state is None
                        else transition_provider_effect_state(
                            event.from_state,
                            control.transition,
                        )
                    )
                    if (
                        expected_state is None
                        or event.to_state is not expected_state
                        or control.expected_state_version + 1 != event.sequence
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite reconciliation control is "
                            "not contiguous"
                        )
                elif event.kind == "retry_same_intent_applied":
                    command = ProviderEffectRetryCommand.from_wire(
                        payload["retryCommand"]
                    )
                    if (
                        event.from_state
                        not in {
                            ProviderEffectState.CONFIRMED_NOT_COMMITTED,
                            ProviderEffectState.CONFIRMED_CANCELLED,
                        }
                        or event.to_state is not ProviderEffectState.PENDING
                        or command.expected_state_version + 1 != event.sequence
                    ):
                        raise SQLiteProviderEffectCorruptionError(
                            "provider-effect SQLite retry authority is not contiguous"
                        )
                previous_event = event
            event_tuple = decoded_events[:limit]
            return StoredProviderEffectEventPage(
                events=event_tuple,
                next_after_sequence=(
                    event_tuple[-1].sequence if has_more and event_tuple else None
                ),
            )

        return self._database._run_read(read)


class SQLiteProviderEffectClaimAuthority:
    """Claim-only facade kept structurally distinct from origin authority."""

    __slots__ = ("_repository", "authority_digest")

    authority_digest: str

    def __init__(self, repository: SQLiteProviderEffectRepository) -> None:
        if type(repository) is not SQLiteProviderEffectRepository:
            raise TypeError(
                "provider-effect SQLite claim authority requires its repository"
            )
        self._repository = repository
        self.authority_digest = repository.claim_authority_digest

    def verify_claim(
        self,
        *,
        intent: ProviderEffectIntent,
        previous_send_attempt: ProviderEffectSendAttempt | None,
        send_attempt_id: str,
        claim_owner_id: str,
        claim_generation: int,
        claim_fencing_token: int,
        claim_expires_at_unix_ms: int,
        admitted_at_unix_ms: int,
    ) -> bool:
        return self._repository._verify_claim(
            intent=intent,
            previous_send_attempt=previous_send_attempt,
            send_attempt_id=send_attempt_id,
            claim_owner_id=claim_owner_id,
            claim_generation=claim_generation,
            claim_fencing_token=claim_fencing_token,
            claim_expires_at_unix_ms=claim_expires_at_unix_ms,
            admitted_at_unix_ms=admitted_at_unix_ms,
        )

    def claim_send(
        self,
        *,
        admission: ProviderEffectAdmission,
        intent: ProviderEffectIntent,
    ) -> tuple[ProviderEffectSendAttempt, ProviderEffectAdmissionReceipt] | None:
        return self._repository._claim_send(
            admission=admission,
            intent=intent,
        )

    def verify_active_send(
        self,
        *,
        current: ProviderEffectState,
        admission_receipt: ProviderEffectAdmissionReceipt,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        return self._repository._verify_active_send(
            current=current,
            admission_receipt=admission_receipt,
            send_attempt=send_attempt,
        )

    def settle_active_send(
        self,
        *,
        current: ProviderEffectState,
        next_state: ProviderEffectState,
        admission_receipt: ProviderEffectAdmissionReceipt,
        send_attempt: ProviderEffectSendAttempt,
        evidence: ProviderReconciliationEvidence,
    ) -> bool:
        return self._repository._settle_active_send(
            current=current,
            next_state=next_state,
            admission_receipt=admission_receipt,
            send_attempt=send_attempt,
            evidence=evidence,
        )


__all__ = [
    "MAX_PROVIDER_EFFECT_CLAIM_LEASE_DURATION_MS",
    "MAX_PROVIDER_EFFECT_EVENT_PAGE_SIZE",
    "PROVIDER_EFFECT_CLAIM_FORMAT_VERSION",
    "PROVIDER_EFFECT_CLAIM_RELEASE_FORMAT_VERSION",
    "PROVIDER_EFFECT_EVENT_FORMAT_VERSION",
    "PROVIDER_EFFECT_RECONCILIATION_CONTROL_FORMAT_VERSION",
    "PROVIDER_EFFECT_RETRY_COMMAND_FORMAT_VERSION",
    "ProviderEffectClaim",
    "ProviderEffectClaimRelease",
    "ProviderEffectClaimRequest",
    "ProviderEffectReconciliationControl",
    "ProviderEffectRetryCommand",
    "ProviderEffectWorkItem",
    "SQLiteProviderEffectClaimAuthority",
    "SQLiteProviderEffectCorruptionError",
    "SQLiteProviderEffectRepository",
    "StaleProviderEffectClaimError",
    "StoredProviderEffect",
    "StoredProviderEffectActiveSend",
    "StoredProviderEffectEvent",
    "StoredProviderEffectEventPage",
]
