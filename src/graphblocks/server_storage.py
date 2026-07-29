from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import re
from typing import Protocol

from .canonical import (
    _has_unicode_surrogate,
    canonical_dumps,
    canonical_hash,
    canonical_loads,
)
from .runtime import RuntimeCheckpoint


CHECKPOINT_FORMAT_VERSION = "graphblocks.runtime-checkpoint.v1"
_MAX_U64 = (1 << 64) - 1
_CANONICAL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_CHECKPOINT_FIELDS = frozenset(
    {
        "checkpoint_id",
        "run_id",
        "graph_hash",
        "wait_node",
        "remaining_nodes",
        "inputs",
        "node_outputs",
        "output_values",
        "operation",
        "state_digest",
    }
)


class AcceptedRunStorageError(RuntimeError):
    """Base error for the restart-durable accepted-run storage boundary."""


class AcceptedRunConflictError(AcceptedRunStorageError):
    """Base error for a rejected state, identity, or idempotency conflict."""


class AcceptedRunIdConflictError(AcceptedRunConflictError):
    def __init__(self, tenant_id: str, run_id: str, reason: str) -> None:
        super().__init__(reason)
        self.tenant_id = tenant_id
        self.run_id = run_id


class AcceptedRunNotFoundError(AcceptedRunStorageError):
    def __init__(self, tenant_id: str, run_id: str) -> None:
        super().__init__(f"accepted run {run_id!r} was not found in tenant")
        self.tenant_id = tenant_id
        self.run_id = run_id


class AdmissionIdempotencyConflictError(AcceptedRunConflictError):
    def __init__(self, identity: AdmissionIdentity) -> None:
        super().__init__(
            "admission idempotency key conflicts with a different request digest"
        )
        self.identity = identity


class CallbackIssuanceConflictError(AcceptedRunConflictError):
    def __init__(
        self,
        expected: CallbackIssuanceIdentity,
        provided: CallbackIssuanceIdentity,
    ) -> None:
        super().__init__(
            "callback does not match the current checkpoint issuance"
        )
        self.expected = expected
        self.provided = provided


class CallbackPayloadConflictError(AcceptedRunConflictError):
    def __init__(self, submission: CallbackSubmissionIdentity) -> None:
        super().__init__(
            "callback idempotency key conflicts with a different payload digest"
        )
        self.submission = submission


class StaleAcceptedRunClaimError(AcceptedRunConflictError):
    def __init__(
        self,
        current: AcceptedRunClaim | None,
        provided: AcceptedRunClaim,
    ) -> None:
        super().__init__(
            "accepted run claim is stale or no longer authoritative"
        )
        self.current = current
        self.provided = provided


class AcceptedRunLeaseExpiredError(AcceptedRunConflictError):
    def __init__(self, claim: AcceptedRunClaim, operation: str) -> None:
        super().__init__(f"accepted run claim expired before {operation}")
        self.claim = claim
        self.operation = operation


class AcceptedRunStateConflictError(AcceptedRunConflictError):
    def __init__(
        self,
        run_id: str,
        expected_state_version: int,
        current_state_version: int,
    ) -> None:
        super().__init__(
            f"accepted run {run_id!r} state version conflict: expected "
            f"{expected_state_version}, current {current_state_version}"
        )
        self.run_id = run_id
        self.expected_state_version = expected_state_version
        self.current_state_version = current_state_version


class InvalidAcceptedRunTransitionError(AcceptedRunConflictError):
    def __init__(
        self,
        current: AcceptedRunPhase,
        target: AcceptedRunPhase,
    ) -> None:
        if current is AcceptedRunPhase.TERMINAL:
            message = (
                "terminal accepted run cannot transition "
                f"from {current.value} to {target.value}"
            )
        else:
            message = (
                "invalid accepted run transition "
                f"from {current.value} to {target.value}"
            )
        super().__init__(message)
        self.current = current
        self.target = target


class CheckpointIntegrityError(AcceptedRunStorageError):
    """Raised when a stored checkpoint cannot be reconstructed exactly."""


def _validate_exact_string(owner: str, field_name: str, value: object) -> str:
    if type(value) is not str:
        raise ValueError(f"{owner} {field_name} must be a string")
    if not value:
        raise ValueError(f"{owner} {field_name} must not be empty")
    if value != value.strip():
        raise ValueError(
            f"{owner} {field_name} must not contain surrounding whitespace"
        )
    if _has_unicode_surrogate(value):
        raise ValueError(
            f"{owner} {field_name} must contain only Unicode scalar values"
        )
    return value


def _validate_digest(owner: str, field_name: str, value: object) -> str:
    digest = _validate_exact_string(owner, field_name, value)
    if _CANONICAL_SHA256.fullmatch(digest) is None:
        raise ValueError(
            f"{owner} {field_name} must be a canonical sha256 digest"
        )
    return digest


def _validate_u64(
    owner: str,
    field_name: str,
    value: object,
    *,
    positive: bool = False,
) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_U64
    ):
        qualifier = "positive " if positive else ""
        raise ValueError(
            f"{owner} {field_name} must be a {qualifier}unsigned 64-bit integer"
        )
    return value


def _validate_canonical_json(
    owner: str,
    field_name: str,
    value: object,
    *,
    require_object: bool = True,
) -> str:
    encoded = _validate_exact_string(owner, field_name, value)
    try:
        decoded = canonical_loads(encoded)
        canonical = canonical_dumps(decoded)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{owner} {field_name} must be canonical JSON"
        ) from error
    if require_object and not isinstance(decoded, dict):
        raise ValueError(f"{owner} {field_name} must encode a JSON object")
    if canonical != encoded:
        raise ValueError(f"{owner} {field_name} must use canonical JSON encoding")
    return encoded


def _validate_json_digest(
    owner: str,
    *,
    json_field_name: str,
    encoded: str,
    digest_field_name: str,
    digest: str,
) -> None:
    if canonical_hash(canonical_loads(encoded)) != digest:
        raise ValueError(
            f"{owner} {digest_field_name} must match {json_field_name}"
        )


class AcceptedRunPhase(StrEnum):
    READY_INITIAL = "ready_initial"
    RUNNING = "running"
    WAITING_CALLBACK = "waiting_callback"
    READY_RESUME = "ready_resume"
    TERMINAL = "terminal"


_ALLOWED_ACCEPTED_RUN_TRANSITIONS = frozenset(
    {
        (AcceptedRunPhase.READY_INITIAL, AcceptedRunPhase.RUNNING),
        (AcceptedRunPhase.RUNNING, AcceptedRunPhase.WAITING_CALLBACK),
        (AcceptedRunPhase.RUNNING, AcceptedRunPhase.TERMINAL),
        (AcceptedRunPhase.WAITING_CALLBACK, AcceptedRunPhase.READY_RESUME),
        (AcceptedRunPhase.READY_RESUME, AcceptedRunPhase.RUNNING),
    }
)


def assert_accepted_run_transition(
    current: AcceptedRunPhase,
    target: AcceptedRunPhase,
) -> None:
    if not isinstance(current, AcceptedRunPhase):
        raise TypeError("current accepted run phase must be an AcceptedRunPhase")
    if not isinstance(target, AcceptedRunPhase):
        raise TypeError("target accepted run phase must be an AcceptedRunPhase")
    if (current, target) not in _ALLOWED_ACCEPTED_RUN_TRANSITIONS:
        raise InvalidAcceptedRunTransitionError(current, target)


@dataclass(frozen=True, slots=True)
class AdmissionIdentity:
    tenant_id: str
    owner_principal_id: str
    admission_scope: str
    idempotency_key: str
    request_digest: str

    def __post_init__(self) -> None:
        owner = "accepted run admission identity"
        for field_name in (
            "tenant_id",
            "owner_principal_id",
            "admission_scope",
            "idempotency_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "request_digest",
            _validate_digest(owner, "request_digest", self.request_digest),
        )

    @property
    def idempotency_namespace(self) -> tuple[str, str, str, str]:
        return (
            self.tenant_id,
            self.owner_principal_id,
            self.admission_scope,
            self.idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    run_id: str
    ticket_json: str
    replayed: bool = False

    def __post_init__(self) -> None:
        owner = "accepted run admission result"
        object.__setattr__(
            self,
            "run_id",
            _validate_exact_string(owner, "run_id", self.run_id),
        )
        object.__setattr__(
            self,
            "ticket_json",
            _validate_canonical_json(owner, "ticket_json", self.ticket_json),
        )
        if type(self.replayed) is not bool:
            raise ValueError(
                "accepted run admission result replayed must be a boolean"
            )


def resolve_admission_replay(
    *,
    existing_identity: AdmissionIdentity,
    existing_result: AdmissionResult,
    requested_identity: AdmissionIdentity,
) -> AdmissionResult | None:
    if (
        existing_identity.idempotency_namespace
        != requested_identity.idempotency_namespace
    ):
        return None
    if existing_identity.request_digest != requested_identity.request_digest:
        raise AdmissionIdempotencyConflictError(requested_identity)
    return replace(existing_result, replayed=True)


@dataclass(frozen=True, slots=True)
class AcceptedRunClaim:
    tenant_id: str
    run_id: str
    lease_owner_id: str
    lease_generation: int
    fencing_token: int
    lease_expires_at_unix_ms: int

    def __post_init__(self) -> None:
        owner = "accepted run claim"
        for field_name in ("tenant_id", "run_id", "lease_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
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
            "lease_expires_at_unix_ms",
            _validate_u64(
                owner,
                "lease_expires_at_unix_ms",
                self.lease_expires_at_unix_ms,
                positive=True,
            ),
        )


def assert_current_claim(
    *,
    current: AcceptedRunClaim | None,
    provided: AcceptedRunClaim,
) -> None:
    if not isinstance(provided, AcceptedRunClaim):
        raise TypeError("provided claim must be an AcceptedRunClaim")
    if current is not None and not isinstance(current, AcceptedRunClaim):
        raise TypeError("current claim must be an AcceptedRunClaim or None")
    if current != provided:
        raise StaleAcceptedRunClaimError(current, provided)


@dataclass(frozen=True, slots=True)
class CallbackIssuanceIdentity:
    run_id: str
    checkpoint_digest: str
    operation_id: str
    operation_attempt_id: str
    callback_idempotency_key: str
    lease_generation: int
    fencing_token: int

    def __post_init__(self) -> None:
        owner = "accepted run callback issuance"
        for field_name in (
            "run_id",
            "operation_id",
            "operation_attempt_id",
            "callback_idempotency_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "checkpoint_digest",
            _validate_digest(owner, "checkpoint_digest", self.checkpoint_digest),
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


@dataclass(frozen=True, slots=True)
class CallbackSubmissionIdentity:
    issuance: CallbackIssuanceIdentity
    payload_digest: str

    def __post_init__(self) -> None:
        owner = "accepted run callback submission"
        if not isinstance(self.issuance, CallbackIssuanceIdentity):
            raise ValueError(
                f"{owner} issuance must be a CallbackIssuanceIdentity"
            )
        object.__setattr__(
            self,
            "payload_digest",
            _validate_digest(owner, "payload_digest", self.payload_digest),
        )


@dataclass(frozen=True, slots=True)
class CallbackAcceptance:
    submission: CallbackSubmissionIdentity
    receipt_json: str
    accepted_event_sequence: int
    state_version: int

    def __post_init__(self) -> None:
        owner = "accepted run callback acceptance"
        if not isinstance(self.submission, CallbackSubmissionIdentity):
            raise ValueError(
                f"{owner} submission must be a CallbackSubmissionIdentity"
            )
        object.__setattr__(
            self,
            "receipt_json",
            _validate_canonical_json(owner, "receipt_json", self.receipt_json),
        )
        object.__setattr__(
            self,
            "accepted_event_sequence",
            _validate_u64(
                owner,
                "accepted_event_sequence",
                self.accepted_event_sequence,
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "state_version",
            _validate_u64(owner, "state_version", self.state_version),
        )


def resolve_callback_replay(
    *,
    expected_issuance: CallbackIssuanceIdentity,
    existing_acceptance: CallbackAcceptance | None,
    requested_submission: CallbackSubmissionIdentity,
) -> CallbackAcceptance | None:
    if requested_submission.issuance != expected_issuance:
        raise CallbackIssuanceConflictError(
            expected_issuance,
            requested_submission.issuance,
        )
    if existing_acceptance is None:
        return None
    if existing_acceptance.submission.issuance != expected_issuance:
        raise CallbackIssuanceConflictError(
            expected_issuance,
            existing_acceptance.submission.issuance,
        )
    if (
        existing_acceptance.submission.payload_digest
        != requested_submission.payload_digest
    ):
        raise CallbackPayloadConflictError(requested_submission)
    return existing_acceptance


@dataclass(frozen=True, slots=True)
class StoredRuntimeCheckpoint:
    format_version: str
    checkpoint_digest: str
    checkpoint_json: str

    def __post_init__(self) -> None:
        owner = "stored runtime checkpoint"
        object.__setattr__(
            self,
            "format_version",
            _validate_exact_string(owner, "format_version", self.format_version),
        )
        object.__setattr__(
            self,
            "checkpoint_digest",
            _validate_digest(owner, "checkpoint_digest", self.checkpoint_digest),
        )
        object.__setattr__(
            self,
            "checkpoint_json",
            _validate_canonical_json(
                owner,
                "checkpoint_json",
                self.checkpoint_json,
            ),
        )


def encode_runtime_checkpoint(
    checkpoint: RuntimeCheckpoint,
) -> StoredRuntimeCheckpoint:
    if not isinstance(checkpoint, RuntimeCheckpoint):
        raise TypeError("checkpoint must be a RuntimeCheckpoint")
    return StoredRuntimeCheckpoint(
        format_version=CHECKPOINT_FORMAT_VERSION,
        checkpoint_digest=checkpoint.state_digest,
        checkpoint_json=canonical_dumps(checkpoint.to_json()),
    )


def decode_runtime_checkpoint(
    stored: StoredRuntimeCheckpoint,
) -> RuntimeCheckpoint:
    if not isinstance(stored, StoredRuntimeCheckpoint):
        raise TypeError("stored checkpoint must be a StoredRuntimeCheckpoint")
    if stored.format_version != CHECKPOINT_FORMAT_VERSION:
        raise CheckpointIntegrityError(
            f"unsupported runtime checkpoint format {stored.format_version!r}"
        )
    payload = canonical_loads(stored.checkpoint_json)
    if not isinstance(payload, dict) or set(payload) != _RUNTIME_CHECKPOINT_FIELDS:
        raise CheckpointIntegrityError(
            "stored runtime checkpoint must contain the closed checkpoint fields"
        )
    try:
        checkpoint = RuntimeCheckpoint(**payload)
    except (TypeError, ValueError) as error:
        raise CheckpointIntegrityError(
            "stored runtime checkpoint failed validating reconstruction"
        ) from error
    if checkpoint.state_digest != stored.checkpoint_digest:
        raise CheckpointIntegrityError(
            "stored checkpoint digest does not match reconstructed checkpoint"
        )
    return checkpoint


class AcceptedRunEffectKind(StrEnum):
    OPERATION_DISPATCH = "operation_dispatch"
    COMPLETION = "completion"


@dataclass(frozen=True, slots=True)
class AcceptedRunEventIntent:
    kind: str
    payload_json: str
    payload_digest: str
    created_at_unix_ms: int

    def __post_init__(self) -> None:
        owner = "accepted run event intent"
        object.__setattr__(
            self,
            "kind",
            _validate_exact_string(owner, "kind", self.kind),
        )
        object.__setattr__(
            self,
            "payload_json",
            _validate_canonical_json(owner, "payload_json", self.payload_json),
        )
        object.__setattr__(
            self,
            "payload_digest",
            _validate_digest(owner, "payload_digest", self.payload_digest),
        )
        _validate_json_digest(
            owner,
            json_field_name="payload_json",
            encoded=self.payload_json,
            digest_field_name="payload_digest",
            digest=self.payload_digest,
        )
        object.__setattr__(
            self,
            "created_at_unix_ms",
            _validate_u64(
                owner,
                "created_at_unix_ms",
                self.created_at_unix_ms,
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedRunEffectIntent:
    effect_id: str
    kind: AcceptedRunEffectKind
    idempotency_key: str
    payload_json: str
    payload_digest: str

    def __post_init__(self) -> None:
        owner = "accepted run effect intent"
        object.__setattr__(
            self,
            "effect_id",
            _validate_exact_string(owner, "effect_id", self.effect_id),
        )
        if not isinstance(self.kind, AcceptedRunEffectKind):
            raise ValueError(
                f"{owner} kind must be an AcceptedRunEffectKind"
            )
        object.__setattr__(
            self,
            "idempotency_key",
            _validate_exact_string(
                owner,
                "idempotency_key",
                self.idempotency_key,
            ),
        )
        object.__setattr__(
            self,
            "payload_json",
            _validate_canonical_json(owner, "payload_json", self.payload_json),
        )
        object.__setattr__(
            self,
            "payload_digest",
            _validate_digest(owner, "payload_digest", self.payload_digest),
        )
        _validate_json_digest(
            owner,
            json_field_name="payload_json",
            encoded=self.payload_json,
            digest_field_name="payload_digest",
            digest=self.payload_digest,
        )


@dataclass(frozen=True, slots=True)
class AcceptedRunAdmission:
    run_id: str
    identity: AdmissionIdentity
    graph_json: str
    graph_hash: str
    inputs_json: str
    ticket_json: str
    graph_format_version: str
    runtime_format_version: str
    checkpoint_format_version: str
    created_at_unix_ms: int
    accepted_event: AcceptedRunEventIntent

    def __post_init__(self) -> None:
        owner = "accepted run admission"
        object.__setattr__(
            self,
            "run_id",
            _validate_exact_string(owner, "run_id", self.run_id),
        )
        if not isinstance(self.identity, AdmissionIdentity):
            raise ValueError(
                "accepted run admission identity must be an AdmissionIdentity"
            )
        for field_name in ("graph_json", "inputs_json", "ticket_json"):
            object.__setattr__(
                self,
                field_name,
                _validate_canonical_json(
                    owner,
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "graph_hash",
            _validate_digest(owner, "graph_hash", self.graph_hash),
        )
        for field_name in (
            "graph_format_version",
            "runtime_format_version",
            "checkpoint_format_version",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "created_at_unix_ms",
            _validate_u64(
                owner,
                "created_at_unix_ms",
                self.created_at_unix_ms,
            ),
        )
        if not isinstance(self.accepted_event, AcceptedRunEventIntent):
            raise ValueError(
                "accepted run admission accepted_event must be an "
                "AcceptedRunEventIntent"
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunSnapshot:
    run_id: str
    tenant_id: str
    owner_principal_id: str
    phase: AcceptedRunPhase
    state_version: int
    event_low_watermark: int
    event_high_watermark: int
    checkpoint_digest: str | None = None
    claim: AcceptedRunClaim | None = None
    terminal_status: str | None = None
    terminal_result_json: str | None = None

    def __post_init__(self) -> None:
        owner = "accepted run snapshot"
        for field_name in ("run_id", "tenant_id", "owner_principal_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        if not isinstance(self.phase, AcceptedRunPhase):
            raise ValueError(
                "accepted run snapshot phase must be an AcceptedRunPhase"
            )
        for field_name in (
            "state_version",
            "event_low_watermark",
            "event_high_watermark",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(owner, field_name, getattr(self, field_name)),
            )
        if self.event_low_watermark > self.event_high_watermark:
            raise ValueError(
                "accepted run snapshot event_low_watermark must not exceed "
                "event_high_watermark"
            )
        if self.checkpoint_digest is not None:
            object.__setattr__(
                self,
                "checkpoint_digest",
                _validate_digest(
                    owner,
                    "checkpoint_digest",
                    self.checkpoint_digest,
                ),
            )
        if self.claim is not None:
            if not isinstance(self.claim, AcceptedRunClaim):
                raise ValueError(
                    "accepted run snapshot claim must be an AcceptedRunClaim or None"
                )
            if self.claim.run_id != self.run_id:
                raise ValueError(
                    "accepted run snapshot claim run_id must match snapshot run_id"
                )
            if self.claim.tenant_id != self.tenant_id:
                raise ValueError(
                    "accepted run snapshot claim tenant_id must match snapshot "
                    "tenant_id"
                )
        if self.phase is AcceptedRunPhase.RUNNING and self.claim is None:
            raise ValueError(
                "running accepted run snapshot must include its current claim"
            )
        if self.phase is not AcceptedRunPhase.RUNNING and self.claim is not None:
            raise ValueError(
                "non-running accepted run snapshot must not include a claim"
            )
        if self.terminal_status is not None:
            object.__setattr__(
                self,
                "terminal_status",
                _validate_exact_string(
                    owner,
                    "terminal_status",
                    self.terminal_status,
                ),
            )
        if self.terminal_result_json is not None:
            object.__setattr__(
                self,
                "terminal_result_json",
                _validate_canonical_json(
                    owner,
                    "terminal_result_json",
                    self.terminal_result_json,
                ),
            )
        if self.phase is AcceptedRunPhase.TERMINAL:
            if self.terminal_status is None or self.terminal_result_json is None:
                raise ValueError(
                    "terminal accepted run snapshot must include status and result"
                )
        elif self.terminal_status is not None or self.terminal_result_json is not None:
            raise ValueError(
                "non-terminal accepted run snapshot must not include terminal data"
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunEvent:
    run_id: str
    sequence: int
    kind: str
    payload_json: str
    payload_digest: str
    created_at_unix_ms: int

    def __post_init__(self) -> None:
        owner = "accepted run event"
        for field_name in ("run_id", "kind"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "sequence",
            _validate_u64(owner, "sequence", self.sequence, positive=True),
        )
        object.__setattr__(
            self,
            "payload_json",
            _validate_canonical_json(owner, "payload_json", self.payload_json),
        )
        object.__setattr__(
            self,
            "payload_digest",
            _validate_digest(owner, "payload_digest", self.payload_digest),
        )
        _validate_json_digest(
            owner,
            json_field_name="payload_json",
            encoded=self.payload_json,
            digest_field_name="payload_digest",
            digest=self.payload_digest,
        )
        object.__setattr__(
            self,
            "created_at_unix_ms",
            _validate_u64(
                owner,
                "created_at_unix_ms",
                self.created_at_unix_ms,
            ),
        )


@dataclass(frozen=True, slots=True)
class AcceptedRunEventPage:
    events: tuple[AcceptedRunEvent, ...]
    low_watermark: int
    high_watermark: int
    next_after_sequence: int | None

    def __post_init__(self) -> None:
        owner = "accepted run event page"
        if isinstance(self.events, (str, bytes, bytearray)):
            raise ValueError(
                "accepted run event page events must contain AcceptedRunEvent values"
            )
        try:
            events = tuple(self.events)
        except TypeError as error:
            raise ValueError(
                "accepted run event page events must contain AcceptedRunEvent values"
            ) from error
        if any(not isinstance(event, AcceptedRunEvent) for event in events):
            raise ValueError(
                "accepted run event page events must contain AcceptedRunEvent values"
            )
        if any(
            following.sequence != current.sequence + 1
            for current, following in zip(events, events[1:])
        ):
            raise ValueError(
                "accepted run event page events must have contiguous sequences"
            )
        object.__setattr__(self, "events", events)
        for field_name in ("low_watermark", "high_watermark"):
            object.__setattr__(
                self,
                field_name,
                _validate_u64(owner, field_name, getattr(self, field_name)),
            )
        if self.low_watermark > self.high_watermark:
            raise ValueError(
                "accepted run event page low_watermark must not exceed high_watermark"
            )
        if events and (
            events[0].sequence < self.low_watermark
            or events[-1].sequence > self.high_watermark
        ):
            raise ValueError(
                "accepted run event page sequences must be within its watermarks"
            )
        if self.next_after_sequence is not None:
            object.__setattr__(
                self,
                "next_after_sequence",
                _validate_u64(
                    owner,
                    "next_after_sequence",
                    self.next_after_sequence,
                    positive=True,
                ),
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunClaimRequest:
    tenant_id: str
    run_id: str
    lease_owner_id: str
    now_unix_ms: int
    lease_duration_ms: int

    def __post_init__(self) -> None:
        owner = "accepted run claim request"
        for field_name in ("tenant_id", "run_id", "lease_owner_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "now_unix_ms",
            _validate_u64(owner, "now_unix_ms", self.now_unix_ms),
        )
        object.__setattr__(
            self,
            "lease_duration_ms",
            _validate_u64(
                owner,
                "lease_duration_ms",
                self.lease_duration_ms,
                positive=True,
            ),
        )
        if self.now_unix_ms > _MAX_U64 - self.lease_duration_ms:
            raise ValueError(
                "accepted run claim request lease expiration exceeds unsigned "
                "64-bit time"
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunWaitingCommit:
    claim: AcceptedRunClaim
    expected_state_version: int
    checkpoint: StoredRuntimeCheckpoint
    callback_issuance: CallbackIssuanceIdentity
    waiting_event: AcceptedRunEventIntent
    dispatch_effect: AcceptedRunEffectIntent

    def __post_init__(self) -> None:
        owner = "accepted run waiting commit"
        if not isinstance(self.claim, AcceptedRunClaim):
            raise ValueError(
                "accepted run waiting commit claim must be an AcceptedRunClaim"
            )
        object.__setattr__(
            self,
            "expected_state_version",
            _validate_u64(
                owner,
                "expected_state_version",
                self.expected_state_version,
            ),
        )
        if not isinstance(self.checkpoint, StoredRuntimeCheckpoint):
            raise ValueError(
                "accepted run waiting commit checkpoint must be a "
                "StoredRuntimeCheckpoint"
            )
        if not isinstance(self.callback_issuance, CallbackIssuanceIdentity):
            raise ValueError(
                "accepted run waiting commit callback_issuance must be a "
                "CallbackIssuanceIdentity"
            )
        if not isinstance(self.waiting_event, AcceptedRunEventIntent):
            raise ValueError(
                "accepted run waiting commit waiting_event must be an "
                "AcceptedRunEventIntent"
            )
        if not isinstance(self.dispatch_effect, AcceptedRunEffectIntent):
            raise ValueError(
                "accepted run waiting commit dispatch_effect must be an "
                "AcceptedRunEffectIntent"
            )
        if (
            self.dispatch_effect.kind
            is not AcceptedRunEffectKind.OPERATION_DISPATCH
        ):
            raise ValueError(
                "accepted run waiting commit requires an operation dispatch effect"
            )
        checkpoint = decode_runtime_checkpoint(self.checkpoint)
        issuance = self.callback_issuance
        if (
            checkpoint.run_id != self.claim.run_id
            or issuance.run_id != self.claim.run_id
            or issuance.checkpoint_digest != self.checkpoint.checkpoint_digest
            or issuance.lease_generation != self.claim.lease_generation
            or issuance.fencing_token != self.claim.fencing_token
            or issuance.operation_id != checkpoint.operation["operation_id"]
            or issuance.operation_attempt_id
            != checkpoint.operation["attempt_id"]
        ):
            raise ValueError(
                "accepted run waiting commit identities must match its claim "
                "and checkpoint"
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunCallbackCommit:
    tenant_id: str
    owner_principal_id: str
    expected_state_version: int
    submission: CallbackSubmissionIdentity
    payload_json: str
    receipt_json: str
    received_at_unix_ms: int
    accepted_event: AcceptedRunEventIntent

    def __post_init__(self) -> None:
        owner = "accepted run callback commit"
        for field_name in ("tenant_id", "owner_principal_id"):
            object.__setattr__(
                self,
                field_name,
                _validate_exact_string(owner, field_name, getattr(self, field_name)),
            )
        object.__setattr__(
            self,
            "expected_state_version",
            _validate_u64(
                owner,
                "expected_state_version",
                self.expected_state_version,
            ),
        )
        if not isinstance(self.submission, CallbackSubmissionIdentity):
            raise ValueError(
                "accepted run callback commit submission must be a "
                "CallbackSubmissionIdentity"
            )
        object.__setattr__(
            self,
            "payload_json",
            _validate_canonical_json(owner, "payload_json", self.payload_json),
        )
        _validate_json_digest(
            owner,
            json_field_name="payload_json",
            encoded=self.payload_json,
            digest_field_name="submission.payload_digest",
            digest=self.submission.payload_digest,
        )
        object.__setattr__(
            self,
            "receipt_json",
            _validate_canonical_json(owner, "receipt_json", self.receipt_json),
        )
        object.__setattr__(
            self,
            "received_at_unix_ms",
            _validate_u64(
                owner,
                "received_at_unix_ms",
                self.received_at_unix_ms,
            ),
        )
        if not isinstance(self.accepted_event, AcceptedRunEventIntent):
            raise ValueError(
                "accepted run callback commit accepted_event must be an "
                "AcceptedRunEventIntent"
            )


@dataclass(frozen=True, slots=True)
class AcceptedRunTerminalCommit:
    claim: AcceptedRunClaim
    expected_state_version: int
    terminal_status: str
    result_json: str
    result_digest: str
    terminal_event: AcceptedRunEventIntent
    completion_effect: AcceptedRunEffectIntent

    def __post_init__(self) -> None:
        owner = "accepted run terminal commit"
        if not isinstance(self.claim, AcceptedRunClaim):
            raise ValueError(
                "accepted run terminal commit claim must be an AcceptedRunClaim"
            )
        object.__setattr__(
            self,
            "expected_state_version",
            _validate_u64(
                owner,
                "expected_state_version",
                self.expected_state_version,
            ),
        )
        object.__setattr__(
            self,
            "terminal_status",
            _validate_exact_string(
                owner,
                "terminal_status",
                self.terminal_status,
            ),
        )
        object.__setattr__(
            self,
            "result_json",
            _validate_canonical_json(owner, "result_json", self.result_json),
        )
        object.__setattr__(
            self,
            "result_digest",
            _validate_digest(owner, "result_digest", self.result_digest),
        )
        _validate_json_digest(
            owner,
            json_field_name="result_json",
            encoded=self.result_json,
            digest_field_name="result_digest",
            digest=self.result_digest,
        )
        if not isinstance(self.terminal_event, AcceptedRunEventIntent):
            raise ValueError(
                "accepted run terminal commit terminal_event must be an "
                "AcceptedRunEventIntent"
            )
        if not isinstance(self.completion_effect, AcceptedRunEffectIntent):
            raise ValueError(
                "accepted run terminal commit completion_effect must be an "
                "AcceptedRunEffectIntent"
            )
        if self.completion_effect.kind is not AcceptedRunEffectKind.COMPLETION:
            raise ValueError(
                "accepted run terminal commit requires a completion effect"
            )


class AcceptedRunRepository(Protocol):
    """Atomic use-case boundary for one durable accepted-run authority."""

    def accept_run(self, admission: AcceptedRunAdmission) -> AdmissionResult:
        ...

    def get_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> AcceptedRunSnapshot | None:
        ...

    def read_events(
        self,
        *,
        tenant_id: str,
        run_id: str,
        after_sequence: int,
        limit: int,
    ) -> AcceptedRunEventPage:
        ...

    def get_checkpoint(
        self,
        *,
        tenant_id: str,
        run_id: str,
        checkpoint_digest: str,
    ) -> StoredRuntimeCheckpoint | None:
        ...

    def claim_run(
        self,
        request: AcceptedRunClaimRequest,
    ) -> AcceptedRunClaim | None:
        ...

    def commit_waiting(
        self,
        command: AcceptedRunWaitingCommit,
    ) -> AcceptedRunSnapshot:
        ...

    def accept_callback_and_queue_resume(
        self,
        command: AcceptedRunCallbackCommit,
    ) -> CallbackAcceptance:
        ...

    def commit_terminal(
        self,
        command: AcceptedRunTerminalCommit,
    ) -> AcceptedRunSnapshot:
        ...
