"""Closed contracts for provider-side effects and outcome reconciliation.

This module defines authority and evidence only. It does not perform provider
I/O, claim storage, or reconciliation scheduling. Callers must supply verifier
implementations and run snapshots from trusted deployment and repository
dependencies, never from request data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .canonical import canonical_dumps, canonical_hash, canonical_loads


PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION = (
    "graphblocks.provider-capability-snapshot.v1"
)
PROVIDER_EFFECT_INTENT_FORMAT_VERSION = "graphblocks.provider-effect-intent.v1"
PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION = (
    "graphblocks.provider-effect-send-attempt.v1"
)
PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION = (
    "graphblocks.provider-run-authority-snapshot.v1"
)
PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION = (
    "graphblocks.provider-reconciliation-evidence.v1"
)

_MAX_U64 = (1 << 64) - 1
_CAPABILITY_SNAPSHOT_FIELDS = frozenset(
    {
        "adapterId",
        "adapterReleaseDigest",
        "authorityDigest",
        "cancellation",
        "deduplication",
        "formatVersion",
        "operation",
        "reconciliationVerificationAuthorityDigest",
        "reconciliationVerifierId",
        "reconciliationVerifierReleaseDigest",
        "statusLookup",
        "target",
    }
)
_RUN_AUTHORITY_SNAPSHOT_FIELDS = frozenset(
    {
        "checkpointDigest",
        "fencingToken",
        "formatVersion",
        "leaseGeneration",
        "ownerPrincipalId",
        "runId",
        "runStateVersion",
        "tenantId",
    }
)
_INTENT_FIELDS = frozenset(
    {
        "createdAtUnixMs",
        "effectId",
        "effectKind",
        "formatVersion",
        "idempotencyKey",
        "originAuthority",
        "ownerPrincipalId",
        "provider",
        "request",
        "runId",
        "tenantId",
    }
)
_INTENT_ORIGIN_FIELDS = frozenset(
    {
        "authorityDigest",
        "checkpointDigest",
        "fencingToken",
        "leaseGeneration",
        "runStateVersion",
    }
)
_INTENT_PROVIDER_FIELDS = frozenset(
    {
        "adapterId",
        "adapterReleaseDigest",
        "capabilitySnapshotDigest",
        "correlationId",
        "operation",
        "target",
    }
)
_INTENT_REQUEST_FIELDS = frozenset({"canonicalJson", "digest"})
_SEND_ATTEMPT_FIELDS = frozenset(
    {
        "admissionDigest",
        "attemptId",
        "capabilitySnapshotDigest",
        "claimFencingToken",
        "claimGeneration",
        "claimOwnerId",
        "claimAuthorityDigest",
        "effectId",
        "formatVersion",
        "intentDigest",
        "startedAtUnixMs",
    }
)
_RECONCILIATION_EVIDENCE_FIELDS = frozenset(
    {
        "capabilitySnapshotDigest",
        "effectId",
        "formatVersion",
        "intentDigest",
        "method",
        "observedAtUnixMs",
        "outcome",
        "providerCorrelationId",
        "providerEvidenceJson",
        "providerEvidenceDigest",
        "sendAttemptDigest",
        "verificationAuthorityDigest",
        "verifierId",
        "verifierReleaseDigest",
    }
)


class ProviderEffectContractError(ValueError):
    """Base error for provider-effect contract violations."""


class ProviderEffectDecodeError(ProviderEffectContractError):
    """Raised when a provider-effect wire value is not closed and exact."""


class ProviderEffectAdmissionError(ProviderEffectContractError):
    """Raised when an intent lacks an applicable recovery capability."""


class ProviderEffectIdentityConflictError(ProviderEffectContractError):
    """Raised when a retry changes an immutable logical provider effect."""


class ProviderEffectEvidenceError(ProviderEffectContractError):
    """Raised when provider reconciliation evidence is not authoritative."""


class ProviderEffectStateConflictError(ProviderEffectContractError):
    """Raised when a provider-effect state transition is not permitted."""


class ProviderEffectKind(StrEnum):
    PROVIDER_MUTATION = "provider_mutation"
    TOOL_MUTATION = "tool_mutation"


class ProviderDeduplication(StrEnum):
    NONE = "none"
    ATOMIC_BY_IDEMPOTENCY_KEY = "atomic_by_idempotency_key"


class ProviderStatusLookup(StrEnum):
    NONE = "none"
    DEFINITIVE_BY_IDEMPOTENCY_KEY = "definitive_by_idempotency_key"
    DEFINITIVE_BY_PREBOUND_CORRELATION_ID = "definitive_by_prebound_correlation_id"


class ProviderCancellation(StrEnum):
    NONE = "none"
    REQUEST_ONLY = "request_only"
    CONFIRMED_BY_IDEMPOTENCY_KEY = "confirmed_by_idempotency_key"
    CONFIRMED_BY_PREBOUND_CORRELATION_ID = "confirmed_by_prebound_correlation_id"


class ProviderReconciliationMethod(StrEnum):
    ATOMIC_DEDUPE_REPLAY = "atomic_dedupe_replay"
    STATUS_LOOKUP = "status_lookup"
    CONFIRMED_CANCELLATION = "confirmed_cancellation"


class ProviderReconciliationOutcome(StrEnum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CANCELLED_CONFIRMED = "cancelled_confirmed"
    UNKNOWN = "unknown"


class ProviderEffectState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SEND_STARTED = "send_started"
    QUARANTINED_UNKNOWN = "quarantined_unknown"
    RECONCILING = "reconciling"
    MANUAL_REVIEW_UNKNOWN = "manual_review_unknown"
    CONFIRMED_COMMITTED = "confirmed_committed"
    CONFIRMED_NOT_COMMITTED = "confirmed_not_committed"
    CONFIRMED_CANCELLED = "confirmed_cancelled"


class ProviderEffectTransition(StrEnum):
    CLAIM = "claim"
    RELEASE_BEFORE_SEND = "release_before_send"
    BEGIN_SEND = "begin_send"
    RECORD_AMBIGUOUS = "record_ambiguous"
    BEGIN_RECONCILIATION = "begin_reconciliation"
    RECORD_UNKNOWN = "record_unknown"
    CONFIRM_COMMITTED = "confirm_committed"
    CONFIRM_NOT_COMMITTED = "confirm_not_committed"
    CONFIRM_CANCELLED = "confirm_cancelled"
    ESCALATE_MANUAL_REVIEW = "escalate_manual_review"
    RESUME_RECONCILIATION = "resume_reconciliation"
    RETRY_SAME_INTENT = "retry_same_intent"


_ALLOWED_TRANSITIONS = {
    (ProviderEffectState.PENDING, ProviderEffectTransition.CLAIM): (
        ProviderEffectState.CLAIMED
    ),
    (
        ProviderEffectState.CLAIMED,
        ProviderEffectTransition.RELEASE_BEFORE_SEND,
    ): ProviderEffectState.PENDING,
    (ProviderEffectState.CLAIMED, ProviderEffectTransition.BEGIN_SEND): (
        ProviderEffectState.SEND_STARTED
    ),
    (
        ProviderEffectState.SEND_STARTED,
        ProviderEffectTransition.RECORD_AMBIGUOUS,
    ): ProviderEffectState.QUARANTINED_UNKNOWN,
    (
        ProviderEffectState.SEND_STARTED,
        ProviderEffectTransition.CONFIRM_COMMITTED,
    ): ProviderEffectState.CONFIRMED_COMMITTED,
    (
        ProviderEffectState.SEND_STARTED,
        ProviderEffectTransition.CONFIRM_NOT_COMMITTED,
    ): ProviderEffectState.CONFIRMED_NOT_COMMITTED,
    (
        ProviderEffectState.SEND_STARTED,
        ProviderEffectTransition.CONFIRM_CANCELLED,
    ): ProviderEffectState.CONFIRMED_CANCELLED,
    (
        ProviderEffectState.QUARANTINED_UNKNOWN,
        ProviderEffectTransition.BEGIN_RECONCILIATION,
    ): ProviderEffectState.RECONCILING,
    (
        ProviderEffectState.QUARANTINED_UNKNOWN,
        ProviderEffectTransition.ESCALATE_MANUAL_REVIEW,
    ): ProviderEffectState.MANUAL_REVIEW_UNKNOWN,
    (
        ProviderEffectState.MANUAL_REVIEW_UNKNOWN,
        ProviderEffectTransition.RESUME_RECONCILIATION,
    ): ProviderEffectState.RECONCILING,
    (
        ProviderEffectState.RECONCILING,
        ProviderEffectTransition.RECORD_UNKNOWN,
    ): ProviderEffectState.QUARANTINED_UNKNOWN,
    (
        ProviderEffectState.RECONCILING,
        ProviderEffectTransition.CONFIRM_COMMITTED,
    ): ProviderEffectState.CONFIRMED_COMMITTED,
    (
        ProviderEffectState.RECONCILING,
        ProviderEffectTransition.CONFIRM_NOT_COMMITTED,
    ): ProviderEffectState.CONFIRMED_NOT_COMMITTED,
    (
        ProviderEffectState.RECONCILING,
        ProviderEffectTransition.CONFIRM_CANCELLED,
    ): ProviderEffectState.CONFIRMED_CANCELLED,
    (
        ProviderEffectState.CONFIRMED_NOT_COMMITTED,
        ProviderEffectTransition.RETRY_SAME_INTENT,
    ): ProviderEffectState.PENDING,
    (
        ProviderEffectState.CONFIRMED_CANCELLED,
        ProviderEffectTransition.RETRY_SAME_INTENT,
    ): ProviderEffectState.PENDING,
}
_EVIDENCE_BOUND_TRANSITIONS = frozenset(
    {
        ProviderEffectTransition.RECORD_UNKNOWN,
        ProviderEffectTransition.CONFIRM_COMMITTED,
        ProviderEffectTransition.CONFIRM_NOT_COMMITTED,
        ProviderEffectTransition.CONFIRM_CANCELLED,
    }
)
_IDENTITY_BOUND_TRANSITIONS = frozenset({ProviderEffectTransition.RETRY_SAME_INTENT})
_ADMISSION_BOUND_TRANSITIONS = frozenset({ProviderEffectTransition.BEGIN_SEND})


def _validate_exact_string(owner: str, field_name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be an exact non-empty string"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ProviderEffectContractError(
            f"{owner} {field_name} must contain Unicode scalar values"
        ) from None
    return value


def _validate_optional_exact_string(
    owner: str,
    field_name: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    return _validate_exact_string(owner, field_name, value)


def _validate_digest(owner: str, field_name: str, value: object) -> str:
    digest = _validate_exact_string(owner, field_name, value)
    if (
        len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be a canonical sha256 digest"
        )
    return digest


def _validate_optional_digest(
    owner: str,
    field_name: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    return _validate_digest(owner, field_name, value)


def _validate_u64(
    owner: str,
    field_name: str,
    value: object,
    *,
    positive: bool = False,
) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _MAX_U64:
        qualifier = "positive " if positive else ""
        raise ProviderEffectContractError(
            f"{owner} {field_name} must be a {qualifier}unsigned 64-bit integer"
        )
    return value


def _validate_canonical_object(
    owner: str,
    json_field_name: str,
    digest_field_name: str,
    encoded_value: object,
    digest_value: object,
) -> tuple[str, str]:
    encoded = _validate_exact_string(owner, json_field_name, encoded_value)
    digest = _validate_digest(owner, digest_field_name, digest_value)
    try:
        decoded = canonical_loads(encoded)
    except (TypeError, ValueError) as error:
        raise ProviderEffectContractError(
            f"{owner} {json_field_name} must be canonical JSON"
        ) from error
    if type(decoded) is not dict or canonical_dumps(decoded) != encoded:
        raise ProviderEffectContractError(
            f"{owner} {json_field_name} must be a canonical JSON object"
        )
    if canonical_hash(decoded) != digest:
        raise ProviderEffectContractError(
            f"{owner} {digest_field_name} must match {json_field_name}"
        )
    return encoded, digest


def _require_closed_object(
    value: object,
    fields: frozenset[str],
    owner: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise ProviderEffectDecodeError(f"{owner} must be an object")
    if set(value) != fields:
        raise ProviderEffectDecodeError(f"{owner} must contain the closed fields")
    return value


@dataclass(frozen=True, slots=True)
class ProviderRunAuthoritySnapshot:
    tenant_id: str
    run_id: str
    owner_principal_id: str
    run_state_version: int
    lease_generation: int
    fencing_token: int
    checkpoint_digest: str | None = None
    format_version: str = PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider run authority snapshot"
        for field_name in ("tenant_id", "run_id", "owner_principal_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "run_state_version",
            _validate_u64(owner, "run_state_version", self.run_state_version),
        )
        for field_name in ("lease_generation", "fencing_token"):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(
                    owner,
                    field_name,
                    getattr(self, field_name),
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "checkpoint_digest",
            _validate_optional_digest(
                owner,
                "checkpoint_digest",
                self.checkpoint_digest,
            ),
        )
        format_version = _validate_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "checkpointDigest": self.checkpoint_digest,
            "fencingToken": self.fencing_token,
            "formatVersion": self.format_version,
            "leaseGeneration": self.lease_generation,
            "ownerPrincipalId": self.owner_principal_id,
            "runId": self.run_id,
            "runStateVersion": self.run_state_version,
            "tenantId": self.tenant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderRunAuthoritySnapshot:
        owner = "provider run authority snapshot"
        payload = _require_closed_object(
            value,
            _RUN_AUTHORITY_SNAPSHOT_FIELDS,
            owner,
        )
        try:
            return cls(
                tenant_id=_validate_exact_string(
                    owner,
                    "tenantId",
                    payload["tenantId"],
                ),
                run_id=_validate_exact_string(owner, "runId", payload["runId"]),
                owner_principal_id=_validate_exact_string(
                    owner,
                    "ownerPrincipalId",
                    payload["ownerPrincipalId"],
                ),
                run_state_version=_validate_u64(
                    owner,
                    "runStateVersion",
                    payload["runStateVersion"],
                ),
                lease_generation=_validate_u64(
                    owner,
                    "leaseGeneration",
                    payload["leaseGeneration"],
                    positive=True,
                ),
                fencing_token=_validate_u64(
                    owner,
                    "fencingToken",
                    payload["fencingToken"],
                    positive=True,
                ),
                checkpoint_digest=_validate_optional_digest(
                    owner,
                    "checkpointDigest",
                    payload["checkpointDigest"],
                ),
                format_version=_validate_exact_string(
                    owner,
                    "formatVersion",
                    payload["formatVersion"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise ProviderEffectDecodeError(
                "provider run authority snapshot is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class ProviderCapabilitySnapshot:
    authority_digest: str
    adapter_id: str
    adapter_release_digest: str
    target: str
    operation: str
    reconciliation_verifier_id: str
    reconciliation_verifier_release_digest: str
    reconciliation_verification_authority_digest: str
    deduplication: ProviderDeduplication
    status_lookup: ProviderStatusLookup
    cancellation: ProviderCancellation
    format_version: str = PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider capability snapshot"
        object.__setattr__(
            self,
            "authority_digest",
            _validate_digest(owner, "authority_digest", self.authority_digest),
        )
        for field_name in (
            "adapter_id",
            "target",
            "operation",
            "reconciliation_verifier_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "adapter_release_digest",
            _validate_digest(
                owner,
                "adapter_release_digest",
                self.adapter_release_digest,
            ),
        )
        for field_name in (
            "reconciliation_verifier_release_digest",
            "reconciliation_verification_authority_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_digest(owner, field_name, getattr(self, field_name)),
            )
        for field_name, enum_type in (
            ("deduplication", ProviderDeduplication),
            ("status_lookup", ProviderStatusLookup),
            ("cancellation", ProviderCancellation),
        ):
            if type(getattr(self, field_name)) is not enum_type:
                raise ProviderEffectContractError(
                    f"{owner} {field_name} must be a {enum_type.__name__}"
                )
        format_version = _validate_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "adapterId": self.adapter_id,
            "adapterReleaseDigest": self.adapter_release_digest,
            "authorityDigest": self.authority_digest,
            "cancellation": self.cancellation.value,
            "deduplication": self.deduplication.value,
            "formatVersion": self.format_version,
            "operation": self.operation,
            "reconciliationVerificationAuthorityDigest": (
                self.reconciliation_verification_authority_digest
            ),
            "reconciliationVerifierId": self.reconciliation_verifier_id,
            "reconciliationVerifierReleaseDigest": (
                self.reconciliation_verifier_release_digest
            ),
            "statusLookup": self.status_lookup.value,
            "target": self.target,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderCapabilitySnapshot:
        owner = "provider capability snapshot"
        payload = _require_closed_object(
            value,
            _CAPABILITY_SNAPSHOT_FIELDS,
            owner,
        )
        try:
            return cls(
                authority_digest=_validate_digest(
                    owner,
                    "authorityDigest",
                    payload["authorityDigest"],
                ),
                adapter_id=_validate_exact_string(
                    owner,
                    "adapterId",
                    payload["adapterId"],
                ),
                adapter_release_digest=_validate_digest(
                    owner,
                    "adapterReleaseDigest",
                    payload["adapterReleaseDigest"],
                ),
                target=_validate_exact_string(owner, "target", payload["target"]),
                operation=_validate_exact_string(
                    owner,
                    "operation",
                    payload["operation"],
                ),
                reconciliation_verifier_id=_validate_exact_string(
                    owner,
                    "reconciliationVerifierId",
                    payload["reconciliationVerifierId"],
                ),
                reconciliation_verifier_release_digest=_validate_digest(
                    owner,
                    "reconciliationVerifierReleaseDigest",
                    payload["reconciliationVerifierReleaseDigest"],
                ),
                reconciliation_verification_authority_digest=_validate_digest(
                    owner,
                    "reconciliationVerificationAuthorityDigest",
                    payload["reconciliationVerificationAuthorityDigest"],
                ),
                deduplication=ProviderDeduplication(
                    _validate_exact_string(
                        owner,
                        "deduplication",
                        payload["deduplication"],
                    )
                ),
                status_lookup=ProviderStatusLookup(
                    _validate_exact_string(
                        owner,
                        "statusLookup",
                        payload["statusLookup"],
                    )
                ),
                cancellation=ProviderCancellation(
                    _validate_exact_string(
                        owner,
                        "cancellation",
                        payload["cancellation"],
                    )
                ),
                format_version=_validate_exact_string(
                    owner,
                    "formatVersion",
                    payload["formatVersion"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise ProviderEffectDecodeError(
                "provider capability snapshot is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class ProviderEffectIntent:
    effect_id: str
    effect_kind: ProviderEffectKind
    tenant_id: str
    run_id: str
    owner_principal_id: str
    idempotency_key: str
    request_json: str
    request_digest: str
    provider_target: str
    provider_operation: str
    adapter_id: str
    adapter_release_digest: str
    capability_snapshot_digest: str
    origin_run_state_version: int
    origin_lease_generation: int
    origin_fencing_token: int
    origin_authority_digest: str
    created_at_unix_ms: int
    provider_correlation_id: str | None = None
    origin_checkpoint_digest: str | None = None
    format_version: str = PROVIDER_EFFECT_INTENT_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect intent"
        for field_name in (
            "effect_id",
            "tenant_id",
            "run_id",
            "owner_principal_id",
            "idempotency_key",
            "provider_target",
            "provider_operation",
            "adapter_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        if type(self.effect_kind) is not ProviderEffectKind:
            raise ProviderEffectContractError(
                f"{owner} effect_kind must be a ProviderEffectKind"
            )
        request_json, request_digest = _validate_canonical_object(
            owner,
            "request_json",
            "request_digest",
            self.request_json,
            self.request_digest,
        )
        object.__setattr__(self, "request_json", request_json)
        object.__setattr__(self, "request_digest", request_digest)
        for field_name in (
            "adapter_release_digest",
            "capability_snapshot_digest",
            "origin_authority_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_digest(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "origin_checkpoint_digest",
            _validate_optional_digest(
                owner,
                "origin_checkpoint_digest",
                self.origin_checkpoint_digest,
            ),
        )
        object.__setattr__(
            self,
            "provider_correlation_id",
            _validate_optional_exact_string(
                owner,
                "provider_correlation_id",
                self.provider_correlation_id,
            ),
        )
        object.__setattr__(
            self,
            "origin_run_state_version",
            _validate_u64(
                owner,
                "origin_run_state_version",
                self.origin_run_state_version,
            ),
        )
        for field_name in ("origin_lease_generation", "origin_fencing_token"):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(
                    owner,
                    field_name,
                    getattr(self, field_name),
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "created_at_unix_ms",
            _validate_u64(owner, "created_at_unix_ms", self.created_at_unix_ms),
        )
        format_version = _validate_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_EFFECT_INTENT_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "createdAtUnixMs": self.created_at_unix_ms,
            "effectId": self.effect_id,
            "effectKind": self.effect_kind.value,
            "formatVersion": self.format_version,
            "idempotencyKey": self.idempotency_key,
            "originAuthority": {
                "authorityDigest": self.origin_authority_digest,
                "checkpointDigest": self.origin_checkpoint_digest,
                "fencingToken": self.origin_fencing_token,
                "leaseGeneration": self.origin_lease_generation,
                "runStateVersion": self.origin_run_state_version,
            },
            "ownerPrincipalId": self.owner_principal_id,
            "provider": {
                "adapterId": self.adapter_id,
                "adapterReleaseDigest": self.adapter_release_digest,
                "capabilitySnapshotDigest": self.capability_snapshot_digest,
                "correlationId": self.provider_correlation_id,
                "operation": self.provider_operation,
                "target": self.provider_target,
            },
            "request": {
                "canonicalJson": self.request_json,
                "digest": self.request_digest,
            },
            "runId": self.run_id,
            "tenantId": self.tenant_id,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectIntent:
        owner = "provider effect intent"
        payload = _require_closed_object(
            value,
            _INTENT_FIELDS,
            owner,
        )
        request = _require_closed_object(
            payload["request"],
            _INTENT_REQUEST_FIELDS,
            "provider effect intent request",
        )
        provider = _require_closed_object(
            payload["provider"],
            _INTENT_PROVIDER_FIELDS,
            "provider effect intent provider",
        )
        origin = _require_closed_object(
            payload["originAuthority"],
            _INTENT_ORIGIN_FIELDS,
            "provider effect intent originAuthority",
        )
        try:
            return cls(
                effect_id=_validate_exact_string(
                    owner,
                    "effectId",
                    payload["effectId"],
                ),
                effect_kind=ProviderEffectKind(
                    _validate_exact_string(
                        owner,
                        "effectKind",
                        payload["effectKind"],
                    )
                ),
                tenant_id=_validate_exact_string(
                    owner,
                    "tenantId",
                    payload["tenantId"],
                ),
                run_id=_validate_exact_string(owner, "runId", payload["runId"]),
                owner_principal_id=_validate_exact_string(
                    owner,
                    "ownerPrincipalId",
                    payload["ownerPrincipalId"],
                ),
                idempotency_key=_validate_exact_string(
                    owner,
                    "idempotencyKey",
                    payload["idempotencyKey"],
                ),
                request_json=_validate_exact_string(
                    owner,
                    "request.canonicalJson",
                    request["canonicalJson"],
                ),
                request_digest=_validate_digest(
                    owner,
                    "request.digest",
                    request["digest"],
                ),
                provider_target=_validate_exact_string(
                    owner,
                    "provider.target",
                    provider["target"],
                ),
                provider_operation=_validate_exact_string(
                    owner,
                    "provider.operation",
                    provider["operation"],
                ),
                adapter_id=_validate_exact_string(
                    owner,
                    "provider.adapterId",
                    provider["adapterId"],
                ),
                adapter_release_digest=_validate_digest(
                    owner,
                    "provider.adapterReleaseDigest",
                    provider["adapterReleaseDigest"],
                ),
                capability_snapshot_digest=_validate_digest(
                    owner,
                    "provider.capabilitySnapshotDigest",
                    provider["capabilitySnapshotDigest"],
                ),
                provider_correlation_id=_validate_optional_exact_string(
                    owner,
                    "provider.correlationId",
                    provider["correlationId"],
                ),
                origin_run_state_version=_validate_u64(
                    owner,
                    "originAuthority.runStateVersion",
                    origin["runStateVersion"],
                ),
                origin_lease_generation=_validate_u64(
                    owner,
                    "originAuthority.leaseGeneration",
                    origin["leaseGeneration"],
                    positive=True,
                ),
                origin_fencing_token=_validate_u64(
                    owner,
                    "originAuthority.fencingToken",
                    origin["fencingToken"],
                    positive=True,
                ),
                origin_authority_digest=_validate_digest(
                    owner,
                    "originAuthority.authorityDigest",
                    origin["authorityDigest"],
                ),
                origin_checkpoint_digest=_validate_optional_digest(
                    owner,
                    "originAuthority.checkpointDigest",
                    origin["checkpointDigest"],
                ),
                created_at_unix_ms=_validate_u64(
                    owner,
                    "createdAtUnixMs",
                    payload["createdAtUnixMs"],
                ),
                format_version=_validate_exact_string(
                    owner,
                    "formatVersion",
                    payload["formatVersion"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise ProviderEffectDecodeError(
                "provider effect intent is invalid"
            ) from error


_ADMISSION_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class ProviderEffectAdmission:
    intent_digest: str
    capability_snapshot_digest: str
    capability_authority_digest: str
    run_authority_digest: str
    run_authority_verifier_digest: str
    claim_authority_digest: str
    send_attempt_id: str
    claim_owner_id: str
    claim_generation: int
    claim_fencing_token: int
    claim_expires_at_unix_ms: int
    applicable_methods: frozenset[ProviderReconciliationMethod]
    admitted_at_unix_ms: int
    previous_send_attempt_digest: str | None

    def __init__(
        self,
        *,
        intent_digest: str,
        capability_snapshot_digest: str,
        capability_authority_digest: str,
        run_authority_digest: str,
        run_authority_verifier_digest: str,
        claim_authority_digest: str,
        send_attempt_id: str,
        claim_owner_id: str,
        claim_generation: int,
        claim_fencing_token: int,
        claim_expires_at_unix_ms: int,
        applicable_methods: frozenset[ProviderReconciliationMethod],
        admitted_at_unix_ms: int,
        previous_send_attempt_digest: str | None,
        _seal: object,
    ) -> None:
        if _seal is not _ADMISSION_SEAL:
            raise TypeError(
                "provider effect admission must be issued by the authority validator"
            )
        owner = "provider effect admission"
        for field_name, value in (
            ("intent_digest", intent_digest),
            ("capability_snapshot_digest", capability_snapshot_digest),
            ("capability_authority_digest", capability_authority_digest),
            ("run_authority_digest", run_authority_digest),
            ("run_authority_verifier_digest", run_authority_verifier_digest),
            ("claim_authority_digest", claim_authority_digest),
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_digest(owner, field_name, value),
            )
        for field_name, value in (
            ("send_attempt_id", send_attempt_id),
            ("claim_owner_id", claim_owner_id),
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, value),
            )
        for field_name, integer_value in (
            ("claim_generation", claim_generation),
            ("claim_fencing_token", claim_fencing_token),
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(owner, field_name, integer_value, positive=True),
            )
        object.__setattr__(
            self,
            "claim_expires_at_unix_ms",
            _validate_u64(
                owner,
                "claim_expires_at_unix_ms",
                claim_expires_at_unix_ms,
                positive=True,
            ),
        )
        if (
            type(applicable_methods) is not frozenset
            or not applicable_methods
            or any(
                type(method) is not ProviderReconciliationMethod
                for method in applicable_methods
            )
        ):
            raise ProviderEffectContractError(
                f"{owner} applicable_methods must be a non-empty exact frozenset"
            )
        object.__setattr__(self, "applicable_methods", applicable_methods)
        object.__setattr__(
            self,
            "admitted_at_unix_ms",
            _validate_u64(owner, "admitted_at_unix_ms", admitted_at_unix_ms),
        )
        if self.claim_expires_at_unix_ms <= self.admitted_at_unix_ms:
            raise ProviderEffectContractError(
                f"{owner} claim expiry must be after admission"
            )
        object.__setattr__(
            self,
            "previous_send_attempt_digest",
            _validate_optional_digest(
                owner,
                "previous_send_attempt_digest",
                previous_send_attempt_digest,
            ),
        )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "admittedAtUnixMs": self.admitted_at_unix_ms,
            "applicableMethods": sorted(
                method.value for method in self.applicable_methods
            ),
            "capabilityAuthorityDigest": self.capability_authority_digest,
            "capabilitySnapshotDigest": self.capability_snapshot_digest,
            "claimAuthorityDigest": self.claim_authority_digest,
            "claimExpiresAtUnixMs": self.claim_expires_at_unix_ms,
            "claimFencingToken": self.claim_fencing_token,
            "claimGeneration": self.claim_generation,
            "claimOwnerId": self.claim_owner_id,
            "intentDigest": self.intent_digest,
            "previousSendAttemptDigest": self.previous_send_attempt_digest,
            "runAuthorityDigest": self.run_authority_digest,
            "runAuthorityVerifierDigest": self.run_authority_verifier_digest,
            "sendAttemptId": self.send_attempt_id,
        }


@dataclass(frozen=True, slots=True)
class ProviderEffectSendAttempt:
    effect_id: str
    intent_digest: str
    capability_snapshot_digest: str
    admission_digest: str
    claim_authority_digest: str
    attempt_id: str
    claim_owner_id: str
    claim_generation: int
    claim_fencing_token: int
    started_at_unix_ms: int
    format_version: str = PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider effect send attempt"
        for field_name in ("effect_id", "attempt_id", "claim_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        for field_name in (
            "intent_digest",
            "capability_snapshot_digest",
            "admission_digest",
            "claim_authority_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_digest(owner, field_name, getattr(self, field_name)),
            )
        for field_name in ("claim_generation", "claim_fencing_token"):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(
                    owner,
                    field_name,
                    getattr(self, field_name),
                    positive=True,
                ),
            )
        object.__setattr__(
            self,
            "started_at_unix_ms",
            _validate_u64(owner, "started_at_unix_ms", self.started_at_unix_ms),
        )
        format_version = _validate_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "admissionDigest": self.admission_digest,
            "attemptId": self.attempt_id,
            "capabilitySnapshotDigest": self.capability_snapshot_digest,
            "claimAuthorityDigest": self.claim_authority_digest,
            "claimFencingToken": self.claim_fencing_token,
            "claimGeneration": self.claim_generation,
            "claimOwnerId": self.claim_owner_id,
            "effectId": self.effect_id,
            "formatVersion": self.format_version,
            "intentDigest": self.intent_digest,
            "startedAtUnixMs": self.started_at_unix_ms,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderEffectSendAttempt:
        owner = "provider effect send attempt"
        payload = _require_closed_object(value, _SEND_ATTEMPT_FIELDS, owner)
        try:
            return cls(
                effect_id=_validate_exact_string(
                    owner,
                    "effectId",
                    payload["effectId"],
                ),
                intent_digest=_validate_digest(
                    owner,
                    "intentDigest",
                    payload["intentDigest"],
                ),
                capability_snapshot_digest=_validate_digest(
                    owner,
                    "capabilitySnapshotDigest",
                    payload["capabilitySnapshotDigest"],
                ),
                admission_digest=_validate_digest(
                    owner,
                    "admissionDigest",
                    payload["admissionDigest"],
                ),
                claim_authority_digest=_validate_digest(
                    owner,
                    "claimAuthorityDigest",
                    payload["claimAuthorityDigest"],
                ),
                attempt_id=_validate_exact_string(
                    owner,
                    "attemptId",
                    payload["attemptId"],
                ),
                claim_owner_id=_validate_exact_string(
                    owner,
                    "claimOwnerId",
                    payload["claimOwnerId"],
                ),
                claim_generation=_validate_u64(
                    owner,
                    "claimGeneration",
                    payload["claimGeneration"],
                    positive=True,
                ),
                claim_fencing_token=_validate_u64(
                    owner,
                    "claimFencingToken",
                    payload["claimFencingToken"],
                    positive=True,
                ),
                started_at_unix_ms=_validate_u64(
                    owner,
                    "startedAtUnixMs",
                    payload["startedAtUnixMs"],
                ),
                format_version=_validate_exact_string(
                    owner,
                    "formatVersion",
                    payload["formatVersion"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise ProviderEffectDecodeError(
                "provider effect send attempt is invalid"
            ) from error


@dataclass(frozen=True, slots=True)
class ProviderReconciliationEvidence:
    effect_id: str
    intent_digest: str
    capability_snapshot_digest: str
    send_attempt_digest: str
    method: ProviderReconciliationMethod
    outcome: ProviderReconciliationOutcome
    provider_evidence_json: str
    provider_evidence_digest: str
    verifier_id: str
    verifier_release_digest: str
    verification_authority_digest: str
    observed_at_unix_ms: int
    provider_correlation_id: str | None = None
    format_version: str = PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION

    def __post_init__(self) -> None:
        owner = "provider reconciliation evidence"
        object.__setattr__(
            self,
            "effect_id",
            _validate_exact_string(owner, "effect_id", self.effect_id),
        )
        for field_name in (
            "intent_digest",
            "capability_snapshot_digest",
            "send_attempt_digest",
            "verifier_release_digest",
            "verification_authority_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_digest(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "verifier_id",
            _validate_exact_string(owner, "verifier_id", self.verifier_id),
        )
        if type(self.method) is not ProviderReconciliationMethod:
            raise ProviderEffectContractError(
                f"{owner} method must be a ProviderReconciliationMethod"
            )
        if type(self.outcome) is not ProviderReconciliationOutcome:
            raise ProviderEffectContractError(
                f"{owner} outcome must be a ProviderReconciliationOutcome"
            )
        allowed_outcomes = {
            ProviderReconciliationMethod.ATOMIC_DEDUPE_REPLAY: {
                ProviderReconciliationOutcome.COMMITTED,
                ProviderReconciliationOutcome.UNKNOWN,
            },
            ProviderReconciliationMethod.STATUS_LOOKUP: {
                ProviderReconciliationOutcome.COMMITTED,
                ProviderReconciliationOutcome.NOT_COMMITTED,
                ProviderReconciliationOutcome.UNKNOWN,
            },
            ProviderReconciliationMethod.CONFIRMED_CANCELLATION: {
                ProviderReconciliationOutcome.COMMITTED,
                ProviderReconciliationOutcome.CANCELLED_CONFIRMED,
                ProviderReconciliationOutcome.UNKNOWN,
            },
        }[self.method]
        if self.outcome not in allowed_outcomes:
            raise ProviderEffectContractError(
                f"{owner} outcome is not valid for method {self.method.value!r}"
            )
        provider_evidence_json, provider_evidence_digest = _validate_canonical_object(
            owner,
            "provider_evidence_json",
            "provider_evidence_digest",
            self.provider_evidence_json,
            self.provider_evidence_digest,
        )
        object.__setattr__(
            self,
            "provider_evidence_json",
            provider_evidence_json,
        )
        object.__setattr__(
            self,
            "provider_evidence_digest",
            provider_evidence_digest,
        )
        object.__setattr__(
            self,
            "provider_correlation_id",
            _validate_optional_exact_string(
                owner,
                "provider_correlation_id",
                self.provider_correlation_id,
            ),
        )
        object.__setattr__(
            self,
            "observed_at_unix_ms",
            _validate_u64(owner, "observed_at_unix_ms", self.observed_at_unix_ms),
        )
        format_version = _validate_exact_string(
            owner,
            "format_version",
            self.format_version,
        )
        object.__setattr__(self, "format_version", format_version)
        if format_version != PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION:
            raise ProviderEffectContractError(
                f"{owner} format_version is not supported"
            )

    @property
    def digest(self) -> str:
        return canonical_hash(self.to_wire())

    def to_wire(self) -> dict[str, object]:
        return {
            "capabilitySnapshotDigest": self.capability_snapshot_digest,
            "effectId": self.effect_id,
            "formatVersion": self.format_version,
            "intentDigest": self.intent_digest,
            "method": self.method.value,
            "observedAtUnixMs": self.observed_at_unix_ms,
            "outcome": self.outcome.value,
            "providerCorrelationId": self.provider_correlation_id,
            "providerEvidenceJson": self.provider_evidence_json,
            "providerEvidenceDigest": self.provider_evidence_digest,
            "sendAttemptDigest": self.send_attempt_digest,
            "verificationAuthorityDigest": self.verification_authority_digest,
            "verifierId": self.verifier_id,
            "verifierReleaseDigest": self.verifier_release_digest,
        }

    @classmethod
    def from_wire(cls, value: object) -> ProviderReconciliationEvidence:
        owner = "provider reconciliation evidence"
        payload = _require_closed_object(
            value,
            _RECONCILIATION_EVIDENCE_FIELDS,
            owner,
        )
        try:
            return cls(
                effect_id=_validate_exact_string(
                    owner,
                    "effectId",
                    payload["effectId"],
                ),
                intent_digest=_validate_digest(
                    owner,
                    "intentDigest",
                    payload["intentDigest"],
                ),
                capability_snapshot_digest=_validate_digest(
                    owner,
                    "capabilitySnapshotDigest",
                    payload["capabilitySnapshotDigest"],
                ),
                send_attempt_digest=_validate_digest(
                    owner,
                    "sendAttemptDigest",
                    payload["sendAttemptDigest"],
                ),
                method=ProviderReconciliationMethod(
                    _validate_exact_string(owner, "method", payload["method"])
                ),
                outcome=ProviderReconciliationOutcome(
                    _validate_exact_string(owner, "outcome", payload["outcome"])
                ),
                provider_evidence_json=_validate_exact_string(
                    owner,
                    "providerEvidenceJson",
                    payload["providerEvidenceJson"],
                ),
                provider_evidence_digest=_validate_digest(
                    owner,
                    "providerEvidenceDigest",
                    payload["providerEvidenceDigest"],
                ),
                verifier_id=_validate_exact_string(
                    owner,
                    "verifierId",
                    payload["verifierId"],
                ),
                verifier_release_digest=_validate_digest(
                    owner,
                    "verifierReleaseDigest",
                    payload["verifierReleaseDigest"],
                ),
                verification_authority_digest=_validate_digest(
                    owner,
                    "verificationAuthorityDigest",
                    payload["verificationAuthorityDigest"],
                ),
                observed_at_unix_ms=_validate_u64(
                    owner,
                    "observedAtUnixMs",
                    payload["observedAtUnixMs"],
                ),
                provider_correlation_id=_validate_optional_exact_string(
                    owner,
                    "providerCorrelationId",
                    payload["providerCorrelationId"],
                ),
                format_version=_validate_exact_string(
                    owner,
                    "formatVersion",
                    payload["formatVersion"],
                ),
            )
        except (TypeError, ValueError) as error:
            raise ProviderEffectDecodeError(
                "provider reconciliation evidence is invalid"
            ) from error


class ProviderCapabilityAuthorityVerifier(Protocol):
    """Deployment-owned verifier for capability-registry snapshots."""

    authority_digest: str

    def verify(self, capability: ProviderCapabilitySnapshot) -> bool:
        """Return exact ``True`` only for a registry-authentic snapshot."""


class ProviderRunAuthorityVerifier(Protocol):
    """Repository-owned live verifier for a run authority snapshot."""

    authority_digest: str

    def verify(
        self,
        *,
        intent: ProviderEffectIntent,
        run_authority: ProviderRunAuthoritySnapshot,
        admitted_at_unix_ms: int,
    ) -> bool:
        """Return exact ``True`` only for current transactional run authority."""


class ProviderEffectClaimAuthority(Protocol):
    """Repository authority for one-shot send claims and active attempts."""

    authority_digest: str

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
        """Return exact ``True`` only for the current repository claim."""

    def claim_send(
        self,
        *,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        """Atomically consume admission and install this active send attempt."""

    def verify_active_send(
        self,
        *,
        current: ProviderEffectState,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
    ) -> bool:
        """Return exact ``True`` only when this attempt remains repository-active."""

    def settle_active_send(
        self,
        *,
        current: ProviderEffectState,
        next_state: ProviderEffectState,
        admission: ProviderEffectAdmission,
        send_attempt: ProviderEffectSendAttempt,
        evidence_digest: str,
    ) -> bool:
        """Atomically recheck the attempt and commit its evidence transition."""


class ProviderReconciliationEvidenceVerifier(Protocol):
    """Deployment-owned verifier for one adapter evidence format."""

    verifier_id: str
    verifier_release_digest: str
    verification_authority_digest: str

    def verify(
        self,
        *,
        intent: ProviderEffectIntent,
        capability: ProviderCapabilitySnapshot,
        send_attempt: ProviderEffectSendAttempt,
        evidence: ProviderReconciliationEvidence,
    ) -> bool:
        """Return exact ``True`` only for an authentic, correctly mapped receipt."""


class ProviderReconciliationVerifierAuthority(Protocol):
    """Deployment trust root that resolves registered verifier implementations."""

    authority_digest: str

    def resolve(
        self,
        *,
        capability: ProviderCapabilitySnapshot,
    ) -> ProviderReconciliationEvidenceVerifier | None:
        """Resolve the authenticated implementation for the admitted verifier tuple."""


def _applicable_reconciliation_methods(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
) -> frozenset[ProviderReconciliationMethod]:
    methods: set[ProviderReconciliationMethod] = set()
    if capability.deduplication is ProviderDeduplication.ATOMIC_BY_IDEMPOTENCY_KEY:
        methods.add(ProviderReconciliationMethod.ATOMIC_DEDUPE_REPLAY)
    if capability.status_lookup is ProviderStatusLookup.DEFINITIVE_BY_IDEMPOTENCY_KEY:
        methods.add(ProviderReconciliationMethod.STATUS_LOOKUP)
    elif (
        capability.status_lookup
        is ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID
        and intent.provider_correlation_id is not None
    ):
        methods.add(ProviderReconciliationMethod.STATUS_LOOKUP)
    if capability.cancellation is ProviderCancellation.CONFIRMED_BY_IDEMPOTENCY_KEY:
        methods.add(ProviderReconciliationMethod.CONFIRMED_CANCELLATION)
    elif (
        capability.cancellation
        is ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID
        and intent.provider_correlation_id is not None
    ):
        methods.add(ProviderReconciliationMethod.CONFIRMED_CANCELLATION)
    return frozenset(methods)


def _validate_intent_capability_binding(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
) -> None:
    if (
        intent.capability_snapshot_digest != capability.digest
        or intent.adapter_id != capability.adapter_id
        or intent.adapter_release_digest != capability.adapter_release_digest
        or intent.provider_target != capability.target
        or intent.provider_operation != capability.operation
    ):
        raise ProviderEffectAdmissionError(
            "provider effect intent does not match its capability snapshot"
        )


def _validate_intent_run_authority_binding(
    intent: ProviderEffectIntent,
    run_authority: ProviderRunAuthoritySnapshot,
) -> None:
    if (
        intent.tenant_id != run_authority.tenant_id
        or intent.run_id != run_authority.run_id
        or intent.owner_principal_id != run_authority.owner_principal_id
        or intent.origin_run_state_version != run_authority.run_state_version
        or intent.origin_lease_generation != run_authority.lease_generation
        or intent.origin_fencing_token != run_authority.fencing_token
        or intent.origin_checkpoint_digest != run_authority.checkpoint_digest
        or intent.origin_authority_digest != run_authority.digest
    ):
        raise ProviderEffectAdmissionError(
            "provider effect intent does not match repository run authority"
        )


def _resolve_registered_reconciliation_verifier(
    capability: ProviderCapabilitySnapshot,
    verifier_authority: ProviderReconciliationVerifierAuthority,
) -> ProviderReconciliationEvidenceVerifier:
    try:
        verifier_authority_digest = _validate_digest(
            "provider reconciliation verifier authority",
            "authority_digest",
            verifier_authority.authority_digest,
        )
        verifier = verifier_authority.resolve(capability=capability)
        if verifier is None:
            raise ProviderEffectContractError(
                "provider reconciliation verifier is not registered"
            )
        verifier_id = _validate_exact_string(
            "provider reconciliation verifier",
            "verifier_id",
            verifier.verifier_id,
        )
        verifier_release_digest = _validate_digest(
            "provider reconciliation verifier",
            "verifier_release_digest",
            verifier.verifier_release_digest,
        )
        verification_authority_digest = _validate_digest(
            "provider reconciliation verifier",
            "verification_authority_digest",
            verifier.verification_authority_digest,
        )
    except Exception as error:
        raise ProviderEffectContractError(
            "provider reconciliation verifier resolution failed"
        ) from error
    if (
        verifier_authority_digest
        != capability.reconciliation_verification_authority_digest
        or verifier_id != capability.reconciliation_verifier_id
        or verifier_release_digest != capability.reconciliation_verifier_release_digest
        or verification_authority_digest != verifier_authority_digest
    ):
        raise ProviderEffectContractError(
            "provider reconciliation verifier is not authenticated by the "
            "admitted authority"
        )
    return verifier


def admit_provider_effect_intent(
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    run_authority: ProviderRunAuthoritySnapshot,
    *,
    capability_authority: ProviderCapabilityAuthorityVerifier,
    verifier_authority: ProviderReconciliationVerifierAuthority,
    run_authority_verifier: ProviderRunAuthorityVerifier,
    claim_authority: ProviderEffectClaimAuthority,
    send_attempt_id: str,
    claim_owner_id: str,
    claim_generation: int,
    claim_fencing_token: int,
    claim_expires_at_unix_ms: int,
    admitted_at_unix_ms: int,
    previous_send_attempt: ProviderEffectSendAttempt | None = None,
) -> ProviderEffectAdmission:
    """Issue an opaque admission after deployment and repository validation."""

    if type(intent) is not ProviderEffectIntent:
        raise TypeError("provider effect admission intent must be ProviderEffectIntent")
    if type(capability) is not ProviderCapabilitySnapshot:
        raise TypeError(
            "provider effect admission capability must be ProviderCapabilitySnapshot"
        )
    if type(run_authority) is not ProviderRunAuthoritySnapshot:
        raise TypeError(
            "provider effect admission authority must be ProviderRunAuthoritySnapshot"
        )
    try:
        trusted_authority_digest = _validate_digest(
            "provider capability authority",
            "authority_digest",
            capability_authority.authority_digest,
        )
        capability_verified = capability_authority.verify(capability)
    except Exception as error:
        raise ProviderEffectAdmissionError(
            "provider capability authority verification failed"
        ) from error
    if (
        capability.authority_digest != trusted_authority_digest
        or type(capability_verified) is not bool
        or not capability_verified
    ):
        raise ProviderEffectAdmissionError(
            "provider capability snapshot is not issued by the trusted authority"
        )
    _validate_intent_capability_binding(intent, capability)
    try:
        _resolve_registered_reconciliation_verifier(
            capability,
            verifier_authority,
        )
    except ProviderEffectContractError as error:
        raise ProviderEffectAdmissionError(
            "provider reconciliation verifier is not registered for admission"
        ) from error
    _validate_intent_run_authority_binding(intent, run_authority)
    admitted_at = _validate_u64(
        "provider effect admission",
        "admitted_at_unix_ms",
        admitted_at_unix_ms,
    )
    if admitted_at < intent.created_at_unix_ms:
        raise ProviderEffectAdmissionError(
            "provider effect admission predates its intent"
        )
    owner = "provider effect admission"
    validated_send_attempt_id = _validate_exact_string(
        owner,
        "send_attempt_id",
        send_attempt_id,
    )
    validated_claim_owner_id = _validate_exact_string(
        owner,
        "claim_owner_id",
        claim_owner_id,
    )
    validated_claim_generation = _validate_u64(
        owner,
        "claim_generation",
        claim_generation,
        positive=True,
    )
    validated_claim_fencing_token = _validate_u64(
        owner,
        "claim_fencing_token",
        claim_fencing_token,
        positive=True,
    )
    validated_claim_expiry = _validate_u64(
        owner,
        "claim_expires_at_unix_ms",
        claim_expires_at_unix_ms,
        positive=True,
    )
    if validated_claim_expiry <= admitted_at:
        raise ProviderEffectAdmissionError(
            "provider effect claim must remain live after admission"
        )
    try:
        run_authority_verifier_digest = _validate_digest(
            "provider run authority verifier",
            "authority_digest",
            run_authority_verifier.authority_digest,
        )
        run_authority_verified = run_authority_verifier.verify(
            intent=intent,
            run_authority=run_authority,
            admitted_at_unix_ms=admitted_at,
        )
    except Exception as error:
        raise ProviderEffectAdmissionError(
            "provider run authority verification failed"
        ) from error
    if type(run_authority_verified) is not bool or not run_authority_verified:
        raise ProviderEffectAdmissionError(
            "provider run authority is not live in its repository transaction"
        )
    previous_send_attempt_digest: str | None = None
    if previous_send_attempt is not None:
        if type(previous_send_attempt) is not ProviderEffectSendAttempt:
            raise TypeError(
                "previous provider effect send attempt must be "
                "ProviderEffectSendAttempt"
            )
        if (
            previous_send_attempt.effect_id != intent.effect_id
            or previous_send_attempt.intent_digest != intent.digest
            or previous_send_attempt.capability_snapshot_digest != capability.digest
            or previous_send_attempt.started_at_unix_ms > admitted_at
            or validated_send_attempt_id == previous_send_attempt.attempt_id
            or validated_claim_generation <= previous_send_attempt.claim_generation
            or validated_claim_fencing_token
            <= previous_send_attempt.claim_fencing_token
        ):
            raise ProviderEffectAdmissionError(
                "previous send attempt does not match the admitted intent"
            )
        previous_send_attempt_digest = previous_send_attempt.digest
    try:
        claim_authority_digest = _validate_digest(
            "provider effect claim authority",
            "authority_digest",
            claim_authority.authority_digest,
        )
        claim_verified = claim_authority.verify_claim(
            intent=intent,
            previous_send_attempt=previous_send_attempt,
            send_attempt_id=validated_send_attempt_id,
            claim_owner_id=validated_claim_owner_id,
            claim_generation=validated_claim_generation,
            claim_fencing_token=validated_claim_fencing_token,
            claim_expires_at_unix_ms=validated_claim_expiry,
            admitted_at_unix_ms=admitted_at,
        )
    except Exception as error:
        raise ProviderEffectAdmissionError(
            "provider effect claim authority verification failed"
        ) from error
    if type(claim_verified) is not bool or not claim_verified:
        raise ProviderEffectAdmissionError(
            "provider effect claim is not current in its repository transaction"
        )
    methods = _applicable_reconciliation_methods(intent, capability)
    if not methods:
        raise ProviderEffectAdmissionError(
            "provider effect has no atomic deduplication, definitive status "
            "lookup, or confirmed cancellation recovery path"
        )
    return ProviderEffectAdmission(
        intent_digest=intent.digest,
        capability_snapshot_digest=capability.digest,
        capability_authority_digest=capability.authority_digest,
        run_authority_digest=run_authority.digest,
        run_authority_verifier_digest=run_authority_verifier_digest,
        claim_authority_digest=claim_authority_digest,
        send_attempt_id=validated_send_attempt_id,
        claim_owner_id=validated_claim_owner_id,
        claim_generation=validated_claim_generation,
        claim_fencing_token=validated_claim_fencing_token,
        claim_expires_at_unix_ms=validated_claim_expiry,
        applicable_methods=methods,
        admitted_at_unix_ms=admitted_at,
        previous_send_attempt_digest=previous_send_attempt_digest,
        _seal=_ADMISSION_SEAL,
    )


def assert_same_provider_effect_intent(
    current: ProviderEffectIntent,
    requested: ProviderEffectIntent,
) -> None:
    """Reject retry/reclaim attempts that alter the logical provider effect."""

    if (
        type(current) is not ProviderEffectIntent
        or type(requested) is not ProviderEffectIntent
    ):
        raise TypeError("provider effect retry identities must be ProviderEffectIntent")
    if current != requested or current.digest != requested.digest:
        raise ProviderEffectIdentityConflictError(
            "provider effect retry must preserve the complete intent identity"
        )


def validate_provider_reconciliation_evidence(
    current: ProviderEffectState,
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    admission: ProviderEffectAdmission,
    send_attempt: ProviderEffectSendAttempt,
    evidence: ProviderReconciliationEvidence,
    claim_authority: ProviderEffectClaimAuthority,
    verifier_authority: ProviderReconciliationVerifierAuthority,
) -> None:
    """Bind authentic provider evidence to one admitted send attempt."""

    if type(current) is not ProviderEffectState:
        raise TypeError("current provider effect state must be ProviderEffectState")
    if current not in {
        ProviderEffectState.SEND_STARTED,
        ProviderEffectState.RECONCILING,
    }:
        raise ProviderEffectStateConflictError(
            "provider evidence requires send_started or reconciling state"
        )
    if type(intent) is not ProviderEffectIntent:
        raise TypeError("provider effect evidence intent must be ProviderEffectIntent")
    if type(capability) is not ProviderCapabilitySnapshot:
        raise TypeError(
            "provider effect evidence capability must be ProviderCapabilitySnapshot"
        )
    if type(admission) is not ProviderEffectAdmission:
        raise TypeError(
            "provider effect evidence admission must be ProviderEffectAdmission"
        )
    if type(send_attempt) is not ProviderEffectSendAttempt:
        raise TypeError(
            "provider effect evidence attempt must be ProviderEffectSendAttempt"
        )
    if type(evidence) is not ProviderReconciliationEvidence:
        raise TypeError(
            "provider effect evidence must be ProviderReconciliationEvidence"
        )
    _validate_intent_capability_binding(intent, capability)
    if (
        admission.intent_digest != intent.digest
        or admission.capability_snapshot_digest != capability.digest
        or admission.capability_authority_digest != capability.authority_digest
        or send_attempt.effect_id != intent.effect_id
        or send_attempt.intent_digest != intent.digest
        or send_attempt.capability_snapshot_digest != capability.digest
        or send_attempt.admission_digest != admission.digest
        or send_attempt.claim_authority_digest != admission.claim_authority_digest
    ):
        raise ProviderEffectEvidenceError(
            "provider reconciliation attempt does not match its admission"
        )
    try:
        claim_authority_digest = _validate_digest(
            "provider effect claim authority",
            "authority_digest",
            claim_authority.authority_digest,
        )
        active_send_verified = claim_authority.verify_active_send(
            current=current,
            admission=admission,
            send_attempt=send_attempt,
        )
    except Exception as error:
        raise ProviderEffectEvidenceError(
            "provider active send authority verification failed"
        ) from error
    if (
        claim_authority_digest != admission.claim_authority_digest
        or type(active_send_verified) is not bool
        or not active_send_verified
    ):
        raise ProviderEffectEvidenceError(
            "provider evidence does not target the repository-active send attempt"
        )
    if (
        evidence.effect_id != intent.effect_id
        or evidence.intent_digest != intent.digest
        or evidence.capability_snapshot_digest != capability.digest
        or evidence.send_attempt_digest != send_attempt.digest
    ):
        raise ProviderEffectEvidenceError(
            "provider reconciliation evidence does not match its send attempt"
        )
    if evidence.method not in admission.applicable_methods:
        raise ProviderEffectEvidenceError(
            "provider reconciliation method is not supported by the snapshot"
        )
    if evidence.observed_at_unix_ms < send_attempt.started_at_unix_ms:
        raise ProviderEffectEvidenceError(
            "provider reconciliation evidence predates its send attempt"
        )
    if (
        intent.provider_correlation_id is not None
        and evidence.provider_correlation_id is not None
        and evidence.provider_correlation_id != intent.provider_correlation_id
    ):
        raise ProviderEffectEvidenceError(
            "provider reconciliation correlation does not match its intent"
        )
    correlation_required = (
        evidence.method is ProviderReconciliationMethod.STATUS_LOOKUP
        and capability.status_lookup
        is ProviderStatusLookup.DEFINITIVE_BY_PREBOUND_CORRELATION_ID
    ) or (
        evidence.method is ProviderReconciliationMethod.CONFIRMED_CANCELLATION
        and capability.cancellation
        is ProviderCancellation.CONFIRMED_BY_PREBOUND_CORRELATION_ID
    )
    if correlation_required and (
        evidence.provider_correlation_id != intent.provider_correlation_id
    ):
        raise ProviderEffectEvidenceError(
            "provider reconciliation requires the prebound correlation identity"
        )
    if (
        evidence.verifier_id != capability.reconciliation_verifier_id
        or evidence.verifier_release_digest
        != capability.reconciliation_verifier_release_digest
        or evidence.verification_authority_digest
        != capability.reconciliation_verification_authority_digest
    ):
        raise ProviderEffectEvidenceError(
            "provider reconciliation evidence does not match the admitted verifier"
        )
    try:
        verifier = _resolve_registered_reconciliation_verifier(
            capability,
            verifier_authority,
        )
    except ProviderEffectContractError as error:
        raise ProviderEffectEvidenceError(
            "provider reconciliation verifier resolution failed"
        ) from error
    try:
        verified = verifier.verify(
            intent=intent,
            capability=capability,
            send_attempt=send_attempt,
            evidence=evidence,
        )
    except Exception as error:
        raise ProviderEffectEvidenceError(
            "provider reconciliation evidence verification failed"
        ) from error
    if type(verified) is not bool or not verified:
        raise ProviderEffectEvidenceError(
            "provider reconciliation evidence was not authenticated"
        )


def transition_provider_effect_state(
    current: ProviderEffectState,
    transition: ProviderEffectTransition,
) -> ProviderEffectState:
    """Apply the closed provider-effect state machine without side effects."""

    if type(current) is not ProviderEffectState:
        raise TypeError("current provider effect state must be ProviderEffectState")
    if type(transition) is not ProviderEffectTransition:
        raise TypeError("provider effect transition must be ProviderEffectTransition")
    if transition in _EVIDENCE_BOUND_TRANSITIONS:
        raise ProviderEffectStateConflictError(
            f"provider effect transition {transition.value!r} requires validated "
            "provider reconciliation evidence"
        )
    if transition in _IDENTITY_BOUND_TRANSITIONS:
        raise ProviderEffectStateConflictError(
            f"provider effect transition {transition.value!r} requires exact "
            "intent identity validation"
        )
    if transition in _ADMISSION_BOUND_TRANSITIONS:
        raise ProviderEffectStateConflictError(
            f"provider effect transition {transition.value!r} requires a live "
            "authority admission and send-attempt fence"
        )
    return _transition_provider_effect_state(current, transition)


def _transition_provider_effect_state(
    current: ProviderEffectState,
    transition: ProviderEffectTransition,
) -> ProviderEffectState:
    next_state = _ALLOWED_TRANSITIONS.get((current, transition))
    if next_state is None:
        raise ProviderEffectStateConflictError(
            f"provider effect cannot apply {transition.value!r} from {current.value!r}"
        )
    return next_state


def begin_provider_effect_send(
    current: ProviderEffectState,
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    admission: ProviderEffectAdmission,
    claim_authority: ProviderEffectClaimAuthority,
    *,
    started_at_unix_ms: int,
    previous_send_attempt: ProviderEffectSendAttempt | None = None,
) -> tuple[ProviderEffectState, ProviderEffectSendAttempt]:
    """Enter send-started only with a bound admission and fresh claim fence."""

    if type(current) is not ProviderEffectState:
        raise TypeError("current provider effect state must be ProviderEffectState")
    if type(intent) is not ProviderEffectIntent:
        raise TypeError("provider effect send intent must be ProviderEffectIntent")
    if type(capability) is not ProviderCapabilitySnapshot:
        raise TypeError(
            "provider effect send capability must be ProviderCapabilitySnapshot"
        )
    if type(admission) is not ProviderEffectAdmission:
        raise TypeError(
            "provider effect send admission must be ProviderEffectAdmission"
        )
    next_state = _transition_provider_effect_state(
        current,
        ProviderEffectTransition.BEGIN_SEND,
    )
    _validate_intent_capability_binding(intent, capability)
    if (
        admission.intent_digest != intent.digest
        or admission.capability_snapshot_digest != capability.digest
        or admission.capability_authority_digest != capability.authority_digest
    ):
        raise ProviderEffectAdmissionError(
            "provider effect send does not match its authority admission"
        )
    try:
        claim_authority_digest = _validate_digest(
            "provider effect claim authority",
            "authority_digest",
            claim_authority.authority_digest,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ProviderEffectAdmissionError(
            "provider effect claim authority is invalid"
        ) from error
    if admission.claim_authority_digest != claim_authority_digest:
        raise ProviderEffectAdmissionError(
            "provider effect send does not match its claim authority"
        )
    if (
        previous_send_attempt is not None
        and type(previous_send_attempt) is not ProviderEffectSendAttempt
    ):
        raise TypeError(
            "previous provider effect send attempt must be ProviderEffectSendAttempt"
        )
    previous_digest = (
        None if previous_send_attempt is None else previous_send_attempt.digest
    )
    if previous_digest != admission.previous_send_attempt_digest:
        raise ProviderEffectAdmissionError(
            "provider effect send does not match admitted attempt history"
        )
    started_at = _validate_u64(
        "provider effect send attempt",
        "started_at_unix_ms",
        started_at_unix_ms,
    )
    if (
        started_at < admission.admitted_at_unix_ms
        or started_at >= admission.claim_expires_at_unix_ms
    ):
        raise ProviderEffectAdmissionError(
            "provider effect send is outside its admitted claim interval"
        )
    if previous_send_attempt is not None:
        if (
            previous_send_attempt.effect_id != intent.effect_id
            or previous_send_attempt.intent_digest != intent.digest
            or previous_send_attempt.capability_snapshot_digest != capability.digest
            or admission.send_attempt_id == previous_send_attempt.attempt_id
            or admission.claim_generation <= previous_send_attempt.claim_generation
            or admission.claim_fencing_token
            <= previous_send_attempt.claim_fencing_token
            or started_at < previous_send_attempt.started_at_unix_ms
        ):
            raise ProviderEffectAdmissionError(
                "retried provider effect send must advance attempt identity and fence"
            )
    attempt = ProviderEffectSendAttempt(
        effect_id=intent.effect_id,
        intent_digest=intent.digest,
        capability_snapshot_digest=capability.digest,
        admission_digest=admission.digest,
        claim_authority_digest=claim_authority_digest,
        attempt_id=admission.send_attempt_id,
        claim_owner_id=admission.claim_owner_id,
        claim_generation=admission.claim_generation,
        claim_fencing_token=admission.claim_fencing_token,
        started_at_unix_ms=started_at,
    )
    try:
        claimed = claim_authority.claim_send(
            admission=admission,
            send_attempt=attempt,
        )
    except Exception as error:
        raise ProviderEffectAdmissionError(
            "provider effect claim consumption failed"
        ) from error
    if type(claimed) is not bool or not claimed:
        raise ProviderEffectAdmissionError(
            "provider effect admission was stale or already consumed"
        )
    return next_state, attempt


def retry_same_provider_effect_intent(
    current: ProviderEffectState,
    current_intent: ProviderEffectIntent,
    requested_intent: ProviderEffectIntent,
) -> ProviderEffectState:
    """Retry only a terminally safe, byte-identical provider effect intent."""

    if type(current) is not ProviderEffectState:
        raise TypeError("current provider effect state must be ProviderEffectState")
    assert_same_provider_effect_intent(current_intent, requested_intent)
    return _transition_provider_effect_state(
        current,
        ProviderEffectTransition.RETRY_SAME_INTENT,
    )


def apply_provider_reconciliation_evidence(
    current: ProviderEffectState,
    intent: ProviderEffectIntent,
    capability: ProviderCapabilitySnapshot,
    admission: ProviderEffectAdmission,
    send_attempt: ProviderEffectSendAttempt,
    evidence: ProviderReconciliationEvidence,
    claim_authority: ProviderEffectClaimAuthority,
    verifier_authority: ProviderReconciliationVerifierAuthority,
) -> ProviderEffectState:
    """Validate evidence and project its outcome through the state machine."""

    validate_provider_reconciliation_evidence(
        current,
        intent,
        capability,
        admission,
        send_attempt,
        evidence,
        claim_authority,
        verifier_authority,
    )
    if current is ProviderEffectState.SEND_STARTED:
        unknown_transition = ProviderEffectTransition.RECORD_AMBIGUOUS
    elif current is ProviderEffectState.RECONCILING:
        unknown_transition = ProviderEffectTransition.RECORD_UNKNOWN
    else:
        raise ProviderEffectStateConflictError(
            "provider evidence requires send_started or reconciling state"
        )
    transition = {
        ProviderReconciliationOutcome.COMMITTED: (
            ProviderEffectTransition.CONFIRM_COMMITTED
        ),
        ProviderReconciliationOutcome.NOT_COMMITTED: (
            ProviderEffectTransition.CONFIRM_NOT_COMMITTED
        ),
        ProviderReconciliationOutcome.CANCELLED_CONFIRMED: (
            ProviderEffectTransition.CONFIRM_CANCELLED
        ),
        ProviderReconciliationOutcome.UNKNOWN: unknown_transition,
    }[evidence.outcome]
    next_state = _transition_provider_effect_state(current, transition)
    try:
        settled = claim_authority.settle_active_send(
            current=current,
            next_state=next_state,
            admission=admission,
            send_attempt=send_attempt,
            evidence_digest=evidence.digest,
        )
    except Exception as error:
        raise ProviderEffectEvidenceError(
            "provider evidence settlement transaction failed"
        ) from error
    if type(settled) is not bool or not settled:
        raise ProviderEffectEvidenceError(
            "provider send attempt was no longer active at settlement"
        )
    return next_state


__all__ = [
    "PROVIDER_CAPABILITY_SNAPSHOT_FORMAT_VERSION",
    "PROVIDER_EFFECT_INTENT_FORMAT_VERSION",
    "PROVIDER_EFFECT_SEND_ATTEMPT_FORMAT_VERSION",
    "PROVIDER_RECONCILIATION_EVIDENCE_FORMAT_VERSION",
    "PROVIDER_RUN_AUTHORITY_SNAPSHOT_FORMAT_VERSION",
    "ProviderCancellation",
    "ProviderCapabilityAuthorityVerifier",
    "ProviderCapabilitySnapshot",
    "ProviderDeduplication",
    "ProviderEffectAdmission",
    "ProviderEffectAdmissionError",
    "ProviderEffectClaimAuthority",
    "ProviderEffectContractError",
    "ProviderEffectDecodeError",
    "ProviderEffectEvidenceError",
    "ProviderEffectIdentityConflictError",
    "ProviderEffectIntent",
    "ProviderEffectKind",
    "ProviderEffectSendAttempt",
    "ProviderEffectState",
    "ProviderEffectStateConflictError",
    "ProviderEffectTransition",
    "ProviderReconciliationEvidence",
    "ProviderReconciliationEvidenceVerifier",
    "ProviderReconciliationVerifierAuthority",
    "ProviderReconciliationMethod",
    "ProviderReconciliationOutcome",
    "ProviderRunAuthoritySnapshot",
    "ProviderRunAuthorityVerifier",
    "ProviderStatusLookup",
    "admit_provider_effect_intent",
    "apply_provider_reconciliation_evidence",
    "assert_same_provider_effect_intent",
    "begin_provider_effect_send",
    "retry_same_provider_effect_intent",
    "transition_provider_effect_state",
    "validate_provider_reconciliation_evidence",
]
