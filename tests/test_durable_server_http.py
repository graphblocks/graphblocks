from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphblocks.compiler import compile_graph_reference
from graphblocks.durable_server import (
    DurableAcceptedRunServerApp,
    DurableAcceptedRunService,
)
from graphblocks.policy import PrincipalRef
from graphblocks.server import ServerRequest, StaticBearerAuthHook
from graphblocks.sqlite_server_storage import SQLiteAcceptedRunRepository


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


def test_durable_http_adapter_reads_run_and_events_after_restart(
    tmp_path,
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


def test_durable_http_adapter_is_fail_closed_and_keeps_health_public(
    tmp_path,
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
