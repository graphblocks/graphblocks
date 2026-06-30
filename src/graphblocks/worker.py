from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Literal


WORKER_PROTOCOL_VERSION = 1

WorkerState = Literal[
    "starting",
    "warming",
    "ready",
    "saturated",
    "draining",
    "degraded",
    "unhealthy",
    "terminated",
]
VALID_WORKER_STATES = frozenset(
    {
        "starting",
        "warming",
        "ready",
        "saturated",
        "draining",
        "degraded",
        "unhealthy",
        "terminated",
    }
)
WorkerDrainWorkloadKind = Literal["online_request", "durable_task", "realtime_session"]
WorkerDrainDisposition = Literal[
    "finish_in_place",
    "cancel",
    "checkpoint",
    "disconnect_with_resume_token",
]


@dataclass(frozen=True, slots=True)
class BlockCapability:
    block: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "block",
            _validate_worker_non_empty_string("block capability", "block", self.block),
        )

    def to_wire(self) -> dict[str, str]:
        return {"block": self.block}

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> BlockCapability:
        return cls(block=payload["block"])


@dataclass(frozen=True, slots=True)
class WorkerAdvertisement:
    worker_id: str
    target_id: str
    package_lock_hash: str
    image_digest: str
    supported_blocks: tuple[BlockCapability, ...]
    protocol_version: int = WORKER_PROTOCOL_VERSION
    state: WorkerState = "ready"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "worker_id",
            _validate_worker_non_empty_string("worker advertisement", "worker_id", self.worker_id),
        )
        object.__setattr__(
            self,
            "target_id",
            _validate_worker_non_empty_string("worker advertisement", "target_id", self.target_id),
        )
        object.__setattr__(
            self,
            "package_lock_hash",
            _validate_worker_non_empty_string(
                "worker advertisement",
                "package_lock_hash",
                self.package_lock_hash,
            ),
        )
        object.__setattr__(
            self,
            "image_digest",
            _validate_worker_non_empty_string("worker advertisement", "image_digest", self.image_digest),
        )
        if not isinstance(self.protocol_version, int) or isinstance(self.protocol_version, bool):
            raise WorkerProtocolError("worker advertisement protocol_version must be an integer")
        if self.protocol_version < 0:
            raise WorkerProtocolError("worker advertisement protocol_version must not be negative")
        if self.state not in VALID_WORKER_STATES:
            raise WorkerProtocolError(f"worker advertisement state has invalid value {self.state!r}")
        if isinstance(self.supported_blocks, str):
            raise WorkerProtocolError("worker advertisement supported_blocks must be iterable")
        try:
            supported_blocks = tuple(self.supported_blocks)
        except TypeError as error:
            raise WorkerProtocolError("worker advertisement supported_blocks must be iterable") from error
        if any(not isinstance(block, BlockCapability) for block in supported_blocks):
            raise WorkerProtocolError("worker advertisement supported_blocks must be BlockCapability")
        object.__setattr__(
            self,
            "supported_blocks",
            supported_blocks,
        )

    @classmethod
    def new(
        cls,
        worker_id: str,
        target_id: str,
        package_lock_hash: str,
        image_digest: str,
        supported_blocks: list[BlockCapability] | tuple[BlockCapability, ...],
    ) -> WorkerAdvertisement:
        return cls(
            worker_id=worker_id,
            target_id=target_id,
            package_lock_hash=package_lock_hash,
            image_digest=image_digest,
            supported_blocks=tuple(supported_blocks),
        )

    def with_state(self, state: WorkerState) -> WorkerAdvertisement:
        return replace(self, state=state)

    def with_protocol_version(self, protocol_version: int) -> WorkerAdvertisement:
        return replace(self, protocol_version=protocol_version)

    def to_wire(self) -> dict[str, object]:
        return {
            "protocolVersion": self.protocol_version,
            "workerId": self.worker_id,
            "targetId": self.target_id,
            "packageLockHash": self.package_lock_hash,
            "imageDigest": self.image_digest,
            "supportedBlocks": [capability.to_wire() for capability in self.supported_blocks],
            "state": self.state,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerAdvertisement:
        supported_blocks = payload.get("supportedBlocks", [])
        if not isinstance(supported_blocks, list):
            raise WorkerProtocolError("worker advertisement supportedBlocks must be a list")
        for item in supported_blocks:
            if not isinstance(item, dict):
                raise WorkerProtocolError("worker advertisement supportedBlocks entries must be mappings")
        return cls(
            worker_id=payload["workerId"],
            target_id=payload["targetId"],
            package_lock_hash=payload["packageLockHash"],
            image_digest=payload["imageDigest"],
            supported_blocks=tuple(
                BlockCapability.from_wire(item)
                for item in supported_blocks
            ),
            protocol_version=payload.get("protocolVersion", WORKER_PROTOCOL_VERSION),
            state=payload.get("state", "ready"),
        )


@dataclass(frozen=True, slots=True)
class WorkerAdmissionPolicy:
    protocol_version: int = WORKER_PROTOCOL_VERSION
    package_lock_hash: str | None = None
    required_block: str | None = None

    @classmethod
    def current(cls) -> WorkerAdmissionPolicy:
        return cls()

    def require_package_lock_hash(self, package_lock_hash: str) -> WorkerAdmissionPolicy:
        return replace(self, package_lock_hash=package_lock_hash)

    def require_block(self, block: str) -> WorkerAdmissionPolicy:
        return replace(self, required_block=block)


@dataclass(frozen=True, slots=True)
class WorkerAdmissionDecision:
    admitted: bool
    worker_id: str
    target_id: str
    protocol_version: int
    package_lock_hash: str
    state: WorkerState
    reason_codes: tuple[str, ...] = ()
    required_block: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise WorkerProtocolError("worker admission decision admitted must be a boolean")
        for field_name in ("worker_id", "target_id", "package_lock_hash"):
            object.__setattr__(
                self,
                field_name,
                _validate_worker_non_empty_string(
                    "worker admission decision",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        if not isinstance(self.protocol_version, int) or isinstance(self.protocol_version, bool):
            raise WorkerProtocolError("worker admission decision protocol_version must be an integer")
        if self.protocol_version < 0:
            raise WorkerProtocolError("worker admission decision protocol_version must not be negative")
        if self.state not in VALID_WORKER_STATES:
            raise WorkerProtocolError(f"worker admission decision state has invalid value {self.state!r}")
        if isinstance(self.reason_codes, str):
            raise WorkerProtocolError("worker admission decision reason_codes must be iterable")
        try:
            reason_codes = tuple(self.reason_codes)
        except TypeError as error:
            raise WorkerProtocolError("worker admission decision reason_codes must be iterable") from error
        for reason_code in reason_codes:
            _validate_worker_non_empty_string(
                "worker admission decision",
                "reason_code",
                reason_code,
            )
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(
            self,
            "required_block",
            _validate_worker_optional_non_empty_string(
                "worker admission decision",
                "required_block",
                self.required_block,
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "workerId": self.worker_id,
            "targetId": self.target_id,
            "protocolVersion": self.protocol_version,
            "packageLockHash": self.package_lock_hash,
            "state": self.state,
            "reasonCodes": list(self.reason_codes),
            "requiredBlock": self.required_block,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerAdmissionDecision:
        reason_codes = payload.get("reasonCodes", [])
        if not isinstance(reason_codes, list):
            raise WorkerProtocolError("worker admission decision reasonCodes must be a list")
        return cls(
            admitted=payload["admitted"],
            worker_id=payload["workerId"],
            target_id=payload["targetId"],
            protocol_version=payload["protocolVersion"],
            package_lock_hash=payload["packageLockHash"],
            state=payload["state"],
            reason_codes=tuple(reason_codes),
            required_block=payload.get("requiredBlock"),
        )


class WorkerProtocolError(ValueError):
    """Base error for invalid worker protocol contracts."""


class WorkerIncompatibleVersionError(WorkerProtocolError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"incompatible worker protocol version: expected {expected}, got {actual}")


class WorkerIncompatiblePackageLockError(WorkerProtocolError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"incompatible worker package lock: expected {expected!r}, got {actual!r}")


class WorkerEmptyWorkerIdError(WorkerProtocolError):
    pass


class WorkerEmptyTargetIdError(WorkerProtocolError):
    pass


class WorkerEmptyPackageLockHashError(WorkerProtocolError):
    pass


class WorkerEmptyImageDigestError(WorkerProtocolError):
    pass


class WorkerEmptySupportedBlocksError(WorkerProtocolError):
    pass


class WorkerMissingRequiredBlockError(WorkerProtocolError):
    def __init__(self, required_block: str) -> None:
        self.required_block = required_block
        super().__init__(f"worker does not support required block {required_block!r}")


def _validate_worker_non_empty_string(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str):
        raise WorkerProtocolError(f"{owner} {field_name} must be a string")
    if not value.strip():
        raise WorkerProtocolError(f"{owner} {field_name} must not be empty")
    return value


def _validate_worker_optional_non_empty_string(
    owner: str,
    field_name: str,
    value: object,
) -> str | None:
    if value is None:
        return None
    return _validate_worker_non_empty_string(owner, field_name, value)


def _validate_worker_string_attributes(owner: str, value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise WorkerProtocolError(f"{owner} attributes must be a mapping")
    attributes = dict(value)
    for key, item in attributes.items():
        if not isinstance(key, str):
            raise WorkerProtocolError(f"{owner} attribute keys must be strings")
        if not key.strip():
            raise WorkerProtocolError(f"{owner} attribute keys must not be empty")
        if not isinstance(item, str):
            raise WorkerProtocolError(f"{owner} attribute values must be strings")
    return attributes


def _validate_worker_non_negative_integer(owner: str, field_name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerProtocolError(f"{owner} {field_name} must be an integer")
    if value < 0:
        raise WorkerProtocolError(f"{owner} {field_name} must not be negative")
    return value


def admit_worker(advertisement: WorkerAdvertisement) -> None:
    admit_worker_with_policy(WorkerAdmissionPolicy.current(), advertisement)


def admit_worker_with_policy(policy: WorkerAdmissionPolicy, advertisement: WorkerAdvertisement) -> None:
    if advertisement.protocol_version != policy.protocol_version:
        raise WorkerIncompatibleVersionError(policy.protocol_version, advertisement.protocol_version)
    if advertisement.worker_id == "":
        raise WorkerEmptyWorkerIdError("worker_id must not be empty")
    if advertisement.target_id == "":
        raise WorkerEmptyTargetIdError("target_id must not be empty")
    if advertisement.package_lock_hash == "":
        raise WorkerEmptyPackageLockHashError("package_lock_hash must not be empty")
    if advertisement.image_digest == "":
        raise WorkerEmptyImageDigestError("image_digest must not be empty")
    if (
        policy.package_lock_hash is not None
        and advertisement.package_lock_hash != policy.package_lock_hash
    ):
        raise WorkerIncompatiblePackageLockError(policy.package_lock_hash, advertisement.package_lock_hash)
    if not advertisement.supported_blocks:
        raise WorkerEmptySupportedBlocksError("supported_blocks must not be empty")
    if policy.required_block is not None and policy.required_block not in {
        capability.block for capability in advertisement.supported_blocks
    }:
        raise WorkerMissingRequiredBlockError(policy.required_block)


def evaluate_worker_admission(
    policy: WorkerAdmissionPolicy,
    advertisement: WorkerAdvertisement,
) -> WorkerAdmissionDecision:
    reason_codes: list[str] = []
    if advertisement.protocol_version != policy.protocol_version:
        reason_codes.append("worker.incompatible_protocol_version")
    if advertisement.worker_id == "":
        reason_codes.append("worker.empty_worker_id")
    if advertisement.target_id == "":
        reason_codes.append("worker.empty_target_id")
    if advertisement.package_lock_hash == "":
        reason_codes.append("worker.empty_package_lock_hash")
    if advertisement.image_digest == "":
        reason_codes.append("worker.empty_image_digest")
    if (
        policy.package_lock_hash is not None
        and advertisement.package_lock_hash != policy.package_lock_hash
    ):
        reason_codes.append("worker.incompatible_package_lock")
    if not advertisement.supported_blocks:
        reason_codes.append("worker.empty_supported_blocks")
    if advertisement.state != "ready":
        reason_codes.append("worker.not_ready")
    if policy.required_block is not None and policy.required_block not in {
        capability.block for capability in advertisement.supported_blocks
    }:
        reason_codes.append("worker.missing_required_block")
    return WorkerAdmissionDecision(
        admitted=not reason_codes,
        worker_id=advertisement.worker_id,
        target_id=advertisement.target_id,
        protocol_version=advertisement.protocol_version,
        package_lock_hash=advertisement.package_lock_hash,
        state=advertisement.state,
        reason_codes=tuple(reason_codes),
        required_block=policy.required_block,
    )


class WorkerSelectionError(ValueError):
    """Base error for worker selection failures."""


class WorkerNoEligibleWorkerError(WorkerSelectionError):
    def __init__(self, block: str) -> None:
        self.block = block
        super().__init__(f"no eligible worker for block {block!r}")


def select_worker_for_block(workers: list[WorkerAdvertisement] | tuple[WorkerAdvertisement, ...], block: str) -> WorkerAdvertisement:
    selected: WorkerAdvertisement | None = None
    for worker in workers:
        if worker.state != "ready":
            continue
        if block not in {capability.block for capability in worker.supported_blocks}:
            continue
        if selected is None or worker.worker_id < selected.worker_id:
            selected = worker
    if selected is None:
        raise WorkerNoEligibleWorkerError(block)
    return selected


@dataclass(frozen=True, slots=True)
class RunOwnershipLease:
    run_id: str
    owner_instance_id: str
    lease_epoch: int
    expires_at_unix_ms: int
    last_checkpoint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            _validate_worker_non_empty_string("run ownership lease", "run_id", self.run_id),
        )
        object.__setattr__(
            self,
            "owner_instance_id",
            _validate_worker_non_empty_string(
                "run ownership lease",
                "owner_instance_id",
                self.owner_instance_id,
            ),
        )
        object.__setattr__(
            self,
            "lease_epoch",
            _validate_worker_non_negative_integer(
                "run ownership lease",
                "lease_epoch",
                self.lease_epoch,
            ),
        )
        object.__setattr__(
            self,
            "expires_at_unix_ms",
            _validate_worker_non_negative_integer(
                "run ownership lease",
                "expires_at_unix_ms",
                self.expires_at_unix_ms,
            ),
        )
        object.__setattr__(
            self,
            "last_checkpoint",
            _validate_worker_optional_non_empty_string(
                "run ownership lease",
                "last_checkpoint",
                self.last_checkpoint,
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "ownerInstanceId": self.owner_instance_id,
            "leaseEpoch": self.lease_epoch,
            "expiresAtUnixMs": self.expires_at_unix_ms,
            "lastCheckpoint": self.last_checkpoint,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> RunOwnershipLease:
        last_checkpoint = payload.get("lastCheckpoint")
        return cls(
            run_id=payload["runId"],
            owner_instance_id=payload["ownerInstanceId"],
            lease_epoch=payload["leaseEpoch"],
            expires_at_unix_ms=payload["expiresAtUnixMs"],
            last_checkpoint=last_checkpoint,
        )


@dataclass(frozen=True, slots=True)
class RemotePayloadLimits:
    max_inline_bytes: int


class RemotePayloadError(ValueError):
    """Base error for invalid remote payload contracts."""


class RemotePayloadOversizedInlineError(RemotePayloadError):
    def __init__(self, max_inline_bytes: int, actual_inline_bytes: int) -> None:
        self.max_inline_bytes = max_inline_bytes
        self.actual_inline_bytes = actual_inline_bytes
        super().__init__(
            f"remote inline payload is {actual_inline_bytes} bytes, exceeding limit {max_inline_bytes}"
        )


class RemotePayloadInvalidArtifactRefError(RemotePayloadError):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"remote artifact reference has invalid {field}")


class RemotePayloadInlineJsonEncodingError(RemotePayloadError):
    pass


class RemotePayloadInvalidModeError(RemotePayloadError):
    def __init__(self, mode: object) -> None:
        self.mode = mode
        super().__init__(f"invalid remote payload mode {mode!r}")


def validate_remote_payload(payload: Mapping[str, object], limits: RemotePayloadLimits) -> None:
    mode = payload.get("mode")
    if mode == "inline":
        try:
            actual_inline_bytes = len(
                json.dumps(
                    payload.get("value"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise RemotePayloadInlineJsonEncodingError("remote inline payload is not JSON serializable") from error
        if actual_inline_bytes > limits.max_inline_bytes:
            raise RemotePayloadOversizedInlineError(limits.max_inline_bytes, actual_inline_bytes)
        return
    if mode == "artifact_ref":
        artifact = payload.get("artifact")
        if not isinstance(artifact, Mapping):
            raise RemotePayloadInvalidArtifactRefError("artifact")
        artifact_id = artifact.get("artifact_id", artifact.get("artifactId"))
        if not isinstance(artifact_id, str) or artifact_id == "":
            raise RemotePayloadInvalidArtifactRefError("artifact_id")
        uri = artifact.get("uri")
        if not isinstance(uri, str) or uri == "":
            raise RemotePayloadInvalidArtifactRefError("uri")
        return
    raise RemotePayloadInvalidModeError(mode)


@dataclass(frozen=True, slots=True)
class RemoteEdgePayload:
    mode: Literal["inline", "artifact_ref"]
    schema: str
    value: object | None = None
    artifact: Mapping[str, object] | None = None
    value_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.schema, str) or not self.schema.strip():
            raise RemotePayloadInvalidModeError("empty_schema")
        if self.mode == "inline":
            if self.value_digest is not None and not isinstance(self.value_digest, str):
                raise RemotePayloadInvalidModeError("value_digest")
            try:
                encoded = json.dumps(
                    self.value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            except (TypeError, ValueError) as error:
                raise RemotePayloadInlineJsonEncodingError("remote inline payload is not JSON serializable") from error
            computed_digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if self.value_digest is not None and self.value_digest != computed_digest:
                raise RemotePayloadInvalidModeError("value_digest_mismatch")
            object.__setattr__(self, "value", json.loads(encoded))
            object.__setattr__(
                self,
                "value_digest",
                computed_digest,
            )
            object.__setattr__(self, "artifact", None)
            return
        if self.mode == "artifact_ref":
            if not isinstance(self.artifact, Mapping):
                raise RemotePayloadInvalidArtifactRefError("artifact")
            artifact = dict(self.artifact)
            if any(not isinstance(key, str) for key in artifact):
                raise RemotePayloadInvalidArtifactRefError("artifact")
            if self.value is not None:
                raise RemotePayloadInvalidModeError("artifact_ref_with_inline_value")
            validate_remote_payload(
                {"mode": "artifact_ref", "schema": self.schema, "artifact": artifact},
                RemotePayloadLimits(max_inline_bytes=0),
            )
            object.__setattr__(self, "artifact", {key: artifact[key] for key in sorted(artifact)})
            object.__setattr__(self, "value_digest", None)
            return
        raise RemotePayloadInvalidModeError(self.mode)

    @classmethod
    def inline(
        cls,
        schema: str,
        value: object,
        limits: RemotePayloadLimits,
    ) -> RemoteEdgePayload:
        payload = cls(mode="inline", schema=schema, value=value)
        validate_remote_payload(payload.to_wire(), limits)
        return payload

    @classmethod
    def artifact_ref(
        cls,
        schema: str,
        *,
        artifact_id: str,
        uri: str,
        size_bytes: int | None = None,
        digest: str | None = None,
    ) -> RemoteEdgePayload:
        artifact: dict[str, object] = {
            "artifact_id": artifact_id,
            "uri": uri,
        }
        if size_bytes is not None:
            artifact["size_bytes"] = size_bytes
        if digest is not None:
            artifact["digest"] = digest
        payload = cls(mode="artifact_ref", schema=schema, artifact=artifact)
        validate_remote_payload(payload.to_wire(), RemotePayloadLimits(max_inline_bytes=0))
        return payload

    def to_wire(self) -> dict[str, object]:
        if self.mode == "inline":
            return {
                "mode": self.mode,
                "schema": self.schema,
                "value": self.value,
                "valueDigest": self.value_digest,
            }
        return {
            "mode": self.mode,
            "schema": self.schema,
            "artifact": dict(self.artifact or {}),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> RemoteEdgePayload:
        mode = payload.get("mode")
        if mode == "inline":
            return cls(
                mode="inline",
                schema=payload["schema"],
                value=payload.get("value"),
                value_digest=payload.get("valueDigest"),
            )
        if mode == "artifact_ref":
            artifact = payload.get("artifact")
            if not isinstance(artifact, Mapping):
                raise RemotePayloadInvalidArtifactRefError("artifact")
            return cls(mode="artifact_ref", schema=payload["schema"], artifact=artifact)
        raise RemotePayloadInvalidModeError(mode)

    def content_digest(self) -> str:
        encoded = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkerInvocationContext:
    release_id: str
    deployment_revision_id: str
    trace_id: str | None = None
    parent_span_id: str | None = None
    policy_snapshot_id: str | None = None
    policy_snapshot_digest: str | None = None
    budget_permit_id: str | None = None
    budget_permit_digest: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "release_id",
            _validate_worker_non_empty_string(
                "worker invocation context",
                "release_id",
                self.release_id,
            ),
        )
        object.__setattr__(
            self,
            "deployment_revision_id",
            _validate_worker_non_empty_string(
                "worker invocation context",
                "deployment_revision_id",
                self.deployment_revision_id,
            ),
        )
        for field_name in (
            "trace_id",
            "parent_span_id",
            "policy_snapshot_id",
            "policy_snapshot_digest",
            "budget_permit_id",
            "budget_permit_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_worker_optional_non_empty_string(
                    "worker invocation context",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        if (self.policy_snapshot_id is None) != (self.policy_snapshot_digest is None):
            raise WorkerProtocolError(
                "worker invocation context policy snapshot id and digest must be provided together"
            )
        if (self.budget_permit_id is None) != (self.budget_permit_digest is None):
            raise WorkerProtocolError(
                "worker invocation context budget permit id and digest must be provided together"
            )
        object.__setattr__(
            self,
            "attributes",
            _validate_worker_string_attributes("worker invocation context", self.attributes),
        )

    def with_trace(self, trace_id: str, parent_span_id: str) -> WorkerInvocationContext:
        return replace(self, trace_id=trace_id, parent_span_id=parent_span_id)

    def with_policy_snapshot(self, policy_snapshot_id: str, policy_snapshot_digest: str) -> WorkerInvocationContext:
        return replace(
            self,
            policy_snapshot_id=policy_snapshot_id,
            policy_snapshot_digest=policy_snapshot_digest,
        )

    def with_budget_permit(self, budget_permit_id: str, budget_permit_digest: str) -> WorkerInvocationContext:
        return replace(self, budget_permit_id=budget_permit_id, budget_permit_digest=budget_permit_digest)

    def with_attribute(self, key: str, value: str) -> WorkerInvocationContext:
        attributes = dict(self.attributes)
        attributes[key] = value
        return replace(self, attributes=attributes)

    def to_wire(self) -> dict[str, object]:
        return {
            "releaseId": self.release_id,
            "deploymentRevisionId": self.deployment_revision_id,
            "traceId": self.trace_id,
            "parentSpanId": self.parent_span_id,
            "policySnapshotId": self.policy_snapshot_id,
            "policySnapshotDigest": self.policy_snapshot_digest,
            "budgetPermitId": self.budget_permit_id,
            "budgetPermitDigest": self.budget_permit_digest,
            "attributes": dict(sorted(self.attributes.items())),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerInvocationContext:
        attributes = payload.get("attributes", {})
        return cls(
            release_id=payload.get("releaseId"),
            deployment_revision_id=payload.get("deploymentRevisionId"),
            trace_id=payload.get("traceId"),
            parent_span_id=payload.get("parentSpanId"),
            policy_snapshot_id=payload.get("policySnapshotId"),
            policy_snapshot_digest=payload.get("policySnapshotDigest"),
            budget_permit_id=payload.get("budgetPermitId"),
            budget_permit_digest=payload.get("budgetPermitDigest"),
            attributes=attributes,
        )


@dataclass(frozen=True, slots=True)
class WorkerInvokeRequest:
    invocation_id: str
    run_id: str
    node_id: str
    node_attempt_id: str
    lease_epoch: int
    block: str
    context: WorkerInvocationContext
    inputs: object
    config: object

    def __post_init__(self) -> None:
        for field_name in (
            "invocation_id",
            "run_id",
            "node_id",
            "node_attempt_id",
            "block",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_worker_non_empty_string(
                    "worker invoke request",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool):
            raise WorkerProtocolError("worker invoke request lease_epoch must be an integer")
        if self.lease_epoch < 0:
            raise WorkerProtocolError("worker invoke request lease_epoch must not be negative")
        if not isinstance(self.context, WorkerInvocationContext):
            raise WorkerProtocolError(
                "worker invoke request context must be a WorkerInvocationContext"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "invocationId": self.invocation_id,
            "runId": self.run_id,
            "nodeId": self.node_id,
            "nodeAttemptId": self.node_attempt_id,
            "leaseEpoch": self.lease_epoch,
            "block": self.block,
            "context": self.context.to_wire(),
            "inputs": self.inputs,
            "config": self.config,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerInvokeRequest:
        context = payload["context"]
        if not isinstance(context, Mapping):
            raise WorkerProtocolError("worker invoke request context must be a mapping")
        return cls(
            invocation_id=payload["invocationId"],
            run_id=payload["runId"],
            node_id=payload["nodeId"],
            node_attempt_id=payload["nodeAttemptId"],
            lease_epoch=payload["leaseEpoch"],
            block=payload["block"],
            context=WorkerInvocationContext.from_wire(dict(context)),
            inputs=payload.get("inputs"),
            config=payload.get("config"),
        )


@dataclass(frozen=True, slots=True)
class WorkerDrainPolicy:
    online_request_timeout_ms: int = 30_000
    durable_task_timeout_ms: int = 300_000
    realtime_session_timeout_ms: int = 600_000
    on_deadline_online_request: WorkerDrainDisposition = "cancel"
    on_deadline_durable_task: WorkerDrainDisposition = "checkpoint"
    on_deadline_realtime_session: WorkerDrainDisposition = "disconnect_with_resume_token"

    def __post_init__(self) -> None:
        for field_name, value in (
            ("online_request_timeout_ms", self.online_request_timeout_ms),
            ("durable_task_timeout_ms", self.durable_task_timeout_ms),
            ("realtime_session_timeout_ms", self.realtime_session_timeout_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise WorkerProtocolError(f"{field_name} must be an integer")
            if value <= 0:
                raise WorkerProtocolError(f"{field_name} must be positive")
        valid_dispositions = {"finish_in_place", "cancel", "checkpoint", "disconnect_with_resume_token"}
        for field_name, value in (
            ("on_deadline_online_request", self.on_deadline_online_request),
            ("on_deadline_durable_task", self.on_deadline_durable_task),
            ("on_deadline_realtime_session", self.on_deadline_realtime_session),
        ):
            if value not in valid_dispositions:
                raise WorkerProtocolError(f"{field_name} has invalid disposition {value!r}")

    def to_wire(self) -> dict[str, object]:
        return {
            "onlineRequestTimeoutMs": self.online_request_timeout_ms,
            "durableTaskTimeoutMs": self.durable_task_timeout_ms,
            "realtimeSessionTimeoutMs": self.realtime_session_timeout_ms,
            "onDeadline": {
                "onlineRequest": self.on_deadline_online_request,
                "durableTask": self.on_deadline_durable_task,
                "realtimeSession": self.on_deadline_realtime_session,
            },
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerDrainPolicy:
        on_deadline = payload.get("onDeadline", {})
        if not isinstance(on_deadline, Mapping):
            raise WorkerProtocolError("worker drain policy onDeadline must be a mapping")
        return cls(
            online_request_timeout_ms=payload.get("onlineRequestTimeoutMs", 30_000),
            durable_task_timeout_ms=payload.get("durableTaskTimeoutMs", 300_000),
            realtime_session_timeout_ms=payload.get("realtimeSessionTimeoutMs", 600_000),
            on_deadline_online_request=on_deadline.get("onlineRequest", "cancel"),
            on_deadline_durable_task=on_deadline.get("durableTask", "checkpoint"),
            on_deadline_realtime_session=(
                on_deadline.get("realtimeSession", "disconnect_with_resume_token")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerDrainTask:
    workload: WorkerDrainWorkloadKind
    request: WorkerInvokeRequest
    started_at_unix_ms: int
    checkpointable: bool = False

    def __post_init__(self) -> None:
        if self.workload not in {"online_request", "durable_task", "realtime_session"}:
            raise WorkerProtocolError(f"invalid drain workload {self.workload!r}")
        if not isinstance(self.request, WorkerInvokeRequest):
            raise WorkerProtocolError("worker drain task request must be a WorkerInvokeRequest")
        object.__setattr__(
            self,
            "started_at_unix_ms",
            _validate_worker_non_negative_integer(
                "worker drain task",
                "started_at_unix_ms",
                self.started_at_unix_ms,
            ),
        )
        if not isinstance(self.checkpointable, bool):
            raise WorkerProtocolError("worker drain task checkpointable must be a boolean")

    def to_wire(self) -> dict[str, object]:
        return {
            "workload": self.workload,
            "request": self.request.to_wire(),
            "startedAtUnixMs": self.started_at_unix_ms,
            "checkpointable": self.checkpointable,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerDrainTask:
        request = payload.get("request")
        if not isinstance(request, dict):
            raise WorkerProtocolError("worker drain task request must be a mapping")
        return cls(
            workload=payload["workload"],
            request=WorkerInvokeRequest.from_wire(request),
            started_at_unix_ms=payload["startedAtUnixMs"],
            checkpointable=payload.get("checkpointable", False),
        )


@dataclass(frozen=True, slots=True)
class WorkerDrainDecision:
    workload: WorkerDrainWorkloadKind
    run_id: str
    invocation_id: str
    node_attempt_id: str
    lease_epoch: int
    release_id: str
    deployment_revision_id: str
    disposition: WorkerDrainDisposition
    deadline_unix_ms: int
    reason: str

    def __post_init__(self) -> None:
        if self.workload not in {"online_request", "durable_task", "realtime_session"}:
            raise WorkerProtocolError(f"invalid drain workload {self.workload!r}")
        valid_dispositions = {"finish_in_place", "cancel", "checkpoint", "disconnect_with_resume_token"}
        if self.disposition not in valid_dispositions:
            raise WorkerProtocolError(
                f"worker drain decision disposition has invalid disposition {self.disposition!r}"
            )
        for field_name in (
            "run_id",
            "invocation_id",
            "node_attempt_id",
            "release_id",
            "deployment_revision_id",
            "reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_worker_non_empty_string(
                    "worker drain decision",
                    field_name,
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "lease_epoch",
            _validate_worker_non_negative_integer(
                "worker drain decision",
                "lease_epoch",
                self.lease_epoch,
            ),
        )
        object.__setattr__(
            self,
            "deadline_unix_ms",
            _validate_worker_non_negative_integer(
                "worker drain decision",
                "deadline_unix_ms",
                self.deadline_unix_ms,
            ),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "workload": self.workload,
            "runId": self.run_id,
            "invocationId": self.invocation_id,
            "nodeAttemptId": self.node_attempt_id,
            "leaseEpoch": self.lease_epoch,
            "releaseId": self.release_id,
            "deploymentRevisionId": self.deployment_revision_id,
            "disposition": self.disposition,
            "deadlineUnixMs": self.deadline_unix_ms,
            "reason": self.reason,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerDrainDecision:
        return cls(
            workload=payload["workload"],
            run_id=payload["runId"],
            invocation_id=payload["invocationId"],
            node_attempt_id=payload["nodeAttemptId"],
            lease_epoch=payload["leaseEpoch"],
            release_id=payload["releaseId"],
            deployment_revision_id=payload["deploymentRevisionId"],
            disposition=payload["disposition"],
            deadline_unix_ms=payload["deadlineUnixMs"],
            reason=payload["reason"],
        )


@dataclass(frozen=True, slots=True)
class WorkerDrainPlan:
    worker_id: str
    target_id: str
    drain_started_at_unix_ms: int
    decisions: tuple[WorkerDrainDecision, ...]
    worker_state: WorkerState = "draining"
    admission_closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "worker_id",
            _validate_worker_non_empty_string("worker drain plan", "worker_id", self.worker_id),
        )
        object.__setattr__(
            self,
            "target_id",
            _validate_worker_non_empty_string("worker drain plan", "target_id", self.target_id),
        )
        if self.worker_state != "draining":
            raise WorkerProtocolError("worker drain plan state must be draining")
        if not isinstance(self.admission_closed, bool):
            raise WorkerProtocolError("worker drain plan admission_closed must be a boolean")
        object.__setattr__(
            self,
            "drain_started_at_unix_ms",
            _validate_worker_non_negative_integer(
                "worker drain plan",
                "drain_started_at_unix_ms",
                self.drain_started_at_unix_ms,
            ),
        )
        try:
            decisions = tuple(self.decisions)
        except TypeError as error:
            raise WorkerProtocolError("worker drain plan decisions must be iterable") from error
        if any(not isinstance(decision, WorkerDrainDecision) for decision in decisions):
            raise WorkerProtocolError("worker drain plan decisions must be WorkerDrainDecision")
        object.__setattr__(self, "decisions", decisions)

    @classmethod
    def for_worker(
        cls,
        worker: WorkerAdvertisement,
        policy: WorkerDrainPolicy,
        tasks: tuple[WorkerDrainTask, ...] | list[WorkerDrainTask],
        *,
        drain_started_at_unix_ms: int,
        now_unix_ms: int,
    ) -> WorkerDrainPlan:
        decisions: list[WorkerDrainDecision] = []
        for task in tasks:
            if task.workload == "online_request":
                timeout_ms = policy.online_request_timeout_ms
                deadline_disposition = policy.on_deadline_online_request
            elif task.workload == "durable_task":
                timeout_ms = policy.durable_task_timeout_ms
                deadline_disposition = policy.on_deadline_durable_task
            elif task.workload == "realtime_session":
                timeout_ms = policy.realtime_session_timeout_ms
                deadline_disposition = policy.on_deadline_realtime_session
            else:
                raise WorkerProtocolError(f"invalid drain workload {task.workload!r}")
            deadline_unix_ms = task.started_at_unix_ms + timeout_ms
            if now_unix_ms >= deadline_unix_ms:
                disposition = deadline_disposition
                reason = "deadline_reached"
                if disposition == "checkpoint" and not task.checkpointable:
                    disposition = "cancel"
                    reason = "checkpoint_unavailable"
            else:
                disposition = "finish_in_place"
                reason = "within_drain_deadline"
            decisions.append(
                WorkerDrainDecision(
                    workload=task.workload,
                    run_id=task.request.run_id,
                    invocation_id=task.request.invocation_id,
                    node_attempt_id=task.request.node_attempt_id,
                    lease_epoch=task.request.lease_epoch,
                    release_id=task.request.context.release_id,
                    deployment_revision_id=task.request.context.deployment_revision_id,
                    disposition=disposition,
                    deadline_unix_ms=deadline_unix_ms,
                    reason=reason,
                )
            )
        return cls(
            worker_id=worker.worker_id,
            target_id=worker.target_id,
            drain_started_at_unix_ms=drain_started_at_unix_ms,
            decisions=tuple(decisions),
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "workerId": self.worker_id,
            "targetId": self.target_id,
            "workerState": self.worker_state,
            "admissionClosed": self.admission_closed,
            "drainStartedAtUnixMs": self.drain_started_at_unix_ms,
            "decisions": [decision.to_wire() for decision in self.decisions],
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerDrainPlan:
        decisions = payload.get("decisions", [])
        if not isinstance(decisions, list):
            raise WorkerProtocolError("worker drain plan decisions must be a list")
        for decision in decisions:
            if not isinstance(decision, dict):
                raise WorkerProtocolError("worker drain plan decisions must be mappings")
        return cls(
            worker_id=payload["workerId"],
            target_id=payload["targetId"],
            worker_state=payload.get("workerState", "draining"),
            admission_closed=payload.get("admissionClosed", True),
            drain_started_at_unix_ms=payload["drainStartedAtUnixMs"],
            decisions=tuple(
                WorkerDrainDecision.from_wire(decision)
                for decision in decisions
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerInvokeResult:
    invocation_id: str
    node_attempt_id: str
    lease_epoch: int
    outputs: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "invocation_id",
            _validate_worker_non_empty_string(
                "worker invoke result",
                "invocation_id",
                self.invocation_id,
            ),
        )
        object.__setattr__(
            self,
            "node_attempt_id",
            _validate_worker_non_empty_string(
                "worker invoke result",
                "node_attempt_id",
                self.node_attempt_id,
            ),
        )
        if not isinstance(self.lease_epoch, int) or isinstance(self.lease_epoch, bool):
            raise WorkerProtocolError("worker invoke result lease_epoch must be an integer")
        if self.lease_epoch < 0:
            raise WorkerProtocolError("worker invoke result lease_epoch must not be negative")
        if not isinstance(self.outputs, Mapping):
            raise WorkerProtocolError("worker invoke result outputs must be a mapping")
        outputs = dict(self.outputs)
        for key in outputs:
            if not isinstance(key, str):
                raise WorkerProtocolError("worker invoke result output keys must be strings")
            if not key.strip():
                raise WorkerProtocolError("worker invoke result output keys must not be empty")
        object.__setattr__(self, "outputs", outputs)

    def to_wire(self) -> dict[str, object]:
        return {
            "invocationId": self.invocation_id,
            "nodeAttemptId": self.node_attempt_id,
            "leaseEpoch": self.lease_epoch,
            "outputs": dict(sorted(self.outputs.items())),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> WorkerInvokeResult:
        outputs = payload.get("outputs", {})
        if not isinstance(outputs, Mapping):
            raise WorkerProtocolError("worker invoke result outputs must be a mapping")
        return cls(
            invocation_id=payload["invocationId"],
            node_attempt_id=payload["nodeAttemptId"],
            lease_epoch=payload["leaseEpoch"],
            outputs=dict(outputs),
        )


class WorkerResultError(ValueError):
    """Base error for worker result validation failures."""


class WorkerMismatchedInvocationIdError(WorkerResultError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"mismatched invocation_id: expected {expected!r}, got {actual!r}")


class WorkerMismatchedNodeAttemptError(WorkerResultError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"mismatched node_attempt_id: expected {expected!r}, got {actual!r}")


class WorkerStaleLeaseEpochError(WorkerResultError):
    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale lease_epoch: expected {expected}, got {actual}")


def validate_worker_result(request: WorkerInvokeRequest, result: WorkerInvokeResult) -> None:
    if request.invocation_id != result.invocation_id:
        raise WorkerMismatchedInvocationIdError(request.invocation_id, result.invocation_id)
    if request.node_attempt_id != result.node_attempt_id:
        raise WorkerMismatchedNodeAttemptError(request.node_attempt_id, result.node_attempt_id)
    if request.lease_epoch != result.lease_epoch:
        raise WorkerStaleLeaseEpochError(request.lease_epoch, result.lease_epoch)


__all__ = [
    "WORKER_PROTOCOL_VERSION",
    "BlockCapability",
    "RemoteEdgePayload",
    "RemotePayloadError",
    "RemotePayloadInlineJsonEncodingError",
    "RemotePayloadInvalidArtifactRefError",
    "RemotePayloadInvalidModeError",
    "RemotePayloadLimits",
    "RemotePayloadOversizedInlineError",
    "RunOwnershipLease",
    "WorkerAdmissionDecision",
    "WorkerAdmissionPolicy",
    "WorkerAdvertisement",
    "WorkerDrainDecision",
    "WorkerDrainDisposition",
    "WorkerDrainPlan",
    "WorkerDrainPolicy",
    "WorkerDrainTask",
    "WorkerDrainWorkloadKind",
    "WorkerEmptyImageDigestError",
    "WorkerEmptyPackageLockHashError",
    "WorkerEmptySupportedBlocksError",
    "WorkerEmptyTargetIdError",
    "WorkerEmptyWorkerIdError",
    "WorkerIncompatiblePackageLockError",
    "WorkerIncompatibleVersionError",
    "WorkerInvocationContext",
    "WorkerInvokeRequest",
    "WorkerInvokeResult",
    "WorkerMismatchedInvocationIdError",
    "WorkerMismatchedNodeAttemptError",
    "WorkerMissingRequiredBlockError",
    "WorkerNoEligibleWorkerError",
    "WorkerProtocolError",
    "WorkerResultError",
    "WorkerSelectionError",
    "WorkerStaleLeaseEpochError",
    "WorkerState",
    "admit_worker",
    "admit_worker_with_policy",
    "evaluate_worker_admission",
    "select_worker_for_block",
    "validate_remote_payload",
    "validate_worker_result",
]
