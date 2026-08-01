from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
from threading import Event, Lock

import graphblocks.server as graphblocks_server
import pytest

from graphblocks.admission import AdmissionTicketQueue
from graphblocks.compiler import compile_graph_reference
from graphblocks.policy import PrincipalRef
from graphblocks.runtime import RuntimeRegistry
from graphblocks.server import (
    GraphBlocksServerApp,
    ServerRequest,
    StaticBearerAuthHook,
)


def _graph(block: str = "prompt.render@1") -> dict[str, object]:
    node: dict[str, object] = {
        "block": block,
        "outputs": {"value": "$output.value"},
    }
    if block == "prompt.render@1":
        node = {
            "block": block,
            "config": {"template": "Ticketed {message.text}"},
            "inputs": {"message": "$input.message"},
            "outputs": {"prompt": "$output.prompt"},
        }
    return {
        "apiVersion": "graphblocks.ai/v1alpha3",
        "kind": "Graph",
        "metadata": {"name": "ticketed-admission"},
        "spec": {"nodes": {"work": node}},
    }


def _submit(
    app: GraphBlocksServerApp,
    run_id: str,
    *,
    graph: dict[str, object] | None = None,
) -> dict[str, object]:
    response = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": graph or _graph(),
                    "inputs": {"message": {"text": run_id}},
                    "runId": run_id,
                    "requestId": f"request-{run_id}",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-10T00:00:00Z",
                }
            ).encode(),
        )
    )
    assert response.status_code == 202
    return json.loads(response.body)


def test_ticketed_server_returns_cursor_zero_and_promotes_fifo() -> None:
    clock = [0]
    queue = AdmissionTicketQueue(
        "interactive",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: clock[0],
    )

    first = _submit(app, "run-ticket-1")
    duplicate = _submit(app, "run-ticket-1")
    second = _submit(app, "run-ticket-2")

    assert first["admissionTicket"]["state"] == "admitted"
    assert duplicate["duplicate"] is True
    assert (
        duplicate["admissionTicket"]["ticketId"]
        == first["admissionTicket"]["ticketId"]
    )
    assert second["admissionTicket"] == {
        "ticketId": "interactive-ticket-000002",
        "runId": "run-ticket-2",
        "limiterId": "interactive",
        "state": "queued",
        "units": 1,
        "sequence": 2,
        "stateVersion": 1,
        "issuedAtUnixMs": 0,
        "expiresAtUnixMs": 60_000,
        "queuePosition": 1,
        "retryAfterMs": None,
        "startedAtUnixMs": None,
        "completedAtUnixMs": None,
    }
    assert app._events_by_run_id["run-ticket-1"] == ()
    assert app._events_by_run_id["run-ticket-2"] == ()

    status_response = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/run-ticket-2",
            headers={},
            query={},
            cookies={},
        )
    )
    status = json.loads(status_response.body)
    assert status["state"] == "queued"
    assert status["startedAt"] is None
    assert status["lastCursor"] == "run-ticket-2:0"
    assert status["waitingOn"] == [
        {
            "kind": "admission",
            "ticketId": "interactive-ticket-000002",
            "limiterId": "interactive",
        }
    ]
    with pytest.raises(ValueError, match="queued for admission"):
        app.advance_accepted_run("run-ticket-2")

    first_completion = app.advance_accepted_run("run-ticket-1")

    assert first_completion["status"] == "succeeded"
    assert queue.get("interactive-ticket-000002").state == "admitted"
    assert app._events_by_run_id["run-ticket-2"] == ()

    second_completion = app.advance_accepted_run("run-ticket-2")

    assert second_completion["status"] == "succeeded"
    assert [
        event["kind"] for event in app._events_by_run_id["run-ticket-2"]
    ] == ["RunStarted", "RunSucceeded"]
    assert queue.get("interactive-ticket-000002").state == "completed"


def test_ticketed_server_replay_respects_disabled_process_local_mode() -> None:
    queue = AdmissionTicketQueue(
        "replay-gate",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )
    run_id = "run-ticket-replay-gate-1"
    first = _submit(app, run_id)
    ticket_id = first["admissionTicket"]["ticketId"]
    assert isinstance(ticket_id, str)
    events_before = app._events_by_run_id[run_id]
    pending_before = app.pending_accepted_run_ids()
    ticket_before = queue.get(ticket_id).contract()

    app.allow_process_local_accepted_runs_dev = False
    replay = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": _graph(),
                    "inputs": {"message": {"text": run_id}},
                    "runId": run_id,
                    "requestId": f"request-{run_id}",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-10T00:00:00Z",
                }
            ).encode(),
        )
    )

    assert replay.status_code == 503
    assert json.loads(replay.body)["reasonCode"] == (
        "server.durable_accepted_run_required"
    )
    assert app._events_by_run_id[run_id] == events_before
    assert app.pending_accepted_run_ids() == pending_before
    assert queue.get(ticket_id).contract() == ticket_before


def test_ticketed_server_reserves_run_id_before_concurrent_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compile_started = Event()
    release_compile = Event()
    compile_lock = Lock()
    compile_calls = 0

    def blocking_compile(*args, **kwargs):
        nonlocal compile_calls
        with compile_lock:
            compile_calls += 1
        compile_started.set()
        if not release_compile.wait(10):
            raise TimeoutError("test did not release graph compilation")
        return compile_graph_reference(*args, **kwargs)

    monkeypatch.setattr(graphblocks_server, "compile_graph", blocking_compile)
    queue = AdmissionTicketQueue(
        "compile-reservation",
        max_concurrent=1,
        rate_limit=100,
        window_ms=1_000,
        max_pending=100,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )

    def submit(response_id: str) -> graphblocks_server.ServerResponse:
        return app.handle(
            ServerRequest(
                method="POST",
                path="/runs",
                headers={},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "graph": _graph(),
                        "inputs": {"message": {"text": response_id}},
                        "runId": "run-ticket-compile-reservation",
                        "requestId": "request-ticket-compile-reservation",
                        "responseId": response_id,
                        "responseMode": "accepted",
                        "occurredAt": "2026-07-30T00:00:00Z",
                    }
                ).encode(),
            )
        )

    with ThreadPoolExecutor(max_workers=16) as request_executor:
        first_future = request_executor.submit(
            submit,
            "response-ticket-compile-reservation-first",
        )
        if not compile_started.wait(5):
            release_compile.set()
            pytest.fail("graph compilation did not start")
        duplicate_futures = tuple(
            request_executor.submit(
                submit,
                f"response-ticket-compile-reservation-{index}",
            )
            for index in range(49)
        )
        try:
            completed_duplicates, _ = wait(
                duplicate_futures,
                timeout=5,
            )
        finally:
            release_compile.set()
        first = first_future.result(timeout=10)
        duplicates = tuple(
            future.result(timeout=10)
            for future in duplicate_futures
        )

    assert len(completed_duplicates) == 49
    assert compile_calls == 1
    assert first.status_code == 202
    assert {response.status_code for response in duplicates} == {409}
    assert {
        json.loads(response.body)["error"]
        for response in duplicates
    } == {"run 'run-ticket-compile-reservation' already exists"}
    assert queue.get("compile-reservation-ticket-000001").run_id == (
        "run-ticket-compile-reservation"
    )
    assert app._accepted_run_reservations_by_run_id == {}


def test_ticketed_server_releases_compile_reservation_when_queue_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graphblocks_server,
        "compile_graph",
        compile_graph_reference,
    )
    queue = AdmissionTicketQueue(
        "compile-queue-full",
        max_concurrent=1,
        rate_limit=100,
        window_ms=1_000,
        max_pending=1,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )
    _submit(app, "run-compile-queue-full-1")
    _submit(app, "run-compile-queue-full-2")

    rejected = app.handle(
        ServerRequest(
            method="POST",
            path="/runs",
            headers={},
            query={},
            cookies={},
            body=json.dumps(
                {
                    "graph": _graph(),
                    "inputs": {"message": {"text": "rejected"}},
                    "runId": "run-compile-queue-full-3",
                    "requestId": "request-run-compile-queue-full-3",
                    "responseMode": "accepted",
                    "occurredAt": "2026-07-30T00:00:00Z",
                }
            ).encode(),
        )
    )

    assert rejected.status_code == 429
    assert "run-compile-queue-full-3" not in app._events_by_run_id
    assert app._accepted_run_reservations_by_run_id == {}

    app.advance_accepted_run("run-compile-queue-full-1")
    retried = _submit(app, "run-compile-queue-full-3")

    assert retried["admissionTicket"]["state"] == "queued"


def test_ticketed_server_scopes_replay_subject_by_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graphblocks_server, "compile_graph", compile_graph_reference)
    queue = AdmissionTicketQueue(
        "multi-tenant",
        max_concurrent=2,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        auth_hook=StaticBearerAuthHook(
            {
                "tenant-a-token": PrincipalRef(
                    "shared-principal",
                    tenant_id="tenant-a",
                ),
                "tenant-b-token": PrincipalRef(
                    "shared-principal",
                    tenant_id="tenant-b",
                ),
            }
        ),
        allow_unsafe_multi_tenant_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )

    def submit(token: str, run_id: str) -> graphblocks_server.ServerResponse:
        return app.handle(
            ServerRequest(
                method="POST",
                path="/runs",
                headers={"Authorization": f"Bearer {token}"},
                query={},
                cookies={},
                body=json.dumps(
                    {
                        "graph": _graph(),
                        "inputs": {"message": {"text": run_id}},
                        "runId": run_id,
                        "requestId": "shared-request",
                        "responseMode": "accepted",
                        "occurredAt": "2026-07-30T00:00:00Z",
                    }
                ).encode(),
            )
        )

    tenant_a = submit("tenant-a-token", "shared-run")
    foreign_collision = submit("tenant-b-token", "shared-run")
    foreign_status = app.handle(
        ServerRequest(
            method="GET",
            path="/runs/shared-run",
            headers={"Authorization": "Bearer tenant-b-token"},
            query={},
            cookies={},
            body=b"",
        )
    )
    foreign_cancel = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/shared-run/cancel",
            headers={"Authorization": "Bearer tenant-b-token"},
            query={},
            cookies={},
            body=b"{}",
        )
    )
    tenant_b = submit("tenant-b-token", "tenant-b-run")
    tenant_a_payload = json.loads(tenant_a.body)
    tenant_b_payload = json.loads(tenant_b.body)
    tenant_a_ticket_id = tenant_a_payload["admissionTicket"]["ticketId"]
    tenant_b_ticket_id = tenant_b_payload["admissionTicket"]["ticketId"]

    assert tenant_a.status_code == 202
    assert foreign_collision.status_code == 404
    assert b"admissionTicket" not in foreign_collision.body
    assert foreign_status.status_code == 404
    assert foreign_cancel.status_code == 404
    assert tenant_b.status_code == 202
    assert tenant_a_ticket_id != tenant_b_ticket_id
    assert queue.get(tenant_a_ticket_id).tenant_id == "tenant-a"
    assert queue.get(tenant_b_ticket_id).tenant_id == "tenant-b"


def test_executor_never_runs_more_blocks_than_ticket_capacity() -> None:
    first_started = Event()
    release_first = Event()
    calls_lock = Lock()
    calls: list[str] = []
    active = 0
    peak_active = 0

    def mocked_external_block(
        inputs: dict[str, object],
        config: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        nonlocal active, peak_active
        run_id = str(context["run_id"])
        with calls_lock:
            calls.append(run_id)
            active += 1
            peak_active = max(peak_active, active)
        if run_id == "run-external-1":
            first_started.set()
            assert release_first.wait(timeout=5)
        with calls_lock:
            active -= 1
        return {"value": f"mocked:{run_id}"}

    registry = RuntimeRegistry(allow_untyped=True)
    registry.register("test.mocked-external@1", mocked_external_block)
    queue = AdmissionTicketQueue(
        "external-api",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        app = GraphBlocksServerApp(
            allow_unauthenticated_dev=True,
            allow_process_local_accepted_runs_dev=True,
            registry=registry,
            accepted_run_executor=executor,
            admission_ticket_queue=queue,
            admission_clock=lambda: 0,
        )
        _submit(app, "run-external-1", graph=_graph("test.mocked-external@1"))
        assert first_started.wait(timeout=5)
        second = _submit(
            app,
            "run-external-2",
            graph=_graph("test.mocked-external@1"),
        )

        assert second["admissionTicket"]["state"] == "queued"
        assert calls == ["run-external-1"]
        assert app._events_by_run_id["run-external-2"] == ()

        release_first.set()
        assert app.wait_for_accepted_run("run-external-1", timeout=5)["status"] == "succeeded"
        assert app.wait_for_accepted_run("run-external-2", timeout=5)["status"] == "succeeded"

    assert calls == ["run-external-1", "run-external-2"]
    assert peak_active == 1


def test_cancelling_queued_ticket_never_executes_it() -> None:
    queue = AdmissionTicketQueue(
        "cancel",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=60_000,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: 0,
    )
    _submit(app, "run-cancel-1")
    _submit(app, "run-cancel-2")

    cancelled = app.handle(
        ServerRequest(
            method="POST",
            path="/runs/run-cancel-2/cancel",
            headers={},
            query={},
            cookies={},
            body=json.dumps({"reason": "client left"}).encode(),
            requested_at="2026-07-10T00:00:01Z",
        )
    )

    assert cancelled.status_code == 202
    assert queue.get("cancel-ticket-000002").state == "cancelled"
    assert [
        event["kind"] for event in app._events_by_run_id["run-cancel-2"]
    ] == ["RunCancelled"]
    assert app.advance_accepted_run("run-cancel-2")["status"] == "cancelled"


def test_maintenance_expires_queued_run_without_starting_it() -> None:
    clock = [0]
    queue = AdmissionTicketQueue(
        "ttl",
        max_concurrent=1,
        rate_limit=10,
        window_ms=1_000,
        max_pending=10,
        ticket_ttl_ms=100,
    )
    app = GraphBlocksServerApp(
        allow_unauthenticated_dev=True,
        allow_process_local_accepted_runs_dev=True,
        admission_ticket_queue=queue,
        admission_clock=lambda: clock[0],
    )
    _submit(app, "run-ttl-active")
    _submit(app, "run-ttl-expired")

    clock[0] = 100
    assert app.promote_admission_tickets() == ()

    assert queue.get("ttl-ticket-000002").state == "expired"
    assert [
        event["kind"] for event in app._events_by_run_id["run-ttl-expired"]
    ] == ["RunExpired"]
    assert "run-ttl-expired" not in app.pending_accepted_run_ids()
    status = json.loads(
        app.handle(
            ServerRequest(
                method="GET",
                path="/runs/run-ttl-expired",
                headers={},
                query={},
                cookies={},
            )
        ).body
    )
    assert status["state"] == "expired"
    assert status["startedAt"] is None
