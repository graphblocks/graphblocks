from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from graphblocks.canonical import canonical_dumps, canonical_hash, canonical_loads
from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_server import DurableAcceptedRunService
from graphblocks.server_storage import (
    AcceptedRunCallbackCommit,
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunEffectDeliveryState,
    AcceptedRunEventIntent,
    AcceptedRunPhase,
    CallbackIssuanceIdentity,
    CallbackSubmissionIdentity,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_RESUME_TOKEN_HASH = "sha256:" + ("a" * 64)


def _service(
    path: Path,
    *,
    worker_id: str,
    clock: Callable[[], int],
) -> DurableAcceptedRunService:
    return DurableAcceptedRunService(
        repository=SQLiteAcceptedRunRepository(path),
        lease_owner_id=worker_id,
        lease_duration_ms=10_000,
        compiler=compile_graph_reference,
        clock=clock,
    )


def test_durable_service_executes_admitted_run_after_process_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-restart"},
        "spec": {"nodes": {}},
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    admitted = _service(
        path,
        worker_id="admission-process",
        clock=lambda: 1_000,
    ).admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="run-1",
        idempotency_key="request-1",
        graph=graph,
        inputs={},
        invocation=invocation,
    )

    restarted = _service(
        path,
        worker_id="worker-after-restart",
        clock=lambda: 2_000,
    )
    replay = restarted.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="run-1",
        idempotency_key="request-1",
        graph=graph,
        inputs={},
        invocation=invocation,
        created_at_unix_ms=1_000,
    )
    completed = restarted.advance_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )
    duplicate = restarted.advance_run(
        tenant_id="tenant-1",
        run_id="run-1",
    )

    assert not admitted.replayed
    assert replay.replayed
    assert completed.phase is AcceptedRunPhase.TERMINAL
    assert completed.terminal_status == "succeeded"
    assert canonical_loads(completed.terminal_result_json) == {
        "outputs": {},
        "status": "succeeded",
    }
    assert duplicate == completed
    events = restarted.read_events(
        tenant_id="tenant-1",
        run_id="run-1",
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
        "run_succeeded",
    ]


def test_durable_service_executes_next_tenant_scoped_run_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server-next.sqlite3"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-next"},
        "spec": {"nodes": {}},
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    admission_process = _service(
        path,
        worker_id="admission-process",
        clock=lambda: 1_000,
    )
    admission_process.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id="tenant-1-run",
        idempotency_key="tenant-1-request",
        graph=graph,
        inputs={},
        invocation=invocation,
    )
    admission_process.admit_run(
        tenant_id="tenant-2",
        owner_principal_id="principal-2",
        run_id="tenant-2-run",
        idempotency_key="tenant-2-request",
        graph=graph,
        inputs={},
        invocation=invocation,
    )

    restarted = _service(
        path,
        worker_id="worker-after-restart",
        clock=lambda: 2_000,
    )
    tenant_work = restarted.advance_next_run(tenant_id="tenant-2")
    remaining_work = restarted.advance_next_run()
    no_work = restarted.advance_next_run()

    assert tenant_work is not None
    assert tenant_work.tenant_id == "tenant-2"
    assert tenant_work.run_id == "tenant-2-run"
    assert tenant_work.phase is AcceptedRunPhase.TERMINAL
    assert remaining_work is not None
    assert remaining_work.tenant_id == "tenant-1"
    assert remaining_work.run_id == "tenant-1-run"
    assert remaining_work.phase is AcceptedRunPhase.TERMINAL
    assert no_work is None


def test_durable_service_resumes_accepted_callback_after_process_restart(
    tmp_path,
) -> None:
    path = tmp_path / "durable-server-callback.sqlite3"
    run_id = "run-callback-1"
    operation_id = "operation-callback-1"
    operation_idempotency_key = "operation-idempotency-1"
    graph = {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "durable-server-callback"},
        "spec": {
            "nodes": {
                "start": {
                    "block": "async.start_operation@1",
                    "config": {
                        "operationId": operation_id,
                        "runId": run_id,
                        "nodeId": "wait",
                        "attemptId": "attempt-1",
                        "kind": "ci_job",
                        "providerOperationId": "provider-operation-1",
                        "resumeTokenHash": _RESUME_TOKEN_HASH,
                        "idempotencyKey": operation_idempotency_key,
                        "expectedSchema": "schemas/CICallback@1",
                        "createdAtUnixMs": 1_000,
                        "submittedAtUnixMs": 1_050,
                        "timeoutMs": 60_000,
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                },
                "wait": {
                    "block": "async.await_callback@1",
                    "inputs": {"operation": "start.operation"},
                    "config": {
                        "checkpoint": True,
                        "onTimeout": "fail",
                        "timeoutMs": 60_000,
                        "idempotencyKey": operation_idempotency_key,
                        "callback": {"schema": "schemas/CICallback@1"},
                        "resume": {
                            "requirePolicyReevaluation": True,
                            "requireBudgetReservation": True,
                            "requireReleaseCompatibility": True,
                            "requireOwnershipFence": True,
                        },
                        "attemptFencing": True,
                    },
                },
            }
        },
    }
    invocation = {
        "policySnapshotId": "policy-1",
        "releaseId": "release-1",
        "responseId": "response-1",
        "turnId": None,
    }
    first_process = _service(
        path,
        worker_id="worker-before-callback",
        clock=lambda: 2_000,
    )
    first_process.admit_run(
        tenant_id="tenant-1",
        owner_principal_id="principal-1",
        run_id=run_id,
        idempotency_key="request-callback-1",
        graph=graph,
        inputs={},
        invocation=invocation,
        created_at_unix_ms=1_000,
    )
    waiting = first_process.advance_run(
        tenant_id="tenant-1",
        run_id=run_id,
    )

    assert waiting.phase is AcceptedRunPhase.WAITING_CALLBACK
    dispatcher = SQLiteOutboxDispatcherRepository(path)
    dispatch = dispatcher.claim_next_effect(
        AcceptedRunEffectDeliveryClaimRequest(
            delivery_owner_id="operation-dispatcher",
            now_unix_ms=2_100,
            lease_duration_ms=5_000,
        )
    )
    assert dispatch is not None
    dispatch_payload = canonical_loads(dispatch.payload_json)
    assert isinstance(dispatch_payload, dict)
    issuance_payload = dispatch_payload["callbackIssuance"]
    assert isinstance(issuance_payload, dict)
    issuance = CallbackIssuanceIdentity(
        run_id=str(issuance_payload["runId"]),
        checkpoint_digest=str(issuance_payload["checkpointDigest"]),
        operation_id=str(issuance_payload["operationId"]),
        operation_attempt_id=str(
            issuance_payload["operationAttemptId"]
        ),
        callback_idempotency_key=str(
            issuance_payload["callbackIdempotencyKey"]
        ),
        lease_generation=int(issuance_payload["leaseGeneration"]),
        fencing_token=int(issuance_payload["fencingToken"]),
    )
    callback_payload = {"status": "completed"}
    receipt = {
        "operation_id": operation_id,
        "run_id": run_id,
        "node_id": "wait",
        "attempt_id": "attempt-1",
        "provider_operation_id": "provider-operation-1",
        "operation_idempotency_key": operation_idempotency_key,
        "callback_idempotency_key": issuance.callback_idempotency_key,
        "resume_token_hash": _RESUME_TOKEN_HASH,
        "schema_id": "schemas/CICallback@1",
        "schema_validated": True,
        "payload": callback_payload,
        "payload_digest": canonical_hash(callback_payload),
        "received_at_unix_ms": 3_000,
        "verified_by": "callback-relay",
        "resume_admission": {
            "policy_reevaluated": True,
            "budget_reserved": True,
            "release_compatible": True,
            "ownership_fenced": True,
        },
    }
    accepted_event_payload = {
        "checkpointDigest": issuance.checkpoint_digest,
        "resumeState": "ready_resume",
        "runId": run_id,
    }
    acceptance = first_process.accept_callback(
        AcceptedRunCallbackCommit(
            tenant_id="tenant-1",
            owner_principal_id="principal-1",
            expected_state_version=waiting.state_version,
            submission=CallbackSubmissionIdentity(
                issuance=issuance,
                payload_digest=canonical_hash(callback_payload),
            ),
            payload_json=canonical_dumps(callback_payload),
            receipt_json=canonical_dumps(receipt),
            received_at_unix_ms=3_000,
            accepted_event=AcceptedRunEventIntent(
                kind="external_callback_received",
                payload_json=canonical_dumps(accepted_event_payload),
                payload_digest=canonical_hash(accepted_event_payload),
                created_at_unix_ms=3_000,
            ),
        )
    )

    assert acceptance.state_version == 4
    settled_dispatch = dispatcher.get_effect(effect_id=dispatch.effect_id)
    assert settled_dispatch is not None
    assert (
        settled_dispatch.delivery_state
        is AcceptedRunEffectDeliveryState.SATISFIED_BY_CALLBACK
    )

    restarted = _service(
        path,
        worker_id="worker-after-callback",
        clock=lambda: 4_000,
    )
    completed = restarted.advance_next_run(tenant_id="tenant-1")

    assert completed is not None
    assert completed.phase is AcceptedRunPhase.TERMINAL
    assert completed.terminal_status == "succeeded"
    assert completed.state_version == 6
    events = restarted.read_events(
        tenant_id="tenant-1",
        run_id=run_id,
        after_sequence=0,
        limit=10,
    )
    assert [event.kind for event in events.events] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
        "external_callback_received",
        "run_resume_claimed",
        "run_succeeded",
    ]
