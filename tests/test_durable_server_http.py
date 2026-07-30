from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sqlite3

import pytest

from graphblocks.canonical import canonical_hash, canonical_loads
from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_server import (
    DurableAcceptedRunServerApp,
    DurableAcceptedRunService,
)
from graphblocks.policy import PrincipalRef
from graphblocks.server import ServerRequest, StaticBearerAuthHook
from graphblocks.server_storage import (
    AcceptedRunEffectDeliveryClaimRequest,
    AcceptedRunPhase,
    AcceptedRunSnapshot,
    CallbackIssuanceIdentity,
)
from graphblocks.sqlite_outbox import SQLiteOutboxDispatcherRepository
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


_CALLBACK_RUN_ID = "run-callback-http"
_CALLBACK_OPERATION_ID = "operation-callback-http"
_CALLBACK_OPERATION_IDEMPOTENCY_KEY = "operation-idempotency-http"
_CALLBACK_RESUME_TOKEN_HASH = "sha256:" + ("a" * 64)
_CALLBACK_GRAPH = {
    "apiVersion": "graphblocks.ai/v1alpha3",
    "kind": "Graph",
    "metadata": {"name": "durable-http-callback"},
    "spec": {
        "nodes": {
            "start": {
                "block": "async.start_operation@1",
                "config": {
                    "operationId": _CALLBACK_OPERATION_ID,
                    "runId": _CALLBACK_RUN_ID,
                    "nodeId": "wait",
                    "attemptId": "attempt-1",
                    "kind": "ci_job",
                    "providerOperationId": "provider-operation-1",
                    "resumeTokenHash": _CALLBACK_RESUME_TOKEN_HASH,
                    "idempotencyKey": (_CALLBACK_OPERATION_IDEMPOTENCY_KEY),
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
                    "idempotencyKey": (_CALLBACK_OPERATION_IDEMPOTENCY_KEY),
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
_CALLBACK_INVOCATION = {
    "policySnapshotId": "policy-1",
    "releaseId": "release-1",
    "responseId": "response-1",
    "turnId": None,
}


def _app(
    path: Path,
    *,
    clock_value: int,
    failpoint: Callable[[str], None] | None = None,
) -> DurableAcceptedRunServerApp:
    def clock() -> int:
        return clock_value

    return DurableAcceptedRunServerApp(
        service=DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(
                path,
                failpoint=failpoint,
                clock=clock,
            ),
            lease_owner_id=f"worker-{clock_value}",
            lease_duration_ms=30_000,
            compiler=compile_graph_reference,
            clock=clock,
        ),
        auth_hook=StaticBearerAuthHook(
            {
                "alice-token": PrincipalRef(
                    "alice",
                    tenant_id="tenant-a",
                ),
                "bob-token": PrincipalRef(
                    "bob",
                    tenant_id="tenant-b",
                ),
                "charlie-token": PrincipalRef(
                    "charlie",
                    tenant_id="tenant-a",
                ),
            }
        ),
    )


def _request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    query: dict[str, str] | None = None,
) -> ServerRequest:
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    return ServerRequest(
        method=method,
        path=path,
        headers=headers,
        query={} if query is None else query,
        cookies={},
        body=(b"" if body is None else json.dumps(body).encode("utf-8")),
    )


def _prepare_waiting_callback(
    path: Path,
) -> tuple[AcceptedRunSnapshot, CallbackIssuanceIdentity]:
    app = _app(path, clock_value=2_000)
    admitted = app.handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body={
                "graph": _CALLBACK_GRAPH,
                "inputs": {},
                "invocation": _CALLBACK_INVOCATION,
                "requestId": "request-callback-http",
                "responseMode": "accepted",
                "runId": _CALLBACK_RUN_ID,
            },
        )
    )
    assert admitted.status_code == 202
    waiting = app.service.advance_run(
        tenant_id="tenant-a",
        run_id=_CALLBACK_RUN_ID,
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
    assert issuance_payload["expectedStateVersion"] == waiting.state_version
    issuance = CallbackIssuanceIdentity(
        run_id=str(issuance_payload["runId"]),
        checkpoint_digest=str(issuance_payload["checkpointDigest"]),
        operation_id=str(issuance_payload["operationId"]),
        operation_attempt_id=str(issuance_payload["operationAttemptId"]),
        callback_idempotency_key=str(issuance_payload["callbackIdempotencyKey"]),
        lease_generation=int(issuance_payload["leaseGeneration"]),
        fencing_token=int(issuance_payload["fencingToken"]),
    )
    return waiting, issuance


def _callback_body(
    waiting: AcceptedRunSnapshot,
    issuance: CallbackIssuanceIdentity,
) -> dict[str, object]:
    callback_payload = {"status": "completed"}
    return {
        "callbackIdempotencyKey": issuance.callback_idempotency_key,
        "checkpointDigest": issuance.checkpoint_digest,
        "expectedStateVersion": waiting.state_version,
        "fencingToken": issuance.fencing_token,
        "leaseGeneration": issuance.lease_generation,
        "operationAttemptId": issuance.operation_attempt_id,
        "payload": callback_payload,
        "receipt": {
            "operation_id": _CALLBACK_OPERATION_ID,
            "run_id": _CALLBACK_RUN_ID,
            "node_id": "wait",
            "attempt_id": "attempt-1",
            "provider_operation_id": "provider-operation-1",
            "operation_idempotency_key": (_CALLBACK_OPERATION_IDEMPOTENCY_KEY),
            "callback_idempotency_key": (issuance.callback_idempotency_key),
            "resume_token_hash": _CALLBACK_RESUME_TOKEN_HASH,
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
        },
    }


def test_durable_http_adapter_reads_run_and_events_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http.sqlite3"
    request_body = {
        "graph": {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "durable-http-restart"},
            "spec": {"nodes": {}},
        },
        "inputs": {},
        "invocation": {
            "policySnapshotId": "policy-1",
            "releaseId": "release-1",
            "responseId": "response-1",
            "turnId": None,
        },
        "requestId": "request-1",
        "responseMode": "accepted",
        "runId": "run-1",
    }
    admitted = _app(path, clock_value=1_000).handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body=request_body,
        )
    )

    assert admitted.status_code == 202
    assert json.loads(admitted.body) == {
        "duplicate": False,
        "events": "/runs/run-1/events",
        "ok": True,
        "runId": "run-1",
        "state": "ready_initial",
        "status": "accepted",
    }

    restarted = _app(path, clock_value=2_000)
    before_execution = restarted.handle(
        _request(
            "GET",
            "/runs/run-1",
            token="alice-token",
        )
    )
    cross_tenant = restarted.handle(
        _request(
            "GET",
            "/runs/run-1",
            token="bob-token",
        )
    )
    same_tenant_other_owner = restarted.handle(
        _request(
            "GET",
            "/runs/run-1",
            token="charlie-token",
        )
    )
    same_tenant_run_id_collision = restarted.handle(
        _request(
            "POST",
            "/runs",
            token="charlie-token",
            body=request_body,
        )
    )
    assert before_execution.status_code == 200
    assert json.loads(before_execution.body)["state"] == "ready_initial"
    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404
    assert same_tenant_run_id_collision.status_code == 404

    restarted.service.advance_run(
        tenant_id="tenant-a",
        run_id="run-1",
    )
    after_second_restart = _app(path, clock_value=3_000)
    completed = after_second_restart.handle(
        _request(
            "GET",
            "/runs/run-1",
            token="alice-token",
        )
    )
    events = after_second_restart.handle(
        _request(
            "GET",
            "/runs/run-1/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    replayed = after_second_restart.handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body=request_body,
        )
    )

    assert completed.status_code == 200
    assert json.loads(completed.body) == {
        "eventHighWatermark": 3,
        "eventLowWatermark": 1,
        "ok": True,
        "result": {"outputs": {}, "status": "succeeded"},
        "runId": "run-1",
        "state": "terminal",
        "stateVersion": 3,
        "terminalStatus": "succeeded",
    }
    assert events.status_code == 200
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_claimed",
        "run_succeeded",
    ]
    assert replayed.status_code == 202
    assert json.loads(replayed.body)["duplicate"] is True


def test_durable_http_callback_resumes_after_process_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback_body = _callback_body(waiting, issuance)
    callback_path = f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"

    accepted = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=callback_body,
        )
    )
    replayed = _app(path, clock_value=3_500).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=callback_body,
        )
    )
    conflicting_body = dict(callback_body)
    conflicting_payload = {"status": "failed"}
    conflicting_body["payload"] = conflicting_payload
    conflicting_receipt = dict(callback_body["receipt"])
    conflicting_receipt["payload"] = conflicting_payload
    conflicting_receipt["payload_digest"] = canonical_hash(conflicting_payload)
    conflicting_body["receipt"] = conflicting_receipt
    conflicting = _app(path, clock_value=3_500).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=conflicting_body,
        )
    )

    expected_acceptance = {
        "acceptedEventSequence": 4,
        "ok": True,
        "runId": _CALLBACK_RUN_ID,
        "stateVersion": 4,
        "status": "accepted",
    }
    assert accepted.status_code == 202
    assert json.loads(accepted.body) == expected_acceptance
    assert replayed.status_code == 202
    assert json.loads(replayed.body) == expected_acceptance
    assert conflicting.status_code == 409

    resumed = _app(path, clock_value=4_000)
    completed = resumed.service.advance_next_run(tenant_id="tenant-a")
    assert completed is not None
    assert completed.phase is AcceptedRunPhase.TERMINAL
    assert completed.terminal_status == "succeeded"

    after_restart = _app(path, clock_value=5_000)
    status = after_restart.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    events = after_restart.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )

    assert status.status_code == 200
    assert json.loads(status.body)["state"] == "terminal"
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
        "external_callback_received",
        "run_resume_claimed",
        "run_succeeded",
    ]


def test_durable_http_rejects_divergent_callback_receipt_payload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback-divergence.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback_body = _callback_body(waiting, issuance)
    receipt = callback_body["receipt"]
    assert isinstance(receipt, dict)
    forged_payload = {"status": "failed"}
    receipt["payload"] = forged_payload
    receipt["payload_digest"] = canonical_hash(forged_payload)
    app = _app(path, clock_value=3_000)

    rejected = app.handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}",
            token="alice-token",
            body=callback_body,
        )
    )
    status = app.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    events = app.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )

    assert rejected.status_code == 400
    assert status.status_code == 200
    assert json.loads(status.body)["state"] == "waiting_callback"
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
    ]


def test_durable_http_callback_hides_foreign_runs_and_rejects_stale_fence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback-authz.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback_body = _callback_body(waiting, issuance)
    callback_path = f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"

    cross_tenant = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="bob-token",
            body=callback_body,
        )
    )
    same_tenant_other_owner = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="charlie-token",
            body=callback_body,
        )
    )
    stale_body = dict(callback_body)
    stale_body["fencingToken"] = issuance.fencing_token + 1
    stale_fence = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=stale_body,
        )
    )
    wrong_operation = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/callbacks/wrong-operation",
            token="alice-token",
            body=callback_body,
        )
    )
    malformed_body = dict(callback_body)
    malformed_body["unknown"] = True
    malformed = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=malformed_body,
        )
    )
    limited = _app(path, clock_value=3_000)
    limited.max_request_body_bytes = 16
    oversized = limited.handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=callback_body,
        )
    )

    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404
    assert stale_fence.status_code == 409
    assert wrong_operation.status_code == 409
    assert malformed.status_code == 400
    assert oversized.status_code == 413
    unchanged = _app(path, clock_value=4_000).handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert json.loads(unchanged.body)["state"] == "waiting_callback"
    assert json.loads(unchanged.body)["stateVersion"] == waiting.state_version


def test_durable_http_cancel_is_restart_safe_and_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-cancel.sqlite3"
    run_body = {
        "graph": {
            "apiVersion": "graphblocks.ai/v1alpha3",
            "kind": "Graph",
            "metadata": {"name": "durable-http-cancel"},
            "spec": {"nodes": {}},
        },
        "inputs": {},
        "invocation": _CALLBACK_INVOCATION,
        "requestId": "request-cancel-http",
        "responseMode": "accepted",
        "runId": "run-cancel-http",
    }
    admitted = _app(path, clock_value=1_000).handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body=run_body,
        )
    )
    assert admitted.status_code == 202
    cancel_body = {
        "expectedStateVersion": 1,
        "reason": "user_requested",
        "requestId": "cancel-request-1",
    }

    cancelled = _app(path, clock_value=2_000).handle(
        _request(
            "POST",
            "/runs/run-cancel-http/cancel",
            token="alice-token",
            body=cancel_body,
        )
    )
    replayed = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            "/runs/run-cancel-http/cancel",
            token="alice-token",
            body=cancel_body,
        )
    )
    cross_tenant = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            "/runs/run-cancel-http/cancel",
            token="bob-token",
            body=cancel_body,
        )
    )
    same_tenant_other_owner = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            "/runs/run-cancel-http/cancel",
            token="charlie-token",
            body=cancel_body,
        )
    )

    expected = {
        "acceptedEventSequence": 2,
        "duplicate": False,
        "ok": True,
        "runId": "run-cancel-http",
        "state": "terminal",
        "stateVersion": 2,
        "terminalStatus": "cancelled",
    }
    assert cancelled.status_code == 202
    assert json.loads(cancelled.body) == expected
    assert replayed.status_code == 202
    assert json.loads(replayed.body) == {
        **expected,
        "duplicate": True,
    }
    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404

    restarted = _app(path, clock_value=4_000)
    status = restarted.handle(
        _request(
            "GET",
            "/runs/run-cancel-http",
            token="alice-token",
        )
    )
    events = restarted.handle(
        _request(
            "GET",
            "/runs/run-cancel-http/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    assert status.status_code == 200
    assert json.loads(status.body) == {
        "eventHighWatermark": 2,
        "eventLowWatermark": 1,
        "ok": True,
        "result": {
            "reason": "user_requested",
            "requestId": "cancel-request-1",
            "status": "cancelled",
        },
        "runId": "run-cancel-http",
        "state": "terminal",
        "stateVersion": 2,
        "terminalStatus": "cancelled",
    }
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_cancelled",
    ]


def test_durable_http_cancel_rejects_invalid_requests_and_late_callback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-cancel-callback.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    cancel_path = f"/runs/{_CALLBACK_RUN_ID}/cancel"
    cancel_body = {
        "expectedStateVersion": waiting.state_version,
        "reason": "no_longer_needed",
        "requestId": "cancel-callback-run",
    }
    malformed = dict(cancel_body)
    malformed["unknown"] = True
    stale = dict(cancel_body)
    stale["expectedStateVersion"] = waiting.state_version - 1

    stale_response = _app(path, clock_value=2_500).handle(
        _request(
            "POST",
            cancel_path,
            token="alice-token",
            body=stale,
        )
    )
    malformed_response = _app(path, clock_value=2_500).handle(
        _request(
            "POST",
            cancel_path,
            token="alice-token",
            body=malformed,
        )
    )
    cancelled = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            cancel_path,
            token="alice-token",
            body=cancel_body,
        )
    )
    callback_after_cancel = _app(path, clock_value=4_000).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}",
            token="alice-token",
            body=_callback_body(waiting, issuance),
        )
    )
    limited = _app(path, clock_value=4_000)
    limited.max_request_body_bytes = 16
    oversized = limited.handle(
        _request(
            "POST",
            cancel_path,
            token="alice-token",
            body=cancel_body,
        )
    )

    assert stale_response.status_code == 409
    assert malformed_response.status_code == 400
    assert cancelled.status_code == 202
    assert callback_after_cancel.status_code == 409
    assert oversized.status_code == 413
    after_restart = _app(path, clock_value=5_000).handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert after_restart.status_code == 200
    assert json.loads(after_restart.body)["terminalStatus"] == "cancelled"


def test_durable_http_cancel_fences_ready_resume_before_worker_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-cancel-resume.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback_path = f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"
    callback = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            callback_path,
            token="alice-token",
            body=_callback_body(waiting, issuance),
        )
    )
    assert callback.status_code == 202
    callback_response = json.loads(callback.body)

    cancelled = _app(path, clock_value=3_500).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/cancel",
            token="alice-token",
            body={
                "expectedStateVersion": callback_response["stateVersion"],
                "reason": "resume_not_needed",
                "requestId": "cancel-ready-resume",
            },
        )
    )

    assert cancelled.status_code == 202
    restarted = _app(path, clock_value=4_000)
    assert restarted.service.advance_next_run(tenant_id="tenant-a") is None
    events = restarted.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
        "external_callback_received",
        "run_cancelled",
    ]


def test_durable_http_expire_is_restart_safe_idempotent_and_owner_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-expire.sqlite3"
    run_id = "run-expire-http"
    admitted = _app(path, clock_value=1_000).handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body={
                "graph": {
                    "apiVersion": "graphblocks.ai/v1alpha3",
                    "kind": "Graph",
                    "metadata": {"name": "durable-http-expire"},
                    "spec": {"nodes": {}},
                },
                "inputs": {},
                "invocation": _CALLBACK_INVOCATION,
                "requestId": "request-expire-http",
                "responseMode": "accepted",
                "runId": run_id,
            },
        )
    )
    assert admitted.status_code == 202
    expire_body = {
        "expectedStateVersion": 1,
        "reason": "deadline_elapsed",
        "requestId": "expire-request-1",
    }

    expired = _app(path, clock_value=2_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/expire",
            token="alice-token",
            body=expire_body,
        )
    )
    limited = _app(path, clock_value=3_000)
    limited.max_request_body_bytes = 16
    oversized = limited.handle(
        _request(
            "POST",
            f"/runs/{run_id}/expire",
            token="alice-token",
            body=expire_body,
        )
    )
    replayed = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/expire",
            token="alice-token",
            body=expire_body,
        )
    )
    cross_tenant = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/expire",
            token="bob-token",
            body=expire_body,
        )
    )
    same_tenant_other_owner = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/expire",
            token="charlie-token",
            body=expire_body,
        )
    )

    expected = {
        "acceptedEventSequence": 2,
        "duplicate": False,
        "ok": True,
        "runId": run_id,
        "state": "terminal",
        "stateVersion": 2,
        "terminalStatus": "expired",
    }
    assert expired.status_code == 202
    assert json.loads(expired.body) == expected
    assert replayed.status_code == 202
    assert json.loads(replayed.body) == {
        **expected,
        "duplicate": True,
    }
    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404
    assert oversized.status_code == 413

    restarted = _app(path, clock_value=4_000)
    status = restarted.handle(_request("GET", f"/runs/{run_id}", token="alice-token"))
    events = restarted.handle(
        _request(
            "GET",
            f"/runs/{run_id}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    assert status.status_code == 200
    assert json.loads(status.body) == {
        "eventHighWatermark": 2,
        "eventLowWatermark": 1,
        "ok": True,
        "result": {
            "reason": "deadline_elapsed",
            "requestId": "expire-request-1",
            "status": "expired",
        },
        "runId": run_id,
        "state": "terminal",
        "stateVersion": 2,
        "terminalStatus": "expired",
    }
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_expired",
    ]


def test_durable_http_expire_suppresses_callback_and_fences_resume(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-expire-callback.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    expired = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/expire",
            token="alice-token",
            body={
                "expectedStateVersion": waiting.state_version,
                "reason": "callback_deadline",
                "requestId": "expire-callback-run",
            },
        )
    )
    callback_after_expire = _app(path, clock_value=4_000).handle(
        _request(
            "POST",
            (f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"),
            token="alice-token",
            body=_callback_body(waiting, issuance),
        )
    )

    assert expired.status_code == 202
    assert json.loads(expired.body)["terminalStatus"] == "expired"
    assert callback_after_expire.status_code == 409
    restarted = _app(path, clock_value=5_000)
    assert restarted.service.advance_next_run(tenant_id="tenant-a") is None
    connection = sqlite3.connect(path)
    dispatch_state = connection.execute(
        """
        SELECT delivery_state, cancelled_at_unix_ms
        FROM effect_outbox
        WHERE effect_kind = 'operation_dispatch'
        """
    ).fetchone()
    connection.close()
    assert dispatch_state == ("pending", 3_000)


def test_durable_expire_rolls_back_callback_dispatch_suppression(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-expire-dispatch-rollback.sqlite3"
    waiting, _ = _prepare_waiting_callback(path)

    def inject(point: str) -> None:
        if point == "expire_run.after_dispatch_cancellation":
            raise RuntimeError("injected dispatch suppression failure")

    with pytest.raises(
        RuntimeError,
        match="injected dispatch suppression failure",
    ):
        _app(
            path,
            clock_value=3_000,
            failpoint=inject,
        ).service.expire_run(
            tenant_id="tenant-a",
            owner_principal_id="alice",
            run_id=_CALLBACK_RUN_ID,
            expected_state_version=waiting.state_version,
            idempotency_key="expire-dispatch-rollback",
            reason="rollback",
        )

    status = _app(path, clock_value=4_000).handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert status.status_code == 200
    assert json.loads(status.body)["state"] == "waiting_callback"
    connection = sqlite3.connect(path)
    dispatch_state = connection.execute(
        """
        SELECT delivery_state, cancelled_at_unix_ms
        FROM effect_outbox
        WHERE effect_kind = 'operation_dispatch'
        """
    ).fetchone()
    control_count = int(
        connection.execute("SELECT COUNT(*) FROM run_controls").fetchone()[0]
    )
    connection.close()
    assert dispatch_state == ("claimed", None)
    assert control_count == 0


def test_durable_http_pause_and_resume_are_restart_safe_and_owner_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-pause.sqlite3"
    run_id = "run-pause-http"
    admitted = _app(path, clock_value=1_000).handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body={
                "graph": {
                    "apiVersion": "graphblocks.ai/v1alpha3",
                    "kind": "Graph",
                    "metadata": {"name": "durable-http-pause"},
                    "spec": {"nodes": {}},
                },
                "inputs": {},
                "invocation": _CALLBACK_INVOCATION,
                "requestId": "request-pause-http",
                "responseMode": "accepted",
                "runId": run_id,
            },
        )
    )
    assert admitted.status_code == 202
    pause_body = {
        "expectedStateVersion": 1,
        "reason": "operator_review",
        "requestId": "pause-request-1",
    }

    paused = _app(path, clock_value=2_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/pause",
            token="alice-token",
            body=pause_body,
        )
    )
    replayed = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/pause",
            token="alice-token",
            body=pause_body,
        )
    )
    cross_tenant = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/resume",
            token="bob-token",
            body={
                "expectedStateVersion": 2,
                "reason": "unauthorized",
                "requestId": "foreign-resume",
            },
        )
    )
    same_tenant_other_owner = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            f"/runs/{run_id}/resume",
            token="charlie-token",
            body={
                "expectedStateVersion": 2,
                "reason": "unauthorized",
                "requestId": "foreign-owner-resume",
            },
        )
    )

    expected_pause = {
        "acceptedEventSequence": 2,
        "action": "pause",
        "duplicate": False,
        "ok": True,
        "runId": run_id,
        "state": "paused",
        "stateVersion": 2,
    }
    assert paused.status_code == 202
    assert json.loads(paused.body) == expected_pause
    assert replayed.status_code == 202
    assert json.loads(replayed.body) == {
        **expected_pause,
        "duplicate": True,
    }
    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404

    restarted = _app(path, clock_value=4_000)
    status = restarted.handle(_request("GET", f"/runs/{run_id}", token="alice-token"))
    assert status.status_code == 200
    assert json.loads(status.body) == {
        "eventHighWatermark": 2,
        "eventLowWatermark": 1,
        "ok": True,
        "resumeState": "ready_initial",
        "runId": run_id,
        "state": "paused",
        "stateVersion": 2,
    }
    assert restarted.service.advance_next_run(tenant_id="tenant-a") is None

    resumed = restarted.handle(
        _request(
            "POST",
            f"/runs/{run_id}/resume",
            token="alice-token",
            body={
                "expectedStateVersion": 2,
                "reason": "review_complete",
                "requestId": "resume-request-1",
            },
        )
    )
    assert resumed.status_code == 202
    assert json.loads(resumed.body) == {
        "acceptedEventSequence": 3,
        "action": "resume",
        "duplicate": False,
        "ok": True,
        "runId": run_id,
        "state": "ready_initial",
        "stateVersion": 3,
    }

    events = _app(path, clock_value=5_000).handle(
        _request(
            "GET",
            f"/runs/{run_id}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    assert [event["kind"] for event in json.loads(events.body)["events"]] == [
        "run_accepted",
        "run_paused",
        "run_resumed",
    ]


def test_durable_http_pause_validates_body_state_and_request_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-pause-validation.sqlite3"
    run_id = "run-pause-validation"
    app = _app(path, clock_value=1_000)
    admitted = app.handle(
        _request(
            "POST",
            "/runs",
            token="alice-token",
            body={
                "graph": {
                    "apiVersion": "graphblocks.ai/v1alpha3",
                    "kind": "Graph",
                    "metadata": {"name": "durable-pause-validation"},
                    "spec": {"nodes": {}},
                },
                "inputs": {},
                "invocation": _CALLBACK_INVOCATION,
                "requestId": "request-pause-validation",
                "responseMode": "accepted",
                "runId": run_id,
            },
        )
    )
    assert admitted.status_code == 202
    malformed = app.handle(
        _request(
            "POST",
            f"/runs/{run_id}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": 1,
                "reason": "review",
                "requestId": "pause-malformed",
                "unknown": True,
            },
        )
    )
    stale = app.handle(
        _request(
            "POST",
            f"/runs/{run_id}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": 0,
                "reason": "review",
                "requestId": "pause-stale",
            },
        )
    )
    invalid_resume = app.handle(
        _request(
            "POST",
            f"/runs/{run_id}/resume",
            token="alice-token",
            body={
                "expectedStateVersion": 1,
                "reason": "not_paused",
                "requestId": "resume-invalid-state",
            },
        )
    )
    limited = _app(path, clock_value=2_000)
    limited.max_request_body_bytes = 16
    oversized = limited.handle(
        _request(
            "POST",
            f"/runs/{run_id}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": 1,
                "reason": "review",
                "requestId": "pause-oversized",
            },
        )
    )

    assert malformed.status_code == 400
    assert stale.status_code == 409
    assert invalid_resume.status_code == 409
    assert oversized.status_code == 413


def test_durable_http_callback_can_arrive_paused_but_resume_gates_worker(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-paused-callback.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    paused = _app(path, clock_value=2_500).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": waiting.state_version,
                "reason": "hold_execution",
                "requestId": "pause-waiting-callback",
            },
        )
    )
    assert paused.status_code == 202
    assert json.loads(paused.body)["state"] == "paused"

    callback_body = _callback_body(waiting, issuance)
    callback = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            (f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"),
            token="alice-token",
            body=callback_body,
        )
    )
    assert callback.status_code == 202
    callback_payload = json.loads(callback.body)

    restarted = _app(path, clock_value=3_500)
    status = restarted.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert status.status_code == 200
    assert json.loads(status.body) == {
        "checkpointDigest": waiting.checkpoint_digest,
        "eventHighWatermark": 5,
        "eventLowWatermark": 1,
        "ok": True,
        "resumeState": "ready_resume",
        "runId": _CALLBACK_RUN_ID,
        "state": "paused",
        "stateVersion": callback_payload["stateVersion"],
    }
    events = restarted.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}/events",
            token="alice-token",
            query={"after": "0", "limit": "10"},
        )
    )
    callback_events = [
        event
        for event in json.loads(events.body)["events"]
        if event["kind"] == "external_callback_received"
    ]
    assert callback_events == [
        {
            "kind": "external_callback_received",
            "payload": {
                "checkpointDigest": waiting.checkpoint_digest,
                "resumeState": "ready_resume",
                "runId": _CALLBACK_RUN_ID,
            },
            "sequence": 5,
        }
    ]
    assert restarted.service.advance_next_run(tenant_id="tenant-a") is None

    resumed = restarted.handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/resume",
            token="alice-token",
            body={
                "expectedStateVersion": callback_payload["stateVersion"],
                "reason": "continue_execution",
                "requestId": "resume-after-callback",
            },
        )
    )
    assert resumed.status_code == 202
    assert json.loads(resumed.body)["state"] == "ready_resume"

    completed = _app(path, clock_value=4_000).service.advance_next_run(
        tenant_id="tenant-a"
    )
    assert completed is not None
    assert completed.phase is AcceptedRunPhase.TERMINAL


def test_durable_http_callback_then_pause_preserves_ready_resume_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback-before-pause.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            (f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"),
            token="alice-token",
            body=_callback_body(waiting, issuance),
        )
    )
    assert callback.status_code == 202
    callback_payload = json.loads(callback.body)

    paused = _app(path, clock_value=3_500).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": callback_payload["stateVersion"],
                "reason": "hold_ready_resume",
                "requestId": "pause-after-callback",
            },
        )
    )
    assert paused.status_code == 202
    assert json.loads(paused.body)["state"] == "paused"

    restarted = _app(path, clock_value=4_000)
    status = restarted.handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert status.status_code == 200
    status_payload = json.loads(status.body)
    assert status_payload["state"] == "paused"
    assert status_payload["resumeState"] == "ready_resume"
    assert restarted.service.advance_next_run(tenant_id="tenant-a") is None


def test_durable_http_issued_callback_survives_pause_resume_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback-after-resume.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    paused = _app(path, clock_value=2_500).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/pause",
            token="alice-token",
            body={
                "expectedStateVersion": waiting.state_version,
                "reason": "brief_hold",
                "requestId": "pause-before-callback",
            },
        )
    )
    assert paused.status_code == 202
    paused_payload = json.loads(paused.body)
    resumed = _app(path, clock_value=2_750).handle(
        _request(
            "POST",
            f"/runs/{_CALLBACK_RUN_ID}/resume",
            token="alice-token",
            body={
                "expectedStateVersion": paused_payload["stateVersion"],
                "reason": "hold_released",
                "requestId": "resume-before-callback",
            },
        )
    )
    assert resumed.status_code == 202
    assert json.loads(resumed.body)["state"] == "waiting_callback"

    callback = _app(path, clock_value=3_000).handle(
        _request(
            "POST",
            (f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"),
            token="alice-token",
            body=_callback_body(waiting, issuance),
        )
    )
    assert callback.status_code == 202
    status = _app(path, clock_value=3_500).handle(
        _request(
            "GET",
            f"/runs/{_CALLBACK_RUN_ID}",
            token="alice-token",
        )
    )
    assert status.status_code == 200
    assert json.loads(status.body)["state"] == "ready_resume"


def test_durable_http_adapter_is_fail_closed_and_keeps_health_public(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="requires an auth_hook",
    ):
        DurableAcceptedRunServerApp(
            service=DurableAcceptedRunService(
                repository=SQLiteAcceptedRunRepository(tmp_path / "invalid.sqlite3"),
                lease_owner_id="worker-1",
                compiler=compile_graph_reference,
            ),
            auth_hook=None,  # type: ignore[arg-type]
        )

    app = _app(tmp_path / "auth.sqlite3", clock_value=1_000)
    health = app.handle(_request("GET", "/health"))
    protected = app.handle(_request("GET", "/runs/run-1"))

    assert health.status_code == 200
    assert json.loads(health.body) == {
        "ok": True,
        "profile": "durable-preview",
        "status": "healthy",
    }
    assert protected.status_code == 401
