from __future__ import annotations

import json
from pathlib import Path

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
                    "idempotencyKey": (
                        _CALLBACK_OPERATION_IDEMPOTENCY_KEY
                    ),
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
                    "idempotencyKey": (
                        _CALLBACK_OPERATION_IDEMPOTENCY_KEY
                    ),
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
) -> DurableAcceptedRunServerApp:
    return DurableAcceptedRunServerApp(
        service=DurableAcceptedRunService(
            repository=SQLiteAcceptedRunRepository(path),
            lease_owner_id=f"worker-{clock_value}",
            lease_duration_ms=10_000,
            compiler=compile_graph_reference,
            clock=lambda: clock_value,
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
    headers = (
        {}
        if token is None
        else {"authorization": f"Bearer {token}"}
    )
    return ServerRequest(
        method=method,
        path=path,
        headers=headers,
        query={} if query is None else query,
        cookies={},
        body=(
            b""
            if body is None
            else json.dumps(body).encode("utf-8")
        ),
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
            "operation_idempotency_key": (
                _CALLBACK_OPERATION_IDEMPOTENCY_KEY
            ),
            "callback_idempotency_key": (
                issuance.callback_idempotency_key
            ),
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
    unsupported_memory_route = restarted.handle(
        _request(
            "POST",
            "/runs/run-1/cancel",
            token="alice-token",
        )
    )

    assert before_execution.status_code == 200
    assert json.loads(before_execution.body)["state"] == "ready_initial"
    assert cross_tenant.status_code == 404
    assert same_tenant_other_owner.status_code == 404
    assert same_tenant_run_id_collision.status_code == 404
    assert unsupported_memory_route.status_code == 404

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
    assert [
        event["kind"]
        for event in json.loads(events.body)["events"]
    ] == [
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
    callback_path = (
        f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"
    )

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
    conflicting_body["payload"] = {"status": "failed"}
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
    assert [
        event["kind"]
        for event in json.loads(events.body)["events"]
    ] == [
        "run_accepted",
        "run_claimed",
        "run_waiting_callback",
        "external_callback_received",
        "run_resume_claimed",
        "run_succeeded",
    ]


def test_durable_http_callback_hides_foreign_runs_and_rejects_stale_fence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "durable-http-callback-authz.sqlite3"
    waiting, issuance = _prepare_waiting_callback(path)
    callback_body = _callback_body(waiting, issuance)
    callback_path = (
        f"/runs/{_CALLBACK_RUN_ID}/callbacks/{_CALLBACK_OPERATION_ID}"
    )

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


def test_durable_http_adapter_is_fail_closed_and_keeps_health_public(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="requires an auth_hook",
    ):
        DurableAcceptedRunServerApp(
            service=DurableAcceptedRunService(
                repository=SQLiteAcceptedRunRepository(
                    tmp_path / "invalid.sqlite3"
                ),
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
