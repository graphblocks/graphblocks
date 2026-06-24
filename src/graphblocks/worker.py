from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True, slots=True)
class BlockCapability:
    block: str

    def to_wire(self) -> dict[str, str]:
        return {"block": self.block}

    @classmethod
    def from_wire(cls, payload: dict[str, object]) -> BlockCapability:
        return cls(block=str(payload["block"]))


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
            "supported_blocks",
            tuple(
                block if isinstance(block, BlockCapability) else BlockCapability(str(block))
                for block in self.supported_blocks
            ),
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
            supported_blocks = []
        return cls(
            worker_id=str(payload["workerId"]),
            target_id=str(payload["targetId"]),
            package_lock_hash=str(payload["packageLockHash"]),
            image_digest=str(payload["imageDigest"]),
            supported_blocks=tuple(
                BlockCapability.from_wire(item)
                for item in supported_blocks
                if isinstance(item, dict)
            ),
            protocol_version=int(payload.get("protocolVersion", WORKER_PROTOCOL_VERSION)),
            state=str(payload.get("state", "ready")),
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
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

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
            reason_codes = []
        return cls(
            admitted=bool(payload["admitted"]),
            worker_id=str(payload["workerId"]),
            target_id=str(payload["targetId"]),
            protocol_version=int(payload["protocolVersion"]),
            package_lock_hash=str(payload["packageLockHash"]),
            state=str(payload["state"]),
            reason_codes=tuple(str(code) for code in reason_codes),
            required_block=(
                None if payload.get("requiredBlock") is None else str(payload.get("requiredBlock"))
            ),
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
            run_id=str(payload["runId"]),
            owner_instance_id=str(payload["ownerInstanceId"]),
            lease_epoch=int(payload["leaseEpoch"]),
            expires_at_unix_ms=int(payload["expiresAtUnixMs"]),
            last_checkpoint=None if last_checkpoint is None else str(last_checkpoint),
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
        object.__setattr__(self, "attributes", dict(self.attributes))

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
        attributes = attributes if isinstance(attributes, dict) else {}
        return cls(
            release_id=str(payload["releaseId"]),
            deployment_revision_id=str(payload["deploymentRevisionId"]),
            trace_id=None if payload.get("traceId") is None else str(payload.get("traceId")),
            parent_span_id=None if payload.get("parentSpanId") is None else str(payload.get("parentSpanId")),
            policy_snapshot_id=None if payload.get("policySnapshotId") is None else str(payload.get("policySnapshotId")),
            policy_snapshot_digest=None
            if payload.get("policySnapshotDigest") is None
            else str(payload.get("policySnapshotDigest")),
            budget_permit_id=None if payload.get("budgetPermitId") is None else str(payload.get("budgetPermitId")),
            budget_permit_digest=None
            if payload.get("budgetPermitDigest") is None
            else str(payload.get("budgetPermitDigest")),
            attributes={str(key): str(value) for key, value in attributes.items()},
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
        if not isinstance(context, dict):
            raise ValueError("context must be a mapping")
        return cls(
            invocation_id=str(payload["invocationId"]),
            run_id=str(payload["runId"]),
            node_id=str(payload["nodeId"]),
            node_attempt_id=str(payload["nodeAttemptId"]),
            lease_epoch=int(payload["leaseEpoch"]),
            block=str(payload["block"]),
            context=WorkerInvocationContext.from_wire(context),
            inputs=payload.get("inputs"),
            config=payload.get("config"),
        )


@dataclass(frozen=True, slots=True)
class WorkerInvokeResult:
    invocation_id: str
    node_attempt_id: str
    lease_epoch: int
    outputs: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", dict(self.outputs))

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
        outputs = outputs if isinstance(outputs, dict) else {}
        return cls(
            invocation_id=str(payload["invocationId"]),
            node_attempt_id=str(payload["nodeAttemptId"]),
            lease_epoch=int(payload["leaseEpoch"]),
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
