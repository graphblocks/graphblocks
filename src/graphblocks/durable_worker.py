from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .canonical import canonical_dumps, canonical_hash, canonical_loads
from .compiler import compile_graph_reference
from .durable_registry import durable_intent_registry
from .isolated_worker import ProcessWorkerProtocolError, ProcessWorkerTarget
from .runtime import InProcessRuntime, RuntimeCheckpoint
from .server_storage import (
    AcceptedRunWorkItem,
    CheckpointIntegrityError,
    StoredRuntimeCheckpoint,
    decode_runtime_checkpoint,
    encode_runtime_checkpoint,
)
from .worker import (
    WorkerInvocationContext,
    WorkerInvokeRequest,
    WorkerInvokeResult,
    validate_worker_result,
)


DURABLE_WORKER_BLOCK = "graphblocks.durable.accepted-run@1"
DURABLE_WORKER_NODE_ID = "__durable_graph__"
DEFAULT_DURABLE_WORKER_TARGET = ProcessWorkerTarget(
    "graphblocks.durable_worker",
    "execute_durable_worker_request",
)

_AUTHORITY_FIELDS = frozenset(
    {
        "callbackPayloadDigest",
        "callbackReceiptDigest",
        "checkpointDigest",
        "eventHighWatermark",
        "fencingToken",
        "graphHash",
        "leaseExpiresAtUnixMs",
        "leaseGeneration",
        "leaseOwnerId",
        "ownerPrincipalId",
        "runId",
        "stateVersion",
        "tenantId",
    }
)
_REQUEST_CONFIG_FIELDS = frozenset({"authority", "authorityDigest"})
_REQUEST_INPUT_FIELDS = frozenset({"callbackReceipt", "checkpoint", "graph", "inputs"})
_RUNTIME_RESULT_FIELDS = frozenset({"checkpoint", "outputs", "runId", "status"})
_STORED_CHECKPOINT_FIELDS = frozenset(
    {"checkpointDigest", "checkpointJson", "formatVersion"}
)
_TERMINAL_STATUSES = frozenset({"cancelled", "failed", "succeeded"})


@dataclass(frozen=True, slots=True)
class DurableWorkerOutcome:
    run_id: str
    status: Literal[
        "succeeded",
        "failed",
        "cancelled",
        "waiting_callback",
    ]
    outputs: Mapping[str, object]
    checkpoint: RuntimeCheckpoint | None


def _require_mapping(value: object, owner: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ProcessWorkerProtocolError(f"{owner} must be an object")
    return dict(value)


def _mutable_json_object(value: object, owner: str) -> dict[str, object]:
    payload = _require_mapping(value, owner)
    decoded = canonical_loads(canonical_dumps(payload))
    if not isinstance(decoded, dict):
        raise ProcessWorkerProtocolError(f"{owner} must decode to an object")
    return decoded


def _require_exact_string(value: object, owner: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProcessWorkerProtocolError(f"{owner} must be an exact non-empty string")
    return value


def _require_digest(value: object, owner: str) -> str:
    digest = _require_exact_string(value, owner)
    if (
        len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise ProcessWorkerProtocolError(f"{owner} must be a canonical sha256 digest")
    return digest


def _stored_checkpoint_to_wire(
    checkpoint: StoredRuntimeCheckpoint,
) -> dict[str, object]:
    if not isinstance(checkpoint, StoredRuntimeCheckpoint):
        raise TypeError("durable worker checkpoint must be stored")
    return {
        "checkpointDigest": checkpoint.checkpoint_digest,
        "checkpointJson": checkpoint.checkpoint_json,
        "formatVersion": checkpoint.format_version,
    }


def _stored_checkpoint_from_wire(value: object) -> StoredRuntimeCheckpoint:
    payload = _require_mapping(value, "durable worker checkpoint")
    if set(payload) != _STORED_CHECKPOINT_FIELDS:
        raise ProcessWorkerProtocolError(
            "durable worker checkpoint must contain the closed fields"
        )
    try:
        return StoredRuntimeCheckpoint(
            format_version=_require_exact_string(
                payload["formatVersion"],
                "durable worker checkpoint formatVersion",
            ),
            checkpoint_digest=_require_digest(
                payload["checkpointDigest"],
                "durable worker checkpoint checkpointDigest",
            ),
            checkpoint_json=_require_exact_string(
                payload["checkpointJson"],
                "durable worker checkpoint checkpointJson",
            ),
        )
    except (TypeError, ValueError) as error:
        raise ProcessWorkerProtocolError(
            "durable worker checkpoint is invalid"
        ) from error


def _runtime_checkpoint_from_wire(
    value: object,
) -> tuple[StoredRuntimeCheckpoint, RuntimeCheckpoint]:
    stored_checkpoint = _stored_checkpoint_from_wire(value)
    try:
        checkpoint = decode_runtime_checkpoint(stored_checkpoint)
    except CheckpointIntegrityError as error:
        raise ProcessWorkerProtocolError(
            "durable worker checkpoint failed integrity validation"
        ) from error
    return stored_checkpoint, checkpoint


def _durable_worker_identity(prefix: str, authority_digest: str) -> str:
    return f"{prefix}:{authority_digest.removeprefix('sha256:')}"


def _validate_durable_worker_request(
    request: WorkerInvokeRequest,
) -> tuple[dict[str, object], dict[str, object]]:
    if not isinstance(request, WorkerInvokeRequest):
        raise TypeError("durable worker request must be a WorkerInvokeRequest")
    if request.block != DURABLE_WORKER_BLOCK:
        raise ProcessWorkerProtocolError(
            "durable worker request block is not supported"
        )
    if request.node_id != DURABLE_WORKER_NODE_ID:
        raise ProcessWorkerProtocolError("durable worker request node_id is invalid")

    config = _require_mapping(request.config, "durable worker request config")
    if set(config) != _REQUEST_CONFIG_FIELDS:
        raise ProcessWorkerProtocolError(
            "durable worker request config must contain the closed fields"
        )
    authority = _require_mapping(
        config["authority"],
        "durable worker request authority",
    )
    if set(authority) != _AUTHORITY_FIELDS:
        raise ProcessWorkerProtocolError(
            "durable worker request authority must contain the closed fields"
        )

    for field_name in (
        "leaseOwnerId",
        "ownerPrincipalId",
        "runId",
        "tenantId",
    ):
        _require_exact_string(
            authority[field_name],
            f"durable worker authority {field_name}",
        )
    graph_hash = _require_digest(
        authority["graphHash"],
        "durable worker authority graphHash",
    )
    for field_name in (
        "eventHighWatermark",
        "fencingToken",
        "leaseExpiresAtUnixMs",
        "leaseGeneration",
        "stateVersion",
    ):
        value = authority[field_name]
        if type(value) is not int or value < 1:
            raise ProcessWorkerProtocolError(
                f"durable worker authority {field_name} must be a positive integer"
            )

    checkpoint_digest = authority["checkpointDigest"]
    callback_payload_digest = authority["callbackPayloadDigest"]
    callback_receipt_digest = authority["callbackReceiptDigest"]
    if (checkpoint_digest is None) != (callback_payload_digest is None) or (
        checkpoint_digest is None
    ) != (callback_receipt_digest is None):
        raise ProcessWorkerProtocolError(
            "durable worker resume authority digests must be paired"
        )
    if checkpoint_digest is not None:
        _require_digest(
            checkpoint_digest,
            "durable worker authority checkpointDigest",
        )
        _require_digest(
            callback_payload_digest,
            "durable worker authority callbackPayloadDigest",
        )
        _require_digest(
            callback_receipt_digest,
            "durable worker authority callbackReceiptDigest",
        )

    authority_digest = _require_digest(
        config["authorityDigest"],
        "durable worker authorityDigest",
    )
    if canonical_hash(authority) != authority_digest:
        raise ProcessWorkerProtocolError(
            "durable worker authority digest does not match its fields"
        )
    if request.invocation_id != _durable_worker_identity(
        "durable-invocation",
        authority_digest,
    ):
        raise ProcessWorkerProtocolError(
            "durable worker invocation_id does not match its authority"
        )
    if request.node_attempt_id != _durable_worker_identity(
        "durable-claim",
        authority_digest,
    ):
        raise ProcessWorkerProtocolError(
            "durable worker node_attempt_id does not match its authority"
        )
    if request.run_id != authority["runId"]:
        raise ProcessWorkerProtocolError(
            "durable worker run_id does not match its authority"
        )
    if request.lease_epoch != authority["fencingToken"]:
        raise ProcessWorkerProtocolError(
            "durable worker lease epoch does not match its fencing token"
        )

    expected_attributes = {
        "authorityDigest": authority_digest,
        "fencingToken": str(authority["fencingToken"]),
        "leaseGeneration": str(authority["leaseGeneration"]),
        "leaseOwnerId": str(authority["leaseOwnerId"]),
        "stateVersion": str(authority["stateVersion"]),
        "tenantId": str(authority["tenantId"]),
    }
    if (
        request.context.release_id != graph_hash
        or request.context.deployment_revision_id != graph_hash
        or dict(request.context.attributes) != expected_attributes
    ):
        raise ProcessWorkerProtocolError(
            "durable worker invocation context does not match its authority"
        )

    payload = _require_mapping(request.inputs, "durable worker request inputs")
    if set(payload) != _REQUEST_INPUT_FIELDS:
        raise ProcessWorkerProtocolError(
            "durable worker request inputs must contain the closed fields"
        )
    graph = _mutable_json_object(
        payload["graph"],
        "durable worker graph",
    )
    run_inputs = _mutable_json_object(
        payload["inputs"],
        "durable worker inputs",
    )
    payload["graph"] = graph
    payload["inputs"] = run_inputs

    checkpoint_wire = payload["checkpoint"]
    callback_receipt = payload["callbackReceipt"]
    if (checkpoint_wire is None) != (callback_receipt is None):
        raise ProcessWorkerProtocolError(
            "durable worker checkpoint and callback receipt must be paired"
        )
    if checkpoint_wire is None:
        if checkpoint_digest is not None:
            raise ProcessWorkerProtocolError(
                "durable worker request omitted authorized resume state"
            )
    else:
        if checkpoint_digest is None:
            raise ProcessWorkerProtocolError(
                "durable worker request supplied unauthorized resume state"
            )
        stored_checkpoint, checkpoint = _runtime_checkpoint_from_wire(checkpoint_wire)
        receipt = _mutable_json_object(
            callback_receipt,
            "durable worker callback receipt",
        )
        if "payload" not in receipt or "payload_digest" not in receipt:
            raise ProcessWorkerProtocolError(
                "durable worker callback receipt must bind its payload"
            )
        if (
            stored_checkpoint.checkpoint_digest != checkpoint_digest
            or checkpoint.run_id != request.run_id
            or checkpoint.graph_hash != graph_hash
            or canonical_dumps(checkpoint.inputs) != canonical_dumps(run_inputs)
            or receipt.get("payload_digest") != callback_payload_digest
            or canonical_hash(receipt.get("payload")) != callback_payload_digest
            or canonical_hash(receipt) != callback_receipt_digest
        ):
            raise ProcessWorkerProtocolError(
                "durable worker resume state does not match its authority"
            )
        payload["checkpoint"] = _stored_checkpoint_to_wire(stored_checkpoint)
        payload["callbackReceipt"] = receipt

    return authority, payload


def build_durable_worker_request(
    work: AcceptedRunWorkItem,
    *,
    graph: Mapping[str, object],
    inputs: Mapping[str, object],
) -> WorkerInvokeRequest:
    if not isinstance(work, AcceptedRunWorkItem):
        raise TypeError("durable worker work item must be an AcceptedRunWorkItem")
    if not isinstance(graph, Mapping) or not isinstance(inputs, Mapping):
        raise TypeError("durable worker graph and inputs must be mappings")

    callback_receipt: dict[str, object] | None = None
    if work.callback is not None:
        accepted_receipt = canonical_loads(work.callback.acceptance.receipt_json)
        if not isinstance(accepted_receipt, dict):
            raise ValueError(
                "durable worker accepted callback receipt must encode an object"
            )
        callback_receipt = accepted_receipt

    checkpoint_digest = (
        None if work.checkpoint is None else work.checkpoint.checkpoint_digest
    )
    callback_receipt_digest = (
        None if callback_receipt is None else canonical_hash(dict(callback_receipt))
    )
    callback_payload_digest = (
        None
        if work.callback is None
        else work.callback.acceptance.submission.payload_digest
    )
    authority: dict[str, object] = {
        "callbackPayloadDigest": callback_payload_digest,
        "callbackReceiptDigest": callback_receipt_digest,
        "checkpointDigest": checkpoint_digest,
        "eventHighWatermark": work.event_high_watermark,
        "fencingToken": work.claim.fencing_token,
        "graphHash": work.envelope.graph_hash,
        "leaseExpiresAtUnixMs": work.claim.lease_expires_at_unix_ms,
        "leaseGeneration": work.claim.lease_generation,
        "leaseOwnerId": work.claim.lease_owner_id,
        "ownerPrincipalId": work.envelope.identity.owner_principal_id,
        "runId": work.claim.run_id,
        "stateVersion": work.state_version,
        "tenantId": work.claim.tenant_id,
    }
    authority_digest = canonical_hash(authority)
    return WorkerInvokeRequest(
        invocation_id=_durable_worker_identity(
            "durable-invocation",
            authority_digest,
        ),
        run_id=work.claim.run_id,
        node_id=DURABLE_WORKER_NODE_ID,
        node_attempt_id=_durable_worker_identity(
            "durable-claim",
            authority_digest,
        ),
        lease_epoch=work.claim.fencing_token,
        block=DURABLE_WORKER_BLOCK,
        context=WorkerInvocationContext(
            release_id=work.envelope.graph_hash,
            deployment_revision_id=work.envelope.graph_hash,
            attributes={
                "authorityDigest": authority_digest,
                "fencingToken": str(work.claim.fencing_token),
                "leaseGeneration": str(work.claim.lease_generation),
                "leaseOwnerId": work.claim.lease_owner_id,
                "stateVersion": str(work.state_version),
                "tenantId": work.claim.tenant_id,
            },
        ),
        inputs={
            "callbackReceipt": (
                None if callback_receipt is None else dict(callback_receipt)
            ),
            "checkpoint": (
                None
                if work.checkpoint is None
                else _stored_checkpoint_to_wire(work.checkpoint)
            ),
            "graph": dict(graph),
            "inputs": dict(inputs),
        },
        config={
            "authority": authority,
            "authorityDigest": authority_digest,
        },
    )


def execute_durable_worker_request(
    request: WorkerInvokeRequest,
) -> WorkerInvokeResult:
    authority, payload = _validate_durable_worker_request(request)
    graph = payload["graph"]
    inputs = payload["inputs"]
    if not isinstance(graph, dict) or not isinstance(inputs, dict):
        raise ProcessWorkerProtocolError(
            "durable worker graph and inputs must be objects"
        )

    registry = durable_intent_registry()
    plan = compile_graph_reference(
        graph,
        block_catalog=registry.compilation_catalog(),
        allow_unknown_blocks=registry.allow_untyped,
    )
    if (
        any(
            diagnostic.severity == "error"
            for diagnostic in plan.diagnostics.diagnostics
        )
        or plan.graph_hash != authority["graphHash"]
    ):
        raise ProcessWorkerProtocolError(
            "durable worker graph does not match its admitted plan"
        )

    checkpoint: RuntimeCheckpoint | None = None
    callback_receipt: Mapping[str, object] | None = None
    if payload["checkpoint"] is not None:
        stored_checkpoint, checkpoint = _runtime_checkpoint_from_wire(
            payload["checkpoint"]
        )
        callback_receipt_value = payload["callbackReceipt"]
        if not isinstance(callback_receipt_value, dict):
            raise ProcessWorkerProtocolError(
                "durable worker callback receipt must be an object"
            )
        callback_receipt = callback_receipt_value
        restored_checkpoint = checkpoint
        restored_receipt_digest = canonical_hash(callback_receipt)

        def verify_checkpoint_authority(
            checkpoint: RuntimeCheckpoint,
            *,
            expected_graph_hash: str,
        ) -> bool:
            return (
                checkpoint == restored_checkpoint
                and checkpoint.state_digest == stored_checkpoint.checkpoint_digest
                and expected_graph_hash == authority["graphHash"]
            )

        def verify_callback_receipt(
            receipt: Mapping[str, object],
            *,
            checkpoint: RuntimeCheckpoint,
            expected_checkpoint_digest: str,
            expected_release_digest: str,
        ) -> bool:
            return (
                checkpoint == restored_checkpoint
                and expected_checkpoint_digest == restored_checkpoint.state_digest
                and expected_release_digest == authority["graphHash"]
                and canonical_hash(receipt) == restored_receipt_digest
            )

        runtime = InProcessRuntime(
            registry,
            checkpoint_authority_verifier=verify_checkpoint_authority,
            callback_receipt_verifier=verify_callback_receipt,
        )
    else:
        runtime = InProcessRuntime(registry)

    result = runtime.run(
        graph,
        inputs,
        run_id=request.run_id,
        checkpoint=checkpoint,
        callback_receipt=callback_receipt,
    )
    stored_result_checkpoint = (
        None
        if result.checkpoint is None
        else _stored_checkpoint_to_wire(encode_runtime_checkpoint(result.checkpoint))
    )
    return WorkerInvokeResult(
        invocation_id=request.invocation_id,
        node_attempt_id=request.node_attempt_id,
        lease_epoch=request.lease_epoch,
        outputs={
            "authorityDigest": canonical_hash(authority),
            "runtimeResult": {
                "checkpoint": stored_result_checkpoint,
                "outputs": dict(result.outputs),
                "runId": result.run_id,
                "status": result.status,
            },
        },
    )


def decode_durable_worker_result(
    request: WorkerInvokeRequest,
    result: WorkerInvokeResult,
) -> DurableWorkerOutcome:
    authority, payload = _validate_durable_worker_request(request)
    if not isinstance(result, WorkerInvokeResult):
        raise TypeError("durable worker result must be a WorkerInvokeResult")
    validate_worker_result(request, result)
    outputs = _require_mapping(result.outputs, "durable worker result outputs")
    if set(outputs) != {"authorityDigest", "runtimeResult"}:
        raise ProcessWorkerProtocolError(
            "durable worker result outputs must contain the closed fields"
        )
    if outputs["authorityDigest"] != canonical_hash(authority):
        raise ProcessWorkerProtocolError(
            "durable worker result authority digest does not match its request"
        )
    runtime_result = _require_mapping(
        outputs["runtimeResult"],
        "durable worker runtime result",
    )
    if set(runtime_result) != _RUNTIME_RESULT_FIELDS:
        raise ProcessWorkerProtocolError(
            "durable worker runtime result must contain the closed fields"
        )
    run_id = _require_exact_string(
        runtime_result["runId"],
        "durable worker runtime result runId",
    )
    if run_id != request.run_id:
        raise ProcessWorkerProtocolError(
            "durable worker runtime result runId does not match its request"
        )
    status = runtime_result["status"]
    if type(status) is not str or status not in (
        _TERMINAL_STATUSES | {"waiting_callback"}
    ):
        raise ProcessWorkerProtocolError(
            "durable worker runtime result status is invalid"
        )
    runtime_outputs = _require_mapping(
        runtime_result["outputs"],
        "durable worker runtime outputs",
    )

    checkpoint_wire = runtime_result["checkpoint"]
    checkpoint = None
    if checkpoint_wire is not None:
        _, checkpoint = _runtime_checkpoint_from_wire(checkpoint_wire)
    if (status == "waiting_callback") != (checkpoint is not None):
        raise ProcessWorkerProtocolError(
            "durable worker runtime result checkpoint does not match its status"
        )
    if checkpoint is not None and (
        checkpoint.run_id != request.run_id
        or checkpoint.graph_hash != authority["graphHash"]
        or canonical_dumps(checkpoint.inputs) != canonical_dumps(payload["inputs"])
    ):
        raise ProcessWorkerProtocolError(
            "durable worker result checkpoint does not match its authority"
        )

    decoded_outputs = canonical_loads(canonical_dumps(runtime_outputs))
    if not isinstance(decoded_outputs, dict):
        raise ProcessWorkerProtocolError(
            "durable worker runtime outputs must decode to an object"
        )
    if status == "succeeded":
        return DurableWorkerOutcome(
            run_id=run_id,
            status="succeeded",
            outputs=decoded_outputs,
            checkpoint=checkpoint,
        )
    if status == "failed":
        return DurableWorkerOutcome(
            run_id=run_id,
            status="failed",
            outputs=decoded_outputs,
            checkpoint=checkpoint,
        )
    if status == "cancelled":
        return DurableWorkerOutcome(
            run_id=run_id,
            status="cancelled",
            outputs=decoded_outputs,
            checkpoint=checkpoint,
        )
    return DurableWorkerOutcome(
        run_id=run_id,
        status="waiting_callback",
        outputs=decoded_outputs,
        checkpoint=checkpoint,
    )


__all__ = [
    "DEFAULT_DURABLE_WORKER_TARGET",
    "DURABLE_WORKER_BLOCK",
    "DURABLE_WORKER_NODE_ID",
    "DurableWorkerOutcome",
    "build_durable_worker_request",
    "decode_durable_worker_result",
    "execute_durable_worker_request",
]
