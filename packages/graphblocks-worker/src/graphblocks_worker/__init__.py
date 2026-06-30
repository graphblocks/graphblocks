from __future__ import annotations

from collections.abc import Mapping

from graphblocks.worker import (
    WORKER_PROTOCOL_VERSION,
    BlockCapability,
    RemoteEdgePayload,
    RemotePayloadError,
    RemotePayloadInlineJsonEncodingError,
    RemotePayloadInvalidArtifactRefError,
    RemotePayloadInvalidModeError,
    RemotePayloadLimits,
    RemotePayloadOversizedInlineError,
    RunOwnershipLease,
    WorkerAdmissionDecision,
    WorkerAdmissionPolicy,
    WorkerAdvertisement,
    WorkerDrainDecision,
    WorkerDrainDisposition,
    WorkerDrainPlan,
    WorkerDrainPolicy,
    WorkerDrainTask,
    WorkerDrainWorkloadKind,
    WorkerEmptyImageDigestError,
    WorkerEmptyPackageLockHashError,
    WorkerEmptySupportedBlocksError,
    WorkerEmptyTargetIdError,
    WorkerEmptyWorkerIdError,
    WorkerIncompatiblePackageLockError,
    WorkerIncompatibleVersionError,
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    WorkerMismatchedInvocationIdError,
    WorkerMismatchedNodeAttemptError,
    WorkerMissingRequiredBlockError,
    WorkerNoEligibleWorkerError,
    WorkerProtocolError,
    WorkerProtocolMessage,
    WorkerProtocolMessageKind,
    WorkerResultError,
    WorkerSelectionError,
    WorkerStaleLeaseEpochError,
    WorkerState,
    admit_worker,
    admit_worker_with_policy,
    evaluate_worker_admission,
    select_worker_for_block,
    validate_remote_payload,
    validate_worker_result,
)


def validate_worker_protocol_message_native(
    message: WorkerProtocolMessage | Mapping[str, object],
) -> dict[str, object]:
    """Validate a worker protocol envelope through the Rust runtime binding."""

    from graphblocks_runtime import validate_worker_protocol_message

    if isinstance(message, WorkerProtocolMessage):
        return validate_worker_protocol_message(message.to_wire())
    if not isinstance(message, Mapping):
        raise TypeError("message must be a WorkerProtocolMessage or mapping")
    return validate_worker_protocol_message(dict(message))


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
    "WorkerProtocolMessage",
    "WorkerProtocolMessageKind",
    "WorkerResultError",
    "WorkerSelectionError",
    "WorkerStaleLeaseEpochError",
    "WorkerState",
    "admit_worker",
    "admit_worker_with_policy",
    "evaluate_worker_admission",
    "select_worker_for_block",
    "validate_remote_payload",
    "validate_worker_protocol_message_native",
    "validate_worker_result",
]
