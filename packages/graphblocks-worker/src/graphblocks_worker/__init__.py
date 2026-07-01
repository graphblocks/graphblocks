from __future__ import annotations

from collections.abc import Mapping

from graphblocks.worker import (
    VALID_WORKER_PROTOCOL_MESSAGE_KINDS,
    VALID_WORKER_STATES,
    WORKER_PROTOCOL_VERSION,
    BlockCapability,
    RemoteEdgePayload,
    RemotePayloadError,
    RemotePayloadInlineJsonEncodingError,
    RemotePayloadInvalidArtifactRefError,
    RemotePayloadInvalidLimitError,
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


def validate_worker_advertisement_native(
    advertisement: WorkerAdvertisement | Mapping[str, object],
    *,
    expected_package_lock_hash: str | None = None,
) -> dict[str, object]:
    """Validate a worker advertisement through the Rust runtime binding."""

    from graphblocks_runtime import validate_worker_advertisement

    if isinstance(advertisement, WorkerAdvertisement):
        wire_advertisement = advertisement.to_wire()
    elif isinstance(advertisement, Mapping):
        wire_advertisement = dict(advertisement)
    else:
        raise TypeError("advertisement must be a WorkerAdvertisement or mapping")
    return validate_worker_advertisement(
        wire_advertisement,
        expected_package_lock_hash=expected_package_lock_hash,
    )


def validate_remote_payload_native(
    payload: RemoteEdgePayload | Mapping[str, object],
    *,
    max_inline_bytes: int,
) -> dict[str, object]:
    """Validate a remote edge payload through the Rust runtime binding."""

    from graphblocks_runtime import validate_remote_payload as validate_runtime_remote_payload

    if isinstance(payload, RemoteEdgePayload):
        wire_payload = payload.to_wire()
    elif isinstance(payload, Mapping):
        wire_payload = dict(payload)
    else:
        raise TypeError("payload must be a RemoteEdgePayload or mapping")
    return validate_runtime_remote_payload(wire_payload, max_inline_bytes=max_inline_bytes)


def admit_worker_message_native(
    message: WorkerProtocolMessage | Mapping[str, object],
    *,
    daemon_config: Mapping[str, object] | None = None,
    response_message_id: str = "message-daemon-1",
    response_sequence: int = 1,
) -> dict[str, object]:
    """Admit a worker advertisement envelope through the Rust daemon binding."""

    from graphblocks_runtime import admit_worker_message

    if isinstance(message, WorkerProtocolMessage):
        wire_message = message.to_wire()
    elif isinstance(message, Mapping):
        wire_message = dict(message)
    else:
        raise TypeError("message must be a WorkerProtocolMessage or mapping")
    return admit_worker_message(
        wire_message,
        daemon_config=None if daemon_config is None else dict(daemon_config),
        response_message_id=response_message_id,
        response_sequence=response_sequence,
    )


__all__ = [
    "VALID_WORKER_PROTOCOL_MESSAGE_KINDS",
    "VALID_WORKER_STATES",
    "WORKER_PROTOCOL_VERSION",
    "BlockCapability",
    "RemoteEdgePayload",
    "RemotePayloadError",
    "RemotePayloadInlineJsonEncodingError",
    "RemotePayloadInvalidArtifactRefError",
    "RemotePayloadInvalidLimitError",
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
    "admit_worker_message_native",
    "admit_worker_with_policy",
    "evaluate_worker_admission",
    "select_worker_for_block",
    "validate_remote_payload",
    "validate_remote_payload_native",
    "validate_worker_advertisement_native",
    "validate_worker_protocol_message_native",
    "validate_worker_result",
]
